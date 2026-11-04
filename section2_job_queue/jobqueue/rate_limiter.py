"""Sliding-window-log rate limiter, written from scratch on Redis primitives.

Algorithm (Option B from the assignment): a sorted set holds one member per
admitted request, scored with its admission time in milliseconds.

    1. ZREMRANGEBYSCORE  drop entries older than the window
    2. ZCARD             how many admissions are still inside the window?
    3. ZADD              if fewer than ``limit`` -> admit and record it
       else              deny and report when the oldest entry expires

All four steps run inside ONE Lua script, so Redis executes them atomically:
no other client can interleave between the count and the insert. That is what
makes "never exceed 200 / minute" true with any number of workers.

Time comes from ``redis.call('TIME')`` inside the script, not from the workers,
so clock skew between worker machines cannot break the guarantee.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import redis

_LUA = """
local key       = KEYS[1]
local window_ms = tonumber(ARGV[1])
local limit     = tonumber(ARGV[2])
local member    = ARGV[3]
local audit_key = ARGV[4]

local t   = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)

-- an entry admitted at time s stops counting at s + window_ms
redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window_ms)

if redis.call('ZCARD', key) < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window_ms)
    if audit_key ~= '' then
        redis.call('RPUSH', audit_key, now)
    end
    return {1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local wait_ms = tonumber(oldest[2]) + window_ms - now
if wait_ms < 1 then wait_ms = 1 end
return {0, wait_ms}
"""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    retry_after_s: float  # 0 when allowed


class SlidingWindowLimiter:
    """At most ``limit`` admissions in any rolling ``window_s`` seconds."""

    def __init__(
        self,
        client: redis.Redis,
        key: str,
        limit: int,
        window_s: float,
        audit_key: str = "",
    ) -> None:
        self._client = client
        self._key = key
        self._limit = limit
        self._window_ms = int(window_s * 1000)
        self._audit_key = audit_key
        # register_script -> EVALSHA, transparently falling back to SCRIPT LOAD
        # if the script cache was flushed (restart, failover).
        self._script = client.register_script(_LUA)

    def acquire(self) -> Decision:
        """Try to take one slot.

        Raises ``redis.RedisError`` if Redis is unreachable or errors. The caller
        decides what that means; this class never silently admits ("fail open").
        """
        allowed, wait_ms = self._script(
            keys=[self._key],
            args=[self._window_ms, self._limit, uuid.uuid4().hex, self._audit_key],
        )
        if int(allowed) == 1:
            return Decision(True, 0.0)
        return Decision(False, int(wait_ms) / 1000.0)
