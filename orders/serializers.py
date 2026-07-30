from rest_framework import serializers
from .models import Order, OrderItem, Product


class OrderSummarySerializer(serializers.ModelSerializer):
    """
    Summary serializer for the mobile dashboard.
    Only flat order fields — no related data needed here.
    """
    class Meta:
        model = Order
        fields = ['id', 'status', 'total_amount', 'created_at']
