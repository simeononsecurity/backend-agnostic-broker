"""ADR 0020 measured release + stop()-reports-survival (fake backend + fake telemetry).

No GPU: a scriptable :class:`FakeTelemetry` provider keeps a model's PID "holding"
device memory after the process exits, so the manager's eviction must *wait* for a
measured release before the next load — and give up into a visible ``release_stuck``
503 when the memory never comes back.
"""

from __future__ import annotations

import pytest

from broker.adapters.fake import FakeAdapter
from broker.config import Settings
from broker.errors import ReleaseStuckError
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.resources import DeviceMemory, FakeTelemetry


class HoldingTelemetry(FakeTelemetry):
    """Holds ``pid`` on one device for the first ``hold_samples`` reads, then frees."""

    def __init__(self, pid: int, hold_samples: int, *, forever: bool = False) -> None:
        super().__init__()
        self.pid = pid
        self.hold_samples = hold_samples
        self.forever = forever

    def sample(self):  # type: ignore[override]
        self.samples_taken += 1
        holding = self.forever or self.samples_taken <= self.hold_samples
        procs = {self.pid: 512} if holding else {}
        free = 2000 if holding else 8000
        self.devices = [
            DeviceMemory(
                index="default", name="GPU 0", total_mib=8000, free_mib=free, processes=procs
            )
        ]
        return super().sample()


@pytest.fixture
def settings_1slot(settings: Settings) -> Settings:
    # One load slot so loading a second model must evict the first.
    settings.max_loaded_models = 1
    settings.release_timeout_seconds = 5.0
    settings.gpu_telemetry_poll_seconds = 0.02
    return settings


def _manager(settings: Settings) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    m = ModelManager(reg, settings)
    m.validate_all()
    return m


async def test_eviction_waits_for_measured_release_then_loads(settings_1slot):
    mgr = _manager(settings_1slot)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    pid = mgr.models["echo"].machine.pid
    # Telemetry holds echo's pid for 3 polls after it exits, then frees it.
    mgr.telemetry = HoldingTelemetry(pid, hold_samples=3)
    assert mgr.telemetry_enabled()

    await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    # echo was evicted and released; echo2 is now resident.
    assert mgr.models["echo"].machine.state == State.UNLOADED
    assert mgr.models["echo2"].machine.state in (State.READY, State.BUSY)
    # It actually waited (more than one telemetry poll happened during release).
    assert mgr.telemetry.samples_taken >= 3
    await mgr.shutdown()


async def test_release_never_recovers_marks_stuck_and_503(settings_1slot):
    settings_1slot.release_timeout_seconds = 0.3
    mgr = _manager(settings_1slot)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    pid = mgr.models["echo"].machine.pid
    mgr.telemetry = HoldingTelemetry(pid, hold_samples=0, forever=True)

    with pytest.raises(ReleaseStuckError) as ei:
        await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    assert ei.value.http_status == 503
    stuck = mgr.models["echo"]
    assert stuck.machine.state == State.RELEASE_STUCK
    assert stuck.machine.release_info["pid"] == pid
    # The stuck holder is visible in the resources block.
    block = await mgr.resources_block()
    aliases = [s["alias"] for s in block["release_stuck"]]
    assert "echo" in aliases
    await mgr.shutdown()


async def test_second_load_on_stuck_device_fails_fast(settings_1slot):
    settings_1slot.release_timeout_seconds = 0.2
    mgr = _manager(settings_1slot)
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    pid = mgr.models["echo"].machine.pid
    mgr.telemetry = HoldingTelemetry(pid, hold_samples=0, forever=True)
    with pytest.raises(ReleaseStuckError):
        await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    # echo is now release_stuck; a retry is refused fast by the guard (not an OOM).
    with pytest.raises(ReleaseStuckError):
        await mgr.chat("echo2", {"messages": [{"role": "user", "content": "z"}]}, "r3")
    await mgr.shutdown()


async def test_telemetry_off_keeps_v060_timing_but_reports_survival(settings_1slot):
    # Telemetry OFF (default): eviction must NOT wait on telemetry, but a process
    # that survives SIGKILL is surfaced (survived_kill) instead of silently ignored.
    mgr = _manager(settings_1slot)
    assert not mgr.telemetry_enabled()
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    mgr.models["echo"].adapter.simulate_survive_kill = True  # type: ignore[attr-defined]
    await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "r2")
    assert mgr.models["echo"].machine.survived_kill is True
    block = await mgr.resources_block()
    assert any(s["alias"] == "echo" for s in block["release_stuck"])
    await mgr.shutdown()


def teardown_function(_fn):
    FakeAdapter.fail_on_start = False
    FakeAdapter.load_delay = 0.0
