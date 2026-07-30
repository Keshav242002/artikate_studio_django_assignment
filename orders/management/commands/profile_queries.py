"""
Profile the broken vs fixed order summary endpoints.

Hits both endpoints, counts queries using Django's connection object,
and prints a comparison table. Also pulls stats from Silk's database
if available.

Usage:
    python manage.py profile_queries
"""
import time
from django.core.management.base import BaseCommand
from django.test import RequestFactory
from django.db import connection, reset_queries
from django.conf import settings

from orders.views import OrderSummaryView, OrderSummaryBrokenView
from orders.models import Order
from django.contrib.auth.models import User


class Command(BaseCommand):
    help = 'Profile broken vs fixed order summary endpoints and print query evidence'

    def handle(self, *args, **options):
        # make sure we can track queries
        settings.DEBUG = True

        user = User.objects.filter(username='testuser').first()
        if not user:
            self.stderr.write('Run "python manage.py seed_data" first.')
            return

        order_count = Order.objects.filter(user=user).count()
        self.stdout.write(f'\nUser: {user.username} (id={user.pk})')
        self.stdout.write(f'Orders: {order_count}\n')

        factory = RequestFactory()

        # --- Profile BROKEN endpoint ---
        reset_queries()
        request = factory.get(f'/api/orders/summary-broken/?user_id={user.pk}')
        start = time.time()
        response = OrderSummaryBrokenView.as_view()(request)
        response.render()
        broken_time = time.time() - start
        broken_queries = len(connection.queries)

        self.stdout.write('=' * 60)
        self.stdout.write('BROKEN ENDPOINT  (no prefetch_related)')
        self.stdout.write('=' * 60)
        self.stdout.write(f'  Total SQL queries: {broken_queries}')
        self.stdout.write(f'  Response time:     {broken_time:.3f}s')
        self.stdout.write(f'  Status code:       {response.status_code}')
        self.stdout.write(f'  Records returned:  {len(response.data)}')

        # show first few queries to demonstrate the N+1 pattern
        self.stdout.write(f'\n  First 6 SQL queries (showing N+1 pattern):')
        for i, q in enumerate(connection.queries[:6]):
            sql = q['sql']
            if len(sql) > 100:
                sql = sql[:100] + '...'
            self.stdout.write(f'    [{i+1}] {sql}')
        if broken_queries > 6:
            self.stdout.write(f'    ... and {broken_queries - 6} more queries')

        # --- Profile FIXED endpoint ---
        reset_queries()
        request = factory.get(f'/api/orders/summary/?user_id={user.pk}')
        start = time.time()
        response = OrderSummaryView.as_view()(request)
        response.render()
        fixed_time = time.time() - start
        fixed_queries = len(connection.queries)

        self.stdout.write(f'\n{"=" * 60}')
        self.stdout.write('FIXED ENDPOINT  (with prefetch_related)')
        self.stdout.write('=' * 60)
        self.stdout.write(f'  Total SQL queries: {fixed_queries}')
        self.stdout.write(f'  Response time:     {fixed_time:.3f}s')
        self.stdout.write(f'  Status code:       {response.status_code}')
        self.stdout.write(f'  Records returned:  {len(response.data)}')

        self.stdout.write(f'\n  All {fixed_queries} SQL queries:')
        for i, q in enumerate(connection.queries):
            sql = q['sql']
            if len(sql) > 120:
                sql = sql[:120] + '...'
            self.stdout.write(f'    [{i+1}] {sql}')

        # --- Summary ---
        self.stdout.write(f'\n{"=" * 60}')
        self.stdout.write('SUMMARY')
        self.stdout.write('=' * 60)
        self.stdout.write(f'  {"Metric":<25} {"Broken":>10} {"Fixed":>10} {"Improvement":>15}')
        self.stdout.write(f'  {"-"*25} {"-"*10} {"-"*10} {"-"*15}')
        self.stdout.write(f'  {"SQL queries":<25} {broken_queries:>10} {fixed_queries:>10} {broken_queries/fixed_queries:>14.0f}x fewer')
        self.stdout.write(f'  {"Response time":<25} {broken_time:>9.3f}s {fixed_time:>9.3f}s {broken_time/fixed_time:>14.1f}x faster')
        self.stdout.write('')
