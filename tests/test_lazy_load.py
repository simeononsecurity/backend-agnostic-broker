"""Lazy-loading tests: no load at startup; only the selected model loads."""

from __future__ import annotations

import pytest

from broker.lifecycle import State
from broker.manager import ModelManager
from broker.registry import load_registry


@pytest.fixture
def manager(settings):
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    m = ModelManager(reg, settings)
    m.validate_all()
    return m


async def test_no_model_loads_at_startup(manager):
    assert manager.loaded_count() == 0
    for mm in manager.models.values():
        assert mm.machine.state == State.UNLOADED
        # Fake adapter counts loads; must be zero before any request.
        assert mm.adapter._load_count == 0


async def test_request_loads_only_selected_model(manager):
    await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r1")
    assert manager.models["echo"].machine.state in (State.READY, State.BUSY)
    # The other model must remain unloaded — no silent fallback / preloading.
    assert manager.models["echo2"].machine.state == State.UNLOADED
    assert manager.models["echo2"].adapter._load_count == 0
    await manager.shutdown()


async def test_concurrent_first_requests_load_once(manager):
    import asyncio

    async def call():
        return await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "r")

    await asyncio.gather(*(call() for _ in range(5)))
    # Despite 5 concurrent first-requests, the model started exactly once.
    assert manager.models["echo"].adapter._load_count == 1
    await manager.shutdown()


async def test_max_loaded_models_eviction(manager):
    # max_loaded_models=2 in settings fixture, but only 2 models exist; load both.
    await manager.chat("echo", {"messages": [{"role": "user", "content": "x"}]}, "a")
    await manager.chat("echo2", {"messages": [{"role": "user", "content": "x"}]}, "b")
    assert manager.loaded_count() == 2
    await manager.shutdown()
