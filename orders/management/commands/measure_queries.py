from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.test import Client
from silk.models import Request as SilkRequest

User = get_user_model()

ENDPOINTS = [
    ("BEFORE (broken)", "/api/orders/summary/"),
    ("AFTER  (fixed)", "/api/orders/summary-fixed/"),
]


class Command(BaseCommand):
    help = "Hit the broken and fixed endpoints as heavy_user/light_user and print what django-silk recorded."

    def handle(self, *args, **options):
        SilkRequest.objects.all().delete()  # start from a clean profiler history

        lines = []
        header = f"{'user':<11} {'endpoint':<17} {'orders':>6} {'SQL queries':>12} {'time (ms)':>10}"
        lines += [header, "-" * len(header)]

        for username in ("heavy_user", "light_user"):
            user = User.objects.get(username=username)
            for label, path in ENDPOINTS:
                client = Client(HTTP_HOST="localhost")
                client.force_login(user)  # no password needed
                response = client.get(path)
                assert response.status_code == 200, response.status_code

                # Silk saved this request (path, SQL count, time). Its ids are UUIDs, so pick the newest by start_time.
                rec = SilkRequest.objects.filter(path=path).order_by("-start_time").first()
                lines.append(
                    f"{username:<11} {label:<17} {len(response.json()):>6} "
                    f"{rec.num_sql_queries:>12} {rec.time_taken:>10.0f}"
                )

        out = "\n".join(lines)
        self.stdout.write(out)
        self.stdout.write("\nSame numbers are visible in the browser at /silk/ (run `python manage.py runserver`).")
