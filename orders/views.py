from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny

from .models import Order
from .serializers import OrderSummarySerializer


class OrderSummaryView(ListAPIView):
    """
    GET /api/orders/summary/?user_id=<id>

    Returns order summary for a user's mobile dashboard.
    """
    serializer_class = OrderSummarySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        user_id = self.request.query_params.get('user_id')
        if user_id:
            return Order.objects.filter(user_id=user_id)
        return Order.objects.none()
