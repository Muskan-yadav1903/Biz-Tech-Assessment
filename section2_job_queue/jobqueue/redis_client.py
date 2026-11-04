"""Process-safe, thread-safe Redis client accessor."""
from __future__ import annotations

import os

import redis

from jobqueue.config import settings

_clients: dict[tuple[int, str], redis.Redis] = {}


def get_redis() -> redis.Redis:
    """One client (connection pool) per process.

    Keyed by PID because Celery's prefork pool forks after import: a socket
    opened in the parent must never be shared with a child.
    """
    key = (os.getpid(), settings.redis_url)
    client = _clients.get(key)
    if client is None:
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_socket_timeout_s,
            socket_connect_timeout=settings.redis_socket_timeout_s,
        )
        _clients[key] = client
    return client
