import jwt
from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase

from orders.models import Order
from tenants.context import get_current_tenant, set_current_tenant
from tenants.middleware import TenantMiddleware
from tenants.models import Tenant


class TenantIsolationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_a = Tenant.objects.create(name='Tenant A', slug='tenant-a')
        cls.tenant_b = Tenant.objects.create(name='Tenant B', slug='tenant-b')
        cls.user = User.objects.create_user(username='shared-user', password='pass')

        cls.order_a = Order.objects.create(
            tenant=cls.tenant_a, user=cls.user,
            status='confirmed', total_amount=Decimal('10.00'),
        )
        cls.order_b = Order.objects.create(
            tenant=cls.tenant_b, user=cls.user,
            status='confirmed', total_amount=Decimal('20.00'),
        )

    def tearDown(self):
        # belt-and-braces: make sure no test leaks tenant context into the next
        set_current_tenant(None)

    def test_tenant_a_cannot_see_tenant_b_orders(self):
        set_current_tenant(self.tenant_a)
        orders = Order.objects.all()
        self.assertNotIn(self.order_b, orders)
        self.assertTrue(all(o.tenant_id == self.tenant_a.id for o in orders))

    def test_all_does_not_bypass_scoping(self):
        set_current_tenant(self.tenant_a)
        self.assertEqual(
            Order.objects.all().count(),
            Order.objects.filter(tenant=self.tenant_a).count(),
        )

    def test_filter_and_get_are_also_scoped(self):
        # proves scoping happens in get_queryset(), not just all() —
        # any manager entry point must be constrained to the current tenant.
        set_current_tenant(self.tenant_a)
        self.assertEqual(Order.objects.filter(status='confirmed').count(), 1)
        with self.assertRaises(Order.DoesNotExist):
            Order.objects.get(pk=self.order_b.pk)

    def test_no_tenant_context_returns_nothing(self):
        # fail closed: a forgotten context-set must not leak every tenant's rows
        set_current_tenant(None)
        self.assertEqual(Order.objects.all().count(), 0)

    def test_middleware_resolves_tenant_from_subdomain(self):
        factory = RequestFactory()
        request = factory.get('/api/orders/summary/', HTTP_HOST='tenant-a.example.com')

        seen = {}

        def get_response(req):
            seen['tenant'] = get_current_tenant()
            return _StubResponse()

        middleware = TenantMiddleware(get_response)
        middleware(request)

        self.assertEqual(seen['tenant'], self.tenant_a)
        # context is reset after the request completes
        self.assertIsNone(get_current_tenant())

    def test_middleware_falls_back_to_jwt_header(self):
        token = jwt.encode({'tenant_id': self.tenant_b.id}, settings.SECRET_KEY, algorithm='HS256')
        factory = RequestFactory()
        request = factory.get(
            '/api/orders/summary/',
            HTTP_HOST='unknown-host.example.com',
            HTTP_AUTHORIZATION=f'Bearer {token}',
        )

        seen = {}

        def get_response(req):
            seen['tenant'] = get_current_tenant()
            return _StubResponse()

        middleware = TenantMiddleware(get_response)
        middleware(request)

        self.assertEqual(seen['tenant'], self.tenant_b)

    def test_middleware_resets_context_after_request_even_on_exception(self):
        factory = RequestFactory()
        request = factory.get('/api/orders/summary/', HTTP_HOST='tenant-a.example.com')

        def get_response(req):
            raise RuntimeError('boom')

        middleware = TenantMiddleware(get_response)
        with self.assertRaises(RuntimeError):
            middleware(request)

        self.assertIsNone(get_current_tenant())


class _StubResponse:
    """Minimal stand-in for an HttpResponse — the middleware doesn't inspect it."""
