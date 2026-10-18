"""Health endpoints, non-loading periodic probe, and Ollama /api/ps passthrough."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from broker.adapters.base import HealthResult
from broker.app import create_app
from broker.config import Settings
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry

AUTH = {"Authorization": "Bearer test-key"}

FAKE_REG = """
models:
  - alias: echo
    backend: fake
    runtime_model_id: m
"""

OLLAMA_REG = """
models:
  - alias: remote
    backend: ollama
    runtime_model_id: llama3
    base_url: http://127.0.0.1:11434
"""


def _settings(tmp_path: Path, body: str, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(body).strip())
    kwargs = dict(
        auth_enabled=True,
        api_keys=["test-key"],
        registry_path=str(reg),
        enabled_backends=["fake", "ollama"],
        allowed_model_roots=[str(tmp_path)],
        model_idle_timeout_seconds=100,
        idle_sweep_interval_seconds=0.2,
        health_probe_enabled=False,
        allow_loopback_backends=True,
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _chat(client, model="echo"):
    return client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": model, "messages": [{"role": "user", "content": "x"}]},
    )


def test_healthz_backends_reports_not_loaded(tmp_path):
    s = _settings(tmp_path, FAKE_REG)
    with TestClient(create_app(s)) as c:
        r = c.get("/healthz/backends", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        be = {b["alias"]: b for b in body["backends"]}
        # A broker-managed model that is not loaded is "not_loaded", not a failure.
        assert be["echo"]["status"] == "not_loaded"


def test_healthz_backends_ok_after_load(tmp_path):
    s = _settings(tmp_path, FAKE_REG)
    with TestClient(create_app(s)) as c:
        assert _chat(c).status_code == 200
        be = {b["alias"]: b for b in c.get("/healthz/backends", headers=AUTH).json()["backends"]}
        assert be["echo"]["status"] == "ok"


def test_healthz_backends_requires_auth(tmp_path):
    s = _settings(tmp_path, FAKE_REG)
    with TestClient(create_app(s)) as c:
        assert c.get("/healthz/backends").status_code == 401


def test_status_includes_live_health_field(tmp_path):
    s = _settings(tmp_path, FAKE_REG)
    with TestClient(create_app(s)) as c:
        _chat(c)
        body = c.get("/status", headers=AUTH).json()
        m = {x["alias"]: x for x in body["models"]}["echo"]
        assert "healthy" in m
        assert m["model"] == "echo"
        assert "pools" in body


def test_api_ps_empty_without_ollama(tmp_path):
    s = _settings(tmp_path, FAKE_REG)
    with TestClient(create_app(s)) as c:
        r = c.get("/api/ps", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["daemons"] == []


async def test_periodic_probe_marks_dead_backend_failed(tmp_path):
    s = _settings(tmp_path, FAKE_REG)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    mgr = ModelManager(reg, s)
    mgr.validate_all()
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert mgr.models["echo"].machine.state in (State.READY, State.BUSY)

    async def dead() -> HealthResult:
        return HealthResult(healthy=False, detail="connection refused")

    mgr.models["echo"].adapter.health = dead  # type: ignore[assignment]
    await mgr._health_probe_once()
    # Backend gone -> model marked FAILED (so the next request reloads/reroutes).
    assert mgr.models["echo"].machine.state == State.FAILED
    assert mgr.models["echo"].healthy is False

    # And the next request transparently reloads it.
    mgr.models["echo"].adapter.health = FakeHealthy()  # type: ignore[assignment]
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r2")
    assert mgr.models["echo"].machine.state in (State.READY, State.BUSY)
    await mgr.shutdown()


class FakeHealthy:
    async def __call__(self) -> HealthResult:
        return HealthResult(healthy=True, detail="ok")


async def test_probe_never_loads_an_unloaded_managed_model(tmp_path):
    s = _settings(tmp_path, FAKE_REG)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    mgr = ModelManager(reg, s)
    mgr.validate_all()
    # Nothing loaded; a probe must not start/load anything.
    await mgr._health_probe_once()
    assert mgr.models["echo"].machine.state == State.UNLOADED
    assert mgr.models["echo"].adapter._load_count == 0
    assert mgr.models["echo"].last_health["status"] == "not_loaded"
    await mgr.shutdown()


async def test_ollama_ps_aggregation(tmp_path):
    s = _settings(tmp_path, OLLAMA_REG)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    mgr = ModelManager(reg, s)
    mgr.validate_all()

    async def fake_ps() -> dict:
        return {"models": [{"name": "llama3", "size_vram": 123}]}

    mgr.models["remote"].adapter.ps = fake_ps  # type: ignore[assignment]
    out = await mgr.ollama_ps()
    assert len(out["daemons"]) == 1
    assert out["daemons"][0]["base_url"] == "http://127.0.0.1:11434"
    assert out["daemons"][0]["models"][0]["name"] == "llama3"
    await mgr.shutdown()
