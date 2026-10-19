"""Redis-backed TTL ceiling tests (ADR 0014 follow-up, DEFECT 1).

Proves that against a real (fake) Redis backend every cache write carries a
FINITE TTL that never exceeds the operator TTL, and that ``x-broker-cache-ttl: 0``
stores nothing. Runs only in a venv with the ``[redis]`` extra + ``fakeredis``;
skipped in the shared venv.
"""

from __future__ import annotations

import pytest

from broker.cache import ResponseCache, SemanticCache
from broker.config import Settings
from broker.state import RedisStateStore

pytest.importorskip("redis")  # optional [redis] extra
fr_aio = pytest.importorskip("fakeredis.aioredis")


def _settings(**over) -> Settings:
    base = dict(
        auth_enabled=False,
        registry_path="registry.yaml",
        response_cache_enabled=True,
        semantic_cache_enabled=True,
        semantic_cache_model="e",
    )
    base.update(over)
    return Settings(**base)


def _store(client):
    return RedisStateStore("redis://fake", prefix="t", client=client)


async def test_redis_response_cache_ttl_capped_at_operator():
    client = fr_aio.FakeRedis()
    store = _store(client)
    cache = ResponseCache(store, _settings(response_cache_ttl_seconds=300.0))
    # A huge finite override may only SHORTEN the operator TTL → clamped to 300.
    assert await cache.store("k", {"a": 1}, ttl_override=1e308) is True
    ttl = await client.ttl(store._k("kv", cache._k("k")))
    assert 0 < ttl <= 300  # finite and within the operator ceiling


async def test_redis_response_cache_default_ttl_is_finite():
    client = fr_aio.FakeRedis()
    store = _store(client)
    cache = ResponseCache(store, _settings(response_cache_ttl_seconds=120.0))
    assert await cache.store("k", {"a": 1}) is True
    ttl = await client.ttl(store._k("kv", cache._k("k")))
    assert 0 < ttl <= 120


async def test_redis_response_cache_ttl_zero_not_stored():
    client = fr_aio.FakeRedis()
    store = _store(client)
    cache = ResponseCache(store, _settings(response_cache_ttl_seconds=300.0))
    assert await cache.store("k", {"a": 1}, ttl_override=0) is False
    assert await client.exists(store._k("kv", cache._k("k"))) == 0


async def test_redis_semantic_cache_ttl_capped_at_operator():
    client = fr_aio.FakeRedis()
    store = _store(client)
    cache = SemanticCache(store, _settings(semantic_cache_ttl_seconds=300.0))
    await cache.store("b", [1.0, 0.0], {"a": 1}, ttl_override=1e308)
    ttl = await client.ttl(store._k("kv", cache._k("b")))
    assert 0 < ttl <= 300
