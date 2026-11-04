"""Simulate the flash-sale burst and watch the queue drain at the provider limit.

    python -m scripts.burst --n 2000

Start a worker first (``make worker``). Uses the mock provider; set
``MOCK_PROVIDER_FAIL_RATE=0.05`` on the *worker* to watch retries happen.
"""
from __future__ import annotations

import argparse
import time

from jobqueue.config import settings
from jobqueue.providers import MOCK_SENT_KEY
from jobqueue.redis_client import get_redis
from jobqueue.tasks import enqueue_email


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=2000)
    args = parser.parse_args()
    r = get_redis()
    r.delete(MOCK_SENT_KEY)

    t0 = time.monotonic()
    for i in range(args.n):
        enqueue_email(f"user{i}@example.com", "Order confirmation", "Thanks for your order!", job_id=f"burst-{i}")
    print(f"enqueued {args.n} jobs in {time.monotonic() - t0:.2f}s "
          f"(expected drain ~{args.n / settings.rate_limit * settings.rate_window_s / 60:.1f} min)")

    while True:
        sent = r.hlen(MOCK_SENT_KEY)
        print(f"[{time.monotonic() - t0:6.1f}s] sent={sent}/{args.n} dead={r.llen(settings.dead_letter_key)}")
        if sent >= args.n:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
