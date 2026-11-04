"""Central configuration.

Every value can be overridden with a ``JOBQUEUE_*`` environment variable so the
same code runs in dev, in tests and in production. Tests mutate the module-level
``settings`` object directly (the tasks read it at call time).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


def _env(name: str, default, cast):
    raw = os.environ.get(f"JOBQUEUE_{name.upper()}")
    return default if raw is None else cast(raw)


@dataclass
class Settings:
    # --- infrastructure -----------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    queue_name: str = "emails"
    # Short socket timeouts: a hung Redis must fail fast (and therefore fail
    # *closed* in the limiter) instead of freezing a worker slot.
    redis_socket_timeout_s: float = 2.0

    # --- provider limit: 200 emails / minute --------------------------------
    rate_limit: int = 200
    rate_window_s: float = 60.0
    rate_key: str = "jobqueue:ratelimit:email"
    # Test hook: when set, every admission is also appended to this Redis list
    # (with Redis' own clock) so tests can audit the limiter independently.
    rate_audit_key: str = ""
    # Random extra delay added to "come back later" retries so that ~1,800 denied
    # jobs do not all wake up in the same millisecond (thundering herd).
    rate_jitter_s: float = 5.0
    # How long to wait before retrying when the limiter itself is unavailable.
    limiter_error_retry_s: float = 5.0

    # --- retries / dead letter ----------------------------------------------
    max_attempts: int = 6            # provider failures before dead-lettering
    backoff_base_s: float = 5.0      # 5, 10, 20, 40, 80 ... (with jitter)
    backoff_cap_s: float = 120.0
    dead_letter_key: str = "jobqueue:dead"
    sent_key_prefix: str = "jobqueue:sent:"
    dedupe_ttl_s: int = 7 * 24 * 3600

    # --- Celery / crash recovery ---------------------------------------------
    # How long an un-acked message may sit with a (possibly dead) worker before
    # Redis hands it to another worker. See DESIGN.md "visibility timeout".
    visibility_timeout_s: int = 300
    task_soft_time_limit_s: int = 30
    task_time_limit_s: int = 45

    # --- provider -------------------------------------------------------------
    provider: str = "mock"           # only "mock" ships; plug a real one in providers.py

    @classmethod
    def from_env(cls) -> "Settings":
        kwargs = {}
        for f in fields(cls):
            default = f.default
            kwargs[f.name] = _env(f.name, default, type(default))
        return cls(**kwargs)

    def warnings(self) -> list[str]:
        """Config combinations that silently weaken the delivery guarantees."""
        out = []
        longest_hold = max(self.rate_window_s + self.rate_jitter_s, self.backoff_cap_s)
        if self.visibility_timeout_s <= longest_hold:
            out.append(
                f"visibility_timeout_s ({self.visibility_timeout_s}) <= longest ETA hold "
                f"({longest_hold:.0f}s): delayed retries will be redelivered while still "
                f"waiting, producing duplicate attempts."
            )
        if self.task_time_limit_s <= self.task_soft_time_limit_s:
            out.append("task_time_limit_s should be larger than task_soft_time_limit_s.")
        return out


settings = Settings.from_env()
