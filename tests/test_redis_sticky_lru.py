"""Global LRU bound on Redis sticky entries (ADR 0017).

The Redis sticky map is TTL-bounded by default; an optional sorted-set index
scored by last-pin enforces a global entry cap shared across replicas, evicting
the least-recently pinned sessions (ZPOPMIN). Driven by an in-memory fakeredis
server; skipped cleanly where the optional [redis] extra is absent.
"""

from __future__ import annotations

import pytest

from broker.state import RedisStateStore

try:
    import fakeredis.aioredis as _fakeredis_aio

    _HAVE_FAKEREDIS = True
except Exception:  # noqa: BLE001
    _HAVE_FAKEREDIS = False

pytestmark = pytest.mark.skipif(
    not _HAVE_FAKEREDIS, reason="redis/fakeredis not installed (optional [redis] extra)"
)


def _store(max_entries: int) -> RedisStateStore:
    return RedisStateStore(
        "redis://fake",
        prefix="test",
        client=_fakeredis_aio.FakeRedis(),
        sticky_max_entries=max_entries,
    )


async def test_bound_evicts_least_recently_pinned():
    s = _store(max_entries=3)
    for i in range(6):
        await s.sticky_set("chat", f"s{i}", "node-a")
    assert await s.sticky_len() == 3
    # The 3 oldest are evicted; their sticky keys are gone.
    for i in range(3):
        assert await s.sticky_get("chat", f"s{i}") is None
    for i in range(3, 6):
        assert await s.sticky_get("chat", f"s{i}") == "node-a"
    await s.close()


async def test_resetting_refreshes_lru_position():
    s = _store(max_entries=3)
    for i in range(3):
        await s.sticky_set("chat", f"s{i}", "node-a")
    # Touch s0 so it is now the most-recently pinned, then add two more.
    await s.sticky_set("chat", "s0", "node-b")
    await s.sticky_set("chat", "s3", "node-a")
    await s.sticky_set("chat", "s4", "node-a")
    assert await s.sticky_len() == 3
    # s1 and s2 (now the oldest) are evicted; s0 survived because it was touched.
    assert await s.sticky_get("chat", "s0") == "node-b"
    assert await s.sticky_get("chat", "s1") is None
    assert await s.sticky_get("chat", "s2") is None
    await s.close()


async def test_unbounded_keeps_everything():
    s = _store(max_entries=0)  # TTL-only (v0.5.0 default)
    for i in range(10):
        await s.sticky_set("chat", f"s{i}", "node-a")
    assert await s.sticky_len() == 10
    for i in range(10):
        assert await s.sticky_get("chat", f"s{i}") == "node-a"
    await s.close()


async def test_bounded_len_uses_index():
    s = _store(max_entries=5)
    for i in range(3):
        await s.sticky_set("chat", f"s{i}", "node-a")
    # Overwriting an existing pairing does not inflate the count.
    await s.sticky_set("chat", "s0", "node-b")
    assert await s.sticky_len() == 3
    await s.close()
