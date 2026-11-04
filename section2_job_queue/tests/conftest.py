"""Shared fixtures. Tests run against a REAL Redis (the Lua script and Celery's
Redis transport are the things under test, so a fake would prove nothing).

Safety: tests use Redis DB 15 by default and refuse to run against DB 0, because
every test starts with FLUSHDB.
"""
from __future__ import annotations

import os
import threading
import time
from collections import Counter

import pytest
import redis

TEST_REDIS_URL = os.environ.get("JOBQUEUE_TEST_REDIS_URL", "redis://localhost:6379/15")
# Must be set BEFORE jobqueue is imported anywhere.
os.environ["JOBQUEUE_REDIS_URL"] = TEST_REDIS_URL

from celery.contrib.testing.worker import start_worker  # noqa: E402

from jobqueue import providers  # noqa: E402
from jobqueue.celery_app import app  # noqa: E402
from jobqueue.config import settings  # noqa: E402
from jobqueue.providers import PermanentEmailError, TransientEmailError  # noqa: E402


@pytest.fixture(scope="session")
def redis_client():
    client = redis.Redis.from_url(TEST_REDIS_URL, decode_responses=True)
    try:
        client.ping()
    except redis.RedisError:
        pytest.exit(
            f"Cannot reach Redis at {TEST_REDIS_URL}.\n"
            "Start one with:  docker compose up -d redis   (or: redis-server --daemonize yes)",
            returncode=2,
        )
    if client.connection_pool.connection_kwargs.get("db", 0) == 0:
        pytest.exit("Refusing to FLUSHDB on Redis DB 0. Use a non-zero DB for tests.", returncode=2)
    return client


@pytest.fixture(autouse=True)
def clean_redis(redis_client):
    redis_client.flushdb()
    yield
    redis_client.flushdb()


@pytest.fixture
def fast_settings(monkeypatch):
    """Same code paths as production, scaled so a 500-job run takes seconds, not minutes:
    100 emails / 2 s instead of 200 / 60 s."""
    values = dict(
        rate_limit=100,
        rate_window_s=2.0,
        rate_jitter_s=0.3,
        limiter_error_retry_s=0.3,
        backoff_base_s=0.2,
        backoff_cap_s=1.0,
        max_attempts=4,
        rate_audit_key="test:audit",
    )
    for k, v in values.items():
        monkeypatch.setattr(settings, k, v)
    return settings


class RecordingProvider:
    """In-memory provider that records every call and can be told how to fail."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = Counter()       # job_id -> provider calls (attempts)
        self.successes = Counter()   # job_id -> successful sends
        self.fail_first = {}         # job_id -> number of initial transient failures
        self.always_fail = set()     # transient failure forever
        self.permanent = set()       # PermanentEmailError

    def send(self, *, idempotency_key, to, subject, body):
        with self._lock:
            self.calls[idempotency_key] += 1
            if idempotency_key in self.permanent:
                raise PermanentEmailError("mailbox does not exist")
            if idempotency_key in self.always_fail:
                raise TransientEmailError("provider 503")
            if self.fail_first.get(idempotency_key, 0) > 0:
                self.fail_first[idempotency_key] -= 1
                raise TransientEmailError("provider 503")
            self.successes[idempotency_key] += 1

    @property
    def total_calls(self):
        with self._lock:
            return sum(self.calls.values())

    @property
    def delivered(self):
        with self._lock:
            return set(self.successes)


@pytest.fixture
def provider():
    p = RecordingProvider()
    providers.set_provider(p)
    yield p
    providers.set_provider(None)


@pytest.fixture
def worker(fast_settings, provider):
    """A real Celery worker (threads pool) running inside the test process,
    talking to the real Redis broker."""
    with start_worker(
        app,
        pool="threads",
        concurrency=16,
        loglevel="ERROR",
        perform_ping_check=False,
        shutdown_timeout=15,
    ):
        yield


def wait_until(predicate, timeout=60.0, interval=0.1, message="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")
