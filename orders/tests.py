from decimal import Decimal

from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User

from orders.models import Product, Order, OrderItem
from orders.views import OrderSummaryView


class OrderSummaryTest(TestCase):

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='testuser', password='pass')
        product = Product.objects.create(name='Widget', price=Decimal('10.00'))

        for i in range(20):
            order = Order.objects.create(
                user=cls.user, status='confirmed', total_amount=Decimal('30.00'),
            )
            for _ in range(3):
                OrderItem.objects.create(
                    order=order, product=product,
                    quantity=1, unit_price=Decimal('10.00'),
                )

    def _get_response(self, user_id):
        factory = RequestFactory()
        request = factory.get(f'/api/orders/summary/?user_id={user_id}')
        view = OrderSummaryView.as_view()
        response = view(request)
        response.render()
        return response

    def test_returns_orders_for_user(self):
        response = self._get_response(self.user.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 20)

    def test_empty_for_unknown_user(self):
        response = self._get_response(99999)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 0)

    def test_flat_serializer_minimal_queries(self):
        """Flat serializer should only fire 1 query."""
        with self.assertNumQueries(1):
            self._get_response(self.user.pk)
