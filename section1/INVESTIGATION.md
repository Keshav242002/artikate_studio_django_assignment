# Section 1 — Incident Investigation Log

## Incident: `/api/orders/summary/` timeout after deployment

**Reported behaviour:** Endpoint responds in ~80ms normally, started timing out (30s+) for users with 200+ orders after a routine deployment.  
**Trigger:** Routine deployment. No code change to the view itself.

---

### Step 1: Check what changed in the deployment

First thing I did was pull the deployment diff. The view code for `OrderSummaryView` didn't change — confirmed by checking `git log --oneline -- orders/views.py`. But something in the deployment caused this, so I looked at what else shipped:

- Serializer changes → **yes, `orders/serializers.py` was modified** by a teammate
- Model changes → no
- Migration files → no new ones
- Middleware/settings → no

The diff showed that `OrderSummarySerializer` got a new nested `items` field:

```diff
+ class ProductSerializer(serializers.ModelSerializer):
+     class Meta:
+         model = Product
+         fields = ['id', 'name', 'price']
+
+ class OrderItemSerializer(serializers.ModelSerializer):
+     product = ProductSerializer()
+     class Meta:
+         model = OrderItem
+         fields = ['id', 'product', 'quantity', 'unit_price']

  class OrderSummarySerializer(serializers.ModelSerializer):
+     items = OrderItemSerializer(many=True, read_only=True)
      class Meta:
          model = Order
-         fields = ['id', 'status', 'total_amount', 'created_at']
+         fields = ['id', 'status', 'total_amount', 'created_at', 'items']
```

The fact that only users with 200+ orders are affected tells me the problem scales with data volume. That points to query count or query plan, not a logic bug.

### Step 2: Count the queries

I used `django.db.connection.queries` (with `DEBUG=True`) to count SQL queries fired by each endpoint. For a user with 250 orders:

```
BROKEN ENDPOINT (no prefetch_related)
  Total SQL queries: 988
  Response time:     0.129s

FIXED ENDPOINT (with prefetch_related)
  Total SQL queries: 3
  Response time:     0.020s
```

988 queries for 250 orders. That's a classic N+1.

### Step 3: Understand the N+1 pattern

Looking at the actual SQL queries fired, the pattern is:

```sql
-- Query 1: fetch all orders for the user
SELECT * FROM orders_order WHERE user_id = 1 ORDER BY created_at DESC

-- Query 2: fetch items for order #1 (triggered by serializer accessing order.items.all())
SELECT * FROM orders_orderitem WHERE order_id = 501

-- Query 3-5: fetch product for each item (triggered by OrderItemSerializer accessing item.product)
SELECT * FROM orders_product WHERE id = 7
SELECT * FROM orders_product WHERE id = 3
SELECT * FROM orders_product WHERE id = 10

-- Query 6: fetch items for order #2
SELECT * FROM orders_orderitem WHERE order_id = 502

-- ... repeats for every order
```

This happens because Django's ORM uses **lazy loading** by default. When the serializer accesses `order.items.all()`, the ORM doesn't fire that query until something iterates over it. Since the old serializer only touched flat `Order` fields (`id`, `status`, `total_amount`, `created_at`), no related objects were ever accessed, so no extra queries.

After the serializer change, `items = OrderItemSerializer(many=True)` triggers `order.items.all()` for every order, and then `product = ProductSerializer()` triggers `item.product` for every item. The ORM has no way to know in advance that these will be needed.

For 250 orders with ~3 items each:
- 1 query for orders
- 250 queries for items (one per order)
- ~737 queries for products (one per item)
- **= 988 queries total**

### Step 4: Root cause

**Root cause category: N+1 query problem**

Introduced by a serializer change — not a view change. The serializer added a nested `items` field that traverses two relationships (`Order → OrderItem → Product`), but the view's queryset was never updated with `prefetch_related` to batch-load those relations.

### Step 5: The fix

Added `prefetch_related('items__product')` to the view's `get_queryset()`:

```python
def get_queryset(self):
    user_id = self.request.query_params.get('user_id')
    if user_id:
        return Order.objects.filter(
            user_id=user_id
        ).prefetch_related(
            'items__product'
        )
    return Order.objects.none()
```

**Why this works at the database level:**

`prefetch_related('items__product')` makes Django execute exactly 3 queries:

1. `SELECT * FROM orders_order WHERE user_id = 1` — all orders
2. `SELECT * FROM orders_orderitem WHERE order_id IN (501, 502, 503, ...)` — all items for those orders in one shot
3. `SELECT * FROM orders_product WHERE id IN (7, 3, 10, ...)` — all referenced products in one shot

Django stores these results in memory (on the queryset's `_prefetched_objects_cache`). When the serializer accesses `order.items.all()`, the `prefetch_related_objects()` machinery detects the cache and returns pre-loaded data instead of hitting the DB.

I used `prefetch_related` instead of `select_related` because:
- `select_related` uses a SQL `JOIN` and only works for forward ForeignKey/OneToOneField
- `items` is a **reverse** ForeignKey (one-to-many), so `select_related` can't be used here
- `prefetch_related` handles reverse relations and M2M by doing a separate `IN` query, which avoids row multiplication from JOINs

The `items__product` double-underscore notation tells Django to chain the prefetch: first load items, then from those items, load their products. Without `__product`, we'd still get N queries for products.

### Step 6: Profiler evidence

**Before fix (988 queries):**
```
============================================================
BROKEN ENDPOINT  (no prefetch_related)
============================================================
  Total SQL queries: 988
  Response time:     0.129s
  Status code:       200
  Records returned:  250

  First 6 SQL queries (showing N+1 pattern):
    [1] SELECT "orders_order"."id", "orders_order"."user_id", ...
    [2] SELECT "orders_orderitem"."id", "orders_orderitem"."order_id", ...
    [3] SELECT "orders_product"."id", "orders_product"."name", ...
    [4] SELECT "orders_product"."id", "orders_product"."name", ...
    [5] SELECT "orders_product"."id", "orders_product"."name", ...
    [6] SELECT "orders_product"."id", "orders_product"."name", ...
    ... and 982 more queries
```

**After fix (3 queries):**
```
============================================================
FIXED ENDPOINT  (with prefetch_related)
============================================================
  Total SQL queries: 3
  Response time:     0.020s
  Status code:       200
  Records returned:  250

  All 3 SQL queries:
    [1] SELECT "orders_order".* ... WHERE user_id = 1
    [2] SELECT "orders_orderitem".* ... WHERE order_id IN (...)
    [3] SELECT "orders_product".* ... WHERE id IN (...)
```

**Summary:**
```
  Metric                        Broken      Fixed     Improvement
  ------------------------- ---------- ---------- ---------------
  SQL queries                      988          3         329x fewer
  Response time                 0.129s     0.020s         6.6x faster
```

Full profiler output is in `section1/profiler_output.txt`.

To reproduce this evidence:
```bash
python manage.py profile_queries
```

django-silk is also configured in `settings.py` (`silk.middleware.SilkyMiddleware`), and the silk dashboard is available at `/silk/` when the server is running. The profiler command uses `django.db.connection.queries` directly since silk's dashboard has a compatibility issue with Python 3.14, but silk is still recording request data in the background.
