# DESIGN.md — Section 2: Rate-Limited Async Job Queue

## 1. Architecture choice: Celery + Redis vs. Django-Q vs. custom

| | Celery + Redis | Django-Q(2) | Custom (own worker loop) |
|---|---|---|---|
| Broker durability | Redis (or RabbitMQ) — survives worker crash if `acks_late` is set | Uses Django's own DB or Redis as broker | Whatever you build — usually a DB table polled by a loop |
| Retry / backoff | Built-in (`autoretry_for`, `retry_backoff`, `self.retry(countdown=...)`) | Basic retry, no native exponential backoff | Must hand-write retry bookkeeping and countdown scheduling |
| Rate limiting | Celery has a *per-task* `rate_limit` option, but it's per-worker-process, not cluster-wide — not good enough for a single global 200/min cap across many workers. We still need our own Redis limiter regardless of framework choice. | No native rate limiting | Fully custom, but that's most of the work we'd have to do anyway |
| Crash safety | `task_acks_late` + `task_reject_on_worker_lost` are first-class, well-documented settings | Weaker guarantees; default config can lose in-flight tasks on SIGKILL | Depends entirely on how carefully you build acknowledgement — easy to get wrong |
| Operational maturity | Mature, widely deployed, monitoring via Flower, well understood failure modes | Smaller ecosystem, fewer people who've hit its edge cases in production | None — every failure mode is something we discover ourselves |
| Cost | Extra moving part (Redis broker) and worker process to run/monitor | Simpler ops (can reuse existing DB), but weaker guarantees | Cheapest to deploy, most expensive to make *correct* |

**Chosen: Celery + Redis.**

Reasoning: the requirements — "does not lose jobs if the worker crashes mid-run," retries with backoff, dead-letter handling — are exactly the problems Celery's `acks_late`/`reject_on_worker_lost` and retry machinery solve natively and are battle-tested. Django-Q would require re-implementing backoff and crash-safety semantics on a less mature base, and a fully custom queue means re-solving problems (message acknowledgement, redelivery, retry scheduling) that Celery already solves correctly. The one thing no framework gives us for free is the *cluster-wide* rate limit — Celery's own `rate_limit` throttles per worker process, not globally — so that part is hand-built on Redis regardless of which task framework sits on top (see §2).

Trade-off accepted: Celery + Redis adds a broker and a worker process to operate (vs. Django-Q's DB-only option), and Redis becomes a single point of failure for both the broker and the rate limiter. We treat that as acceptable because a transactional-email system already needs a reliable queue; a broker outage should surface as delayed sends via retries, not silent data loss (see §4).

## 2. Rate limiter: sliding window over token bucket / fixed window

Implementation: `emailqueue/rate_limiter.py`, `SlidingWindowRateLimiter` — a Redis **sorted set** per rate-limit key, where the score is a microsecond timestamp.

**1. Why sliding window over the alternatives**

- **Fixed window (`INCR` + `EXPIRE`)** — simplest to implement, but allows up to 2× the limit at window boundaries. With `limit=200/min`, a burst of 200 requests at `:59` followed by 200 more at `:01` is 400 requests inside a 2-second span even though each window individually stayed under 200. Given the brief explicitly describes flash-sale bursts of 2,000 requests in under 10 seconds, boundary bursting is exactly the failure mode we can't afford.
- **Token bucket (`DECR` + `TTL`)** — smooths bursts well, but the "remaining tokens" counter is stateful and drifts if Redis restarts mid-window or if the refill logic and the consume logic disagree on timing. It also requires a background or lazy refill calculation, which is another place to get the math wrong.
- **Sliding window (sorted set + `ZREMRANGEBYSCORE`)** — precise: at any instant, `ZCARD` after evicting entries older than `now - window` gives the *exact* count of requests in the trailing window, with no boundary artifact and no separate refill process. The cost is O(log N) per operation and a bit more memory (one sorted-set entry per request within the window, capped at `limit` entries) — negligible at 200 entries.

**2. How atomicity is guaranteed**

Redis `WATCH` + `MULTI`/`EXEC` (optimistic locking), not a Lua script — both are explicitly acceptable per the assignment, and WATCH/MULTI/EXEC was chosen because it works against plain `fakeredis` in tests (no Lua interpreter needed there) and keeps every command visible as a native Redis command in logs, which is easier to debug than an opaque script blob.

Sequence per `acquire()` call (`_execute_transaction()`):
1. `WATCH <key>` — Redis will abort our transaction if any other client modifies this key before our `EXEC`.
2. Evict stale entries: `ZREMRANGEBYSCORE <key> -inf <now - window>` (runs immediately, pre-`MULTI`).
3. Read the count: `ZCARD <key>`.
4. If `count >= limit` → `pipe.reset()` (unwatch) and return "blocked" without writing anything.
5. Otherwise, queue the write inside `MULTI`: `ZADD <key> {member: now}` + `EXPIRE <key> window+1`, then `EXEC`.
6. If any other worker touched `<key>` between steps 1 and 5, `EXEC` raises `WatchError`; we catch it and retry the whole read-then-write cycle from scratch (bounded to 10 attempts as a safety cap).

This gives the same guarantee as a single atomic Lua script: two workers can never both observe `count == 199` and both proceed to write the 200th and 201st entries — one of them will see its `EXEC` rejected and re-read the now-updated count.

**3. Redis failure behaviour: fail closed**

If Redis raises `ConnectionError` at any point in `acquire()`, we catch it and raise `RateLimitExceeded("Redis unavailable — failing closed")` — the caller (the Celery task) treats this exactly like a normal rate-limit hit and retries with a short countdown. We deliberately do **not** fail open (i.e., we do not let the email through when we can't verify the rate limit). For a transactional email system talking to a third-party provider with a hard 200/min cap, silently bursting past that limit during a Redis outage risks the provider throttling or banning the account entirely — a worse outcome than a delayed send. The cost of failing closed is that all email sending pauses during a Redis outage; we accept that because Redis is also the Celery broker in this design, so a Redis outage already stops task processing — failing closed here doesn't introduce a *new* dependency, it's consistent with one that already exists.

## 3. Task design: retries, backoff, dead-letter

`emailqueue/tasks.py` — `send_email` is a `bind=True` task with `max_retries=4` (5 attempts total).

- **Rate-limit hits** (`RateLimitExceeded`) retry on a short fixed 5-second countdown, not exponential backoff — the goal here is "try again once the window has room," not "back off because something is broken." Exponential backoff is for genuine failures.
- **Transient send failures** (`EmailSendError`, standing in for e.g. an SMTP timeout) retry with exponential backoff: `countdown = 2 ** self.request.retries` → 1s, 2s, 4s, 8s, 16s.
- **Dead-letter**: once `max_retries` is exhausted, `MaxRetriesExceededError` is caught and the task writes to the `FailedEmailTask` Django model (`_write_to_dead_letter`) instead of raising — recipient, subject, body, the last error message, retry count, and timestamp are persisted. This is a normal Django model rather than a Redis list because it needs to survive Redis restarts, be queryable from the Django admin/shell for manual triage, and give an audit trail — a broker-level dead-letter would disappear if the broker itself gets flushed.
- Any *unexpected* exception (not `EmailSendError`) dead-letters immediately without consuming retries — no point retrying a bug in our own code five times.

## 4. What happens to in-flight tasks on `SIGKILL`

Two settings in `config/settings.py` govern this, both applied globally rather than per-task:

- `CELERY_TASK_ACKS_LATE = True` — normally, Celery acknowledges (removes) a message from the Redis broker the moment a worker *receives* it, before the task body even runs. With `acks_late`, the ack is deferred until the task function **returns** (successfully or by raising an exception Celery handles, e.g. via `retry`). If the worker process is killed mid-execution, the message was never acked, so it's still sitting in the broker.
- `CELERY_TASK_REJECT_ON_WORKER_LOST = True` — when a worker is `SIGKILL`'d, Celery cannot run any of its own cleanup code (SIGKILL can't be trapped). Without this setting, an unacked-but-in-progress message can be left in limbo depending on broker/transport behavior. With it, Celery's broker-side connection monitoring treats the lost worker's unacked messages as *rejected*, which for Redis-as-broker means the message is requeued and becomes available for another worker to pick up.

Net effect: if a worker is `SIGKILL`'d while executing `send_email`, the message is redelivered to a different (or restarted) worker and the email is attempted again from scratch — it is **not** silently dropped. The trade-off we accept: if the task had actually already called `_send_via_provider()` successfully and then the process died *before* returning (e.g., killed between the provider call succeeding and the function's `return`), the email would be sent twice — `acks_late` trades "at-least-once" delivery for "never lost," not exactly-once. We consider duplicate transactional emails a far smaller problem than a silently dropped order confirmation or OTP, so this is the correct trade-off here; a stricter exactly-once guarantee would need an idempotency key at the provider or a dedup table, which this assessment's scope doesn't require.

## 5. A real bug this design caught (and fixed)

The unit tests passed from the first implementation, including the 500-job test — but that test resets the rate-limiter's Redis key every 200 jobs to simulate the window sliding, which accidentally meant no single job was ever rate-limited more than once. Running the same code against a **real** Redis instance and a real `celery worker` process with a genuine 253-job burst (no artificial resets) surfaced a bug the mocked test couldn't: 53 jobs that needed more than one rate-limit retry were silently dropped — no `sent` result, no `FailedEmailTask` row, nothing.

**Cause:** the original code called `self.retry()` identically for both `RateLimitExceeded` (backpressure — not a failure) and `EmailSendError` (a genuine failure), so both shared the task's single `max_retries=4` / `self.request.retries` counter. A job waiting out the rate limit for the 5th time hit Celery's `MaxRetriesExceededError`, which was only caught in the `EmailSendError` branch — so in the rate-limit branch it propagated unhandled and the task simply failed, uncaught by our dead-letter logic.

**Fix:** `emailqueue/tasks.py` now threads an explicit `_send_attempt` kwarg through retries, incremented only on genuine `EmailSendError`s. Rate-limit retries pass a large `max_retries` override (`RATE_LIMIT_RETRY_CEILING`) so throttling waits are effectively unbounded, while `MAX_SEND_ATTEMPTS` — checked manually against `_send_attempt` — is the only thing that ever triggers a dead-letter. Verified against the real worker again after the fix: jobs rate-limited **12 times** in the same burst scenario now succeed instead of vanishing. A regression test (`test_survives_more_than_four_rate_limit_retries`) locks this in.

Lesson for the write-up: a test that manufactures its own "time passing" (deleting a Redis key on a schedule) can hide exactly the interaction it was meant to prove — the fix was only found by running the real broker + real worker, not by the mocked suite.

## 6. Test coverage (`emailqueue/tests.py`)

- Rate limiter unit tests: allows exactly `limit` requests, blocks the next one, correctly slides the window forward, fails closed when Redis is unreachable.
- `test_500_jobs`: submits 500 jobs (490 normal + 10 intentionally failing) through `send_email.apply()` with `CELERY_TASK_ALWAYS_EAGER=True` and a `fakeredis`-backed limiter. Asserts: all 500 produce a terminal result (`sent` or `dead_lettered` — none lost), the sliding window never records more than 200 entries at once, and all 10 intentional failures dead-letter only after exhausting `max_retries=4`.
- `fakeredis` and Celery's eager mode are used so the whole suite runs without a live Redis or worker process — this keeps the "under 5 minutes, clean environment" requirement satisfied without infrastructure setup for grading. A live Redis + `celery worker` run is still described in `README.md` for anyone who wants to see it end-to-end.
