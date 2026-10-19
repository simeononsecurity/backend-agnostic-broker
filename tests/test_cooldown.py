"""Member cooldown / circuit-breaker behavior (ADR 0009).

Covers: a member is benched after N consecutive failures and selection skips it;
the half-open trial after the window elapses; a success resets the failure count;
and cooldown is visible in /status and /healthz/backends. Default-off behavior is
covered by the unchanged routing/sticky suites.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

from broker.config import Settings
from broker.errors import BackendUnavailableError
from broker.manager import ModelManager
from broker.registry import load_registry

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
        max_loaded_models=5,
    )
    kwargs.update(over)
    s = Settings(**kwargs)
    reg_obj = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg_obj, s)
    m.validate_all()
    return m


def _msg() -> dict:
    return {"messages": [{"role": "user", "content": "x"}]}


def _served(result: dict) -> str:
    return result["choices"][0]["message"]["content"]


def _instrument(mgr: ModelManager, failing: set[str]) -> dict[str, int]:
    calls: dict[str, int] = {a: 0 for a in mgr.models}

    def make(alias: str):
        async def chat(payload, request_id):
            calls[alias] += 1
            if alias in failing:
                raise BackendUnavailableError("boom", backend="fake", model=alias)
            return {
                "model": alias,
                "choices": [{"message": {"role": "assistant", "content": f"echo: {alias}"}}],
                "usage": {},
            }

        return chat

    for alias, mm in mgr.models.items():
        mm.adapter.chat = make(alias)  # type: ignore[method-assign]
    return calls


async def test_member_benched_after_failures_and_skipped(tmp_path):
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_seconds=30)
    calls = _instrument(mgr, failing={"node-a"})
    # Pin a session to node-a; it fails, fails over to node-b, and is benched.
    await mgr.state.sticky_set("chat", "s", "node-a")
    r1 = _served(await mgr.chat("chat", _msg(), "r1", route_key="s"))
    assert r1 == "echo: node-b"
    cd = await mgr.state.get_cooldown("node-a")
    assert cd.in_cooldown

    # A fresh session must now skip the benched node-a entirely.
    calls["node-a"] = 0
    for i in range(4):
        out = _served(await mgr.chat("chat", _msg(), f"n{i}", route_key=f"sess-{i}"))
        assert out == "echo: node-b"
    assert calls["node-a"] == 0  # benched member never selected
    await mgr.shutdown()


async def test_half_open_trial_after_expiry(tmp_path):
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_seconds=30)
    _instrument(mgr, failing={"node-a"})
    await mgr.state.sticky_set("chat", "s", "node-a")
    await mgr.chat("chat", _msg(), "r1", route_key="s")  # node-a fails → benched
    assert (await mgr.state.get_cooldown("node-a")).in_cooldown

    # Expire the cooldown window (white-box: rewind 'until' into the past) and
    # let node-a recover. The next request that lands on it is the half-open
    # trial; on success the breaker closes and the failure count resets.
    failures, _until = mgr.state._cooldowns["node-a"]
    mgr.state._cooldowns["node-a"] = (failures, time.time() - 1)
    assert not (await mgr.state.get_cooldown("node-a")).in_cooldown  # eligible again

    _instrument(mgr, failing=set())  # node-a healthy now
    await mgr.state.sticky_set("chat", "s", "node-a")  # re-pin so the trial lands on node-a
    out = _served(await mgr.chat("chat", _msg(), "r2", route_key="s"))
    assert out == "echo: node-a"  # trialed and served
    cd = await mgr.state.get_cooldown("node-a")
    assert not cd.in_cooldown and cd.failures == 0  # breaker closed, reset
    await mgr.shutdown()


async def test_success_resets_failure_count(tmp_path):
    mgr = _mgr(tmp_path, cooldown_failures=3, cooldown_seconds=30)
    _instrument(mgr, failing={"node-a"})
    await mgr.state.sticky_set("chat", "s", "node-a")
    await mgr.chat("chat", _msg(), "r1", route_key="s")  # 1 failure (<3), not benched
    assert mgr.state._cooldowns["node-a"][0] == 1
    assert not (await mgr.state.get_cooldown("node-a")).in_cooldown

    _instrument(mgr, failing=set())  # node-a healthy
    await mgr.state.sticky_set("chat", "s", "node-a")
    await mgr.chat("chat", _msg(), "r2", route_key="s")  # success resets
    assert (await mgr.state.get_cooldown("node-a")).failures == 0
    await mgr.shutdown()


async def test_cooldown_visible_in_status_and_health(tmp_path):
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_seconds=30)
    _instrument(mgr, failing={"node-a"})
    await mgr.state.sticky_set("chat", "s", "node-a")
    await mgr.chat("chat", _msg(), "r1", route_key="s")

    st = await mgr.status()
    assert st["cooldowns_enabled"] is True
    assert st["state_backend"] == "in_process"
    by_alias = {m["alias"]: m for m in st["models"]}
    assert by_alias["node-a"]["cooldown"]["in_cooldown"] is True
    assert by_alias["node-b"]["cooldown"]["in_cooldown"] is False

    health = await mgr.backends_health()
    hb = {b["alias"]: b for b in health["backends"]}
    assert hb["node-a"]["cooldown"]["in_cooldown"] is True
    await mgr.shutdown()


async def test_cooldown_default_off_no_benching(tmp_path):
    mgr = _mgr(tmp_path)  # cooldown_failures defaults to 0
    _instrument(mgr, failing={"node-a"})
    await mgr.state.sticky_set("chat", "s", "node-a")
    await mgr.chat("chat", _msg(), "r1", route_key="s")
    # No cooldown state is tracked at all when disabled.
    cd = await mgr.state.get_cooldown("node-a")
    assert not cd.in_cooldown and cd.failures == 0
    st = await mgr.status()
    assert st["cooldowns_enabled"] is False
    await mgr.shutdown()
