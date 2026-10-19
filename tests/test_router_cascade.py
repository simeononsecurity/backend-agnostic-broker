"""Cascade mode (ADR 0011): cheapest-capable first, escalate once on a signal.

Covers each escalation signal, the no-escalation pass-through, both-hops
recording, the streaming fall-back-to-single, and the pure signal function.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.manager import ModelManager
from broker.registry import CascadeConfig, load_registry
from broker.router import should_escalate
from broker.usage import UsageLog

# Cheapest model (tiny, cost 1) carries a shaping tag so its fake answer trips a
# cascade signal; the stronger model (strong, cost 4) answers cleanly.
ESCALATE_REGISTRY = """
router:
  mode: cascade
  default: tiny
models:
  - alias: tiny
    backend: fake
    runtime_model_id: t
    tags: [general, cheap, {signal}]
    cost_tier: 1
  - alias: strong
    backend: fake
    runtime_model_id: s
    tags: [general, reasoning]
    cost_tier: 4
"""

CLEAN_REGISTRY = """
router:
  mode: cascade
  default: cheap
models:
  - alias: cheap
    backend: fake
    runtime_model_id: c
    tags: [general, cheap]
    cost_tier: 1
  - alias: strong
    backend: fake
    runtime_model_id: s
    tags: [general, reasoning]
    cost_tier: 4
"""


def _settings(tmp_path: Path, body: str, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(body).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        router_backend="heuristic",
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _manager(tmp_path: Path, body: str, **over) -> ModelManager:
    s = _settings(tmp_path, body, **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    return m


def _msg(text: str = "hello there") -> dict:
    return {"messages": [{"role": "user", "content": text}]}


# --- pure signal function -----------------------------------------------------


def _result(content: str = "ok", finish: str = "stop") -> dict:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]
    }


def test_should_escalate_length():
    assert should_escalate(_result(finish="length"), CascadeConfig()) == (
        True,
        "finish_reason=length",
    )


def test_should_escalate_empty():
    assert should_escalate(_result(content=""), CascadeConfig())[0] is True


def test_should_escalate_refusal():
    assert should_escalate(_result("I'm sorry, but I cannot help"), CascadeConfig())[0] is True


def test_should_not_escalate_good_answer():
    assert should_escalate(_result("Here is a complete answer."), CascadeConfig()) == (False, "")


def test_should_escalate_signals_are_toggleable():
    cfg = CascadeConfig(escalate_on_length=False)
    assert should_escalate(_result(finish="length"), cfg)[0] is False


# --- manager-level cascade execution -----------------------------------------


async def test_cascade_escalates_on_length(tmp_path):
    mgr = _manager(tmp_path, ESCALATE_REGISTRY.format(signal="truncate"))
    out = await mgr.chat("auto", _msg(), "r1")
    assert out["model"] == "strong"  # escalated from the truncated primary
    assert out["_broker_route"] == "tiny->strong"
    await mgr.shutdown()


async def test_cascade_escalates_on_refusal(tmp_path):
    mgr = _manager(tmp_path, ESCALATE_REGISTRY.format(signal="refuse"))
    out = await mgr.chat("auto", _msg(), "r1")
    assert out["model"] == "strong"
    await mgr.shutdown()


async def test_cascade_escalates_on_empty(tmp_path):
    mgr = _manager(tmp_path, ESCALATE_REGISTRY.format(signal="empty"))
    out = await mgr.chat("auto", _msg(), "r1")
    assert out["model"] == "strong"
    await mgr.shutdown()


async def test_cascade_no_escalation_serves_primary(tmp_path):
    mgr = _manager(tmp_path, CLEAN_REGISTRY)
    out = await mgr.chat("auto", _msg(), "r1")
    assert out["model"] == "cheap"  # good answer -> stays on the cheapest model
    assert out["_broker_route"] == "cheap"
    await mgr.shutdown()


async def test_cascade_records_both_hops(tmp_path):
    mgr = _manager(tmp_path, ESCALATE_REGISTRY.format(signal="truncate"))
    mgr.usage = UsageLog(":memory:")
    out = await mgr.chat("auto", _msg(), "r1")
    # The final (escalation) row is recorded by the API layer in production; the
    # manager records the intermediate primary hop when it escalates.
    summary = mgr.usage.summary()
    assert summary["requests"] == 1  # the primary hop
    assert any(row["model"] == "tiny" for row in summary["recent"])
    # Decision log shows the cascade path for /status.
    cascades = [d for d in mgr.router.decisions if "cascade" in d]
    assert cascades and cascades[-1]["cascade"]["path"] == "tiny->strong"
    assert out["model"] == "strong"
    await mgr.shutdown()


async def test_single_mode_has_no_cascade(tmp_path):
    mgr = _manager(tmp_path, CLEAN_REGISTRY.replace("mode: cascade", "mode: single"))
    out = await mgr.chat("auto", _msg(), "r1")
    # single mode: served by the cheapest capable; no "->" path.
    assert "->" not in out["_broker_route"]
    await mgr.shutdown()


# --- API level: header + streaming fallback ----------------------------------


def test_api_cascade_sets_route_header(tmp_path):
    with TestClient(
        create_app(_settings(tmp_path, ESCALATE_REGISTRY.format(signal="truncate")))
    ) as c:
        r = c.post("/v1/chat/completions", json={"model": "auto", **_msg()})
        assert r.status_code == 200
        assert r.json()["model"] == "strong"
        assert r.headers["x-broker-route"] == "tiny->strong"


def test_api_cascade_streaming_falls_back_to_single(tmp_path):
    with TestClient(
        create_app(_settings(tmp_path, ESCALATE_REGISTRY.format(signal="truncate")))
    ) as c:
        r = c.post("/v1/chat/completions", json={"model": "auto", "stream": True, **_msg()})
        assert r.status_code == 200
        # Streaming can't re-score a stream -> single strong pick, documented route.
        assert "cascade->single: streaming" in r.headers["x-broker-route"]
        assert r.headers["x-broker-route"].startswith("strong")
