"""Celery application and the settings that make delivery crash-safe.

Run a worker with:

    celery -A jobqueue.celery_app:app worker -Q emails -c 8 -l INFO
"""
from __future__ import annotations

import logging

from celery import Celery

from jobqueue.config import settings

log = logging.getLogger(__name__)
for _w in settings.warnings():
    log.warning("jobqueue config: %s", _w)

app = Celery("jobqueue", broker=settings.redis_url, include=["jobqueue.tasks"])

app.conf.update(
    # ---- never lose a job ---------------------------------------------------
    # Ack AFTER the task finishes (default is before it starts). If the worker
    # dies mid-run the message is still "un-acked" in Redis and gets redelivered.
    task_acks_late=True,
    # If a pool child is killed (OOM-killer, kill -9) requeue instead of dropping.
    task_reject_on_worker_lost=True,
    # Do NOT ack a task that raised an exception we did not handle ourselves
    # (bug, hard time-limit kill). It stays un-acked and is redelivered after the
    # visibility timeout instead of silently disappearing. Every *expected*
    # failure is handled explicitly in tasks.py (retry or dead-letter).
    task_acks_on_failure_or_timeout=False,
    # Redis re-delivers un-acked messages after `visibility_timeout` seconds. That
    # is the crash-recovery latency after a SIGKILL of the whole worker, and it
    # must be longer than the longest ETA a task can be held for (see
    # config.warnings()).
    #
    # Measured gotcha: kombu only *checks* for expired un-acked messages every
    # `unacked_restore_interval` (10 s) x `unacked_restore_throttle` (default 10)
    # = ~100 s, so recovery took ~100 s even with visibility_timeout=5. Throttle
    # 1 makes it "visibility_timeout + at most one 10 s tick" (cost: one cheap
    # ZRANGE per worker every 10 s).
    broker_transport_options={
        "visibility_timeout": settings.visibility_timeout_s,
        "unacked_restore_interval": 10,
        "unacked_restore_throttle": 1,
    },

    # ---- fairness / back-pressure ------------------------------------------
    # Each worker slot reserves only ONE message at a time. With the default
    # (4) a busy worker would hoard messages that other workers could run, and
    # would lose more of them if it died.
    worker_prefetch_multiplier=1,

    # ---- hygiene -------------------------------------------------------------
    task_default_queue=settings.queue_name,
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,  # fire-and-forget; no result backend needed
    task_soft_time_limit=settings.task_soft_time_limit_s,
    task_time_limit=settings.task_time_limit_s,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)
