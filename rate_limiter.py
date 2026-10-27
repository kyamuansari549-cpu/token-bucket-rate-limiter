"""Core token-bucket rate limiting logic.

This module is framework-independent: it knows nothing about FastAPI or HTTP.
It implements the classic token bucket algorithm:

- Every key (user, API key, IP address, ...) owns a bucket.
- A bucket holds at most ``capacity`` tokens.
- Tokens are added continuously at ``refill_rate`` tokens per second.
- A request is allowed when the bucket holds at least ``cost`` tokens,
  in which case ``cost`` tokens are removed. Otherwise it is denied and the
  caller is told exactly how long to wait (``retry_after_seconds``).

Refilling is *lazy*: no background thread runs. On every access we compute
how many tokens would have been added since the last access, using elapsed
wall time from a monotonic clock. The clock is injectable so tests can be
fully deterministic.

Two backends implement the same :class:`RateLimiter` interface:

- :class:`InMemoryRateLimiter` - buckets stored in process memory (default).
- :class:`RedisRateLimiter` - buckets stored in Redis via a Lua script, for
  deployments with multiple server processes. The ``redis`` package is an
  optional dependency; importing this module never requires it.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol

try:  # Optional dependency - only needed for RedisRateLimiter.
    import redis as _redis_module
except ImportError:  # pragma: no cover - depends on environment
    _redis_module = None


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of one :meth:`TokenBucket.allow` check."""

    allowed: bool
    """Whether the request may proceed."""

    limit: int
    """Bucket capacity (the maximum burst size)."""

    remaining: float
    """Tokens left in the bucket after this check."""

    retry_after_seconds: float
    """0.0 when allowed. When denied, the exact seconds until enough tokens
    have refilled. ``math.inf`` when ``cost`` exceeds the bucket capacity
    (the request can never be satisfied)."""

    reset_after_seconds: float
    """Seconds until the bucket is completely full again."""


@dataclass(frozen=True)
class BucketStatus:
    """A snapshot of a bucket, taken without consuming any tokens."""

    capacity: int
    refill_rate: float
    tokens: float

    @property
    def full_after_seconds(self) -> float:
        """Seconds until the bucket is completely full again."""
        if self.tokens >= self.capacity:
            return 0.0
        return (self.capacity - self.tokens) / self.refill_rate


# ---------------------------------------------------------------------------
# Token bucket
# ---------------------------------------------------------------------------


class TokenBucket:
    """A single thread-safe token bucket.

    Parameters
    ----------
    capacity:
        Maximum number of tokens the bucket can hold. Also the maximum
        burst size. Must be > 0.
    refill_rate:
        Tokens added per second. Must be > 0.
    clock:
        Zero-argument function returning seconds (monotonic). Injectable
        for deterministic tests; defaults to ``time.monotonic``.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity!r}")
        if refill_rate <= 0:
            raise ValueError(f"refill_rate must be > 0, got {refill_rate!r}")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._clock = clock
        self._tokens = float(capacity)  # buckets start full
        self._last_refill = float(clock())
        self._lock = threading.Lock()

    # -- internal -----------------------------------------------------------

    def _refill(self, now: float) -> None:
        """Add tokens earned since the last refill. Caller holds the lock."""
        elapsed = now - self._last_refill
        if elapsed > 0:
            earned = elapsed * self.refill_rate
            self._tokens = min(self.capacity, self._tokens + earned)
            self._last_refill = now

    # -- public API ---------------------------------------------------------

    def allow(self, cost: float = 1.0) -> RateLimitResult:
        """Try to consume ``cost`` tokens.

        Returns a :class:`RateLimitResult`. When denied, no tokens are
        consumed. Raises ``ValueError`` for ``cost <= 0``.
        """
        if cost <= 0:
            raise ValueError(f"cost must be > 0, got {cost!r}")
        with self._lock:
            now = float(self._clock())
            self._refill(now)
            if cost > self.capacity:
                # Can never be satisfied, no matter how long we wait.
                allowed, retry_after = False, math.inf
            elif self._tokens >= cost:
                self._tokens -= cost
                allowed, retry_after = True, 0.0
            else:
                deficit = cost - self._tokens
                allowed, retry_after = False, deficit / self.refill_rate
            reset_after = (self.capacity - self._tokens) / self.refill_rate
            return RateLimitResult(
                allowed=allowed,
                limit=int(self.capacity),
                remaining=self._tokens,
                retry_after_seconds=retry_after,
                reset_after_seconds=reset_after,
            )

    def status(self) -> BucketStatus:
        """Current bucket state, without consuming tokens."""
        with self._lock:
            self._refill(float(self._clock()))
            return BucketStatus(
                capacity=int(self.capacity),
                refill_rate=self.refill_rate,
                tokens=self._tokens,
            )


# ---------------------------------------------------------------------------
# Pluggable per-key stores / limiter interface
# ---------------------------------------------------------------------------


class BucketStore(Protocol):
    """Storage backend that hands out one :class:`TokenBucket` per key."""

    def get_or_create(self, key: str) -> TokenBucket:
        """Return the bucket for ``key``, creating it (full) if needed."""
        ...


class InMemoryBucketStore:
    """Keeps all buckets in a dict in process memory. Thread-safe."""

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._clock = clock
        self._buckets: Dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def get_or_create(self, key: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self._capacity, self._refill_rate, self._clock)
                self._buckets[key] = bucket
            return bucket


class RateLimiter(Protocol):
    """Per-key rate limiting interface implemented by every backend."""

    def allow(self, key: str, cost: float = 1.0) -> RateLimitResult:
        """Try to consume ``cost`` tokens from ``key``'s bucket."""
        ...

    def status(self, key: str) -> BucketStatus:
        """Snapshot of ``key``'s bucket, without consuming tokens."""
        ...


class InMemoryRateLimiter:
    """Default limiter: one in-memory :class:`TokenBucket` per key.

    State is per-process: if you run multiple server processes, each has
    its own buckets. Use :class:`RedisRateLimiter` when the limit must be
    shared across processes/machines.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store: BucketStore = InMemoryBucketStore(capacity, refill_rate, clock)

    def allow(self, key: str, cost: float = 1.0) -> RateLimitResult:
        return self._store.get_or_create(key).allow(cost)

    def status(self, key: str) -> BucketStatus:
        return self._store.get_or_create(key).status()


# ---------------------------------------------------------------------------
# Optional Redis backend
# ---------------------------------------------------------------------------

# The whole check-and-consume runs as one Lua script so it is atomic in
# Redis: no two clients can spend the same token. Time comes from the Redis
# server itself (TIME), so all clients agree on "now".
_REDIS_ALLOW_LUA = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])
local cost     = tonumber(ARGV[3])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000.0

local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)

local allowed = 0
local retry   = 0.0
if cost > capacity then
  retry = -1  -- sentinel for "can never be satisfied"
elseif tokens >= cost then
  tokens  = tokens - cost
  allowed = 1
else
  retry = (cost - tokens) / rate
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
-- Keep idle keys around a little past a full refill, then let them expire.
redis.call('EXPIRE', key, math.ceil(capacity / rate) + 60)

local reset = (capacity - tokens) / rate
return {allowed, tostring(tokens), tostring(retry), tostring(reset)}
"""

_REDIS_STATUS_LUA = """
local key      = KEYS[1]
local capacity = tonumber(ARGV[1])
local rate     = tonumber(ARGV[2])

local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000.0

local data   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts     = tonumber(data[2])
if tokens == nil then
  return {tostring(capacity)}
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * rate)
return {tostring(tokens)}
"""


class RedisRateLimiter:
    """Rate limiter backed by Redis, shared across server processes.

    Requires the optional ``redis`` package (``pip install redis``) and a
    reachable Redis server. Raises ``RuntimeError`` at construction time if
    the package is not installed, and ``ValueError`` for bad parameters.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        redis_url: str = "redis://localhost:6379/0",
        key_prefix: str = "ratelimit:",
    ) -> None:
        if _redis_module is None:
            raise RuntimeError(
                "RedisRateLimiter requires the 'redis' package: pip install redis"
            )
        if capacity <= 0:
            raise ValueError(f"capacity must be > 0, got {capacity!r}")
        if refill_rate <= 0:
            raise ValueError(f"refill_rate must be > 0, got {refill_rate!r}")
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self._prefix = key_prefix
        self._client = _redis_module.Redis.from_url(redis_url, decode_responses=True)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def allow(self, key: str, cost: float = 1.0) -> RateLimitResult:
        if cost <= 0:
            raise ValueError(f"cost must be > 0, got {cost!r}")
        allowed, tokens, retry, reset = self._client.eval(
            _REDIS_ALLOW_LUA,
            1,
            self._key(key),
            self.capacity,
            self.refill_rate,
            cost,
        )
        retry_f = float(retry)
        return RateLimitResult(
            allowed=bool(int(allowed)),
            limit=int(self.capacity),
            remaining=float(tokens),
            retry_after_seconds=math.inf if retry_f < 0 else retry_f,
            reset_after_seconds=float(reset),
        )

    def status(self, key: str) -> BucketStatus:
        (tokens,) = self._client.eval(
            _REDIS_STATUS_LUA, 1, self._key(key), self.capacity, self.refill_rate
        )
        return BucketStatus(
            capacity=int(self.capacity),
            refill_rate=self.refill_rate,
            tokens=float(tokens),
        )
