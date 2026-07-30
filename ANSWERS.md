# Answers

## Section 1 — Diagnose a Broken System

### Root Cause

**N+1 query problem** introduced by a serializer change (commit `9970542` by Arjun Mehta).

The view code for `OrderSummaryView` was never modified. What changed was `OrderSummarySerializer` — a teammate added a nested `items` field using `OrderItemSerializer`, which itself nests `ProductSerializer`. The view's queryset (`Order.objects.filter(user_id=...)`) had no `prefetch_related`, which was fine when the serializer only touched flat `Order` fields. After the change, every `order.items.all()` and `item.product` access triggered a separate SQL query due to Django's lazy loading.

For 250 orders averaging ~3 items each: 1 + 250 + 737 = **988 queries**.

You can see the exact diff that caused this:
```bash
git log --oneline -3
git diff HEAD~1 HEAD~0 -- orders/serializers.py   # teammate's change
git diff HEAD~0 -- orders/views.py                 # the fix
```

### Why the fix works

Added `prefetch_related('items__product')` to the view's `get_queryset()`.

This tells Django to batch-load related objects upfront. Instead of lazy-loading each relation on access, Django fires:
1. `SELECT * FROM orders_order WHERE user_id = ?` — all orders
2. `SELECT * FROM orders_orderitem WHERE order_id IN (...)` — all items in one query
3. `SELECT * FROM orders_product WHERE id IN (...)` — all products in one query

Django caches these on the queryset's `_prefetched_objects_cache`. When the serializer later accesses `order.items.all()`, the `prefetch_related_objects()` method detects the cache and returns in-memory data instead of hitting the database.

I used `prefetch_related` instead of `select_related` because `items` is a reverse ForeignKey (one-to-many). `select_related` works via SQL `JOIN` and only handles forward FK/OneToOne — using it on a reverse relation would cause row duplication. `prefetch_related` uses a separate `SELECT ... WHERE id IN (...)` query, which is the correct approach for reverse relations and M2M.

### Profiler evidence

Run `python manage.py profile_queries` to see the full comparison. Summary:

| Metric | Broken | Fixed | Improvement |
|---|---|---|---|
| SQL queries | 988 | 3 | 329x fewer |
| Response time | 0.129s | 0.020s | 6.6x faster |

django-silk is configured in settings (`silk.middleware.SilkyMiddleware`) and records every request. The profiler command uses `django.db.connection.queries` with `DEBUG=True` for direct evidence.

Full output: `section1/profiler_output.txt`

### What I didn't do (and why)

- Didn't add `select_related('user')` — the serializer doesn't expose user details, no point prefetching what's unused.
- Didn't add pagination — it would help in production but would mask the N+1 without fixing it. The root cause needs to be fixed regardless.
- Considered whether a missing database index could be involved. But missing indexes cause slow individual queries, not high query counts. Silk showed many fast queries (988 of them), not a few slow ones — that's an N+1 signature, not a missing index.

## Section 2 — Rate-Limited Async Job Queue

Architecture choice, rate limiter design, and full trade-off analysis are in [`DESIGN.md`](DESIGN.md). The one question the brief asks for explicitly as a written answer is reproduced here:

### What happens to in-flight tasks if the Celery worker process is `SIGKILL`'d?

Two settings make this safe, both set globally in `config/settings.py`:

- `CELERY_TASK_ACKS_LATE = True` — a task message is normally acknowledged (removed from the Redis broker) the instant a worker *receives* it, before the task body runs. `acks_late` defers that acknowledgement until the task function actually returns. If the worker is killed mid-run, the message was never acked, so it is still present in the broker.
- `CELERY_TASK_REJECT_ON_WORKER_LOST = True` — `SIGKILL` gives the worker no chance to run cleanup code. This setting makes Celery's broker-side connection monitoring treat that worker's unacked messages as rejected the moment the connection drops, which for a Redis broker means the message is requeued immediately for another worker to pick up.

Net effect: a task being executed when its worker is `SIGKILL`'d is **redelivered and retried from scratch on another worker** — it is not lost. The trade-off is at-least-once delivery, not exactly-once: if the process died *after* `_send_via_provider()` succeeded but *before* the task returned, the email would be sent twice. We accept this because a duplicate transactional email is a far smaller problem than a silently dropped order confirmation or OTP. A true exactly-once guarantee would need an idempotency key at the provider side, which is out of scope here.

Full reasoning for the rate limiter's atomicity guarantee and fail-closed behaviour under Redis failure is in `DESIGN.md` §2.

## Section 3 — Multi-Tenant Data Isolation

Implementation lives in the new `tenants` app: `tenants/models.py` (`Tenant`), `tenants/context.py` (contextvar utility), `tenants/managers.py` (`TenantManager`), `tenants/middleware.py` (`TenantMiddleware`). `Order` gained a required `tenant` ForeignKey and now uses `TenantManager` as `objects`. Tests are in `tenants/tests.py`.

### Why `contextvars.ContextVar`, not `threading.local`, from the start

Thread-local storage (`threading.local()`) fails under async because Django's ASGI mode can run multiple coroutines concurrently on the same OS thread via the event loop. A thread-local value set in one request's coroutine is visible to another request's coroutine running on that same thread, because thread-locals key on the thread, not the logical request. This means Tenant A's context can leak into Tenant B's queryset mid-request — a direct data isolation failure, which is the exact vulnerability this section is designed to prevent.

`contextvars.ContextVar`, introduced in Python 3.7 specifically for async code, instead keys on the current execution context, which `asyncio` copies automatically at each `await` point — so each coroutine sees only its own value regardless of which thread runs it. Since I used `ContextVar` from the start (`tenants/context.py`) rather than `threading.local()`, this implementation is already async-safe — the async answer and the shipped code are the same thing, not a hypothetical fix.

### Why `get_queryset()` is overridden, not `all()`

The scaffold's comment ("even when the caller has no knowledge of tenant context") points at `get_queryset()` specifically: every other manager entry point — `filter()`, `get()`, `first()`, `count()` — routes through `get_queryset()` internally. Overriding only `all()` would leave those other calls completely unscoped, which is the exact bypass this section is testing for. `tenants/tests.py::test_filter_and_get_are_also_scoped` proves `.filter()` and `.get()` are constrained too, not just `.all()`.

### Fail-closed, not fail-open

`TenantManager.get_queryset()` returns `qs.none()` when `get_current_tenant()` is `None`, rather than falling back to unscoped `super().get_queryset()`. A forgotten `set_current_tenant()` call (e.g. a new code path that bypasses the middleware) then shows zero rows instead of every tenant's rows. That is the difference between a visibly broken feature — caught immediately in testing — and a silent cross-tenant data breach. `test_no_tenant_context_returns_nothing` proves this explicitly.

### Middleware `try/finally`

`TenantMiddleware.__call__` wraps `get_response(request)` in `try/finally`, resetting the contextvar in `finally`. Without it, a view raising an exception would leave that request's tenant bound to the context — and since Gunicorn workers and ASGI tasks are reused across requests, that stale tenant could leak into whichever request runs next on the same worker/context. `test_middleware_resets_context_after_request_even_on_exception` proves the reset happens even when `get_response` raises.

### Tenant extraction: subdomain primary, JWT fallback — explicit scope note

`TenantMiddleware._resolve_tenant()` first looks up `Tenant.objects.filter(slug=<subdomain>)` from `request.get_host()`. If no tenant matches, it falls back to decoding a `tenant_id` claim from an `Authorization: Bearer <jwt>` header, verified with `PyJWT` against `settings.SECRET_KEY`. This is a hand-decoded check, not a full `djangorestframework-simplejwt` integration — there is no login endpoint, token issuance, or refresh flow, since building real auth infrastructure was out of scope for this section. In a production system the JWT would come from a proper auth service and be verified against its public key, not the Django `SECRET_KEY`.

**Known edge case:** subdomain extraction is `request.get_host().split(':')[0].split('.')[0]` — the first label before the first dot. For a real subdomain (`acme.example.com`) this correctly yields `acme`. But for a bare domain with no subdomain at all (`example.com`), it yields `example`, which would silently match a tenant slugged `"example"` if one existed — there's no explicit check that the host actually *has* a subdomain segment before treating the first label as one. This is a real gap, not just a hypothetical: a production version would validate the host has at least 3 labels (`sub.domain.tld`) before treating the first as a tenant slug, or use an explicit `X-Tenant-Slug` header instead of parsing it out of the host.

### Admin / management-command context gap

`TenantMiddleware` only binds tenant context during the HTTP request/response cycle. `python manage.py seed_data` and Django admin access at a non-tenant host both fall outside that cycle:
- `seed_data.py` works around this by explicitly binding `set_current_tenant(tenant)` for the duration of the command (see its `handle()`), since row creation always needs `tenant=` regardless of context, but its `count()`/`filter()` reads still go through the scoped manager.
- Admin access at a non-tenant host (e.g. `localhost/admin` with no matching subdomain and no JWT) currently sees **no orders**, by design, under the fail-closed manager. This is intentional given the "fail closed, not open" decision above, but it is a real limitation: a production follow-up would add an explicit tenant-selection mechanism for staff/admin use (e.g. a superuser-only tenant switcher bound into the same contextvar) rather than leaving admin unusable at non-tenant hosts.

### Migration strategy

Chose to make the `tenant` FK non-nullable and reseed (`rm db.sqlite3 && migrate && seed_data`) rather than a null → backfill → non-null migration sequence, since this is a disposable dev/seed SQLite database, not a production table requiring a zero-downtime backfill. In production, the safe sequence would be: add `tenant` as nullable, backfill existing rows in batches, then add a `NOT NULL` constraint in a separate migration — done here in one step because there is no real data or uptime constraint to protect.

### Async failure modes of thread-local tenant scoping (if it had been used) and what would change

Even though this implementation avoids the problem by using `ContextVar`, to state the async failure mode explicitly: under Django's ASGI/async views, multiple requests' coroutines can interleave on the same OS thread inside the event loop. `threading.local()` state is keyed per-thread, not per-coroutine, so a value set for Tenant A's request can still be readable — or silently overwritten — from Tenant B's request if both happen to execute on the same thread at overlapping times. This is a genuine cross-tenant data leak, not just a theoretical concern, since Django's async request handling does not guarantee one thread per request the way WSGI does. The fix is exactly what was implemented here: replace `threading.local()` with `contextvars.ContextVar`, whose values are scoped to the async context that `asyncio`/ASGI propagates correctly across `await` boundaries, and set/read it via the middleware's `set_current_tenant`/`get_current_tenant` rather than a module-level thread-local object.
