"""Per-request cache-control tests (ADR 0014): TTL override, namespace, max-age."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings


def _settings(fake_registry_file, tmp_models_root, **over) -> Settings:
    base = dict(
        auth_enabled=True,
        api_keys=["key-a"],
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
def client(fake_registry_file, tmp_models_root):
    app = create_app(_settings(fake_registry_file, tmp_models_root))
    with TestClient(app) as c:
        yield c


H = {"Authorization": "Bearer key-a"}
CHAT = {"model": "echo", "messages": [{"role": "user", "content": "ttl"}], "temperature": 0}


def test_cache_ttl_override_expires_entry(client):
    # a very short per-request TTL makes the stored entry expire before the next read
    r1 = client.post("/v1/chat/completions", headers={**H, "x-broker-cache-ttl": "0.05"}, json=CHAT)
    assert r1.headers["x-broker-cache"] == "miss"
    time.sleep(0.08)
    r2 = client.post("/v1/chat/completions", headers=H, json=CHAT)
    assert r2.headers["x-broker-cache"] == "miss"  # expired via the override TTL


def test_cache_ttl_override_invalid_rejected(client):
    r = client.post("/v1/chat/completions", headers={**H, "x-broker-cache-ttl": "-1"}, json=CHAT)
    assert r.status_code == 400
    r2 = client.post("/v1/chat/completions", headers={**H, "x-broker-cache-ttl": "abc"}, json=CHAT)
    assert r2.status_code == 400


def test_cache_namespace_isolates(client):
    client.post("/v1/chat/completions", headers={**H, "x-broker-cache-namespace": "ns1"}, json=CHAT)
    # same request under a different namespace is a fresh miss
    r = client.post(
        "/v1/chat/completions", headers={**H, "x-broker-cache-namespace": "ns2"}, json=CHAT
    )
    assert r.headers["x-broker-cache"] == "miss"
    # same namespace hits
    r2 = client.post(
        "/v1/chat/completions", headers={**H, "x-broker-cache-namespace": "ns1"}, json=CHAT
    )
    assert r2.headers["x-broker-cache"] == "hit"


def test_cache_namespace_invalid_rejected(client):
    r = client.post(
        "/v1/chat/completions", headers={**H, "x-broker-cache-namespace": "bad space"}, json=CHAT
    )
    assert r.status_code == 400


def test_cache_control_max_age_zero_bypasses(client):
    client.post("/v1/chat/completions", headers=H, json=CHAT)  # warm
    r = client.post("/v1/chat/completions", headers={**H, "Cache-Control": "max-age=0"}, json=CHAT)
    assert r.headers["x-broker-cache"] == "bypass"


def test_rejected_s_maxage_directive_is_ignored(client):
    # s-maxage is a shared-cache response directive the broker rejects (ADR 0014):
    # it must NOT bypass and must NOT change caching behavior.
    r1 = client.post(
        "/v1/chat/completions", headers={**H, "Cache-Control": "s-maxage=5"}, json=CHAT
    )
    assert r1.headers["x-broker-cache"] == "miss"
    r2 = client.post(
        "/v1/chat/completions", headers={**H, "Cache-Control": "s-maxage=5"}, json=CHAT
    )
    assert r2.headers["x-broker-cache"] == "hit"  # behaves as a normal cacheable request
