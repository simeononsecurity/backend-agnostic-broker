"""The `examples/cache-aware-routing/` example is real and runnable (ADR 0018).

Validates that the shipped example registry parses into the documented 3-member pool,
that the `.env.example` encodes the cache-aware settings it claims, and that the same
settings drive the real cache-aware selection end-to-end against the fake backend
(repeat prefix → warm member; `/status` exposes the `cache_aware` stats the README
tells operators to read).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from broker.config import Settings
from broker.manager import ModelManager
from broker.registry import load_registry

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "cache-aware-routing"

_FAKE_POOL = """
models:
  - {alias: chat-node-1, backend: fake, runtime_model_id: shared-model, pool: chat}
  - {alias: chat-node-2, backend: fake, runtime_model_id: shared-model, pool: chat}
  - {alias: chat-node-3, backend: fake, runtime_model_id: shared-model, pool: chat}
"""


def test_example_registry_parses_into_three_member_pool():
    reg = load_registry(str(_EXAMPLE / "registry.example.yaml"), {"vllm"})
    pools = {m.pool for m in reg.models if m.pool}
    assert "chat" in pools
    members = [m for m in reg.models if m.pool == "chat"]
    assert len(members) == 3
    assert all(m.backend == "vllm" for m in members)


def test_example_env_documents_cache_aware_defaults():
    env = (_EXAMPLE / ".env.example").read_text()
    assert "BROKER_ROUTE_STRATEGY=cache_aware" in env
    assert "BROKER_CACHE_AWARE_BETA=0.5" in env
    # The documented β matches the shipped config default.
    assert Settings().cache_aware_beta == 0.5


async def test_example_settings_drive_cache_aware_end_to_end(tmp_path):
    reg_path = tmp_path / "registry.yaml"
    reg_path.write_text(textwrap.dedent(_FAKE_POOL).strip())
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg_path),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        route_strategy="cache_aware",  # as in the example .env
        cache_aware_beta=Settings().cache_aware_beta,  # the shipped 0.5 default
    )
    reg = load_registry(settings.registry_path, set(settings.enabled_backends))
    mgr = ModelManager(reg, settings)
    mgr.validate_all()
    try:
        shared = [
            {"role": "system", "content": "You are a meticulous assistant. " * 10},
            {"role": "user", "content": "Summarize the CAP theorem in detail please."},
        ]
        first = await mgr.chat("chat", {"messages": shared}, "r0")
        served = first["choices"][0]["message"]["content"].split(": ", 1)[1]
        # A second request on the SAME prefix routes back to the warm member.
        again = await mgr.chat("chat", {"messages": shared}, "r1")
        assert again["choices"][0]["message"]["content"].split(": ", 1)[1] == served
        stats = (await mgr.status())["cache_aware"]
        assert stats["enabled"] is True
        assert stats["hits"] >= 1 and stats["index_size"] >= 1
    finally:
        await mgr.shutdown()
