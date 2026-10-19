"""Latency-budget routing (ADR 0011): pick a faster capable model under an SLA.

Latency is estimated from the usage log (p90 + cold-start penalty); the budget
never overrides the capability filter. Covers the budget pick, no-op when the
pick is already fast enough, capability safety, and header parsing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.router import CallableRouterBackend
from broker.usage import UsageLog

LATENCY_REGISTRY = """
models:
  - alias: fast
    backend: fake
    runtime_model_id: f
    tags: [general]
    cost_tier: 3
  - alias: slow
    backend: fake
    runtime_model_id: s
    tags: [general, reasoning]
    cost_tier: 5
  - alias: vizard
    backend: fake
    runtime_model_id: v
    tags: [vision]
    supports_vision: true
    cost_tier: 4
"""


def _settings(tmp_path: Path, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(LATENCY_REGISTRY).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        router_backend="heuristic",
        router_cold_start_ms=0.0,  # make estimates deterministic in tests
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _manager(tmp_path: Path, **over) -> ModelManager:
    s = _settings(tmp_path, **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    # Seed the usage log: slow is slow (p90 ~5000ms), fast is fast (~100ms).
    m.usage = UsageLog(":memory:")
    for _ in range(10):
        m.usage.record(endpoint="chat", model="slow", latency_ms=5000.0)
        m.usage.record(endpoint="chat", model="fast", latency_ms=100.0)
        m.usage.record(endpoint="chat", model="vizard", latency_ms=5000.0)
    return m


def _msg(text: str = "hello there") -> dict:
    return {"messages": [{"role": "user", "content": text}]}


def test_budget_switches_to_faster_capable_model(tmp_path):
    mgr = _manager(tmp_path)
    # Force the classifier to pick the slow model; a tight budget should override.
    mgr.router.backend = CallableRouterBackend(lambda f, m, c: "slow")
    d = mgr.router.route(_msg(), latency_budget_ms=1000.0)
    assert d.chosen == "fast"
    assert d.via == "latency_budget"


def test_budget_keeps_pick_when_fast_enough(tmp_path):
    mgr = _manager(tmp_path)
    mgr.router.backend = CallableRouterBackend(lambda f, m, c: "fast")
    d = mgr.router.route(_msg(), latency_budget_ms=10000.0)
    assert d.chosen == "fast"
    assert d.via != "latency_budget"  # already within budget, no change


def test_budget_never_violates_capability(tmp_path):
    mgr = _manager(tmp_path)
    # An image request: only the (slow) vision model is capable. The budget must
    # not switch to a faster non-vision model.
    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
        ]
    }
    d = mgr.router.route(payload, latency_budget_ms=100.0)
    assert d.chosen == "vizard"  # fastest *capable* model, not fast/slow


def test_budget_default_from_config(tmp_path):
    mgr = _manager(tmp_path, router_latency_budget_ms=1000.0)
    mgr.router.backend = CallableRouterBackend(lambda f, m, c: "slow")
    d = mgr.router.route(_msg())  # no per-request budget -> config default applies
    assert d.chosen == "fast"


# --- header parsing / validation (API) ---------------------------------------


def test_latency_header_invalid_rejected(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", **_msg()},
            headers={"x-broker-latency-budget-ms": "not-a-number"},
        )
        assert r.status_code == 400


def test_latency_header_nonpositive_rejected(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", **_msg()},
            headers={"x-broker-latency-budget-ms": "0"},
        )
        assert r.status_code == 400


def test_latency_header_valid_routes(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", **_msg()},
            headers={"x-broker-latency-budget-ms": "1500"},
        )
        assert r.status_code == 200
