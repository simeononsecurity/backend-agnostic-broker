"""Ejection-percentage cap (ADR 0017, Envoy max_ejection_percent).

Covers the cap on simultaneously-benched members (keep the soonest-to-recover
eligible), the never-strand guarantee when a correlated failure would bench a
whole pool, and that the default (0) preserves ADR 0009 behavior.
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path

from broker.config import Settings
from broker.errors import BackendUnavailableError
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.state import CooldownRecord

REG = """
models:
  - alias: node-a
    backend: fake
    runtime_model_id: shared
    pool: chat
  - alias: node-b
    backend: fake
    runtime_model_id: shared
    pool: chat
  - alias: node-c
    backend: fake
    runtime_model_id: shared
    pool: chat
  - alias: node-d
    backend: fake
    runtime_model_id: shared
    pool: chat
"""


def _mgr(tmp_path: Path, **over) -> ModelManager:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(REG).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        max_loaded_models=8,
        route_strategy="least_busy",
    )
    kwargs.update(over)
    s = Settings(**kwargs)
    reg_obj = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg_obj, s)
    m.validate_all()
    return m


def _msg() -> dict:
    return {"messages": [{"role": "user", "content": "x"}]}


def _instrument_ok(mgr: ModelManager) -> dict[str, int]:
    calls: dict[str, int] = {a: 0 for a in mgr.models}

    def make(alias: str):
        async def chat(payload, request_id):
            calls[alias] += 1
            return {
                "model": alias,
                "choices": [{"message": {"role": "assistant", "content": f"echo: {alias}"}}],
                "usage": {},
            }

        return chat

    for alias, mm in mgr.models.items():
        mm.adapter.chat = make(alias)  # type: ignore[method-assign]
    return calls


# --- unit: the cap function --------------------------------------------


def test_apply_ejection_cap_disabled_returns_all(tmp_path):
    mgr = _mgr(tmp_path, max_ejection_percent=0, cooldown_failures=1)
    now = 1000.0
    recs = {a: CooldownRecord(until=now + 10, now=now) for a in ("node-a", "node-b")}
    benched = {"node-a", "node-b"}
    assert mgr._apply_ejection_cap(benched, recs, 4) == benched


def test_apply_ejection_cap_keeps_soonest_to_recover_eligible(tmp_path):
    mgr = _mgr(tmp_path, max_ejection_percent=50, cooldown_failures=1)
    now = 1000.0
    # 3 of 4 benched with staggered expiry; cap = floor(50*4/100) = 2 benched.
    recs = {
        "node-a": CooldownRecord(until=now + 10, now=now),  # soonest → reinstated
        "node-b": CooldownRecord(until=now + 20, now=now),
        "node-c": CooldownRecord(until=now + 30, now=now),
    }
    kept = mgr._apply_ejection_cap({"node-a", "node-b", "node-c"}, recs, 4)
    assert kept == {"node-b", "node-c"}  # the two latest-recovering stay benched


def test_apply_ejection_cap_never_strands_small_pool(tmp_path):
    # 2-member pool, 50% cap → floor(1) benched; both benched means 1 reinstated.
    mgr = _mgr(tmp_path, max_ejection_percent=50, cooldown_failures=1)
    now = 1000.0
    recs = {
        "node-a": CooldownRecord(until=now + 5, now=now),
        "node-b": CooldownRecord(until=now + 50, now=now),
    }
    kept = mgr._apply_ejection_cap({"node-a", "node-b"}, recs, 2)
    assert kept == {"node-b"}  # node-a (soonest) reinstated → pool not stranded


# --- integration: selection honors the cap -----------------------------


async def test_capped_member_still_selected(tmp_path):
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_seconds=60, max_ejection_percent=50)
    calls = _instrument_ok(mgr)
    now = time.time()
    # Bench 3 of 4 with staggered expiry; node-a recovers soonest.
    await mgr.state.record_failure("node-a", threshold=1, cooldown_seconds=10, now=now)
    await mgr.state.record_failure("node-b", threshold=1, cooldown_seconds=20, now=now)
    await mgr.state.record_failure("node-c", threshold=1, cooldown_seconds=30, now=now)
    # Cap=2 benched → node-a reinstated. node-d is never benched. A burst of
    # requests must only ever land on node-a or node-d (never node-b/node-c).
    for i in range(8):
        out = await mgr.chat("chat", _msg(), f"r{i}")
        assert out["choices"][0]["message"]["content"] in ("echo: node-a", "echo: node-d")
    assert calls["node-b"] == 0 and calls["node-c"] == 0
    assert calls["node-a"] > 0  # the reinstated member is actually used
    await mgr.shutdown()


async def test_never_strand_when_all_benched(tmp_path):
    # All 4 benched, 25% cap → floor(1) stays benched, 3 reinstated. The pool
    # must keep serving rather than erroring.
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_seconds=60, max_ejection_percent=25)
    _instrument_ok(mgr)
    now = time.time()
    for i, a in enumerate(("node-a", "node-b", "node-c", "node-d")):
        await mgr.state.record_failure(a, threshold=1, cooldown_seconds=10 + i * 10, now=now)
    out = await mgr.chat("chat", _msg(), "r0")
    # node-a recovers soonest so it is one of the reinstated; served, not stranded.
    assert out["choices"][0]["message"]["content"].startswith("echo:")
    await mgr.shutdown()


async def test_cap_zero_falls_through_when_all_benched(tmp_path, monkeypatch):
    # Default cap (0): the ADR 0009 all-benched fall-through still self-heals.
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_seconds=60, max_ejection_percent=0)
    _instrument_ok(mgr)

    async def _noop(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    now = time.time()
    for a in ("node-a", "node-b", "node-c", "node-d"):
        await mgr.state.record_failure(a, threshold=1, cooldown_seconds=60, now=now)
    out = await mgr.chat("chat", _msg(), "r0")  # fall-through serves anyway
    assert out["choices"][0]["message"]["content"].startswith("echo:")
    await mgr.shutdown()


async def test_cap_still_lets_pool_fail_over_within_cap(tmp_path, monkeypatch):
    # A genuinely failing member is benched and skipped; the cap does not force a
    # dead member back prematurely while healthy members remain.
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_seconds=60, max_ejection_percent=50)
    calls = _instrument_ok(mgr)

    async def boom(payload, request_id):
        calls["node-a"] += 1
        raise BackendUnavailableError("down", backend="fake", model="node-a")

    mgr.models["node-a"].adapter.chat = boom  # type: ignore[method-assign]

    async def _noop(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    await mgr.state.sticky_set("chat", "s", "node-a")
    out = await mgr.chat("chat", _msg(), "r0", route_key="s")
    assert out["choices"][0]["message"]["content"] != "echo: node-a"  # failed over
    assert (await mgr.state.get_cooldown("node-a")).in_cooldown
    await mgr.shutdown()
