"""ADR 0023: bounded, fair, timed admission queue under overload.

Deterministic async tests on the fake backend (+ fake telemetry / fake host
load). Concurrency is forced with ``per_model_max_concurrency=1`` and a gated
adapter ``chat`` that blocks on an event the test releases, so admission order is
observable without real timing races.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from broker.admission import (
    TRIGGER_CAPACITY_MEMORY,
    TRIGGER_DEVICE_MEMORY,
    TRIGGER_MAX_LOADED,
    AdmissionQueue,
)
from broker.config import Settings
from broker.errors import (
    CapacityUnavailableError,
    ConcurrencyLimitError,
    InsufficientQuotaError,
    QueueFullError,
    QueueTimeoutError,
    RateLimitExceededError,
)
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.resources import DeviceMemory, FakeTelemetry


def _qsettings(settings: Settings, **over) -> Settings:
    """The shared fixture settings with the queue on and serialized serving."""
    settings.queue_enabled = True
    settings.per_model_max_concurrency = 1
    settings.max_concurrent_requests = 8
    settings.max_loaded_models = 5
    settings.health_probe_enabled = False
    settings.queue_max_wait_seconds = 10.0
    settings.queue_pump_interval_seconds = 0.05
    for k, v in over.items():
        setattr(settings, k, v)
    return settings


def _manager(settings: Settings) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    m = ModelManager(reg, settings)
    m.validate_all()
    return m


def _msg() -> dict:
    return {"messages": [{"role": "user", "content": "x"}]}


def _gate(mgr: ModelManager, alias: str = "echo"):
    """Replace ``alias``'s adapter.chat with a coroutine that blocks on an event.

    Returns ``(release, entered)``: set ``release`` to let every blocked call
    return; ``entered`` records request_ids in the order they reach ``chat`` (i.e.
    the order they were admitted and acquired the serving slot).
    """
    release = asyncio.Event()
    entered: list[str] = []

    async def gated(payload, request_id):  # noqa: ANN001
        entered.append(request_id)
        await release.wait()
        return {
            "model": alias,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    mgr.models[alias].adapter.chat = gated
    return release, entered


async def _wait_until(pred, timeout: float = 3.0, interval: float = 0.01) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


async def _warm_and_hold(mgr: ModelManager, alias: str = "echo"):
    """Load ``alias``, then start one request that holds its only serving slot.

    Returns ``(release, entered, holder_task)``. The holder occupies the single
    per-model concurrency slot so every later request must queue.
    """
    # Warm the model with a normal (ungated) request so loading is out of the way.
    await mgr.chat(alias, _msg(), "warm")
    release, entered = _gate(mgr, alias)
    holder = asyncio.create_task(mgr.chat(alias, _msg(), "holder", queue_key="holder"))
    await _wait_until(lambda: "holder" in entered)  # holder now owns the slot
    return release, entered, holder


# --------------------------------------------------------------------------- #
# 1. saturation -> queued then served FIFO
# --------------------------------------------------------------------------- #
async def test_saturation_queues_then_serves_fifo(settings: Settings):
    mgr = _manager(_qsettings(settings))
    release, entered, holder = await _warm_and_hold(mgr)
    done: list[str] = []

    async def req(rid: str):
        await mgr.chat("echo", _msg(), rid, queue_key="k", queue_priority=0)
        done.append(rid)

    tasks = [asyncio.create_task(req(f"r{i}")) for i in range(3)]
    await _wait_until(lambda: mgr.admission.depth() == 3)
    assert mgr.admission.snapshot()["depth"] == 3
    release.set()
    await asyncio.gather(holder, *tasks)
    # Same key + priority -> strict FIFO by arrival.
    assert done == ["r0", "r1", "r2"]
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 2. fairness: one key floods, another key is still served within bound
# --------------------------------------------------------------------------- #
async def test_fairness_round_robin_across_keys(settings: Settings):
    mgr = _manager(_qsettings(settings))
    release, entered, holder = await _warm_and_hold(mgr)
    order: list[str] = []

    async def req(rid: str, key: str):
        await mgr.chat("echo", _msg(), rid, queue_key=key)
        order.append(rid)

    # Key A floods first; key B arrives in the middle of A's burst.
    labels = [("A1", "A"), ("A2", "A"), ("A3", "A"), ("B1", "B"), ("A4", "A"), ("A5", "A")]
    tasks = [asyncio.create_task(req(rid, key)) for rid, key in labels]
    await _wait_until(lambda: mgr.admission.depth() == 6)
    release.set()
    await asyncio.gather(holder, *tasks)
    # Round-robin across keys: after A's first request, B is served before the
    # rest of A's backlog — B is NOT starved behind all five A requests.
    assert order[0] == "A1"
    assert order[1] == "B1"
    assert order.index("B1") < order.index("A2")
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 3. priority orders the queue (aging off)
# --------------------------------------------------------------------------- #
async def test_priority_orders_queue(settings: Settings):
    mgr = _manager(_qsettings(settings, queue_priority_aging_seconds=0.0))
    release, entered, holder = await _warm_and_hold(mgr)
    order: list[str] = []

    async def req(rid: str, prio: int):
        await mgr.chat("echo", _msg(), rid, queue_key=rid, queue_priority=prio)
        order.append(rid)

    low = asyncio.create_task(req("low", 0))
    await _wait_until(lambda: mgr.admission.depth() == 1)
    high = asyncio.create_task(req("high", 10))
    await _wait_until(lambda: mgr.admission.depth() == 2)
    release.set()
    await asyncio.gather(holder, low, high)
    # Higher priority jumps the queue despite arriving second.
    assert order == ["high", "low"]
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 3b. aging lifts a low-priority waiter over a fresh high-priority one (unit)
# --------------------------------------------------------------------------- #
def test_priority_aging_prevents_starvation():
    clock = {"t": 1000.0}
    q = AdmissionQueue(Settings(registry_path="registry.yaml"), clock=lambda: clock["t"])
    q.settings.queue_priority_aging_seconds = 10.0
    low = q.enqueue("m", "k1", priority=0, trigger="x")
    # 100s later a low-priority waiter has aged by +10 effective priority.
    clock["t"] = 1100.0
    high = q.enqueue("m", "k2", priority=5, trigger="x")
    ordered = q.ordered()
    assert ordered[0] is low  # aged 0 -> 10 beats a fresh priority 5
    assert ordered[1] is high


# --------------------------------------------------------------------------- #
# 4. full queue -> 429 + Retry-After (never enqueued)
# --------------------------------------------------------------------------- #
async def test_queue_full_rejects_429_with_retry_after(settings: Settings):
    mgr = _manager(_qsettings(settings, queue_max_depth=1, queue_retry_after_seconds=7))
    release, entered, holder = await _warm_and_hold(mgr)

    # One request fills the single queue slot...
    first = asyncio.create_task(mgr.chat("echo", _msg(), "q1", queue_key="k"))
    await _wait_until(lambda: mgr.admission.depth() == 1)
    # ...the next is rejected immediately with 429 + Retry-After.
    with pytest.raises(QueueFullError) as ei:
        await mgr.chat("echo", _msg(), "q2", queue_key="k")
    assert ei.value.http_status == 429
    assert ei.value.error_type == "queue_full"
    assert ei.value.headers.get("Retry-After") == "7"
    assert ei.value.trigger is not None
    assert mgr.admission.rejections_by_reason  # counted
    release.set()
    await asyncio.gather(holder, first)
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 5. wait timeout -> 503 + Retry-After naming the trigger
# --------------------------------------------------------------------------- #
async def test_queue_wait_timeout_503(settings: Settings):
    mgr = _manager(_qsettings(settings, queue_max_wait_seconds=0.2, queue_retry_after_seconds=3))
    release, entered, holder = await _warm_and_hold(mgr)
    with pytest.raises(QueueTimeoutError) as ei:
        await mgr.chat("echo", _msg(), "slow", queue_key="k")
    assert ei.value.http_status == 503
    assert ei.value.error_type == "queue_timeout"
    assert ei.value.headers.get("Retry-After") == "3"
    assert ei.value.trigger is not None
    assert mgr.admission.timeouts_by_reason
    release.set()
    await holder
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 6. client disconnect (task cancel) frees the slot immediately
# --------------------------------------------------------------------------- #
async def test_client_disconnect_frees_queue_slot(settings: Settings):
    mgr = _manager(_qsettings(settings))
    release, entered, holder = await _warm_and_hold(mgr)
    victim = asyncio.create_task(mgr.chat("echo", _msg(), "victim", queue_key="k"))
    await _wait_until(lambda: mgr.admission.depth() == 1)
    victim.cancel()
    with pytest.raises(asyncio.CancelledError):
        await victim
    # The waiter is removed immediately — no leaked queue slot.
    await _wait_until(lambda: mgr.admission.depth() == 0)
    # And the serving slot is intact: a fresh request still serves once released.
    release.set()
    await holder
    await mgr.chat("echo", _msg(), "after")
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 7. quota 429s are never queueable; exceeds_device_capacity fails fast
# --------------------------------------------------------------------------- #
def test_quota_and_hard_capacity_never_queued(settings: Settings):
    mgr = _manager(_qsettings(settings))
    # Per-key quota 429s (ADR 0008/0016) are a different concern — not queueable.
    assert mgr._queueable_capacity_trigger(RateLimitExceededError("rpm")) is None
    assert mgr._queueable_capacity_trigger(InsufficientQuotaError("budget")) is None
    # A memory capacity error that can never fit fails fast (operator rule).
    hard = CapacityUnavailableError("nope", reason="exceeds_device_capacity")
    assert mgr._queueable_capacity_trigger(hard) is None
    # A transient capacity error (a holder may go idle) IS queueable.
    soft = CapacityUnavailableError("wait", reason="declared_insufficient")
    assert mgr._queueable_capacity_trigger(soft) == TRIGGER_CAPACITY_MEMORY
    assert (
        mgr._queueable_capacity_trigger(ConcurrencyLimitError("max loaded")) == TRIGGER_MAX_LOADED
    )


# --------------------------------------------------------------------------- #
# 8. fast path (no overload) bypasses the queue — no wait recorded
# --------------------------------------------------------------------------- #
async def test_fast_path_no_queue_wait(settings: Settings):
    mgr = _manager(_qsettings(settings))
    result = await mgr.chat("echo", _msg(), "r1", queue_key="k")
    assert "_broker_queue_wait_ms" not in result
    assert mgr.admission.snapshot()["enqueued_total"] == 0
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 9. measured device-memory overload queues, then drains when it clears
# --------------------------------------------------------------------------- #
async def test_device_overload_queues_then_drains(settings: Settings):
    mgr = _manager(
        _qsettings(
            settings,
            gpu_telemetry="host",  # non-"off" so telemetry_enabled() is True
            gpu_telemetry_poll_seconds=0.0,  # re-probe every check
            queue_trigger_device_memory=True,
            queue_device_free_mib_min=2000,  # queue while free < 2000 MiB
        )
    )
    # Start NOT overloaded so the warmup load isn't itself queued.
    tele = FakeTelemetry(
        [DeviceMemory(index="default", name="GPU 0", total_mib=8000, free_mib=6000)]
    )
    mgr.telemetry = tele
    await mgr.start_queue_pump()  # periodic re-check drains a cleared overload
    await mgr.chat("echo", _msg(), "warm")  # load the model

    # Now apply device-memory pressure: a new request queues though the slot is free.
    tele.devices = [DeviceMemory(index="default", name="GPU 0", total_mib=8000, free_mib=500)]
    task = asyncio.create_task(mgr.chat("echo", _msg(), "r1", queue_key="k"))
    await _wait_until(lambda: mgr.admission.depth() == 1)
    assert mgr.admission.snapshot()["waiting_triggers"].get(TRIGGER_DEVICE_MEMORY) == 1
    # Clear the pressure -> the background pump drains it.
    tele.devices = [DeviceMemory(index="default", name="GPU 0", total_mib=8000, free_mib=6000)]
    await task
    assert mgr.admission.depth() == 0
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 10. host-load overload queues, then drains when load drops
# --------------------------------------------------------------------------- #
async def test_host_overload_queues_then_drains(settings: Settings, monkeypatch):
    import broker.manager as mgr_mod

    high = {"v": False}  # start calm so the warmup load isn't queued
    monkeypatch.setattr(mgr_mod.os, "cpu_count", lambda: 1)
    monkeypatch.setattr(mgr_mod.os, "getloadavg", lambda: (9.0 if high["v"] else 0.1, 0.0, 0.0))
    mgr = _manager(
        _qsettings(
            settings,
            queue_trigger_host=True,
            queue_host_load_per_cpu_max=2.0,
        )
    )
    await mgr.chat("echo", _msg(), "warm")  # load while calm
    await mgr.start_queue_pump()  # periodic re-check drains a cleared overload
    high["v"] = True  # host now overloaded
    mgr._host_load_at = 0.0  # invalidate the cheap load cache
    task = asyncio.create_task(mgr.chat("echo", _msg(), "r1", queue_key="k"))
    await _wait_until(lambda: mgr.admission.depth() == 1)
    high["v"] = False  # load drops below the threshold
    mgr._host_load_at = 0.0  # invalidate the cache so the pump sees it clear
    await task
    assert mgr.admission.depth() == 0
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 11. queue composes with failover and never deadlocks (pool, one bad member)
# --------------------------------------------------------------------------- #
POOL_REGISTRY = """
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


async def test_queue_composes_with_failover(tmp_path: Path, tmp_models_root: Path):
    reg_file = tmp_path / "registry.yaml"
    reg_file.write_text(textwrap.dedent(POOL_REGISTRY).strip())
    s = Settings(
        auth_enabled=False,
        registry_path=str(reg_file),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_models_root)],
        health_probe_enabled=False,
        queue_enabled=True,
        per_model_max_concurrency=2,
        max_concurrent_requests=8,
        max_loaded_models=5,
        retry_max_attempts=2,
    )
    reg = load_registry(s.registry_path, {"fake"})
    mgr = ModelManager(reg, s)
    mgr.validate_all()
    # Make node-a always fail on chat; the request must fail over to node-b and
    # complete — with the queue on, slots are released on failover (no deadlock).
    await mgr.chat("chat", _msg(), "warm")  # load a member
    mgr.models["node-a"].adapter.fail_on_chat = True
    try:
        results = await asyncio.gather(
            *(mgr.chat("chat", _msg(), f"r{i}", queue_key="k") for i in range(4))
        )
        assert all(r.get("model") == "chat" for r in results)
        assert mgr.admission.depth() == 0  # everything drained, nothing stuck
    finally:
        mgr.models["node-a"].adapter.fail_on_chat = False
        await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 12. the queue never switches model (no silent cross-model fallback)
# --------------------------------------------------------------------------- #
async def test_queue_never_switches_model(settings: Settings):
    mgr = _manager(_qsettings(settings))
    release, entered, holder = await _warm_and_hold(mgr, "echo")
    # echo is saturated; echo2 is idle. A request for echo must wait for echo —
    # it must never be silently served by echo2.
    task = asyncio.create_task(mgr.chat("echo", _msg(), "r1", queue_key="k"))
    await _wait_until(lambda: mgr.admission.depth() == 1)
    # echo2 stays untouched while echo's request is queued.
    from broker.lifecycle import State

    assert mgr.models["echo2"].machine.state in (State.UNLOADED, State.FAILED)
    release.set()
    result = await task
    await holder
    assert result.get("model") == "echo"
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 13. default (queue off) == v0.6.0: no queue machinery engages
# --------------------------------------------------------------------------- #
async def test_queue_off_is_v060(settings: Settings):
    settings.queue_enabled = False
    settings.per_model_max_concurrency = 2
    settings.health_probe_enabled = False
    mgr = _manager(settings)
    r = await mgr.chat("echo", _msg(), "r1")
    assert "_broker_queue_wait_ms" not in r
    snap = mgr.admission.snapshot()
    assert snap["enabled"] is False
    assert snap["enqueued_total"] == 0
    await mgr.shutdown()


# --------------------------------------------------------------------------- #
# 14. the examples/request-queueing/ example actually queues then drains
# --------------------------------------------------------------------------- #
async def test_example_request_queueing(tmp_models_root: Path):
    repo = Path(__file__).resolve().parents[1]
    example = repo / "examples" / "request-queueing"
    s = Settings(
        auth_enabled=False,
        registry_path=str(example / "registry.yaml"),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_models_root)],
        health_probe_enabled=False,
        queue_enabled=True,
        per_model_max_concurrency=1,
        max_concurrent_requests=8,
        max_loaded_models=5,
        queue_max_wait_seconds=10.0,
        queue_pump_interval_seconds=0.05,
    )
    reg = load_registry(s.registry_path, {"fake"})
    mgr = ModelManager(reg, s)
    mgr.validate_all()
    # Saturate member chat-a; a second request for the pool queues, then drains.
    await mgr.chat("chat", _msg(), "warm")
    served = [m for m in mgr.models.values() if m.machine.state.name in ("READY", "BUSY")]
    alias = served[0].entry.alias
    release, entered = _gate(mgr, alias)
    holder = asyncio.create_task(mgr.chat("chat", _msg(), "holder", queue_key="h"))
    await _wait_until(lambda: "holder" in entered)
    # Pin the pool to a single member so the next request must queue on it.
    other = [a for a in mgr.models if a != alias]
    for a in other:
        mgr.models[a].draining = True
    task = asyncio.create_task(mgr.chat("chat", _msg(), "r1", queue_key="k"))
    await _wait_until(lambda: mgr.admission.depth() == 1)
    release.set()
    await asyncio.gather(holder, task)
    assert mgr.admission.depth() == 0
    await mgr.shutdown()
