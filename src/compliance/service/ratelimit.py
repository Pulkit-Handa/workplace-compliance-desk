"""Per-client token-bucket rate limiting.

The Redis implementation is shared across API replicas; the memory version is
used for tests and the inline demo.  A rejected request gets a Retry-After value
in milliseconds, never a silent drop.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from redis.asyncio import Redis
from redis.exceptions import RedisError

from compliance.config import settings
from compliance.observability import metrics


@dataclass
class RateDecision:
    allowed: bool
    retry_after_ms: int = 0
    remaining: float = 0.0


@dataclass
class _Bucket:
    tokens: float
    updated: float = field(default_factory=time.monotonic)


class TokenBucketLimiter:
    def __init__(
        self,
        *,
        per_minute: int | None = None,
        burst: int | None = None,
    ) -> None:
        self.per_minute = per_minute or settings.rate_limit_per_minute
        self.capacity = float(max(1, burst or settings.rate_limit_burst))
        self.refill_per_s = self.per_minute / 60.0
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def allow(self, client_id: str) -> RateDecision:
        if not settings.rate_limit_enabled:
            return RateDecision(True, 0, self.capacity)
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(client_id)
            if bucket is None:
                bucket = _Bucket(tokens=self.capacity, updated=now)
                self._buckets[client_id] = bucket
            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.refill_per_s)
            bucket.updated = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return RateDecision(True, 0, bucket.tokens)
            retry_ms = int((1.0 - bucket.tokens) / self.refill_per_s * 1000)
            return RateDecision(False, max(1, retry_ms), bucket.tokens)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class RedisTokenBucketLimiter:
    """Atomic token bucket, shared by all API replicas."""

    _LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local now = tonumber(redis.call('TIME')[1])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + math.max(0, now - ts) * refill)
local allowed = 0
if tokens >= cost then tokens = tokens - cost; allowed = 1 end
redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
local retry = 0
if allowed == 0 then retry = (cost - tokens) / refill end
return {allowed, math.floor(retry * 1000)}
"""

    def __init__(self, url: str | None = None) -> None:
        self._client = Redis.from_url(url or settings.redis_url, decode_responses=True)
        self.capacity = float(max(1, settings.rate_limit_burst))
        self.refill_per_s = settings.rate_limit_per_minute / 60.0
        self._script = self._client.register_script(self._LUA)

    async def allow(self, client_id: str) -> RateDecision:
        if not settings.rate_limit_enabled:
            return RateDecision(True, 0, self.capacity)
        key = f"compliance:ratelimit:{client_id}"
        try:
            allowed, retry_ms = await self._script(
                keys=[key],
                args=[self.capacity, self.refill_per_s, 1, 120],
            )
        except (RedisError, OSError):
            # A limiter outage must not take down the API; the local bucket in
            # the caller still provides a floor.
            return RateDecision(True, 0, 0.0)
        remaining = max(0.0, self.capacity - 1.0) if allowed else 0.0
        return RateDecision(bool(int(allowed)), int(retry_ms), remaining)

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except (RedisError, OSError):
            return False

    async def close(self) -> None:
        await self._client.aclose()


class CompositeLimiter:
    """Apply both the shared limiter and a local floor."""

    def __init__(self, shared, local: TokenBucketLimiter | None = None) -> None:
        self.shared = shared
        self.local = local or TokenBucketLimiter()

    async def allow(self, client_id: str) -> RateDecision:
        local = await self.local.allow(client_id)
        if not local.allowed:
            metrics.RATE_LIMITED_TOTAL.labels(client=client_id[:64]).inc()
            return local
        shared = await self.shared.allow(client_id)
        if not shared.allowed:
            metrics.RATE_LIMITED_TOTAL.labels(client=client_id[:64]).inc()
        return shared

    async def ping(self) -> bool:
        return await self.shared.ping()

    async def close(self) -> None:
        await self.shared.close()


async def make_limiter(*, backend: str | None = None):
    backend = backend or settings.queue_backend
    if backend == "inline":
        return TokenBucketLimiter()
    shared = RedisTokenBucketLimiter()
    if await shared.ping():
        return CompositeLimiter(shared)
    await shared.close()
    if backend == "redis":
        raise RuntimeError(f"Redis is not reachable at {settings.redis_url}")
    return TokenBucketLimiter()


__all__ = [
    "CompositeLimiter",
    "RateDecision",
    "RedisTokenBucketLimiter",
    "TokenBucketLimiter",
    "make_limiter",
]
