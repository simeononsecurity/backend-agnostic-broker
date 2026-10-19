"""Response-cache tests (ADR 0012): key design, isolation, headers, policy."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.cache import ResponseCache
from broker.config import Settings
from broker.state import InProcessStateStore

PROMPT = {"model": "echo", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}


def _cache_settings(fake_registry_file, tmp_models_root, **over) -> Settings:
    base = dict(
        auth_enabled=True,
        api_keys=["key-a", "key-b"],
        registry_path=str(fake_registry_file),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_models_root)],
        model_idle_timeout_seconds=60,
        idle_sweep_interval_seconds=5,
        max_loaded_models=2,
        response_cache_enabled=True,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def cache_client(fake_registry_file, tmp_models_root):
    app = create_app(_cache_settings(fake_registry_file, tmp_models_root))
    with TestClient(app) as c:
        yield c


# --- key design (pure) -----------------------------------------------------
def test_make_key_stable_across_dict_ordering():
    a = ResponseCache.make_key(
        scope="s", model="m", endpoint="chat", payload={"temperature": 0, "messages": [1]}
    )
    b = ResponseCache.make_key(
        scope="s", model="m", endpoint="chat", payload={"messages": [1], "temperature": 0}
    )
    assert a == b


def test_make_key_isolated_by_scope():
    k1 = ResponseCache.make_key(scope="tenant-a", model="m", endpoint="chat", payload=PROMPT)
    k2 = ResponseCache.make_key(scope="tenant-b", model="m", endpoint="chat", payload=PROMPT)
    assert k1 != k2


def test_make_key_isolated_by_model_and_endpoint():
    base = dict(scope="s", endpoint="chat", payload=PROMPT)
    assert ResponseCache.make_key(model="m1", **base) != ResponseCache.make_key(model="m2", **base)
    assert ResponseCache.make_key(
        scope="s", model="m", endpoint="chat", payload=PROMPT
    ) != ResponseCache.make_key(scope="s", model="m", endpoint="completions", payload=PROMPT)


def test_make_key_varies_with_sampling_tools_and_format():
    def k(payload):
        return ResponseCache.make_key(scope="s", model="m", endpoint="chat", payload=payload)

    m = {"messages": [{"role": "user", "content": "x"}]}
    assert k({**m, "temperature": 0}) != k({**m, "temperature": 0.7})
    assert k({**m, "tools": [{"a": 1}]}) != k({**m, "tools": [{"a": 2}]})
    assert k({**m, "response_format": {"type": "json_object"}}) != k(m)
    # routing-only fields must NOT change the key
    assert k({**m, "stream": True}) == k({**m, "stream": False})
    assert k({**m, "user": "alice"}) == k({**m, "user": "bob"})


def test_is_deterministic():
    assert ResponseCache.is_deterministic({"temperature": 0})
    assert ResponseCache.is_deterministic({"seed": 42, "temperature": 0.9})
    assert not ResponseCache.is_deterministic({"temperature": 0.7})
    assert not ResponseCache.is_deterministic({})  # unset temperature → not deterministic


# --- HTTP behavior (hit/miss/bypass headers) -------------------------------
def test_cache_hit_on_second_identical_request(cache_client):
    h = {"Authorization": "Bearer key-a"}
    r1 = cache_client.post("/v1/chat/completions", headers=h, json=PROMPT)
    assert r1.status_code == 200
    assert r1.headers["x-broker-cache"] == "miss"
    r2 = cache_client.post("/v1/chat/completions", headers=h, json=PROMPT)
    assert r2.status_code == 200
    assert r2.headers["x-broker-cache"] == "hit"
    assert r2.json() == r1.json()


def test_cache_isolated_across_api_keys(cache_client):
    cache_client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer key-a"}, json=PROMPT
    )
    # a different key must NOT read key-a's cached answer
    r = cache_client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer key-b"}, json=PROMPT
    )
    assert r.headers["x-broker-cache"] == "miss"


def test_cache_control_no_cache_bypasses(cache_client):
    h = {"Authorization": "Bearer key-a"}
    cache_client.post("/v1/chat/completions", headers=h, json=PROMPT)  # warm
    r = cache_client.post(
        "/v1/chat/completions", headers={**h, "Cache-Control": "no-cache"}, json=PROMPT
    )
    assert r.headers["x-broker-cache"] == "bypass"


def test_x_broker_cache_bypass_header(cache_client):
    h = {"Authorization": "Bearer key-a"}
    cache_client.post("/v1/chat/completions", headers=h, json=PROMPT)  # warm
    r = cache_client.post(
        "/v1/chat/completions", headers={**h, "x-broker-cache": "bypass"}, json=PROMPT
    )
    assert r.headers["x-broker-cache"] == "bypass"


def test_nondeterministic_not_cached_by_default(cache_client):
    h = {"Authorization": "Bearer key-a"}
    body = {"model": "echo", "messages": [{"role": "user", "content": "q"}], "temperature": 0.7}
    r1 = cache_client.post("/v1/chat/completions", headers=h, json=body)
    r2 = cache_client.post("/v1/chat/completions", headers=h, json=body)
    assert r1.headers["x-broker-cache"] == "miss"
    assert r2.headers["x-broker-cache"] == "miss"  # never a hit under deterministic-only default


def test_embeddings_cached(cache_client):
    h = {"Authorization": "Bearer key-a"}
    body = {"model": "echo", "input": "hello world"}
    r1 = cache_client.post("/v1/embeddings", headers=h, json=body)
    r2 = cache_client.post("/v1/embeddings", headers=h, json=body)
    assert r1.headers["x-broker-cache"] == "miss"
    assert r2.headers["x-broker-cache"] == "hit"
    assert r2.json() == r1.json()


def test_cache_disabled_passthrough(fake_registry_file, tmp_models_root):
    s = _cache_settings(fake_registry_file, tmp_models_root, response_cache_enabled=False)
    app = create_app(s)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key-a"}, json=PROMPT)
        assert r.headers["x-broker-cache"] == "miss"
        assert app.state.response_cache.stores == 0  # nothing stored when disabled


def test_errors_are_not_cached(cache_client):
    from broker.adapters.fake import FakeAdapter

    h = {"Authorization": "Bearer key-a"}
    FakeAdapter.fail_on_chat = True
    try:
        r = cache_client.post(
            "/v1/chat/completions",
            headers=h,
            json={
                "model": "echo",
                "messages": [{"role": "user", "content": "boom"}],
                "temperature": 0,
            },
        )
        assert r.status_code >= 500
    finally:
        FakeAdapter.fail_on_chat = False
    assert cache_client.app.state.response_cache.stores == 0


def test_status_reports_cache(cache_client):
    r = cache_client.get("/status", headers={"Authorization": "Bearer key-a"})
    assert r.status_code == 200
    rc = r.json()["response_cache"]
    assert rc["enabled"] is True
    assert rc["backend"] == "in_process"


# --- StateStore-backed policy (unit) ---------------------------------------
def _cache(**over):
    s = Settings(
        auth_enabled=False,
        registry_path="registry.yaml",
        response_cache_enabled=True,
        **over,
    )
    # registry_path need not exist for a bare ResponseCache (no validate_runtime).
    return ResponseCache(InProcessStateStore(), s)


def test_store_and_get_roundtrip_statestore():
    async def run():
        c = _cache()
        assert await c.get("k") is None and c.misses == 1
        await c.store("k", {"v": 1})
        got = await c.get("k")
        assert got == {"v": 1} and c.hits == 1
        # key really lives in the StateStore KV
        assert await c.state.kv_get("respcache:k") is not None

    asyncio.run(run())


def test_max_entries_lru_eviction():
    async def run():
        c = _cache(response_cache_max_entries=2)
        await c.store("a", {"n": 1})
        await c.store("b", {"n": 2})
        await c.store("c", {"n": 3})  # evicts "a" (LRU)
        assert await c.state.kv_get("respcache:a") is None
        assert await c.get("b") == {"n": 2}
        assert await c.get("c") == {"n": 3}

    asyncio.run(run())


def test_ttl_expiry():
    async def run():
        c = _cache(response_cache_ttl_seconds=0.05)
        await c.store("k", {"v": 1})
        assert await c.get("k") == {"v": 1}
        await asyncio.sleep(0.08)
        assert await c.get("k") is None  # expired

    asyncio.run(run())


def test_max_bytes_skips_large_entry():
    async def run():
        c = _cache(response_cache_max_bytes=10)
        stored = await c.store("k", {"big": "x" * 1000})
        assert stored is False
        assert await c.get("k") is None

    asyncio.run(run())
