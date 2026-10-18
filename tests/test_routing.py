"""Routing / load-balancing across pool members, with failover."""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from broker.adapters.fake import FakeAdapter
from broker.config import Settings
from broker.errors import BackendUnavailableError, ConfigError, ModelNotFoundError
from broker.manager import ModelManager
from broker.registry import load_registry

POOL_REGISTRY = """
models:
  - alias: node-a
    backend: fake
    runtime_model_id: shared-model
    pool: chat
  - alias: node-b
    backend: fake
    runtime_model_id: shared-model
    pool: chat
"""


def _make_settings(tmp_path: Path, body: str, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(body).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        model_idle_timeout_seconds=100,
        idle_sweep_interval_seconds=0.2,
        health_probe_enabled=False,
        per_model_max_concurrency=1,
        max_concurrent_requests=32,
        max_loaded_models=5,
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _manager(tmp_path: Path, body: str = POOL_REGISTRY, **over) -> ModelManager:
    s = _make_settings(tmp_path, body, **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    return m


def _msg() -> dict:
    return {"messages": [{"role": "user", "content": "x"}]}


async def test_pool_exposed_as_single_public_model(tmp_path):
    mgr = _manager(tmp_path)
    assert set(mgr.pools) == {"chat"}
    pub = [p for p in mgr.public_models() if p["id"] != "auto"]
    assert len(pub) == 1
    assert pub[0]["id"] == "chat"
    assert pub[0]["pool_size"] == 2
    assert set(pub[0]["pool_members"]) == {"node-a", "node-b"}


async def test_concurrent_requests_spread_across_members(tmp_path):
    mgr = _manager(tmp_path)
    FakeAdapter.load_delay = 0.05  # keep members busy so inflight spreads
    try:

        async def call(i):
            return await mgr.chat("chat", _msg(), f"r{i}")

        results = await asyncio.gather(*(call(i) for i in range(8)))
    finally:
        FakeAdapter.load_delay = 0.0
    # Both endpoints were actually loaded/used — no single-endpoint pileup.
    assert mgr.models["node-a"].adapter._load_count >= 1
    assert mgr.models["node-b"].adapter._load_count >= 1
    # Every response is relabeled with the public pool name.
    assert all(r["model"] == "chat" for r in results)
    # And responses came from both members.
    served = {r["choices"][0]["message"]["content"] for r in results}
    assert served == {"echo: node-a", "echo: node-b"}
    await mgr.shutdown()


async def test_failover_to_healthy_member(tmp_path):
    mgr = _manager(tmp_path)
    # node-a's backend is broken; requests must still succeed via node-b.
    mgr.models["node-a"].adapter.fail_on_chat = True
    contents = []
    for i in range(4):
        r = await mgr.chat("chat", _msg(), f"r{i}")
        contents.append(r["choices"][0]["message"]["content"])
    assert all(c == "echo: node-b" for c in contents)
    assert mgr.models["node-a"].healthy is False
    assert mgr.models["node-b"].healthy is True
    await mgr.shutdown()


async def test_all_members_down_raises(tmp_path):
    mgr = _manager(tmp_path)
    mgr.models["node-a"].adapter.fail_on_chat = True
    mgr.models["node-b"].adapter.fail_on_chat = True
    with pytest.raises(BackendUnavailableError):
        await mgr.chat("chat", _msg(), "r1")
    await mgr.shutdown()


async def test_unknown_model_not_found(tmp_path):
    mgr = _manager(tmp_path)
    with pytest.raises(ModelNotFoundError):
        await mgr.chat("nope", _msg(), "r1")
    await mgr.shutdown()


async def test_round_robin_strategy(tmp_path):
    mgr = _manager(tmp_path, route_strategy="round_robin")
    seen = set()
    for i in range(6):
        r = await mgr.chat("chat", _msg(), f"r{i}")
        seen.add(r["choices"][0]["message"]["content"])
    # round-robin rotates members regardless of load.
    assert seen == {"echo: node-a", "echo: node-b"}
    await mgr.shutdown()


async def test_pool_name_collides_with_standalone_alias(tmp_path):
    bad = """
    models:
      - alias: chat
        backend: fake
        runtime_model_id: m
      - alias: other
        backend: fake
        runtime_model_id: m
        pool: chat
    """
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(bad).strip())
    with pytest.raises(ConfigError):
        load_registry(str(reg), {"fake"})


async def test_streaming_failover_before_first_byte(tmp_path):
    mgr = _manager(tmp_path)
    # node-a cannot load; the stream must transparently route to node-b
    # (failover happens before the first byte is yielded).
    mgr.models["node-a"].adapter.fail_on_start = True
    chunks = []
    async for c in mgr.stream_chat("chat", _msg(), "r1"):
        chunks.append(c)
    body = b"".join(chunks).decode()
    assert "node-b" in body
    assert "[DONE]" in body
    assert mgr.models["node-a"].healthy is False
    await mgr.shutdown()
