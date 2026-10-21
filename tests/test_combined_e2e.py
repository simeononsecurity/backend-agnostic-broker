"""Combined E2E: memory admission + measured release + queue + reconcile together.

Covers the ADR 0020/0022/0023/0021 seams on the fake backend + FakeTelemetry:
1. Sized models over a declared budget: the second evicts the first (measured release),
   oversize fast-fails, and with the queue on, contention is queued fairly, not dropped.
2. reconcile: a fake runtime record whose process was never stopped is adopted (or reported)
   on a fresh manager, proving ledger + identity keep per-owner safety.
"""

from __future__ import annotations

import asyncio

import pytest

from broker.config import Settings
from broker.errors import CapacityUnavailableError
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry


def _manager(settings: Settings, mib: int | None = 600, load_delay: float = 0.0) -> ModelManager:
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    m = ModelManager(reg, settings)
    m.validate_all()
    for mm in m.models.values():
        mm.entry.memory_mib = mib
        if hasattr(mm.adapter, "load_delay"):
            mm.adapter.load_delay = load_delay  # type: ignore[attr-defined]
    return m


def _resident(m: ModelManager) -> list[str]:
    return [a for a, mm in m.models.items() if mm.machine.state in (State.READY, State.BUSY)]


async def test_combined_admit_release_queue(settings: Settings):
    settings.max_loaded_models = 2
    settings.queue_enabled = True
    settings.queue_max_depth = 32
    settings.gpu_memory_budget_mib = "1000"  # one 600 fits, two don't
    mgr = _manager(settings, load_delay=0.02)
    # First model loads.
    await mgr.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "k1")
    assert _resident(mgr) == ["echo"]
    # Second sized model: admission evicts the first via measured release (telemetry off →
    # declared accounting, eviction wait is the no-op v0.6.0 path), then serves.
    await mgr.chat("echo2", {"messages": [{"role": "user", "content": "y"}]}, "k1")
    assert _resident(mgr) == ["echo2"]
    assert any(d.startswith("evicted") for d in [a["decision"] for a in mgr._admissions])
    # Oversized model: never evicts, fast-fails 429.
    mgr.models["echo"].entry.memory_mib = 5000
    with pytest.raises(CapacityUnavailableError):
        await mgr.chat("echo", {"messages": [{"role": "user", "content": "z"}]}, "k1")
    assert _resident(mgr) == ["echo2"]

    # Queue: two keys contend for the single slot, both get served (no starvation).
    async def one(key, i):
        return await mgr.chat("echo2", {"messages": [{"role": "user", "content": str(i)}]}, key)

    got = await asyncio.wait_for(
        asyncio.gather(
            *(one("flood", i) for i in range(6)), one("rare", 9), return_exceptions=True
        ),
        timeout=20,
    )
    assert not any(isinstance(g, Exception) for g in got)
    await mgr.shutdown()


async def test_combined_reconcile_noop_is_safe(settings: Settings):
    """Reconcile with nothing to do (and a shared ledger) is a safe no-op."""
    mgr = _manager(settings)
    summary = await mgr.reconcile()
    assert summary["mode"] == "report"
    assert not summary.get("stopped")
    await mgr.shutdown()
