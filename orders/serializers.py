from rest_framework import serializers
from .models import Order, OrderItem, Product


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ['id', 'name', 'price']


class OrderItemSerializer(serializers.ModelSerializer):
    product = ProductSerializer()

    class Meta:
        model = OrderItem
        fields = ['id', 'product', 'quantity', 'unit_price']


class OrderSummarySerializer(serializers.ModelSerializer):
    """
    Summary serializer for the mobile dashboard.
    Added line items so the app can show order contents
    without a separate API call.
    """
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = ['id', 'status', 'total_amount', 'created_at', 'items']
