"""End-to-end tests: real Celery worker + real Redis broker + real Lua limiter."""
from __future__ import annotations

import time

from redis.exceptions import ConnectionError as RedisConnectionError

from jobqueue import dead_letter, tasks
from jobqueue.tasks import enqueue_email
from tests.conftest import wait_until


def _enqueue(job_id):
    enqueue_email(to=f"{job_id}@example.com", subject="Order confirmed", body="Thanks!", job_id=job_id)


def test_500_jobs_nothing_lost_rate_limit_never_exceeded_failures_retried(
    worker, provider, redis_client, fast_settings
):
    ids = [f"job-{i:03d}" for i in range(500)]

    # Intentional failures: 10 jobs fail once, one job fails twice.
    for job_id in ids[::50]:
        provider.fail_first[job_id] = 1
    provider.fail_first[ids[7]] = 2

    started = time.monotonic()
    for job_id in ids:       # the "flash sale burst"
        _enqueue(job_id)
    enqueue_seconds = time.monotonic() - started

    wait_until(lambda: len(provider.delivered) == 500, timeout=120, message="all 500 jobs delivered")
    elapsed = time.monotonic() - started

    # --- 1. no job is lost, none is dead-lettered, none is sent twice ---------
    assert provider.delivered == set(ids)
    assert dead_letter.count(redis_client) == 0
    assert all(provider.successes[j] == 1 for j in ids), "a job was delivered more than once"

    # --- 2. the rate limit was never exceeded ---------------------------------
    # The limiter records every admission with Redis' own clock. In ANY window of
    # `rate_window_s`, at most `rate_limit` admissions may exist: i.e. admission i
    # must come at least one full window after admission i - limit.
    audit = sorted(int(x) for x in redis_client.lrange(fast_settings.rate_audit_key, 0, -1))
    limit, window_ms = fast_settings.rate_limit, int(fast_settings.rate_window_s * 1000)
    violations = [i for i in range(limit, len(audit)) if audit[i] - audit[i - limit] < window_ms]
    assert not violations, f"rate limit exceeded at admissions {violations[:5]}"

    # ... and nothing reached the provider without passing through the limiter.
    assert len(audit) == provider.total_calls

    # Sanity: it really was throttled (500 jobs + retries at 100 / 2 s needs >= 5 windows)
    assert enqueue_seconds < 10, "publishing the burst should be near-instant"
    assert elapsed >= 8, f"finished in {elapsed:.1f}s - limiter does not look like it throttled"

    # --- 3. intentional failures were retried correctly -----------------------
    for job_id in ids[::50]:
        expected_calls = 3 if job_id == ids[7] else 2
        assert provider.calls[job_id] == expected_calls
    assert provider.calls[ids[7]] == 3          # failed twice, succeeded on 3rd
    assert provider.calls[ids[1]] == 1          # healthy jobs: exactly one attempt


def test_permanent_failures_are_dead_lettered_and_replayable(worker, provider, redis_client):
    healthy = [f"ok-{i}" for i in range(5)]
    provider.always_fail.add("always-503")      # exhausts max_attempts (4)
    provider.permanent.add("bad-address")       # must NOT be retried at all

    for job_id in healthy + ["always-503", "bad-address"]:
        _enqueue(job_id)

    wait_until(lambda: dead_letter.count(redis_client) == 2, timeout=30, message="2 dead letters")
    wait_until(lambda: provider.delivered == set(healthy), timeout=30, message="healthy jobs sent")

    assert provider.calls["always-503"] == 4    # max_attempts
    assert provider.calls["bad-address"] == 1   # permanent -> straight to DLQ
    dead = {d["job"]["job_id"]: d for d in dead_letter.list_jobs(redis_client)}
    assert set(dead) == {"always-503", "bad-address"}
    assert dead["always-503"]["attempts"] == 4
    assert dead["bad-address"]["job"]["to"] == "bad-address@example.com"  # payload preserved

    # Operator fixes the cause, replays the dead letters -> they are delivered.
    provider.always_fail.clear()
    provider.permanent.clear()
    assert dead_letter.requeue_all(redis_client) == 2
    wait_until(
        lambda: provider.delivered == set(healthy) | {"always-503", "bad-address"},
        timeout=30, message="replayed jobs delivered",
    )
    assert dead_letter.count(redis_client) == 0


def test_redelivered_message_is_not_sent_twice(worker, provider, redis_client):
    """At-least-once delivery (acks_late) means the broker may hand us the same
    message again. The sent-marker must turn that into a no-op."""
    _enqueue("order-42-confirmation")
    wait_until(lambda: "order-42-confirmation" in provider.delivered, message="first delivery")

    _enqueue("order-42-confirmation")  # simulated redelivery
    time.sleep(1.5)                    # give the worker time to (not) process it

    assert provider.successes["order-42-confirmation"] == 1
    assert provider.calls["order-42-confirmation"] == 1
    assert redis_client.llen("emails") == 0  # it was consumed, just ignored


def test_limiter_outage_fails_closed_then_recovers(worker, provider, monkeypatch):
    """While the limiter cannot be reached NOTHING may be sent. When it comes
    back, the job is still delivered (it was retried, not dropped)."""
    real_get_limiter = tasks.get_limiter
    state = {"attempts": 0}

    class FlakyLimiter:
        def acquire(self):
            state["attempts"] += 1
            if state["attempts"] <= 3:
                raise RedisConnectionError("simulated Redis outage")
            return real_get_limiter().acquire()

    monkeypatch.setattr(tasks, "get_limiter", lambda: FlakyLimiter())

    _enqueue("otp-1")
    wait_until(lambda: "otp-1" in provider.delivered, timeout=30, message="otp-1 delivered after recovery")

    assert state["attempts"] == 4          # 3 failed limiter calls + 1 success
    assert provider.calls["otp-1"] == 1    # provider was called only once the limiter was back
