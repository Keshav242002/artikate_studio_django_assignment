from decimal import Decimal

from django.test import TestCase, RequestFactory
from django.contrib.auth.models import User

from orders.models import Product, Order, OrderItem
from orders.views import OrderSummaryView, OrderSummaryBrokenView


class OrderSummaryQueryCountTest(TestCase):
    """
    Proves the N+1 exists in the broken view and that
    the fix resolves it without changing the response.
    """

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='querytest', password='pass')

        products = []
        for i in range(5):
            products.append(Product.objects.create(
                name=f'Product {i}', price=Decimal('10.00'),
            ))

        # 50 orders, 3 items each → enough to see the N+1 clearly
        for i in range(50):
            order = Order.objects.create(
                user=cls.user,
                status='confirmed',
                total_amount=Decimal('30.00'),
            )
            for j in range(3):
                OrderItem.objects.create(
                    order=order,
                    product=products[j % 5],
                    quantity=1,
                    unit_price=Decimal('10.00'),
                )

    def _make_request(self, view_class, user_id):
        factory = RequestFactory()
        request = factory.get(f'/api/orders/summary/?user_id={user_id}')
        view = view_class.as_view()
        response = view(request)
        response.render()
        return response

    def test_broken_view_fires_n_plus_1_queries(self):
        """
        Without prefetch_related, the serializer causes:
          1 query for orders
          + 50 queries for items (one per order)
          + 150 queries for products (one per item)
          = 201 total
        """
        with self.assertNumQueries(201):
            response = self._make_request(OrderSummaryBrokenView, self.user.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 50)

    def test_fixed_view_uses_3_queries(self):
        """
        With prefetch_related('items__product'), Django batches
        everything into 3 queries regardless of order count:
          1. orders
          2. items WHERE order_id IN (...)
          3. products WHERE id IN (...)
        """
        with self.assertNumQueries(3):
            response = self._make_request(OrderSummaryView, self.user.pk)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 50)

    def test_fix_does_not_change_response_data(self):
        """The fix is a performance optimization — output must be identical."""
        broken = self._make_request(OrderSummaryBrokenView, self.user.pk)
        fixed = self._make_request(OrderSummaryView, self.user.pk)
        self.assertEqual(broken.data, fixed.data)

    def test_empty_result_for_unknown_user(self):
        response = self._make_request(OrderSummaryView, 99999)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 0)
