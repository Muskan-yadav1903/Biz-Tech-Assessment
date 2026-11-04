from django.urls import path

from .views import OrderSummaryFixedView, OrderSummaryView

urlpatterns = [
    path("summary/", OrderSummaryView.as_view(), name="order-summary"),
    path("summary-fixed/", OrderSummaryFixedView.as_view(), name="order-summary-fixed"),
]
