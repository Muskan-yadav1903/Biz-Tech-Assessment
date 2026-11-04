"""The email task: rate-limit gate -> send -> retry / dead-letter.

Delivery semantics: **at-least-once** from Celery (acks_late) made effectively
**exactly-once** by a per-job "already sent" marker (+ provider idempotency key).

Two different kinds of "try again later", deliberately kept separate:

* rate-limited / limiter unavailable -> NOT a failure. Re-schedule, ``attempt``
  unchanged. A job must never be dead-lettered just because a flash sale made
  it wait in line.
* provider failure -> a real failed attempt. ``attempt`` += 1, exponential
  backoff, and after ``max_attempts`` the job is dead-lettered.
"""
from __future__ import annotations

import random
import uuid

from celery.utils.log import get_task_logger
from redis.exceptions import RedisError

from jobqueue import dead_letter
from jobqueue.celery_app import app
from jobqueue.config import settings
from jobqueue.providers import PermanentEmailError, get_provider
from jobqueue.rate_limiter import SlidingWindowLimiter
from jobqueue.redis_client import get_redis

log = get_task_logger(__name__)


def get_limiter() -> SlidingWindowLimiter:
    return SlidingWindowLimiter(
        get_redis(),
        key=settings.rate_key,
        limit=settings.rate_limit,
        window_s=settings.rate_window_s,
        audit_key=settings.rate_audit_key,
    )


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with 'equal jitter': half fixed, half random.

    attempt=1 -> ~base, 2 -> ~2*base, ... capped. Jitter stops a provider outage
    from being followed by every failed job retrying in lock-step.
    """
    ceiling = min(settings.backoff_cap_s, settings.backoff_base_s * 2 ** (attempt - 1))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def sent_key(job_id: str) -> str:
    return f"{settings.sent_key_prefix}{job_id}"


@app.task(bind=True, name="jobqueue.send_email", max_retries=None)  # we enforce our own budget
def send_email(self, job_id: str, to: str, subject: str, body: str, attempt: int = 0):
    job = {"job_id": job_id, "to": to, "subject": subject, "body": body}
    redis_client = get_redis()

    # 1. Gate: duplicate check + rate limit. Any Redis problem here FAILS CLOSED:
    #    we do not send, we try again shortly. (Emailing past the provider's limit
    #    risks throttling/suspension of the whole account; a delay is cheaper.)
    try:
        if redis_client.exists(sent_key(job_id)):
            log.info("job %s already sent; skipping duplicate delivery", job_id)
            return "duplicate"
        decision = get_limiter().acquire()
    except RedisError as exc:
        log.warning("job %s: limiter unavailable (%s); failing closed, retrying", job_id, exc)
        delay = settings.limiter_error_retry_s + random.uniform(0, settings.rate_jitter_s)
        raise self.retry(kwargs={**job, "attempt": attempt}, countdown=delay, exc=exc)

    if not decision.allowed:
        delay = decision.retry_after_s + random.uniform(0, settings.rate_jitter_s)
        raise self.retry(kwargs={**job, "attempt": attempt}, countdown=delay)

    # 2. We hold a rate-limit slot. Call the provider.
    try:
        get_provider().send(idempotency_key=job_id, to=to, subject=subject, body=body)
    except PermanentEmailError as exc:
        return _dead_letter(redis_client, job, exc, attempts=attempt + 1)
    except Exception as exc:  # TransientEmailError, timeouts, SoftTimeLimitExceeded, ...
        next_attempt = attempt + 1
        if next_attempt >= settings.max_attempts:
            return _dead_letter(redis_client, job, exc, attempts=next_attempt)
        delay = backoff_delay(next_attempt)
        log.warning(
            "job %s: attempt %d/%d failed (%s); retrying in %.1fs",
            job_id, next_attempt, settings.max_attempts, exc, delay,
        )
        raise self.retry(kwargs={**job, "attempt": next_attempt}, countdown=delay, exc=exc)

    # 3. Success: remember it so a redelivery of this message is a no-op.
    try:
        redis_client.set(sent_key(job_id), "1", ex=settings.dedupe_ttl_s)
    except RedisError:
        # The email IS sent. Failing the task now would only risk a resend.
        log.exception("job %s: sent, but could not record the sent marker", job_id)
    return "sent"


def _dead_letter(redis_client, job: dict, exc: Exception, attempts: int) -> str:
    log.error("job %s dead-lettered after %d attempt(s): %r", job["job_id"], attempts, exc)
    dead_letter.push(redis_client, job=job, error=repr(exc), attempts=attempts)
    return "dead"


def enqueue_email(to: str, subject: str, body: str, job_id: str | None = None) -> str:
    """Public API: put an email on the queue and return its job id.

    Pass your own ``job_id`` (e.g. ``f"order-{order.id}-confirmation"``) to make
    enqueueing itself idempotent for the receiving side.
    """
    job_id = job_id or uuid.uuid4().hex
    send_email.apply_async(
        kwargs={"job_id": job_id, "to": to, "subject": subject, "body": body, "attempt": 0},
        queue=settings.queue_name,
    )
    return job_id
