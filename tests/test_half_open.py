"""Single-trial half-open probe (ADR 0017).

A recovering member (cooldown window elapsed) admits exactly ONE probe request
via the StateStore ``try_half_open`` lock; concurrent requests skip it until the
trial resolves. Store-level tests run against both backends (in-process +
fakeredis); manager tests show the gating end-to-end and that it is opt-in.
"""

from __future__ import annotations

import asyncio
import textwrap
import time
from pathlib import Path

import pytest

from broker.config import Settings
from broker.errors import BackendUnavailableError
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.state import InProcessStateStore, RedisStateStore

try:
    import fakeredis.aioredis as _fakeredis_aio

    _HAVE_FAKEREDIS = True
except Exception:  # noqa: BLE001
    _HAVE_FAKEREDIS = False


@pytest.fixture(params=["in_process", "redis"])
async def store(request):
    if request.param == "redis":
        if not _HAVE_FAKEREDIS:
            pytest.skip("redis/fakeredis not installed (optional [redis] extra)")
        s: InProcessStateStore | RedisStateStore = RedisStateStore(
            "redis://fake", prefix="test", client=_fakeredis_aio.FakeRedis()
        )
    else:
        s = InProcessStateStore()
    yield s
    await s.close()


# --- store-level single-claim semantics ---------------------------------


async def test_concurrent_try_half_open_exactly_one_winner(store):
    results = await asyncio.gather(*(store.try_half_open("m", 30) for _ in range(20)))
    assert sum(1 for r in results if r) == 1  # exactly one probe admitted


async def test_half_open_released_on_success(store):
    assert await store.try_half_open("m", 30) is True
    assert await store.try_half_open("m", 30) is False
    await store.record_success("m")
    assert await store.try_half_open("m", 30) is True  # lock released → new trial


async def test_half_open_blocks_until_resolved(store):
    assert await store.try_half_open("m", 30) is True
    # Every subsequent attempt is refused while the trial is outstanding.
    for _ in range(5):
        assert await store.try_half_open("m", 30) is False


# --- manager integration ------------------------------------------------

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


async def _bench_then_expire(mgr: ModelManager, alias: str, seconds: float = 30) -> None:
    """Bench a member, then rewind its window so it is 'recovering'."""
    now = time.time()
    await mgr.state.record_failure(alias, threshold=1, cooldown_seconds=seconds, now=now)
    failures, _until = mgr.state._cooldowns[alias]
    mgr.state._cooldowns[alias] = (failures, now - 1)


async def test_single_trial_routes_concurrent_probes_away(tmp_path):
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_half_open_single_trial=True)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: dict[str, int] = {"node-a": 0, "node-b": 0}

    async def node_a_chat(payload, request_id):
        calls["node-a"] += 1
        started.set()
        await release.wait()  # hold the trial open
        return {
            "model": "node-a",
            "choices": [{"message": {"role": "assistant", "content": "echo: node-a"}}],
            "usage": {},
        }

    async def node_b_chat(payload, request_id):
        calls["node-b"] += 1
        return {
            "model": "node-b",
            "choices": [{"message": {"role": "assistant", "content": "echo: node-b"}}],
            "usage": {},
        }

    mgr.models["node-a"].adapter.chat = node_a_chat  # type: ignore[method-assign]
    mgr.models["node-b"].adapter.chat = node_b_chat  # type: ignore[method-assign]

    await _bench_then_expire(mgr, "node-a")
    # Probe request pinned to node-a claims the one trial and blocks in-serve.
    await mgr.state.sticky_set("chat", "trial", "node-a")
    t1 = asyncio.create_task(mgr.chat("chat", _msg(), "r1", route_key="trial"))
    await started.wait()
    # A concurrent request pinned to node-a must skip the held trial → node-b.
    await mgr.state.sticky_set("chat", "trial2", "node-a")
    r2 = await mgr.chat("chat", _msg(), "r2", route_key="trial2")
    assert _served(r2) == "echo: node-b"
    release.set()
    r1 = await t1
    assert _served(r1) == "echo: node-a"  # the single trial completed on node-a
    assert calls["node-a"] == 1  # probed exactly once
    await mgr.shutdown()


async def test_single_trial_success_closes_breaker(tmp_path):
    mgr = _mgr(tmp_path, cooldown_failures=1, cooldown_half_open_single_trial=True)
    calls = {"node-a": 0}

    async def ok(payload, request_id):
        calls["node-a"] += 1
        return {
            "model": "node-a",
            "choices": [{"message": {"role": "assistant", "content": "echo: node-a"}}],
            "usage": {},
        }

    mgr.models["node-a"].adapter.chat = ok  # type: ignore[method-assign]
    await _bench_then_expire(mgr, "node-a")
    await mgr.state.sticky_set("chat", "s", "node-a")
    out = await mgr.chat("chat", _msg(), "r0", route_key="s")
    assert _served(out) == "echo: node-a"
    cd = await mgr.state.get_cooldown("node-a")
    assert not cd.in_cooldown and cd.failures == 0  # breaker closed
    await mgr.shutdown()


async def test_single_trial_failure_rebenches(tmp_path, monkeypatch):
    mgr = _mgr(
        tmp_path,
        cooldown_failures=1,
        cooldown_seconds=30,
        cooldown_half_open_single_trial=True,
    )

    async def boom(payload, request_id):
        raise BackendUnavailableError("down", backend="fake", model="node-a")

    async def _noop(_d):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    mgr.models["node-a"].adapter.chat = boom  # type: ignore[method-assign]
    await _bench_then_expire(mgr, "node-a")
    await mgr.state.sticky_set("chat", "s", "node-a")
    # The trial fails → node-a re-benched; request fails over to node-b.
    out = await mgr.chat("chat", _msg(), "r0", route_key="s")
    assert _served(out) == "echo: node-b"
    assert (await mgr.state.get_cooldown("node-a")).in_cooldown
    await mgr.shutdown()


async def test_default_off_allows_multiple_probes(tmp_path):
    # With single-trial OFF (default), a recovering member is freely probed —
    # ADR 0009 behavior preserved.
    mgr = _mgr(tmp_path, cooldown_failures=1)  # half_open_single_trial defaults False
    assert mgr.settings.cooldown_half_open_single_trial is False
    calls = {"node-a": 0}

    async def ok(payload, request_id):
        calls["node-a"] += 1
        return {
            "model": "node-a",
            "choices": [{"message": {"role": "assistant", "content": "echo: node-a"}}],
            "usage": {},
        }

    mgr.models["node-a"].adapter.chat = ok  # type: ignore[method-assign]
    for i in range(3):
        await _bench_then_expire(mgr, "node-a")
        await mgr.state.sticky_set("chat", f"s{i}", "node-a")
        out = await mgr.chat("chat", _msg(), f"r{i}", route_key=f"s{i}")
        assert _served(out) == "echo: node-a"
    assert calls["node-a"] == 3  # probed every time, no single-trial gate
    await mgr.shutdown()
