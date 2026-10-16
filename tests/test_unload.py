"""Idle-unload and failure-recovery tests."""

from __future__ import annotations

import asyncio

import pytest

from broker.adapters.fake import FakeAdapter
from broker.errors import ModelLoadError
from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry


@pytest.fixture
def manager(settings):
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    m = ModelManager(reg, settings)
    m.validate_all()
    return m


async def test_idle_unload_via_sweep(manager):
    await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert manager.models["echo"].machine.state in (State.READY, State.BUSY)
    await manager.start_sweeper()
    # idle_timeout_seconds=1, sweep interval 0.2 => should unload within ~2s.
    for _ in range(30):
        if manager.models["echo"].machine.state == State.UNLOADED:
            break
        await asyncio.sleep(0.1)
    assert manager.models["echo"].machine.state == State.UNLOADED
    await manager.shutdown()


async def test_explicit_unload(manager):
    await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    await manager.models["echo"].unload()
    assert manager.models["echo"].machine.state == State.UNLOADED
    await manager.shutdown()


async def test_failure_recovery(manager):
    # Make start fail, confirm FAILED + cleanup, then recover.
    FakeAdapter.fail_on_start = True
    try:
        with pytest.raises(ModelLoadError):
            await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
        assert manager.models["echo"].machine.state == State.FAILED
    finally:
        FakeAdapter.fail_on_start = False
    # Now it should recover on the next request.
    await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r2")
    assert manager.models["echo"].machine.state in (State.READY, State.BUSY)
    await manager.shutdown()


async def test_reload_after_idle_unload(manager):
    await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    await manager.models["echo"].unload()
    assert manager.models["echo"].machine.state == State.UNLOADED
    # A fresh request reloads it.
    await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r2")
    assert manager.models["echo"].machine.state in (State.READY, State.BUSY)
    assert manager.models["echo"].adapter._load_count == 2
    await manager.shutdown()
