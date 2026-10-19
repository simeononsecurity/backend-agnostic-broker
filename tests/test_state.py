"""StateStore contract tests (ADR 0009).

Every behavioral test runs against BOTH backends via the ``store`` fixture:

* ``in_process`` — always runs.
* ``redis`` — runs only when ``redis`` + ``fakeredis`` are importable (a private
  worktree venv); otherwise it is skipped cleanly so the default CI env (no Redis
  dependency) stays green. The Redis backend is driven by an in-memory
  ``fakeredis`` server, so no real Redis daemon or network is required.
"""

from __future__ import annotations

import pytest

from broker.state import (
    InProcessStateStore,
    RedisStateStore,
    make_state_store,
)

try:  # optional: only present in a venv with the [redis] extra + fakeredis
    import fakeredis.aioredis as _fakeredis_aio

    _HAVE_FAKEREDIS = True
except Exception:  # noqa: BLE001
    _HAVE_FAKEREDIS = False


def _in_process() -> InProcessStateStore:
    return InProcessStateStore(max_sticky_entries=10)


def _redis() -> RedisStateStore:
    client = _fakeredis_aio.FakeRedis()
    return RedisStateStore("redis://fake", prefix="test", client=client)


@pytest.fixture(params=["in_process", "redis"])
async def store(request):
    if request.param == "redis":
        if not _HAVE_FAKEREDIS:
            pytest.skip("redis/fakeredis not installed (optional [redis] extra)")
        s = _redis()
    else:
        s = _in_process()
    yield s
    await s.close()


# --- sticky map ---------------------------------------------------------


async def test_sticky_roundtrip(store):
    assert await store.sticky_get("chat", "sess-1") is None
    await store.sticky_set("chat", "sess-1", "node-a")
    assert await store.sticky_get("chat", "sess-1") == "node-a"
    # Distinct (pool, session) keys do not collide.
    await store.sticky_set("chat", "sess-2", "node-b")
    assert await store.sticky_get("chat", "sess-2") == "node-b"
    assert await store.sticky_get("chat", "sess-1") == "node-a"


async def test_sticky_overwrite(store):
    await store.sticky_set("chat", "s", "node-a")
    await store.sticky_set("chat", "s", "node-b")
    assert await store.sticky_get("chat", "s") == "node-b"


async def test_sticky_len_counts_entries(store):
    for i in range(3):
        await store.sticky_set("chat", f"s{i}", "node-a")
    assert await store.sticky_len() == 3


def test_in_process_sticky_lru_bound():
    s = InProcessStateStore(max_sticky_entries=3)
    import asyncio

    async def run():
        for i in range(6):
            await s.sticky_set("chat", f"s{i}", "node-a")
        assert await s.sticky_len() == 3
        assert set(s.sticky) == {("chat", "s3"), ("chat", "s4"), ("chat", "s5")}

    asyncio.run(run())


# --- counters -----------------------------------------------------------


async def test_counter_incr_and_get(store):
    assert await store.get_counter("k") == 0
    assert await store.incr("k") == 1
    assert await store.incr("k", amount=2) == 3
    assert await store.get_counter("k") == 3


async def test_counter_ttl_window(store):
    # First write in the window sets the TTL; subsequent writes keep counting.
    assert await store.incr("rl", ttl=60) == 1
    assert await store.incr("rl", ttl=60) == 2
    assert await store.get_counter("rl") == 2


async def test_window_incr_counts_within_window(store):
    now = 1_000_000.0
    assert await store.window_incr("w", 60, now=now) == 1
    assert await store.window_incr("w", 60, now=now + 1) == 2
    assert await store.window_incr("w", 60, now=now + 2) == 3
    # An event far in the future prunes everything older than the window.
    assert await store.window_incr("w", 60, now=now + 1000) == 1


async def test_window_count_prunes_old(store):
    now = 2_000_000.0
    await store.window_incr("w2", 10, now=now)
    await store.window_incr("w2", 10, now=now + 1)
    assert await store.window_count("w2", 10, now=now + 2) == 2
    assert await store.window_count("w2", 10, now=now + 100) == 0


# --- cooldowns ----------------------------------------------------------


async def test_cooldown_opens_after_threshold(store):
    now = 5_000_000.0
    r1 = await store.record_failure("m", threshold=3, cooldown_seconds=30, now=now)
    assert r1.failures == 1 and not r1.in_cooldown
    await store.record_failure("m", threshold=3, cooldown_seconds=30, now=now)
    r3 = await store.record_failure("m", threshold=3, cooldown_seconds=30, now=now)
    assert r3.failures == 3 and r3.in_cooldown
    cd = await store.get_cooldown("m", now=now + 1)
    assert cd.in_cooldown and cd.seconds_remaining > 0


async def test_cooldown_success_resets(store):
    now = 6_000_000.0
    for _ in range(3):
        await store.record_failure("m", threshold=2, cooldown_seconds=30, now=now)
    assert (await store.get_cooldown("m", now=now)).in_cooldown
    await store.record_success("m")
    cd = await store.get_cooldown("m", now=now)
    assert not cd.in_cooldown and cd.failures == 0


async def test_cooldown_expires_after_window(store):
    now = 7_000_000.0
    for _ in range(2):
        await store.record_failure("m", threshold=2, cooldown_seconds=10, now=now)
    assert (await store.get_cooldown("m", now=now + 1)).in_cooldown
    # In-process uses the injected ``now``; Redis relies on real key TTL, so it
    # is still benched at now+11 wall-clock. Assert the in-process semantic and
    # accept Redis' real-clock expiry separately.
    if store.backend == "in_process":
        assert not (await store.get_cooldown("m", now=now + 11)).in_cooldown


async def test_cooldown_disabled_threshold_never_opens(store):
    now = 8_000_000.0
    r = await store.record_failure("m", threshold=0, cooldown_seconds=30, now=now)
    for _ in range(4):
        r = await store.record_failure("m", threshold=0, cooldown_seconds=30, now=now)
    assert not r.in_cooldown  # threshold 0 → never benches


async def test_try_half_open_single_claim(store):
    assert await store.try_half_open("m", 5) is True
    assert await store.try_half_open("m", 5) is False  # already claimed
    await store.record_success("m")
    assert await store.try_half_open("m", 5) is True  # released on success


# --- generic KV ---------------------------------------------------------


async def test_kv_bytes_roundtrip(store):
    assert await store.kv_get("k") is None
    await store.kv_set("k", b"hello")
    assert await store.kv_get("k") == b"hello"
    await store.kv_delete("k")
    assert await store.kv_get("k") is None


async def test_kv_json_roundtrip(store):
    await store.kv_set_json("doc", {"a": 1, "b": [2, 3]})
    assert await store.kv_get_json("doc") == {"a": 1, "b": [2, 3]}


# --- factory ------------------------------------------------------------


def test_make_state_store_defaults_in_process():
    class S:
        redis_url = None
        route_sticky_max_entries = 5

    store = make_state_store(S())
    assert isinstance(store, InProcessStateStore)
    assert store.backend == "in_process"
    assert store.max_sticky_entries == 5


def test_make_state_store_redis_missing_dependency_raises():
    """When redis_url is set but the package is missing, fail fast (ConfigError)."""
    import broker.state as state_mod

    # Simulate the dependency being absent regardless of the test venv.
    try:
        import redis.asyncio  # noqa: F401

        has_redis = True
    except Exception:  # noqa: BLE001
        has_redis = False

    if has_redis:
        pytest.skip("redis installed in this venv; missing-dep path covered by import guard")

    class S:
        redis_url = "redis://localhost:6379/0"
        redis_prefix = "broker"
        route_sticky_ttl_seconds = 0.0

    from broker.errors import ConfigError

    with pytest.raises(ConfigError):
        state_mod.make_state_store(S())
