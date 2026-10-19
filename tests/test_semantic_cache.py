"""Semantic-cache tests (ADR 0014): cosine, strict scoping, residency gate, headers."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.cache import SemanticCache
from broker.config import Settings
from broker.state import InProcessStateStore


def _sem_settings(fake_registry_file, tmp_models_root, **over) -> Settings:
    base = dict(
        auth_enabled=True,
        api_keys=["key-a", "key-b"],
        registry_path=str(fake_registry_file),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_models_root)],
        model_idle_timeout_seconds=60,
        idle_sweep_interval_seconds=5,
        max_loaded_models=2,
        # exact cache OFF so the semantic layer is exercised directly
        response_cache_enabled=False,
        semantic_cache_enabled=True,
        semantic_cache_model="echo2",
        semantic_cache_require_resident=False,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def sem_client(fake_registry_file, tmp_models_root):
    app = create_app(_sem_settings(fake_registry_file, tmp_models_root))
    with TestClient(app) as c:
        yield c


CHAT = {"model": "echo", "messages": [{"role": "user", "content": "hello world"}], "temperature": 0}


# --- pure cosine / bucket-key / query_text --------------------------------
def test_cosine_identical_and_orthogonal():
    assert SemanticCache.cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert SemanticCache.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert SemanticCache.cosine([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)
    # degenerate inputs never raise
    assert SemanticCache.cosine([], [1.0]) == 0.0
    assert SemanticCache.cosine([0.0, 0.0], [1.0, 1.0]) == 0.0


def test_bucket_key_excludes_prompt_but_includes_params():
    def b(payload):
        return SemanticCache.bucket_key(scope="s", model="m", endpoint="chat", payload=payload)

    base = {"messages": [{"role": "user", "content": "A"}], "temperature": 0}
    # last-user-turn text is fuzzy → same bucket regardless of its wording
    assert b(base) == b({"messages": [{"role": "user", "content": "B"}], "temperature": 0})
    # non-prompt params are EXACT → different bucket
    assert b(base) != b({"messages": base["messages"], "temperature": 0, "max_tokens": 5})
    assert b(base) != b({"messages": base["messages"], "temperature": 0, "tools": [{"a": 1}]})
    # routing-only fields do not change the bucket
    assert b(base) == b({**base, "stream": True, "user": "x"})


def test_bucket_key_separates_system_prompt_and_history():
    def b(messages):
        return SemanticCache.bucket_key(
            scope="s", model="m", endpoint="chat", payload={"messages": messages, "temperature": 0}
        )

    # Differing SYSTEM prompt → different bucket, even with the same user turn.
    fr = [{"role": "system", "content": "Answer in French"}, {"role": "user", "content": "hi"}]
    en = [{"role": "system", "content": "Answer in English"}, {"role": "user", "content": "hi"}]
    assert b(fr) != b(en)
    # Differing earlier HISTORY → different bucket.
    h1 = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "1"},
        {"role": "user", "content": "now"},
    ]
    h2 = [
        {"role": "user", "content": "y"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "now"},
    ]
    assert b(h1) != b(h2)
    # Same system + history, reworded LAST user turn → SAME bucket (only it is fuzzy).
    same_a = [{"role": "system", "content": "s"}, {"role": "user", "content": "hello"}]
    same_b = [{"role": "system", "content": "s"}, {"role": "user", "content": "hi there"}]
    assert b(same_a) == b(same_b)
    # A non-text content part of the last user turn stays EXACT.
    img_a = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "cap"},
                {"type": "image_url", "image_url": {"url": "a"}},
            ],
        }
    ]
    img_b = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "cap"},
                {"type": "image_url", "image_url": {"url": "b"}},
            ],
        }
    ]
    assert b(img_a) != b(img_b)


def test_query_text_is_last_user_turn_only():
    # Only the LAST user turn's text is embedded; system + earlier turns are not.
    text = SemanticCache.query_text(
        {
            "messages": [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": "last question"},
            ]
        }
    )
    assert text == "last question"
    # Text parts of a list-content last user turn are concatenated; non-text dropped.
    parts = SemanticCache.query_text(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "a"},
                        {"type": "image_url", "image_url": {"url": "x"}},
                        {"type": "text", "text": "b"},
                    ],
                }
            ]
        }
    )
    assert parts == "a\nb"


# --- StateStore-backed lookup/store (threshold crossing) ------------------
def _sem(**over):
    s = Settings(
        auth_enabled=False,
        registry_path="registry.yaml",
        semantic_cache_enabled=True,
        semantic_cache_model="e",
        **over,
    )
    return SemanticCache(InProcessStateStore(), s)


def test_lookup_hit_above_threshold_miss_below():
    async def run():
        c = _sem(semantic_cache_threshold=0.95)
        await c.store("bucket", [1.0, 0.0, 0.0], {"answer": 1})
        # near-identical vector → hit
        hit = await c.lookup("bucket", [0.999, 0.01, 0.0])
        assert hit is not None and hit[0] == {"answer": 1} and hit[1] >= 0.95
        # orthogonal vector → miss
        assert await c.lookup("bucket", [0.0, 1.0, 0.0]) is None

    asyncio.run(run())


def test_lookup_respects_threshold_setting():
    async def run():
        strict = _sem(semantic_cache_threshold=0.999)
        await strict.store("b", [1.0, 0.0], {"v": 1})
        # cos ~0.9938 < 0.999 → miss under strict threshold
        assert await strict.lookup("b", [0.9, 0.1]) is None
        loose = _sem(semantic_cache_threshold=0.90)
        await loose.store("b", [1.0, 0.0], {"v": 1})
        assert await loose.lookup("b", [0.9, 0.1]) is not None

    asyncio.run(run())


def test_bucket_bound_evicts_oldest():
    async def run():
        c = _sem(semantic_cache_max_entries=2, semantic_cache_threshold=0.99)
        await c.store("b", [1.0, 0.0], {"n": 1})
        await c.store("b", [0.0, 1.0], {"n": 2})
        await c.store("b", [1.0, 1.0], {"n": 3})  # drops the oldest ([1,0]/{n:1})
        raw = await c.state.kv_get_json("semcache:b")
        assert raw is not None and len(raw) == 2
        # the evicted first entry's exact vector no longer yields a near-1.0 hit
        assert await c.lookup("b", [1.0, 0.0]) is None
        # a surviving entry still matches
        assert (await c.lookup("b", [0.0, 1.0]))[0] == {"n": 2}

    asyncio.run(run())


def test_ttl_expiry_semantic():
    async def run():
        c = _sem(semantic_cache_ttl_seconds=0.05)
        await c.store("b", [1.0, 0.0], {"v": 1})
        assert await c.lookup("b", [1.0, 0.0]) is not None
        await asyncio.sleep(0.08)
        assert await c.lookup("b", [1.0, 0.0]) is None

    asyncio.run(run())


# --- HTTP end-to-end ------------------------------------------------------
def test_semantic_hit_on_repeated_prompt(sem_client):
    h = {"Authorization": "Bearer key-a"}
    r1 = sem_client.post("/v1/chat/completions", headers=h, json=CHAT)
    assert r1.status_code == 200
    assert r1.headers["x-broker-cache"] == "miss"
    r2 = sem_client.post("/v1/chat/completions", headers=h, json=CHAT)
    assert r2.headers["x-broker-cache"] == "semantic-hit"
    assert "x-broker-cache-similarity" in r2.headers
    assert float(r2.headers["x-broker-cache-similarity"]) >= 0.95
    assert r2.json() == r1.json()


def test_semantic_miss_for_dissimilar_prompt(sem_client):
    h = {"Authorization": "Bearer key-a"}
    sem_client.post("/v1/chat/completions", headers=h, json=CHAT)
    other = {
        "model": "echo",
        "messages": [{"role": "user", "content": "zzz totally different"}],
        "temperature": 0,
    }
    r = sem_client.post("/v1/chat/completions", headers=h, json=other)
    assert r.headers["x-broker-cache"] == "miss"


def test_semantic_isolated_across_api_keys(sem_client):
    sem_client.post("/v1/chat/completions", headers={"Authorization": "Bearer key-a"}, json=CHAT)
    r = sem_client.post(
        "/v1/chat/completions", headers={"Authorization": "Bearer key-b"}, json=CHAT
    )
    assert r.headers["x-broker-cache"] == "miss"  # never cross a key boundary


def test_semantic_isolated_across_params(sem_client):
    h = {"Authorization": "Bearer key-a"}
    sem_client.post("/v1/chat/completions", headers=h, json=CHAT)
    # same prompt text, different sampling param → different bucket → miss
    r = sem_client.post("/v1/chat/completions", headers=h, json={**CHAT, "max_tokens": 99})
    assert r.headers["x-broker-cache"] == "miss"


def test_semantic_requires_resident_by_default(fake_registry_file, tmp_models_root):
    s = _sem_settings(fake_registry_file, tmp_models_root, semantic_cache_require_resident=True)
    app = create_app(s)
    with TestClient(app) as c:
        h = {"Authorization": "Bearer key-a"}
        # embedding model not warmed → semantic skipped, NO extra model load
        r1 = c.post("/v1/chat/completions", headers=h, json=CHAT)
        assert r1.headers["x-broker-cache"] == "miss"
        r2 = c.post("/v1/chat/completions", headers=h, json=CHAT)
        assert r2.headers["x-broker-cache"] == "miss"  # still a miss: never consulted
        assert app.state.manager.is_resident("echo2") is False  # embed model never loaded
        assert app.state.semantic_cache.stats()["skipped_not_resident"] >= 2
        # warm the embedding model, then semantic cache engages
        c.post("/v1/embeddings", headers=h, json={"model": "echo2", "input": "warm"})
        assert app.state.manager.is_resident("echo2") is True
        c.post("/v1/chat/completions", headers=h, json=CHAT)  # miss → store
        r4 = c.post("/v1/chat/completions", headers=h, json=CHAT)
        assert r4.headers["x-broker-cache"] == "semantic-hit"


def test_semantic_disabled_passthrough(fake_registry_file, tmp_models_root):
    s = _sem_settings(fake_registry_file, tmp_models_root, semantic_cache_enabled=False)
    app = create_app(s)
    with TestClient(app) as c:
        h = {"Authorization": "Bearer key-a"}
        c.post("/v1/chat/completions", headers=h, json=CHAT)
        r = c.post("/v1/chat/completions", headers=h, json=CHAT)
        assert r.headers["x-broker-cache"] == "miss"  # no semantic layer
        assert app.state.semantic_cache.stats()["stores"] == 0


def test_status_reports_semantic_cache(sem_client):
    r = sem_client.get("/status", headers={"Authorization": "Bearer key-a"})
    sc = r.json()["semantic_cache"]
    assert sc["enabled"] is True
    assert sc["threshold"] == 0.95
    assert sc["model"] == "echo2"


def test_semantic_cache_requires_model_config(fake_registry_file, tmp_models_root):
    from broker.errors import ConfigError

    s = _sem_settings(fake_registry_file, tmp_models_root, semantic_cache_model=None)
    with pytest.raises(ConfigError):
        s.validate_runtime()


def test_bucket_count_bound_evicts_oldest_bucket():
    # The NUMBER of buckets is globally bounded, not just entries per bucket, so a
    # caller varying non-prompt params cannot grow the keyspace without limit.
    async def run():
        c = _sem(semantic_cache_max_buckets=2)
        await c.store("b1", [1.0, 0.0], {"n": 1})
        await c.store("b2", [1.0, 0.0], {"n": 2})
        await c.store("b3", [1.0, 0.0], {"n": 3})  # evicts the LRU bucket (b1)
        assert await c.state.kv_get_json("semcache:b1") is None
        assert await c.state.kv_get_json("semcache:b2") is not None
        assert await c.state.kv_get_json("semcache:b3") is not None
        assert c.stats()["live_buckets"] == 2

    asyncio.run(run())


# --- DEFECT 2: only the last user turn is fuzzy (stub embedder) ------------
# These tests use a stub embedder that returns the SAME vector for every input,
# so any HIT/MISS is decided purely by the exact bucket key — proving system
# prompt and history are matched exactly while only the last user turn is fuzzy.


def _stub_app(fake_registry_file, tmp_models_root, monkeypatch, vec=(1.0, 0.0, 0.0)):
    app = create_app(_sem_settings(fake_registry_file, tmp_models_root))

    async def _const_embed(name, payload, request_id=None, *a, **k):
        return {"data": [{"embedding": list(vec)}], "model": name}

    monkeypatch.setattr(app.state.manager, "embeddings", _const_embed)
    return app


def _chat(system, user):
    msgs = []
    if system is not None:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": user})
    return {"model": "echo", "messages": msgs, "temperature": 0}


def test_semantic_miss_on_different_system_prompt(fake_registry_file, tmp_models_root, monkeypatch):
    app = _stub_app(fake_registry_file, tmp_models_root, monkeypatch)
    with TestClient(app) as c:
        h = {"Authorization": "Bearer key-a"}
        r1 = c.post("/v1/chat/completions", headers=h, json=_chat("Answer in French", "hello"))
        assert r1.headers["x-broker-cache"] == "miss"
        # Identical embedding, but a different SYSTEM prompt → different bucket → MISS.
        r2 = c.post("/v1/chat/completions", headers=h, json=_chat("Answer in English", "hello"))
        assert r2.headers["x-broker-cache"] == "miss"
        # Same system prompt, a REWORDED last user turn → identical embedding → HIT.
        r3 = c.post("/v1/chat/completions", headers=h, json=_chat("Answer in French", "hi there"))
        assert r3.headers["x-broker-cache"] == "semantic-hit"


def test_semantic_miss_on_different_history(fake_registry_file, tmp_models_root, monkeypatch):
    app = _stub_app(fake_registry_file, tmp_models_root, monkeypatch)
    with TestClient(app) as c:
        h = {"Authorization": "Bearer key-a"}
        hist_a = {
            "model": "echo",
            "messages": [
                {"role": "user", "content": "what is 2+2"},
                {"role": "assistant", "content": "4"},
                {"role": "user", "content": "and again"},
            ],
            "temperature": 0,
        }
        hist_b = {
            "model": "echo",
            "messages": [
                {"role": "user", "content": "what is 9+9"},
                {"role": "assistant", "content": "18"},
                {"role": "user", "content": "and again"},
            ],
            "temperature": 0,
        }
        assert (
            c.post("/v1/chat/completions", headers=h, json=hist_a).headers["x-broker-cache"]
            == "miss"
        )
        # Same last user turn + identical embedding, but different earlier history → MISS.
        assert (
            c.post("/v1/chat/completions", headers=h, json=hist_b).headers["x-broker-cache"]
            == "miss"
        )
