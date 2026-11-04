from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from .models import Order, OrderItem

User = get_user_model()

SLOW_URL = "/api/orders/summary/"
FAST_URL = "/api/orders/summary-fixed/"


def make_user_with_orders(username, order_count, items_per_order=4):
    """Helper: a user with `order_count` orders, each with a few items."""
    user = User.objects.create_user(username=username)
    Order.objects.bulk_create([Order(user=user) for _ in range(order_count)])
    orders = list(Order.objects.filter(user=user))
    OrderItem.objects.bulk_create(
        [
            OrderItem(order=o, product_name="Thing", price=Decimal("10.50"), quantity=2)
            for o in orders
            for _ in range(items_per_order)
        ]
    )
    return user


class OrderSummaryQueryCountTests(TestCase):
    def setUp(self):
        self.heavy = make_user_with_orders("heavy", 250)  # the user from the bug report (>200 orders)
        self.light = make_user_with_orders("light", 20)

    def _count_queries(self, user, url):
        self.client.force_login(user)
        with CaptureQueriesContext(connection) as ctx:
            response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        # django-silk adds its own bookkeeping SQL during the request (silk_* inserts,
        # EXPLAIN of each query, BEGIN/COMMIT/SAVEPOINT). That is the profiler's cost,
        # not the endpoint's, so only count the app's real queries.
        noise = ("silk_", "EXPLAIN", "BEGIN", "COMMIT", "SAVEPOINT", "RELEASE")
        app_queries = [q for q in ctx.captured_queries if not any(n in q["sql"] for n in noise)]
        return len(app_queries), response.json()

    def test_broken_endpoint_is_n_plus_one(self):
        # 3 queries per order (user, COUNT, items) -> must be way above the order count
        count, data = self._count_queries(self.heavy, SLOW_URL)
        self.assertEqual(len(data), 250)
        self.assertGreater(count, 3 * 250)

    def test_fixed_endpoint_uses_constant_queries(self):
        heavy_count, _ = self._count_queries(self.heavy, FAST_URL)
        light_count, _ = self._count_queries(self.light, FAST_URL)
        # session + auth user + ONE query for orders (JOIN + GROUP BY)
        self.assertLessEqual(heavy_count, 3)
        # and it must NOT grow with the number of orders (250 vs 20 -> same count)
        self.assertEqual(heavy_count, light_count)

    def test_fix_returns_identical_data(self):
        # A performance fix must not change what the mobile app receives.
        self.client.force_login(self.heavy)
        slow = self.client.get(SLOW_URL).json()
        fast = self.client.get(FAST_URL).json()
        self.assertEqual(slow, fast)
        self.assertEqual(fast[0]["item_count"], 4)
        self.assertEqual(float(fast[0]["total"]), 84.0)  # 4 items * 2 qty * 10.50

    def test_order_without_items_gets_zero_total(self):
        # Edge case: SUM over zero rows is NULL in SQL; Coalesce must turn it into 0.00
        empty_user = User.objects.create_user(username="empty")
        Order.objects.create(user=empty_user)
        self.client.force_login(empty_user)
        row = self.client.get(FAST_URL).json()[0]
        self.assertEqual(row["item_count"], 0)
        self.assertEqual(float(row["total"]), 0.0)

    def test_user_only_sees_own_orders(self):
        self.client.force_login(self.light)
        self.assertEqual(len(self.client.get(FAST_URL).json()), 20)
