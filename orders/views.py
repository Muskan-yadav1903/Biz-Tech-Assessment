from decimal import Decimal

from django.db.models import Count, DecimalField, F, Sum, Value
from django.db.models.functions import Coalesce
from rest_framework.generics import ListAPIView

from .models import Order
from .serializers import OrderSummaryFastSerializer, OrderSummarySerializer


class OrderSummaryView(ListAPIView):
    """
    /api/orders/summary/  -- the BROKEN endpoint.

    Notice this view is perfectly innocent: it is one simple queryset.
    It was unchanged by the deployment; the slowness comes from the serializer
    (see serializers.py) that evaluates this queryset row by row.
    """

    serializer_class = OrderSummarySerializer

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user)


class OrderSummaryFixedView(ListAPIView):
    """
    /api/orders/summary-fixed/  -- the FIXED endpoint (kept side by side so
    both can be profiled and compared; in a real repo you would just edit the
    original view).
    """

    serializer_class = OrderSummaryFastSerializer

    def get_queryset(self):
        return (
            Order.objects.filter(user=self.request.user)
            # JOIN auth_user in the same query -> no extra query to read user.username
            .select_related("user")
            # Let the DB do COUNT and SUM with GROUP BY, in the same query.
            .annotate(
                item_count=Count("items"),
                # Coalesce -> an order with zero items gets 0.00 instead of NULL
                total=Coalesce(
                    Sum(
                        F("items__price") * F("items__quantity"),
                        output_field=DecimalField(max_digits=12, decimal_places=2),
                    ),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                ),
            )
        )
