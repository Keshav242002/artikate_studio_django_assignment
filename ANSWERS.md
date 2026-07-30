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
