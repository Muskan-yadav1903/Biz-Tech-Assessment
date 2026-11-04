"""Unit tests for the Redis sliding-window limiter."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import redis

from jobqueue.rate_limiter import SlidingWindowLimiter


def test_admits_up_to_limit_then_denies_with_wait_hint(redis_client):
    limiter = SlidingWindowLimiter(redis_client, "test:rl", limit=5, window_s=1.0)

    assert [limiter.acquire().allowed for _ in range(5)] == [True] * 5

    denied = limiter.acquire()
    assert denied.allowed is False
    assert 0 < denied.retry_after_s <= 1.0


def test_window_slides_instead_of_resetting(redis_client):
    limiter = SlidingWindowLimiter(redis_client, "test:rl", limit=3, window_s=1.0)
    for _ in range(3):               # admissions at t = 0, 0.3, 0.6 s
        assert limiter.acquire().allowed
        time.sleep(0.3)

    denied = limiter.acquire()       # t = 0.9 s: window still holds all three
    assert not denied.allowed

    time.sleep(denied.retry_after_s + 0.05)  # t ~ 1.0 s: only the t=0 entry has aged out
    assert limiter.acquire().allowed
    # only ONE slot freed up (a fixed-window counter would have reset all three)
    assert not limiter.acquire().allowed


def test_atomic_under_heavy_concurrency(redis_client):
    """64 threads hammering the limiter must admit EXACTLY `limit` requests.
    A naive GET-then-INCR implementation overshoots here."""
    limit = 50
    limiter = SlidingWindowLimiter(redis_client, "test:rl", limit=limit, window_s=30)

    def worker(_):
        return sum(limiter.acquire().allowed for _ in range(20))

    with ThreadPoolExecutor(max_workers=64) as pool:
        admitted = sum(pool.map(worker, range(64)))

    assert admitted == limit
    assert redis_client.zcard("test:rl") == limit


def test_fails_closed_when_redis_is_unreachable():
    """If the limiter cannot reach Redis it must RAISE, never return 'allowed'."""
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)
    limiter = SlidingWindowLimiter(dead, "test:rl", limit=5, window_s=1.0)
    with pytest.raises(redis.RedisError):
        limiter.acquire()
