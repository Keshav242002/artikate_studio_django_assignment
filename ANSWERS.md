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

## Section 4 — Written Architecture Review

The brief asks for any two of the three questions. Answering **B (Pagination Trade-offs)** and **C (File Upload Security)** — both ground more directly in this repo: `OrderSummaryView` already returns an unpaginated list, and Section 3's `Tenant`/`Order` models are the kind of data a real file-upload feature (e.g. tenant logos, order attachments) would eventually sit next to.

### Question B — Pagination Trade-offs

**Offset-based** (DRF's `PageNumberPagination`/`LimitOffsetPagination`) generates `SELECT ... ORDER BY id LIMIT <n> OFFSET <m>`. The database must still walk and discard the first `m` rows before it can return the next `n` — on Postgres this shows up as a `Limit` node sitting on top of a scan that produces and throws away `m` rows first. Cost grows linearly with page depth: page 1 of 10,000 records is cheap, page 500 (`OFFSET 250000`) is not, even with an index on the ordering column, because the index only avoids a full table scan, not the row-skipping itself.

The sharper problem is correctness under mutation. If a row is inserted or deleted ahead of the current offset while a mobile app is mid infinite-scroll, every subsequent page shifts by one — the user either sees a duplicate row (a delete happened) or silently misses one (an insert happened). This is invisible in testing with a static dataset and shows up only in production under real write traffic, which is exactly the mobile-dashboard scenario here.

**Cursor-based** (DRF's `CursorPagination`) instead encodes the last-seen value of a unique, indexed, monotonic ordering field (e.g. `id`, or `created_at` with `id` as a tiebreaker) into an opaque token, and issues `WHERE id > <cursor_value> ORDER BY id LIMIT n`. This is a direct indexed lookup, not a skip — cost stays flat regardless of page depth, and it's immune to the drift problem above because the cursor references a row's identity, not its position; rows inserted or deleted elsewhere in the table don't shift what "next" means.

The trade-off: cursor pagination can't jump to an arbitrary page number ("go to page 50") — there's no numeric page concept — and a reliable total count still costs a full `COUNT(*)` regardless of style, so it's usually dropped for cursor APIs. For this 10,000-record endpoint, with infinite scroll as the access pattern and orders arriving continuously, I'd choose cursor-based: "jump to page N" isn't something infinite scroll needs, so offset's one advantage doesn't apply here.

### Question C — File Upload Security

Five distinct attack vectors and the Django-layer mitigation for each:

1. **Extension/MIME spoofing** — a file named `shell.jpg` that is actually a PHP script or polyglot, uploaded past a naive extension check. Mitigation: never trust the client-supplied filename extension or `Content-Type` header; verify the actual bytes server-side — for images, `PIL.Image.open(f).verify()` inside a `try/except`, or `python-magic` to sniff the real MIME type against an explicit whitelist, rejecting anything that doesn't match what was declared.

2. **Path traversal via filename** — a filename like `../../../etc/cron.d/evil` used to escape the intended upload directory if the server ever joins the raw client filename into a path. Mitigation: never use the client-supplied name to build a storage path; generate the stored filename yourself (e.g. `f"{uuid4()}{ext}"` in `upload_to`), and if using Django's default `FileSystemStorage`, rely on `Storage.get_available_name()` — which calls `django.utils.text.get_valid_name()` — rather than a custom storage backend that skips sanitization.

3. **Unbounded upload size (resource-exhaustion DoS)** — an attacker sends a multi-gigabyte file to exhaust memory or disk. Mitigation: `DATA_UPLOAD_MAX_MEMORY_SIZE`/`FILE_UPLOAD_MAX_MEMORY_SIZE` cap in-memory buffering before spilling to a temp file, but neither rejects the upload outright — explicitly check `request.META['CONTENT_LENGTH']` against a hard limit before the body is read, plus a matching size validator on the serializer's `FileField`.

4. **Stored XSS via inline-served uploads** — an uploaded SVG or HTML file containing `<script>` that executes when served back under the app's own origin. Mitigation: serve uploads from a separate, cookieless domain, force `Content-Disposition: attachment` for non-image types, and either disallow SVG/HTML via a strict MIME whitelist or re-encode SVGs to a raster format server-side before storage.

5. **Image decompression bombs** — a small file that decodes to an enormous pixel grid (e.g. a crafted PNG claiming 50,000×50,000 pixels), exhausting memory during thumbnailing or processing. Mitigation: keep PIL's built-in `Image.MAX_IMAGE_PIXELS` guard enabled (never set it to `None`) so `PIL.Image.DecompressionBombError` is raised above the default ~89-million-pixel threshold, and additionally check `image.size` against an explicit application-level maximum before any further processing.
