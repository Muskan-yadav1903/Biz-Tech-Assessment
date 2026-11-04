import random
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from orders.models import Order, OrderItem

User = get_user_model()


class Command(BaseCommand):
    help = "Create demo data: one 'heavy' user (250 orders, the slow case) and one 'light' user (20 orders)."

    def handle(self, *args, **options):
        random.seed(42)  # same data every run, so results are comparable
        for username, order_count in [("heavy_user", 250), ("light_user", 20)]:
            user, _ = User.objects.get_or_create(username=username)
            user.set_unusable_password()  # no credentials in the repo; we log in via test client / force_login
            user.save()

            # Start clean so running the command twice doesn't double the data
            Order.objects.filter(user=user).delete()

            orders = Order.objects.bulk_create([Order(user=user) for _ in range(order_count)])
            # bulk_create returns pks on SQLite 3.35+, but re-fetch to be safe on any DB
            orders = list(Order.objects.filter(user=user))

            items = []
            for order in orders:
                for n in range(random.randint(3, 6)):  # 3-6 items per order
                    items.append(
                        OrderItem(
                            order=order,
                            product_name=f"Product {random.randint(1, 50)}",
                            price=Decimal(random.randint(100, 5000)) / 100,
                            quantity=random.randint(1, 4),
                        )
                    )
            OrderItem.objects.bulk_create(items)
            self.stdout.write(f"{username}: {len(orders)} orders, {len(items)} items")
