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


@pytest.mark.parametrize(
    "ttl",
    ["nan", "inf", "-inf", "Infinity", "1e400", "9" * 400],
    ids=["nan", "inf", "neg_inf", "Infinity", "1e400_overflow", "400_digits"],
)
def test_cache_ttl_non_finite_or_overflow_rejected(client, ttl):
    # DEFECT 1: non-finite / overflowing TTLs must be rejected, not stored forever.
    r = client.post("/v1/chat/completions", headers={**H, "x-broker-cache-ttl": ttl}, json=CHAT)
    assert r.status_code == 400


def test_cache_ttl_zero_does_not_store(client):
    # DEFECT 1: 0 means "do not store this response" — never "store forever".
    r1 = client.post("/v1/chat/completions", headers={**H, "x-broker-cache-ttl": "0"}, json=CHAT)
    assert r1.headers["x-broker-cache"] == "miss"
    r2 = client.post("/v1/chat/completions", headers=H, json=CHAT)
    assert r2.headers["x-broker-cache"] == "miss"  # nothing was stored under TTL 0
    assert client.app.state.response_cache.stats()["not_stored"] >= 1


def test_cache_ttl_override_cannot_exceed_operator(client):
    # DEFECT 1: a huge FINITE override is clamped to the operator TTL (only shortens),
    # so it is accepted and stored rather than extended beyond the operator ceiling.
    r1 = client.post(
        "/v1/chat/completions", headers={**H, "x-broker-cache-ttl": "1e308"}, json=CHAT
    )
    assert r1.headers["x-broker-cache"] == "miss"  # stored (clamped), not rejected
    r2 = client.post("/v1/chat/completions", headers=H, json=CHAT)
    assert r2.headers["x-broker-cache"] == "hit"


def test_clamp_ttl_only_shortens():
    from broker.cache import _clamp_ttl

    assert _clamp_ttl(300.0, None) == 300.0  # no override → operator TTL
    assert _clamp_ttl(300.0, 60.0) == 60.0  # shorter override → used
    assert _clamp_ttl(300.0, 1e308) == 300.0  # longer override → clamped to operator
    assert _clamp_ttl(0.0, None) is None  # operator unbounded → no TTL
    assert _clamp_ttl(0.0, 60.0) == 60.0  # unbounded operator, finite override bounds it


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
