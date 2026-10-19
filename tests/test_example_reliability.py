"""The examples/reliability demo is real and tested (ADR 0017).

Loads the example's registry + tuned settings, drives the pooled `chat` model
against the fake backend, injects a failure, and asserts the cooldown/ejection
state surfaces exactly where the README says to look (/status, /healthz/backends).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.errors import BackendUnavailableError, ValidationError

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "reliability"


def _settings() -> Settings:
    # Mirror examples/reliability/.env.example (no secrets/hosts).
    return Settings(
        auth_enabled=False,
        registry_path=str(EXAMPLE_DIR / "registry.yaml"),
        enabled_backends=["fake"],
        health_probe_enabled=False,
        retry_max_attempts=4,
        retry_backoff_base_ms=0,  # no real sleeps in the test
        cooldown_failures=2,
        cooldown_seconds=15,
        max_ejection_percent=50,
        cooldown_backoff_multiplier=2.0,
        cooldown_max_seconds=120,
        cooldown_decay_seconds=300,
        cooldown_half_open_single_trial=True,
        retry_on_backpressure=True,
        cooldown_on_backpressure=True,
        retry_respect_retry_after=True,
        retry_max_attempts_by_class={"timeout": 2},
    )


def test_example_registry_and_settings_load():
    s = _settings()
    s.validate_runtime()  # the example config is valid as shipped
    assert Path(s.registry_path).exists()


@pytest.fixture
def client():
    app = create_app(_settings())
    with TestClient(app) as c:
        yield c


def test_example_pool_serves(client):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.json()["model"] == "chat"


def test_example_status_exposes_reliability(client):
    rel = client.get("/status").json()["reliability"]
    assert rel["max_ejection_percent"] == 50
    assert rel["cooldown_backoff_multiplier"] == 2.0
    assert rel["half_open_single_trial"] is True


def test_example_cooldown_visible_after_injected_failure(client):
    import asyncio

    mgr = client.app.state.manager

    # Inject a connection failure on one member; after the configured 2
    # consecutive failures it is benched, then surfaced in /status + /healthz.
    async def boom(payload, request_id):
        raise BackendUnavailableError("down", backend="fake", model="chat-node-1")

    mgr.models["chat-node-1"].adapter.chat = boom  # type: ignore[method-assign]
    for _ in range(2):
        # Re-pin the session to node-1 before each call so both failures land on
        # it (a failover re-pins the session to the member that actually served).
        asyncio.run(mgr.state.sticky_set("chat", "demo", "chat-node-1"))
        client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "demo"},
            json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
        )

    status = client.get("/status").json()
    by_alias = {m["alias"]: m for m in status["models"]}
    assert by_alias["chat-node-1"]["cooldown"]["in_cooldown"] is True

    health = client.get("/healthz/backends").json()
    hb = {b["alias"]: b for b in health["backends"]}
    assert hb["chat-node-1"]["cooldown"]["in_cooldown"] is True


def test_example_client_error_not_retried(client):
    mgr = client.app.state.manager

    # Every member raises a 4xx: whichever is chosen, it propagates unretried and
    # no member is benched (the 4xx-never-retried-or-counted invariant).
    async def bad(payload, request_id):
        raise ValidationError("bad request")

    for mm in mgr.models.values():
        mm.adapter.chat = bad  # type: ignore[method-assign]
    r = client.post(
        "/v1/chat/completions",
        json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400
    status = client.get("/status").json()
    assert all(not m["cooldown"]["in_cooldown"] for m in status["models"])
