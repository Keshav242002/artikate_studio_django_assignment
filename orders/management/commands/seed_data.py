import random
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User

from orders.models import Product, Order, OrderItem
from tenants.context import reset_current_tenant, set_current_tenant
from tenants.models import Tenant


class Command(BaseCommand):
    help = 'Seeds the database with sample data for testing the N+1 problem'

    def add_arguments(self, parser):
        parser.add_argument(
            '--orders',
            type=int,
            default=250,
            help='Number of orders to create for the test user (default: 250)',
        )

    def handle(self, *args, **options):
        num_orders = options['orders']

        # management commands run outside the request/response cycle, so
        # TenantMiddleware never binds a tenant here — bind one explicitly
        # for the duration of this command instead.
        tenant, _ = Tenant.objects.get_or_create(
            slug='acme',
            defaults={'name': 'Acme Corp'},
        )
        token = set_current_tenant(tenant)
        try:
            self._seed(tenant, num_orders)
        finally:
            reset_current_tenant(token)

    def _seed(self, tenant, num_orders):
        # create a test user
        user, created = User.objects.get_or_create(
            username='testuser',
            defaults={'email': 'test@example.com'},
        )
        if created:
            user.set_password('testpass123')
            user.save()
            self.stdout.write(f'Created user: testuser (id={user.pk})')
        else:
            self.stdout.write(f'User already exists: testuser (id={user.pk})')

        # create some products if they don't exist
        product_names = [
            'Wireless Earbuds', 'Phone Case', 'USB-C Cable', 'Power Bank',
            'Screen Protector', 'Laptop Stand', 'Mechanical Keyboard',
            'Mouse Pad', 'Webcam', 'LED Desk Lamp',
        ]
        products = []
        for name in product_names:
            prod, _ = Product.objects.get_or_create(
                name=name,
                defaults={'price': Decimal(random.randint(200, 5000)) / 100},
            )
            products.append(prod)

        self.stdout.write(f'Products ready: {len(products)}')

        # skip if orders already exist
        existing = Order.objects.filter(user=user).count()
        if existing >= num_orders:
            self.stdout.write(f'Already have {existing} orders, skipping seed.')
            return

        # create orders with items
        statuses = ['pending', 'confirmed', 'shipped', 'delivered']
        orders_to_create = []
        for i in range(num_orders):
            orders_to_create.append(Order(
                tenant=tenant,
                user=user,
                status=random.choice(statuses),
                total_amount=0,
            ))

        Order.objects.bulk_create(orders_to_create)
        created_orders = Order.objects.filter(user=user).order_by('-pk')[:num_orders]

        items_to_create = []
        for order in created_orders:
            num_items = random.randint(1, 5)
            order_total = Decimal('0')
            for _ in range(num_items):
                product = random.choice(products)
                qty = random.randint(1, 3)
                items_to_create.append(OrderItem(
                    order=order,
                    product=product,
                    quantity=qty,
                    unit_price=product.price,
                ))
                order_total += product.price * qty
            order.total_amount = order_total

        OrderItem.objects.bulk_create(items_to_create)
        # update totals
        Order.objects.bulk_update(created_orders, ['total_amount'])

        self.stdout.write(self.style.SUCCESS(
            f'Created {num_orders} orders with {len(items_to_create)} items for user testuser (id={user.pk})'
        ))
        self.stdout.write(f'\nTo test, visit:')
        self.stdout.write(f'  Broken:  http://127.0.0.1:8000/api/orders/summary/?user_id={user.pk}')
        self.stdout.write(f'  Fixed:   http://127.0.0.1:8000/api/orders/summary-fixed/?user_id={user.pk}')
        self.stdout.write(f'  Silk:    http://127.0.0.1:8000/silk/')
