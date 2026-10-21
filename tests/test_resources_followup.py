"""ADR 0020/0022 follow-up regression tests (group/memory-aware release,
release_stuck recovery, atomic admission, no needless eviction).

Derived from the orchestrator's repro probes. At the pre-fix head aaed90d: P1
passed; P2, P3[child], P3[nopids], P4, P5 failed. They all pass now, plus a test
that a normal measured release still proceeds promptly.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from broker.adapters.fake import FakeAdapter
from broker.config import Settings
from broker.errors import CapacityUnavailableError, ReleaseStuckError
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.resources import DeviceMemory, FakeTelemetry


def _mgr(settings: Settings, mib: int | None = 600, load_delay: float = 0.3) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    m = ModelManager(reg, settings)
    m.validate_all()
    for mm in m.models.values():
        mm.entry.memory_mib = mib
        if hasattr(mm.adapter, "load_delay"):
            mm.adapter.load_delay = load_delay  # type: ignore[attr-defined]
    return m


def _resident_mib(mgr: ModelManager) -> int:
    return sum(
        mm.entry.memory_mib or 0
        for mm in mgr.models.values()
        if mm.machine.state in (State.READY, State.BUSY, State.LOADING)
    )


async def _both(mgr):
    return await asyncio.gather(
        mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1"),
        mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2"),
        return_exceptions=True,
    )


async def test_P1_declared_concurrent_admission_does_not_overcommit(settings: Settings):
    settings.max_loaded_models = 5
    settings.gpu_memory_budget_mib = "1000"
    mgr = _mgr(settings)
    await _both(mgr)
    assert _resident_mib(mgr) <= 1000
    await mgr.shutdown()


async def test_P2_measured_concurrent_admission_does_not_overcommit(settings: Settings):
    settings.max_loaded_models = 5
    settings.gpu_memory_headroom_mib = 0
    mgr = _mgr(settings)
    mgr.telemetry = FakeTelemetry(
        devices=[
            DeviceMemory(index="default", name="GPU0", total_mib=1000, free_mib=1000, processes={})
        ]
    )
    await _both(mgr)
    # FAILS at aaed90d: both admitted -> 1200 MiB resident on a 1000 MiB device.
    assert _resident_mib(mgr) <= 1000
    await mgr.shutdown()


class _ChildHolds(FakeTelemetry):
    """A runtime CHILD process (pid+1, e.g. vLLM EngineCore/TP worker) keeps holding memory."""

    def __init__(self, child_pid: int) -> None:
        super().__init__()
        self.child = child_pid

    def sample(self):  # type: ignore[override]
        self.devices = [
            DeviceMemory(
                index="default",
                name="GPU0",
                total_mib=8000,
                free_mib=500,
                processes={self.child: 7000},
            )
        ]
        return super().sample()


class _NoPidsLowFree(FakeTelemetry):
    """Container view: nvidia-smi lists no PIDs (PID namespace), but memory is not free."""

    def sample(self):  # type: ignore[override]
        self.devices = [
            DeviceMemory(index="default", name="GPU0", total_mib=8000, free_mib=500, processes={})
        ]
        return super().sample()


@pytest.mark.parametrize("kind", ["child", "nopids"])
async def test_P3_release_wait_sees_memory_still_held(settings: Settings, kind: str):
    settings.max_loaded_models = 1
    settings.release_timeout_seconds = 0.5
    settings.gpu_telemetry_poll_seconds = 0.02
    mgr = _mgr(settings, mib=None, load_delay=0)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    pid = mgr.models["echo"].machine.pid or 4242
    mgr.telemetry = _ChildHolds(pid + 1) if kind == "child" else _NoPidsLowFree()
    # FAILS at aaed90d: echo2 loads immediately although the memory was never released.
    with pytest.raises(ReleaseStuckError):
        await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    await mgr.shutdown()


async def test_P4_release_stuck_recovers_after_memory_frees(settings: Settings):
    settings.max_loaded_models = 1
    settings.release_timeout_seconds = 0.3
    settings.gpu_telemetry_poll_seconds = 0.02
    mgr = _mgr(settings, mib=None, load_delay=0)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    pid = mgr.models["echo"].machine.pid
    hold = FakeTelemetry(
        devices=[
            DeviceMemory(
                index="default", name="GPU0", total_mib=8000, free_mib=500, processes={pid: 7000}
            )
        ]
    )
    mgr.telemetry = hold
    with pytest.raises(ReleaseStuckError):
        await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    hold.devices = [
        DeviceMemory(index="default", name="GPU0", total_mib=8000, free_mib=8000, processes={})
    ]
    # FAILS at aaed90d: still 503 forever ("device still held by release-stuck model 'echo'").
    await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r3")
    assert mgr.models["echo"].machine.state == State.UNLOADED
    await mgr.shutdown()


async def test_P5_impossible_request_fails_fast_without_evicting(settings: Settings):
    settings.max_loaded_models = 5
    settings.gpu_memory_budget_mib = "1000"
    mgr = _mgr(settings, mib=600, load_delay=0)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    mgr.models["echo2"].entry.memory_mib = 5000  # can never fit a 1000 MiB device
    with pytest.raises(CapacityUnavailableError) as ei:
        await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    # FAILS at aaed90d: echo was evicted for nothing.
    assert mgr.models["echo"].machine.state in (State.READY, State.BUSY)
    assert ei.value.reason == "exceeds_device_capacity"
    await mgr.shutdown()


class _RecoversTelemetry(FakeTelemetry):
    """While the victim is loaded the device is busy (its pid holds memory, low
    free); on the first read after it is stopped the memory is back (high free,
    no holders) — the realistic prompt-release case."""

    def __init__(self, pid: int) -> None:
        super().__init__()
        self.pid = pid
        self.freed = False

    def sample(self):  # type: ignore[override]
        self.samples_taken += 1
        if self.samples_taken <= 1:  # the pre-stop snapshot read: still loaded
            self.devices = [
                DeviceMemory(
                    "default", "GPU0", total_mib=8000, free_mib=1000, processes={self.pid: 7000}
                )
            ]
        else:  # after unload: memory returned
            self.devices = [DeviceMemory("default", "GPU0", total_mib=8000, free_mib=8000)]
        return FakeTelemetry.sample(self)


async def test_normal_release_proceeds_promptly(settings: Settings):
    """A measured release where the memory actually comes back must NOT block:
    eviction completes quickly and the next model loads, well under the timeout."""
    settings.max_loaded_models = 1
    settings.release_timeout_seconds = 5.0
    settings.gpu_telemetry_poll_seconds = 0.02
    mgr = _mgr(settings, mib=None, load_delay=0)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    pid = mgr.models["echo"].machine.pid or 4242
    mgr.telemetry = _RecoversTelemetry(pid)
    t0 = time.monotonic()
    await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    assert time.monotonic() - t0 < 2.0  # promptly, nowhere near the 5s timeout
    assert mgr.models["echo"].machine.state == State.UNLOADED
    assert mgr.models["echo2"].machine.state in (State.READY, State.BUSY)
    await mgr.shutdown()


def teardown_function(_fn):
    FakeAdapter.fail_on_start = False
    FakeAdapter.load_delay = 0.0
    FakeAdapter.chat_delay = 0.0
