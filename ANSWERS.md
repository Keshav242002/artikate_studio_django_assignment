# Answers

## Section 1 — Diagnose a Broken System

### Root Cause

**N+1 query problem**, introduced by a serializer change in the deployment.

The view code didn't change. What changed was the serializer — someone added a nested `items` field (`OrderItemSerializer` with a nested `ProductSerializer`). The view's queryset had no `prefetch_related`, which didn't matter before because the old serializer only serialized flat `Order` fields. After the serializer change, the ORM's lazy loading kicked in: every `order.items.all()` and every `item.product` access fires a separate DB query.

For a user with 250 orders averaging 3 items each, that's 1 + 250 + 750 = **1001 queries** instead of 3.

### Why the fix works

Added `prefetch_related('items__product')` to the view's `get_queryset()`.

This tells Django to batch-load related objects. Instead of N lazy queries, Django fires:
1. One query for all orders (`WHERE user_id = ?`)
2. One query for all items (`WHERE order_id IN (...)`)
3. One query for all products (`WHERE id IN (...)`)

It stores the results in a prefetch cache (`_prefetched_objects_cache` on the queryset). When the serializer later accesses `order.items.all()`, Django's `get_prefetch_queryset()` method detects the cache hit and returns the pre-loaded data instead of hitting the database.

I used `prefetch_related` rather than `select_related` because `items` is a reverse ForeignKey relationship. `select_related` performs a SQL `JOIN` and only works for forward ForeignKey/OneToOne fields. For reverse relations (one-to-many), `prefetch_related` is correct — it issues a separate `SELECT ... WHERE order_id IN (...)` query, which avoids the row multiplication you'd get from a JOIN on a one-to-many.

### Profiler evidence

django-silk is integrated in `config/settings.py`. After seeding data and hitting both endpoints, the silk dashboard at `/silk/` shows:

- **Broken endpoint**: 201 queries (for 50 orders with 3 items each)
- **Fixed endpoint**: 3 queries (same data)

The test suite (`orders/tests.py`) also uses Django's `assertNumQueries` to prove this programmatically.

### What I didn't do (and why)

- I considered whether a missing database index could be the issue. But missing indexes cause slow individual queries, not more queries. The silk output showed a high *count* of fast queries, not a few slow ones — that's an N+1 signature.
- I didn't add `select_related` on the Order→User relationship because the serializer doesn't expose user details. No point prefetching what you don't use.
- I didn't add pagination to the view, even though it would help in production. The assessment asked me to identify and fix the root cause, not redesign the endpoint. Pagination would mask the N+1 without fixing it.
