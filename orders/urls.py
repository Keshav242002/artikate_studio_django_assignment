from django.urls import path
from .views import OrderSummaryView, OrderSummaryBrokenView

urlpatterns = [
    path('orders/summary/', OrderSummaryView.as_view(), name='order-summary'),
    # kept for profiling comparison — shows the N+1 problem
    path('orders/summary-broken/', OrderSummaryBrokenView.as_view(), name='order-summary-broken'),
]
