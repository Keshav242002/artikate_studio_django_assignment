from django.urls import path
from .views import OrderSummaryView

urlpatterns = [
    path('orders/summary/', OrderSummaryView.as_view(), name='order-summary'),
]
