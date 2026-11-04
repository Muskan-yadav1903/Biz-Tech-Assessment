"""Dead-letter store for jobs that can never succeed (or exhausted their retries).

Dead jobs go to a Redis list as JSON so they are durable, inspectable and
replayable. Inspect with:   redis-cli LRANGE jobqueue:dead 0 -1
"""
from __future__ import annotations

import json
import time

import redis

from jobqueue.config import settings


def push(client: redis.Redis, *, job: dict, error: str, attempts: int) -> None:
    record = {
        "job": job,
        "error": error,
        "attempts": attempts,
        "dead_at": time.time(),
    }
    client.lpush(settings.dead_letter_key, json.dumps(record))


def count(client: redis.Redis) -> int:
    return int(client.llen(settings.dead_letter_key))


def list_jobs(client: redis.Redis, limit: int = 100) -> list[dict]:
    return [json.loads(x) for x in client.lrange(settings.dead_letter_key, 0, limit - 1)]


def requeue_all(client: redis.Redis) -> int:
    """Replay every dead job with a fresh retry budget (after fixing the cause).

    A job is only removed from the dead-letter list after it has been
    re-published, so a crash in the middle cannot lose it.
    """
    from jobqueue.tasks import send_email  # local import: tasks imports this module

    n = 0
    while True:
        raw = client.lindex(settings.dead_letter_key, -1)
        if raw is None:
            return n
        job = json.loads(raw)["job"]
        send_email.apply_async(kwargs={**job, "attempt": 0}, queue=settings.queue_name)
        client.rpop(settings.dead_letter_key)
        n += 1
