from django.conf import settings
from django.db import models


class Order(models.Model):
    # ForeignKey fields get a DB index automatically, so filtering by user is NOT
    # the slow part here (we rule "missing index" out in ANSWERS.md).
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders"
    )
    status = models.CharField(max_length=20, default="paid")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]  # fixed ordering so slow and fast endpoints return the same list

    def __str__(self):
        return f"Order #{self.pk}"


class OrderItem(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product_name = models.CharField(max_length=100)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)

    def __str__(self):
        return f"{self.quantity} x {self.product_name}"
