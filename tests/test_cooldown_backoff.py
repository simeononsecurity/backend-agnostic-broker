"""Exponential cooldown-window growth + decay (ADR 0017).

Envoy outlier detection grows a host's ejection time by base x consecutive
ejections, capped by max_ejection_time, and decays the multiplier after sustained
health. These tests drive the StateStore cooldown primitives (both backends) with
an injected clock, plus an end-to-end manager check.
"""

from __future__ import annotations

import textwrap
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


async def _eject(store, key, *, base, mult, cap, now):
    """Drive one fresh ejection (threshold=1) at ``now`` and return the record."""
    return await store.record_failure(
        key,
        threshold=1,
        cooldown_seconds=base,
        multiplier=mult,
        max_cooldown_seconds=cap,
        now=now,
    )


# --- growth -------------------------------------------------------------


async def test_window_grows_on_repeated_ejections(store):
    now = 1_000_000.0
    r1 = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    assert r1.ejections == 1
    assert r1.seconds_remaining == pytest.approx(10.0)
    # Advance past expiry, eject again → window doubles.
    now += 11
    r2 = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    assert r2.ejections == 2
    assert r2.seconds_remaining == pytest.approx(20.0)
    now += 21
    r3 = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    assert r3.ejections == 3
    assert r3.seconds_remaining == pytest.approx(40.0)


async def test_growth_capped_by_max_seconds(store):
    now = 2_000_000.0
    r1 = await _eject(store, "m", base=10, mult=2.0, cap=25, now=now)
    assert r1.seconds_remaining == pytest.approx(10.0)
    now += 11
    r2 = await _eject(store, "m", base=10, mult=2.0, cap=25, now=now)
    assert r2.seconds_remaining == pytest.approx(20.0)
    now += 21
    r3 = await _eject(store, "m", base=10, mult=2.0, cap=25, now=now)
    assert r3.seconds_remaining == pytest.approx(25.0)  # capped
    now += 26
    r4 = await _eject(store, "m", base=10, mult=2.0, cap=25, now=now)
    assert r4.seconds_remaining == pytest.approx(25.0)  # stays capped


async def test_multiplier_one_is_fixed_window(store):
    now = 3_000_000.0
    for _i in range(3):
        r = await _eject(store, "m", base=15, mult=1.0, cap=0, now=now)
        now += 16
    assert r.seconds_remaining == pytest.approx(15.0)  # never grows


async def test_repeated_failures_within_open_window_do_not_escalate(store):
    now = 4_000_000.0
    r1 = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    assert r1.ejections == 1
    # A second failure while STILL benched is not a new ejection.
    r2 = await store.record_failure(
        "m", threshold=1, cooldown_seconds=10, multiplier=2.0, now=now + 1
    )
    assert r2.ejections == 1  # unchanged — same ejection episode


# --- decay --------------------------------------------------------------


async def test_decay_on_any_success_by_default(store):
    now = 5_000_000.0
    await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    now += 11
    r2 = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    assert r2.ejections == 2
    # decay_seconds=0 → any success resets the multiplier.
    await store.record_success("m", decay_seconds=0, now=now + 1)
    now += 2
    r = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    assert r.ejections == 1 and r.seconds_remaining == pytest.approx(10.0)


async def test_decay_requires_sustained_success(store):
    now = 6_000_000.0
    await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    now += 11
    r2 = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    assert r2.ejections == 2
    # A success too soon after the ejection (within decay window) keeps the count.
    await store.record_success("m", decay_seconds=100, now=now + 5)
    r_keep = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now + 6)
    assert r_keep.ejections == 3  # escalated, not reset
    # A success well after the last ejection decays it.
    await store.record_success("m", decay_seconds=100, now=now + 6 + 200)
    r_reset = await _eject(store, "m", base=10, mult=2.0, cap=0, now=now + 6 + 201)
    assert r_reset.ejections == 1


async def test_ejections_surfaced_in_to_dict(store):
    now = 7_000_000.0
    await _eject(store, "m", base=10, mult=2.0, cap=0, now=now)
    cd = await store.get_cooldown("m", now=now + 1)
    assert cd.to_dict()["ejections"] == 1


# --- manager end-to-end -------------------------------------------------

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


async def test_manager_grows_cooldown_window(tmp_path):
    mgr = _mgr(
        tmp_path,
        cooldown_failures=1,
        cooldown_seconds=10,
        cooldown_backoff_multiplier=3.0,
    )

    async def boom(payload, request_id):
        raise BackendUnavailableError("down", backend="fake", model="node-a")

    mgr.models["node-a"].adapter.chat = boom  # type: ignore[method-assign]

    await mgr.state.sticky_set("chat", "s", "node-a")
    await mgr.chat("chat", {"messages": [{"role": "user", "content": "x"}]}, "r0", route_key="s")
    cd1 = await mgr.state.get_cooldown("node-a")
    assert cd1.ejections == 1 and cd1.seconds_remaining == pytest.approx(10.0, abs=1.0)

    # Expire the window (white-box rewind) and fail it again → window triples.
    failures, _ = mgr.state._cooldowns["node-a"]
    import time as _t

    mgr.state._cooldowns["node-a"] = (failures, _t.time() - 1)
    await mgr.state.sticky_set("chat", "s", "node-a")
    await mgr.chat("chat", {"messages": [{"role": "user", "content": "x"}]}, "r1", route_key="s")
    cd2 = await mgr.state.get_cooldown("node-a")
    assert cd2.ejections == 2 and cd2.seconds_remaining == pytest.approx(30.0, abs=1.0)
    await mgr.shutdown()
