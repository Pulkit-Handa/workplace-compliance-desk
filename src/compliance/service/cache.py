"""Whole-run cache.

The key includes every input that can change the answer: the normalised factual
request, the prompt variant, the index version, the prompt artifact versions, and
a fingerprint of the deterministic policy/composition modules.  A cached answer
is therefore never reused after the corpus, a prompt, or the legal composer
changes.
"""
from __future__ import annotations

import functools
import hashlib
from collections import OrderedDict
from pathlib import Path
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from compliance.config import settings
from compliance.contracts import AdvisoryResult
from compliance.observability import metrics
from compliance.util import json_dumps

_KEY_PREFIX = "compliance:cache:"
_POLICY_MODULES = ("graph.py", "nodes.py", "policy.py", "state.py", "verification.py")


@functools.lru_cache(maxsize=1)
def policy_fingerprint() -> str:
    """Hash the deterministic composer/verifier sources.

    Prompt and index versions are not enough: a code-only change to the legal
    policy or the advisory renderer can change the answer while leaving both
    unchanged.  Hashing the relevant modules makes the cache self-invalidating.
    """
    base = Path(__file__).resolve().parent.parent / "agents"
    digest = hashlib.sha256()
    for name in _POLICY_MODULES:
        digest.update(name.encode("utf-8"))
        try:
            digest.update((base / name).read_bytes())
        except OSError:
            digest.update(b"unavailable")
    return digest.hexdigest()[:16]


def cache_key(
    request_payload: dict[str, Any],
    *,
    variant: str,
    index_version: str | None,
    prompt_versions: dict[str, str],
) -> str:
    material = json_dumps(
        {
            "request": request_payload,
            "variant": variant,
            "index": index_version,
            "prompts": prompt_versions,
            "policy": policy_fingerprint(),
        }
    )
    return _KEY_PREFIX + hashlib.sha256(material.encode("utf-8")).hexdigest()


class RunCache(Protocol):
    async def get(self, key: str) -> AdvisoryResult | None: ...
    async def set(self, key: str, result: AdvisoryResult) -> None: ...
    async def ping(self) -> bool: ...
    async def close(self) -> None: ...


class MemoryRunCache:
    def __init__(self, max_entries: int = 256) -> None:
        self.max_entries = max_entries
        self._values: OrderedDict[str, str] = OrderedDict()

    async def get(self, key: str) -> AdvisoryResult | None:
        raw = self._values.get(key)
        if raw is None:
            metrics.CACHE_OPS.labels(kind="run", result="miss").inc()
            return None
        self._values.move_to_end(key)
        metrics.CACHE_OPS.labels(kind="run", result="hit").inc()
        return AdvisoryResult.model_validate_json(raw)

    async def set(self, key: str, result: AdvisoryResult) -> None:
        self._values[key] = result.model_dump_json()
        self._values.move_to_end(key)
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class RedisRunCache:
    def __init__(self, url: str | None = None, *, ttl_s: int | None = None) -> None:
        self.url = url or settings.redis_url
        self.ttl_s = ttl_s or settings.cache_ttl_s
        self._client = Redis.from_url(self.url, decode_responses=True)

    async def get(self, key: str) -> AdvisoryResult | None:
        try:
            raw = await self._client.get(key)
        except (RedisError, OSError):
            metrics.CACHE_OPS.labels(kind="run", result="error").inc()
            return None
        if raw is None:
            metrics.CACHE_OPS.labels(kind="run", result="miss").inc()
            return None
        metrics.CACHE_OPS.labels(kind="run", result="hit").inc()
        return AdvisoryResult.model_validate_json(raw)

    async def set(self, key: str, result: AdvisoryResult) -> None:
        try:
            await self._client.set(key, result.model_dump_json(), ex=self.ttl_s)
        except (RedisError, OSError):
            metrics.CACHE_OPS.labels(kind="run", result="error").inc()

    async def ping(self) -> bool:
        try:
            return bool(await self._client.ping())
        except (RedisError, OSError):
            return False

    async def close(self) -> None:
        await self._client.aclose()


async def make_cache(*, backend: str | None = None) -> RunCache:
    backend = backend or settings.queue_backend
    if not settings.run_cache_enabled:
        return MemoryRunCache(max_entries=1)
    if backend == "inline":
        return MemoryRunCache()
    cache = RedisRunCache()
    if await cache.ping():
        return cache
    await cache.close()
    if backend == "redis":
        raise RuntimeError(f"Redis is not reachable at {settings.redis_url}")
    return MemoryRunCache()


__all__ = [
    "MemoryRunCache",
    "RedisRunCache",
    "RunCache",
    "cache_key",
    "make_cache",
    "policy_fingerprint",
]
