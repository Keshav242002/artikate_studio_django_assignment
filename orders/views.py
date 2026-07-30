from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny

from .models import Order
from .serializers import OrderSummarySerializer


class OrderSummaryView(ListAPIView):
    """
    GET /api/orders/summary/?user_id=<id>

    Returns order summary for a user's mobile dashboard.

    Fix: added prefetch_related('items__product') to avoid N+1 queries
    after the serializer was updated to include nested order items.

    Without the prefetch, Django fires a separate query for each
    order's items, and another for each item's product. With 250 orders
    averaging 3 items, that's 1 + 250 + 750 = 1001 queries.

    prefetch_related batches these into 3 total queries:
      1. SELECT * FROM orders_order WHERE user_id = ?
      2. SELECT * FROM orders_orderitem WHERE order_id IN (...)
      3. SELECT * FROM orders_product WHERE id IN (...)
    Django caches the results on the queryset's _prefetched_objects_cache
    so the serializer hits memory instead of the database.
    """
    serializer_class = OrderSummarySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        user_id = self.request.query_params.get('user_id')
        if user_id:
            return Order.objects.filter(
                user_id=user_id
            ).prefetch_related(
                'items__product'
            )
        return Order.objects.none()


class OrderSummaryBrokenView(ListAPIView):
    """
    GET /api/orders/summary-broken/?user_id=<id>

    Kept for profiling comparison — this is the pre-fix version
    WITHOUT prefetch_related. Hit this endpoint and the fixed one,
    then compare query counts in Silk at /silk/.
    """
    serializer_class = OrderSummarySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        user_id = self.request.query_params.get('user_id')
        if user_id:
            return Order.objects.filter(user_id=user_id)
        return Order.objects.none()
