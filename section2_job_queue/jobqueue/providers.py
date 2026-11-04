"""Email provider abstraction + a Redis-backed mock.

The real third-party provider (SES, SendGrid, ...) would implement ``send`` with
an HTTP call, a client-side timeout, and the same two exception types:

* ``TransientEmailError``  - 5xx, timeout, connection reset  -> retry with backoff
* ``PermanentEmailError``  - 4xx such as invalid address     -> dead-letter now

``idempotency_key`` is our ``job_id``. Providers that support idempotency keys
(SES, Stripe-style APIs, SendGrid ``X-Entity-Ref-ID``) will drop a repeat of the
same key, which closes the one remaining duplicate window (see DESIGN.md).
"""
from __future__ import annotations

import os
import random
import time
from typing import Protocol

from jobqueue.config import settings
from jobqueue.redis_client import get_redis

MOCK_STARTED_KEY = "mock:started"  # hash: job_id -> times a send began
MOCK_SENT_KEY = "mock:sent"        # hash: job_id -> times a send succeeded


class TransientEmailError(Exception):
    """Temporary failure; safe to retry."""


class PermanentEmailError(Exception):
    """The provider rejected this email for good; retrying cannot help."""


class EmailProvider(Protocol):
    def send(self, *, idempotency_key: str, to: str, subject: str, body: str) -> None: ...


class MockProvider:
    """Pretends to be the provider. State lives in Redis so that separate worker
    processes (and the SIGKILL test) can be observed from outside.

    ``MOCK_PROVIDER_DELAY_S``   simulated latency per send
    ``MOCK_PROVIDER_FAIL_RATE`` fraction of sends that raise TransientEmailError
    """

    def __init__(self, delay_s: float | None = None, fail_rate: float | None = None) -> None:
        self.delay_s = float(os.environ.get("MOCK_PROVIDER_DELAY_S", 0)) if delay_s is None else delay_s
        self.fail_rate = float(os.environ.get("MOCK_PROVIDER_FAIL_RATE", 0)) if fail_rate is None else fail_rate

    def send(self, *, idempotency_key: str, to: str, subject: str, body: str) -> None:
        r = get_redis()
        r.hincrby(MOCK_STARTED_KEY, idempotency_key, 1)
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail_rate and random.random() < self.fail_rate:
            raise TransientEmailError("mock provider: simulated 503")
        r.hincrby(MOCK_SENT_KEY, idempotency_key, 1)


_provider: EmailProvider | None = None


def get_provider() -> EmailProvider:
    global _provider
    if _provider is None:
        if settings.provider != "mock":
            raise RuntimeError(
                f"Unknown provider {settings.provider!r}; implement it in providers.py"
            )
        _provider = MockProvider()
    return _provider


def set_provider(provider: EmailProvider | None) -> None:
    """Swap the provider (used by tests)."""
    global _provider
    _provider = provider
