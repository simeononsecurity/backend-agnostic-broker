"""Sticky session routing: pin a session to one member, switch only on failure."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.manager import ModelManager
from broker.registry import load_registry

POOL_REGISTRY = """
models:
  - alias: node-a
    backend: fake
    runtime_model_id: shared-model
    pool: chat
  - alias: node-b
    backend: fake
    runtime_model_id: shared-model
    pool: chat
"""


def _make_settings(tmp_path: Path, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(POOL_REGISTRY).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        model_idle_timeout_seconds=100,
        idle_sweep_interval_seconds=0.2,
        health_probe_enabled=False,
        per_model_max_concurrency=4,
        max_concurrent_requests=32,
        max_loaded_models=5,
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _manager(tmp_path: Path, **over) -> ModelManager:
    s = _make_settings(tmp_path, **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    return m


def _msg() -> dict:
    return {"messages": [{"role": "user", "content": "x"}]}


def _served(result: dict) -> str:
    return result["choices"][0]["message"]["content"]


# --- manager level ------------------------------------------------------


async def test_sticky_is_the_default_strategy(tmp_path):
    mgr = _manager(tmp_path)
    assert mgr.settings.route_strategy == "sticky"
    await mgr.shutdown()


async def test_sticky_pins_one_session_to_one_member(tmp_path):
    mgr = _manager(tmp_path)
    served = set()
    for i in range(12):
        served.add(_served(await mgr.chat("chat", _msg(), f"r{i}", route_key="sess-1")))
    # Every request for the same session stuck to the SAME member.
    assert len(served) == 1, served
    # Only that member ever loaded.
    loaded = {a for a, m in mgr.models.items() if m.adapter._load_count > 0}
    assert len(loaded) == 1, loaded
    await mgr.shutdown()


async def test_sticky_distinct_sessions_spread_across_members(tmp_path):
    mgr = _manager(tmp_path)
    served = set()
    for i in range(8):
        served.add(_served(await mgr.chat("chat", _msg(), f"r{i}", route_key=f"sess-{i}")))
    # New sessions are assigned round-robin/least-busy, so both members get used.
    assert served == {"echo: node-a", "echo: node-b"}, served
    await mgr.shutdown()


async def test_sticky_switches_only_when_member_fails_and_repins(tmp_path):
    mgr = _manager(tmp_path)
    first = _served(await mgr.chat("chat", _msg(), "r0", route_key="sess-x"))
    pinned = first.split(": ")[1]
    other = "node-b" if pinned == "node-a" else "node-a"
    assert mgr._sticky[("chat", "sess-x")] == pinned

    # Break the pinned member; the same session must fail over to the other one.
    mgr.models[pinned].adapter.fail_on_chat = True
    r1 = _served(await mgr.chat("chat", _msg(), "r1", route_key="sess-x"))
    assert r1 == f"echo: {other}"
    # The session is now RE-PINNED to the healthy member (switch only as needed).
    assert mgr._sticky[("chat", "sess-x")] == other

    # Subsequent requests go straight to the new member (no repeated failover).
    mgr.models[pinned].adapter.fail_on_chat = False
    r2 = _served(await mgr.chat("chat", _msg(), "r2", route_key="sess-x"))
    assert r2 == f"echo: {other}"
    assert mgr._sticky[("chat", "sess-x")] == other
    await mgr.shutdown()


async def test_sticky_lru_bound_evicts_oldest(tmp_path):
    mgr = _manager(tmp_path, route_sticky_max_entries=3)
    for i in range(10):
        await mgr.chat("chat", _msg(), f"r{i}", route_key=f"sess-{i}")
    assert len(mgr._sticky) == 3  # bounded
    # Only the most-recent keys survive.
    assert set(mgr._sticky) == {("chat", "sess-7"), ("chat", "sess-8"), ("chat", "sess-9")}
    await mgr.shutdown()


# --- API level (session key derivation) ---------------------------------


def test_api_x_session_id_header_pins(tmp_path):
    with TestClient(create_app(_make_settings(tmp_path))) as c:

        def ask(sid):
            r = c.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": sid},
                json={"model": "chat", **_msg()},
            )
            return _served(r.json())

        assert ask("alpha") == ask("alpha") == ask("alpha")  # pinned by header


def test_api_distinct_sessions_spread(tmp_path):
    with TestClient(create_app(_make_settings(tmp_path))) as c:
        seen = set()
        for i in range(8):
            r = c.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": f"s{i}"},
                json={"model": "chat", **_msg()},
            )
            seen.add(_served(r.json()))
        assert seen == {"echo: node-a", "echo: node-b"}, seen


def test_api_user_field_pins_when_no_header(tmp_path):
    with TestClient(create_app(_make_settings(tmp_path))) as c:

        def ask(user):
            r = c.post("/v1/chat/completions", json={"model": "chat", "user": user, **_msg()})
            return _served(r.json())

        assert ask("bob") == ask("bob")  # pinned by OpenAI `user` field


def test_api_status_reports_sticky(tmp_path):
    with TestClient(create_app(_make_settings(tmp_path))) as c:
        c.post(
            "/v1/chat/completions", headers={"X-Session-Id": "z"}, json={"model": "chat", **_msg()}
        )
        st = c.get("/status").json()
        assert st["route_strategy"] == "sticky"
        assert st["sticky_sessions"] >= 1
