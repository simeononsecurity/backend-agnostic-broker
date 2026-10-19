"""Cache-aware pool routing (ADR 0010): prefix hashing, longest-prefix match,
load-aware scoring, index eviction/TTL, health/cooldown interplay, failover,
StateStore backing, and a bench smoke run.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from broker.adapters.httpbase import _parse_waiting
from broker.cache_routing import (
    MAX_PREFIX_BLOCKS,
    CacheRouter,
    block_hashes,
    normalize_prompt,
)
from broker.config import Settings
from broker.errors import BackendUnavailableError
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.state import InProcessStateStore, RedisStateStore

try:  # optional: only present in a venv with the [redis] extra + fakeredis
    import fakeredis.aioredis as _fakeredis_aio

    _HAVE_FAKEREDIS = True
except Exception:  # noqa: BLE001
    _HAVE_FAKEREDIS = False

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

TRIPLE_REGISTRY = """
models:
  - alias: node-a
    backend: fake
    runtime_model_id: shared-model
    pool: chat
  - alias: node-b
    backend: fake
    runtime_model_id: shared-model
    pool: chat
  - alias: node-c
    backend: fake
    runtime_model_id: shared-model
    pool: chat
"""

SINGLE_REGISTRY = """
models:
  - alias: solo
    backend: fake
    runtime_model_id: shared-model
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
        per_model_max_concurrency=4,
        max_concurrent_requests=32,
        max_loaded_models=5,
        route_strategy="cache_aware",
        cache_aware_block_chars=8,
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _manager(tmp_path: Path, body: str = POOL_REGISTRY, **over) -> ModelManager:
    s = _make_settings(tmp_path, body, **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    return m


def _chat(prefix: str) -> dict:
    # A single long user message; >block_chars so it yields full blocks.
    return {"messages": [{"role": "user", "content": prefix}]}


def _served(result: dict) -> str:
    return result["choices"][0]["message"]["content"].split(": ", 1)[1]


LONG = "the quick brown fox jumps over the lazy dog " * 4  # ~176 chars


# --- normalization + hashing (unit) -------------------------------------


def test_normalize_prompt_messages_is_ordered_and_deterministic():
    p = {"messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]}
    out = normalize_prompt(p)
    assert out == "system:sys\nuser:hi"
    assert normalize_prompt(p) == out  # deterministic


def test_normalize_prompt_handles_completion_and_input_and_parts():
    assert normalize_prompt({"prompt": "hello"}) == "hello"
    assert normalize_prompt({"input": ["a", "b"]}) == "a\nb"
    # multimodal content parts: text parts are kept, non-text ignored.
    parts = [{"type": "text", "text": "describe"}, {"type": "image_url", "image_url": {}}]
    assert normalize_prompt({"messages": [{"role": "user", "content": parts}]}) == "user:describe"
    assert normalize_prompt({}) == ""


def test_block_hashes_are_chained():
    # Changing an EARLY block changes its hash and every later chained hash.
    a = block_hashes("AAAAAAAABBBBBBBBCCCCCCCC", 8)
    b = block_hashes("XAAAAAAABBBBBBBBCCCCCCCC", 8)
    assert len(a) == len(b) == 3
    assert a[0] != b[0] and a[1] != b[1] and a[2] != b[2]
    # A shared leading block yields an identical leading hash (chained prefix).
    c = block_hashes("AAAAAAAAZZZZZZZZ", 8)
    assert c[0] == a[0] and c[1] != a[1]


def test_block_hashes_only_full_blocks_and_bounds():
    assert block_hashes("short", 8) == []  # sub-block prompt → no blocks
    assert len(block_hashes("A" * 23, 8)) == 2  # trailing partial block dropped
    assert len(block_hashes("A" * (8 * (MAX_PREFIX_BLOCKS + 50)), 8)) == MAX_PREFIX_BLOCKS


# --- CacheRouter index (in-process) -------------------------------------


async def test_record_then_longest_prefix_match():
    cr = CacheRouter(InProcessStateStore(), block_chars=8, beta=1.0, max_entries=0, ttl_seconds=0)
    blocks = block_hashes(LONG, 8)
    await cr.record("chat", blocks, "node-a")
    # node-a holds the whole prefix; node-b holds nothing.
    frac = await cr.match_fractions("chat", blocks, ["node-a", "node-b"])
    assert frac["node-a"] == 1.0
    assert "node-b" not in frac
    # A request sharing only the first 2 blocks matches 2/len on node-a.
    shared = blocks[:2] + block_hashes("ZZZZZZZZYYYYYYYY", 8)
    partial = await cr.match_fractions("chat", shared, ["node-a"])
    assert partial["node-a"] == pytest.approx(2 / len(shared))


async def test_no_match_returns_empty():
    cr = CacheRouter(InProcessStateStore(), block_chars=8, beta=1.0, max_entries=0, ttl_seconds=0)
    assert await cr.match_fractions("chat", block_hashes(LONG, 8), ["node-a"]) == {}
    assert await cr.match_fractions("chat", [], ["node-a"]) == {}


async def test_index_lru_eviction_bounds_size():
    cr = CacheRouter(InProcessStateStore(), block_chars=8, beta=1.0, max_entries=5, ttl_seconds=0)
    for i in range(20):
        await cr.record("chat", block_hashes(f"{i:08d}" * 2, 8), "node-a")
    assert cr.stats()["index_size"] == 5  # LRU-bounded


async def test_ttl_expiry_drops_entries():
    state = InProcessStateStore()
    cr = CacheRouter(state, block_chars=8, beta=1.0, max_entries=0, ttl_seconds=0.05)
    blocks = block_hashes(LONG, 8)
    await cr.record("chat", blocks, "node-a")
    assert (await cr.match_fractions("chat", blocks, ["node-a"]))["node-a"] == 1.0
    time.sleep(0.08)  # TTL elapses
    assert await cr.match_fractions("chat", blocks, ["node-a"]) == {}


async def test_stats_hit_rate_tracks_lookups():
    cr = CacheRouter(InProcessStateStore(), block_chars=8, beta=1.0, max_entries=0, ttl_seconds=0)
    blocks = block_hashes(LONG, 8)
    await cr.match_fractions("chat", blocks, ["node-a"])  # miss
    await cr.record("chat", blocks, "node-a")
    await cr.match_fractions("chat", blocks, ["node-a"])  # hit
    st = cr.stats()
    assert st["lookups"] == 2 and st["hits"] == 1 and st["hit_rate"] == 0.5


def test_parse_vllm_waiting_metric():
    text = (
        "# HELP vllm:num_requests_waiting Number waiting\n"
        "# TYPE vllm:num_requests_waiting gauge\n"
        'vllm:num_requests_waiting{model="m"} 3.0\n'
        "other_metric 99\n"
    )
    assert _parse_waiting(text) == 3.0
    assert _parse_waiting("no metric here") is None


# --- manager-level cache-aware routing ----------------------------------


async def test_cache_aware_routes_repeat_prefix_to_warm_member(tmp_path):
    mgr = _manager(tmp_path)
    first = _served(await mgr.chat("chat", _chat(LONG), "r0"))
    # Every subsequent request with the SAME prefix sticks to the warm member.
    for i in range(6):
        assert _served(await mgr.chat("chat", _chat(LONG), f"r{i}")) == first
    assert mgr.cache_router.stats()["hits"] >= 1
    await mgr.shutdown()


async def test_cache_aware_no_match_falls_back_to_least_busy(tmp_path):
    mgr = _manager(tmp_path)
    # A brand-new prefix matches no member → least_busy fallback. With node-a
    # already loaded up, the less-busy node-b must win.
    mgr.models["node-a"].machine.inflight = 5
    served = _served(await mgr.chat("chat", _chat("brand new unseen prefix " * 6), "r0"))
    assert served == "node-b"
    await mgr.shutdown()


async def test_load_penalty_flips_choice_away_from_warm_member(tmp_path):
    mgr = _manager(tmp_path, cache_aware_use_metrics=True, cache_aware_beta=1.0)
    # Warm BOTH members on the same prefix so affinity alone would tie.
    blocks = block_hashes(normalize_prompt(_chat(LONG)), 8)
    await mgr.cache_router.record("chat", blocks, "node-a")
    await mgr.cache_router.record("chat", blocks, "node-b")
    # node-a is heavily loaded (real metric), node-b idle → node-b must win.
    mgr.models["node-a"].last_load = 10.0
    mgr.models["node-b"].last_load = 0.0
    assert _served(await mgr.chat("chat", _chat(LONG), "r0")) == "node-b"
    await mgr.shutdown()


async def test_beta_zero_is_pure_affinity(tmp_path):
    mgr = _manager(tmp_path, cache_aware_use_metrics=True, cache_aware_beta=0.0)
    full = block_hashes(normalize_prompt(_chat(LONG)), 8)
    await mgr.cache_router.record("chat", full, "node-a")  # node-a: fraction 1.0
    await mgr.cache_router.record("chat", full[:1], "node-b")  # node-b: smaller
    # node-a is heavily loaded, but beta=0 ignores load entirely → the member with
    # the longer cached prefix (node-a) still wins.
    mgr.models["node-a"].last_load = 10.0
    mgr.models["node-b"].last_load = 0.0
    assert _served(await mgr.chat("chat", _chat(LONG), "r0")) == "node-a"
    await mgr.shutdown()


async def test_cache_aware_excludes_unhealthy_member(tmp_path):
    mgr = _manager(tmp_path)
    first = _served(await mgr.chat("chat", _chat(LONG), "r0"))
    # Mark the warm member unhealthy; routing must avoid it despite the prefix.
    mgr.models[first].healthy = False
    other = "node-b" if first == "node-a" else "node-a"
    assert _served(await mgr.chat("chat", _chat(LONG), "r1")) == other
    await mgr.shutdown()


async def test_cache_aware_excludes_cooled_down_member(tmp_path):
    mgr = _manager(tmp_path, cooldown_failures=1, cooldown_seconds=60)
    first = _served(await mgr.chat("chat", _chat(LONG), "r0"))
    # Bench the warm member via the cooldown store; selection must skip it.
    await mgr.state.record_failure(first, threshold=1, cooldown_seconds=60)
    other = "node-b" if first == "node-a" else "node-a"
    assert _served(await mgr.chat("chat", _chat(LONG), "r1")) == other
    await mgr.shutdown()


async def test_cache_aware_failover_still_works(tmp_path):
    mgr = _manager(tmp_path)
    mgr.models["node-a"].adapter.fail_on_chat = True
    contents = [_served(await mgr.chat("chat", _chat(LONG), f"r{i}")) for i in range(4)]
    assert all(c == "node-b" for c in contents)
    await mgr.shutdown()


async def test_cache_aware_all_down_raises(tmp_path):
    mgr = _manager(tmp_path)
    mgr.models["node-a"].adapter.fail_on_chat = True
    mgr.models["node-b"].adapter.fail_on_chat = True
    with pytest.raises(BackendUnavailableError):
        await mgr.chat("chat", _chat(LONG), "r0")
    await mgr.shutdown()


async def test_cache_aware_single_member_pool(tmp_path):
    mgr = _manager(tmp_path, SINGLE_REGISTRY)
    assert _served(await mgr.chat("solo", _chat(LONG), "r0")) == "solo"
    await mgr.shutdown()


async def test_cache_aware_three_members_longest_prefix_wins(tmp_path):
    mgr = _manager(tmp_path, TRIPLE_REGISTRY, cache_aware_block_chars=8)
    # Pre-seed: node-c holds the full prefix; node-b holds only the first block.
    full = block_hashes(normalize_prompt(_chat(LONG)), 8)
    await mgr.cache_router.record("chat", full, "node-c")
    await mgr.cache_router.record("chat", full[:1], "node-b")
    assert _served(await mgr.chat("chat", _chat(LONG), "r0")) == "node-c"
    await mgr.shutdown()


async def test_cache_aware_does_not_use_sticky_map(tmp_path):
    # Interplay: under cache_aware the sticky session map is never written; prefix
    # affinity (not session pinning) provides locality.
    mgr = _manager(tmp_path)
    await mgr.chat("chat", _chat(LONG), "r0", route_key="sess-1")
    assert await mgr.state.sticky_len() == 0
    await mgr.shutdown()


async def test_cache_aware_status_reports_index_stats(tmp_path):
    mgr = _manager(tmp_path)
    await mgr.chat("chat", _chat(LONG), "r0")
    st = await mgr.status()
    assert st["route_strategy"] == "cache_aware"
    assert st["cache_aware"]["enabled"] is True
    assert "index_size" in st["cache_aware"] and "hit_rate" in st["cache_aware"]
    await mgr.shutdown()


async def test_default_strategy_builds_no_cache_router(tmp_path):
    mgr = _manager(tmp_path, route_strategy="sticky")
    assert mgr.cache_router is None
    st = await mgr.status()
    assert st["cache_aware"] == {"enabled": False}
    await mgr.shutdown()


async def test_embeddings_route_through_cache_aware(tmp_path):
    mgr = _manager(tmp_path)
    r0 = await mgr.embeddings("chat", {"input": LONG}, "r0")
    first = r0["model"]  # relabelled to public name "chat"
    assert first == "chat"
    # Embeddings also populate the index (served blocks recorded).
    await mgr.embeddings("chat", {"input": LONG}, "r1")
    assert mgr.cache_router.stats()["index_size"] >= 1
    await mgr.shutdown()


# --- StateStore backing (Redis via fakeredis, else skip) ----------------


@pytest.mark.skipif(not _HAVE_FAKEREDIS, reason="redis/fakeredis not installed")
async def test_prefix_index_on_redis_statestore():
    client = _fakeredis_aio.FakeRedis()
    store = RedisStateStore("redis://fake", prefix="t", client=client)
    cr = CacheRouter(store, block_chars=8, beta=1.0, max_entries=0, ttl_seconds=0)
    blocks = block_hashes(LONG, 8)
    await cr.record("chat", blocks, "node-a")
    frac = await cr.match_fractions("chat", blocks, ["node-a", "node-b"])
    assert frac["node-a"] == 1.0 and "node-b" not in frac
    await store.close()


# --- bench smoke --------------------------------------------------------


def test_bench_routing_smoke_runs():
    repo = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        [sys.executable, "scripts/bench_routing.py", "--smoke"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": str(repo), "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert proc.returncode == 0, proc.stderr
    assert "cache_aware" in proc.stdout
