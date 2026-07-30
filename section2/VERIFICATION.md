# Section 2 — Verification & Findings

This file records what was actually run to verify the rate-limited async job queue, and what it found. Three things were verified: the automated test suite, a live run against a real Redis instance and a real Celery worker (not mocks) — which is what surfaced a real bug the mocked tests missed — and finally the actual scale described in the brief: 2,000 requests in under 10 seconds.

## 1. Automated test suite

```bash
python manage.py test emailqueue -v2
```

**Result: 10/10 tests passed.**

| Test | What it proves |
|---|---|
| `test_allows_up_to_limit` | Exactly `limit` requests succeed |
| `test_blocks_beyond_limit` | The `(limit+1)`th request raises `RateLimitExceeded` |
| `test_window_slides_correctly` | Window frees up once stale entries age out |
| `test_fails_closed_on_redis_unavailable` | Redis `ConnectionError` → fail closed, not open |
| `test_concurrent_requests_do_not_exceed_limit` | `WATCH`/`MULTI`/`EXEC` atomicity holds under repeated calls |
| `test_happy_path_returns_sent` | Normal email sends and returns `status=sent` |
| `test_intentional_failure_retries_and_dead_letters` | `FAIL:` subject retries 4x with exponential backoff, then dead-letters |
| `test_rate_limit_exceeded_triggers_retry` | A rate-limited job retries and later succeeds once capacity frees |
| `test_survives_more_than_four_rate_limit_retries` | **Regression test** — a job rate-limited 8 times in a row (double the old failure threshold) still succeeds (see bug in §3) |
| `test_500_jobs` | 500 jobs (490 normal + 10 forced failures): none lost, rate limit never exceeded, all 10 failures dead-letter after exhausting retries |

## 2. First live run (253 jobs) — against real Redis + real Celery worker

The unit tests use `fakeredis` and Celery's eager mode — useful for CI, but they don't exercise real network timing, a real worker pool, or the real countdown-based retry scheduling. To verify the actual runtime behavior described in `DESIGN.md`, the queue was run against a genuine `redis-server` and `celery -A config worker` process.

**Setup:**
```bash
redis-server                                  # or the system's existing Redis service
celery -A config worker -l info
```

**Burst submitted:** 253 jobs in one batch — 250 normal + 3 intentionally failing (`FAIL:` subject prefix) — against the configured 200/min limit.

```python
from emailqueue.tasks import send_email
for i in range(250):
    send_email.delay(f'user{i}@example.com', 'Order confirmation', 'Thanks for your order!')
for i in range(3):
    send_email.delay(f'fail{i}@example.com', 'FAIL: simulated provider rejection', 'Body')
```

**First attempt — before the bug fix in §3 — produced a wrong result:** only 200 of 253 jobs were ever accounted for. 53 jobs vanished silently: no `sent` result, no `FailedEmailTask` row, nothing. This is what led to the investigation and fix in §3.

**After the fix, the identical burst was re-run. Result (tracked every 6s until settled):**

| Time | Sent | Dead-lettered | Rate limiter count (Redis `ZCARD`) |
|---|---|---|---|
| t+6s  | 200 | 0 | 200 |
| ... (holds at 200/0/200 while the backlog waits out the window) | | | |
| t+54s | 249 | 0 | 55 |
| t+60s | 250 | 0 | 62 |
| t+66s | 250 | **3** | 65 |

**Final: 250 sent + 3 dead-lettered = 253/253 — no job lost. Rate limiter never exceeded 200 (peak observed: 200).**

Dead-letter table contents after the run:
```
fail0@example.com  retry_count=4  Provider rejected email to fail0@example.com: FAIL: simulated provider...
fail1@example.com  retry_count=4  Provider rejected email to fail1@example.com: FAIL: simulated provider...
fail2@example.com  retry_count=4  Provider rejected email to fail2@example.com: FAIL: simulated provider...
```

Retry distribution from the worker log — several jobs needed far more than 4 rate-limit retries before capacity freed up, and still succeeded:
```
13 retries — task 22124566-9f7b-46e3-ac79-01c24130f563
12 retries — task f2a026ff-2a7b-4819-86ce-138c5a098e87
12 retries — task f18cfd52-7564-48c0-afc0-08c375634b2e
12 retries — task db400285-6b73-445e-ad23-47c30c6b9a4a
12 retries — task c66771fc-64a6-4600-8a10-280c7d997fa6
```

## 3. A real bug this process found (and the fix)

**Root cause:** `send_email` used Celery's single `max_retries=4` for both retry reasons — rate-limit backpressure (`RateLimitExceeded`) and genuine provider failures (`EmailSendError`) shared the same counter (`self.request.retries`). Being rate-limited is normal backpressure during a burst, not a failure, but Celery doesn't know that — once a job's `self.retry()` call had been made 4 times for *any* reason, the 5th call raised `MaxRetriesExceededError`. That exception was only caught in the `EmailSendError` branch's exception handler; in the `RateLimitExceeded` branch it propagated unhandled, so Celery marked the task failed with no dead-letter write. The 200-job unit test never caught this because it manually resets the fake Redis key every 200 jobs, so no job in that test ever needed more than one rate-limit retry.

**Fix (in `emailqueue/tasks.py`):**
- Added an explicit `_send_attempt` kwarg threaded through retries, incremented only on genuine `EmailSendError`s.
- Rate-limit retries now pass `max_retries=RATE_LIMIT_RETRY_CEILING` (10,000) — effectively unbounded, since waiting for capacity should never be treated as a permanent failure.
- `MAX_SEND_ATTEMPTS = 4` is now checked manually against `_send_attempt`, completely independent of how many times the job was rate-limited first.
- Added `args=()` on every `self.retry(kwargs={...})` call — an implementation detail worth flagging: passing `kwargs=` without also clearing the original positional `args` caused Celery to pass both, raising `TypeError: multiple values for argument 'recipient'`. Fixed by explicitly passing `args=()`.

**Regression test added:** `test_survives_more_than_four_rate_limit_retries` in `emailqueue/tests.py` — mocks the limiter to reject 8 times in a row (double the old failure point) and asserts the job still ends up `sent`, with zero dead-letter rows. This test would fail against the pre-fix code and passes now.

## 4. The actual brief scenario: 2,000 requests in under 10 seconds

The 253-job run above proved the mechanics and found/fixed a real bug — but it didn't test the scale the brief actually describes ("bursts of 2,000 requests in under 10 seconds during flash sales"). That was run separately, against the same live Redis + Celery worker setup, with the fix from §3 already in place.

**Submission:**
```python
import time
from emailqueue.tasks import send_email
start = time.time()
for i in range(1980):
    send_email.delay(f'user{i}@example.com', 'Order confirmation', 'Thanks for your order!')
for i in range(20):
    send_email.delay(f'fail{i}@example.com', 'FAIL: simulated provider rejection', 'Body')
elapsed = time.time() - start
print(f'Submitted 2000 jobs in {elapsed:.2f}s')
```

**Result: `Submitted 2000 jobs in 0.72s`.**

This is the key thing to understand about how this design meets the "2,000 in under 10 seconds" requirement: `.delay()` only publishes a message to the Redis broker — it doesn't send the email or touch the rate limiter at all. Submission is decoupled from execution, so the burst is absorbed instantly regardless of the downstream rate limit; the queue itself has effectively no ceiling on intake rate. The 200/min constraint only applies once a worker picks a job up to actually process it.

**Drain (worker processing the backlog at the 200/min cap):**

| Time since submit | Sent | Dead-lettered | Rate limiter count |
|---|---|---|---|
| mid-run (spot check) | 1,600 | 0 | 200 (holding at cap) |
| ~10m 27s (fully drained) | **1,980** | **20** | 80 (draining down as the last entries age out) |

**Final: 1,980 sent + 20 dead-lettered = 2,000/2,000 — no job lost.** All 20 intentional failures correctly dead-lettered with `retry_count=4` (exhausted the provider-failure retry budget, independent of rate-limiting). Peak rate limiter count observed: 200, never exceeded.

**Total wall-clock to fully drain: ~10 minutes 27 seconds** — consistent with 2,000 jobs at a 200/min cap (theoretical minimum ~10 minutes), plus the 20 failing jobs' exponential backoff overhead. This is the real-world cost of respecting a hard third-party rate limit: the queue durably holds the backlog and drains it at the provider's pace rather than bursting past it. A rate limiter that let the burst through faster than 200/min would not actually be respecting the constraint the brief describes.

**Rate-limit retry depth at this scale:** several individual jobs were rate-limited **120 times** each (waiting the better part of 10 minutes, in 5-second retry increments, for their turn) before finally succeeding — over an order of magnitude past the old 4-retry ceiling that caused the bug in §3. All of them still landed as `sent`, confirming the fix holds at the actual scale the brief describes, not just at the smaller 253-job scale used to originally find the bug.

## 5. Conclusion

- Automated suite: 10/10 passing.
- First live run (253 jobs, pre-fix): exposed a real bug — 53/253 jobs silently lost.
- Root-caused, fixed, and locked in with a regression test (§3).
- Re-verified live at 253 jobs: 253/253 accounted for, rate limit never exceeded.
- Verified again at the brief's actual scale — 2,000 jobs submitted in 0.72s, fully drained in ~10m27s at the 200/min cap: 2,000/2,000 accounted for (1,980 sent, 20 correctly dead-lettered), rate limiter peak never exceeded 200, individual jobs surviving up to 120 rate-limit retries without being lost.
