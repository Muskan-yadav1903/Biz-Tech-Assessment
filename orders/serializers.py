from decimal import Decimal

from rest_framework import serializers

from .models import Order


class OrderSummarySerializer(serializers.ModelSerializer):
    """
    THE REGRESSION. This is what the "routine deployment" shipped:
    the dashboard wanted an item count and an order total on every row,
    so two SerializerMethodFields were added.

    The view file was NOT touched, so nobody looked at it. But the serializer
    now runs queries *inside the per-order loop*:
        - order.user.username  -> 1 query per order (lazy ForeignKey load)
        - order.items.count()  -> 1 COUNT query per order
        - order.items.all()    -> 1 SELECT per order
    That is roughly 3 x N extra queries for N orders (the classic N+1).
    """

    customer = serializers.CharField(source="user.username")  # lazy FK -> query per row
    item_count = serializers.SerializerMethodField()
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "status", "created_at", "customer", "item_count", "total"]

    def get_item_count(self, order):
        return order.items.count()  # hits the DB every single time it is called

    def get_total(self, order):
        # Another query per order, plus Python-side summing of every row.
        return sum((i.price * i.quantity for i in order.items.all()), Decimal("0.00"))


class OrderSummaryFastSerializer(serializers.ModelSerializer):
    """
    THE FIX (serializer side). Same JSON output, but it never touches the DB:
    `customer` comes from a select_related JOIN, and `item_count` / `total`
    are values the database already computed (annotate) in the main query.
    """

    customer = serializers.CharField(source="user.username")
    item_count = serializers.IntegerField()
    # coerce_to_string=False -> JSON number, exactly like the old endpoint (mobile app contract)
    total = serializers.DecimalField(max_digits=12, decimal_places=2, coerce_to_string=False)

    class Meta:
        model = Order
        fields = ["id", "status", "created_at", "customer", "item_count", "total"]
