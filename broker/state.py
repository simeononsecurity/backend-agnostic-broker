"""Shared routing/runtime state behind one async abstraction (ADR 0009).

The broker's routing hooks (sticky session map, per-member cooldowns) and
future subsystems (per-key rate-limit counters — ADR 0008, prefix→member index
— ADR 0010, response cache — ADR 0012) all need a place to keep small pieces of
state. In a single process that is just in-memory dicts. To run several broker
replicas behind a load balancer they must share one view, which means an
external store.

:class:`StateStore` is that abstraction. Two implementations ship:

* :class:`InProcessStateStore` — the default. Exact current in-memory
  semantics, zero new dependencies, with an LRU bound on the sticky map. This is
  what every single-replica deployment uses and what preserves v0.4.0 behavior.
* :class:`RedisStateStore` — selected when ``BROKER_REDIS_URL`` is set. Backs the
  same API with Redis so replicas share sticky routing, cooldowns, counters, and
  cached values. ``redis`` is an *optional* dependency (``pip install
  'backend-agnostic-broker[redis]'``); it is imported lazily and a clear
  :class:`ConfigError` is raised if the URL is configured but the package is
  missing.

The API is intentionally broader than today's manager needs: it is the platform
surface the P1/P2 ADRs build on. Everything is ``async`` so the Redis backend can
do real I/O without a second (sync) code path.

Design notes / prior art
------------------------
* Sliding-window counters use the sorted-set "sliding window log" pattern
  (antirez's Redis rate-limiting patterns; redis.io ratelimiting howto): score =
  timestamp, drop entries older than the window, count what remains.
* Cooldown records implement a circuit-breaker (Fowler) / Envoy outlier-detection
  style benching: N consecutive failures open the circuit for a window, after
  which a single half-open trial decides re-close vs re-open.
* Keys use wall-clock ``time.time()`` (not ``monotonic``) so TTLs and cooldown
  windows are comparable across replicas sharing one Redis.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .errors import ConfigError


@dataclass
class CooldownRecord:
    """A member's consecutive-failure + cooldown state at a point in time."""

    failures: int = 0
    until: float = 0.0  # wall-clock ts the cooldown expires (0 = not benched)
    now: float = 0.0

    @property
    def in_cooldown(self) -> bool:
        """True while the member is benched (selection must skip it)."""
        return self.until > self.now

    @property
    def active(self) -> bool:
        """True if there is any tracked failure/cooldown state for the member."""
        return self.failures > 0 or self.until > 0

    @property
    def seconds_remaining(self) -> float:
        return max(0.0, self.until - self.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "failures": self.failures,
            "in_cooldown": self.in_cooldown,
            "until": self.until if self.until else None,
            "seconds_remaining": round(self.seconds_remaining, 3),
        }


class StateStore:
    """Async abstraction over routing/runtime state (in-process | Redis).

    Grouped by concern:

    * **Sticky map** — ``sticky_get`` / ``sticky_set`` / ``sticky_len`` pin a
      session to a pool member.
    * **Counters** — ``incr`` / ``get_counter`` (atomic, optional TTL) and
      ``window_incr`` / ``window_count`` (sliding-window log). Substrate for
      per-key rate limits (ADR 0008).
    * **Cooldowns** — ``record_failure`` / ``record_success`` / ``get_cooldown``
      / ``try_half_open`` implement circuit-breaker benching of pool members.
    * **Generic KV** — ``kv_get`` / ``kv_set`` / ``kv_get_json`` / ``kv_set_json``
      / ``kv_delete`` (bytes or JSON, optional TTL). Substrate for the response
      cache (ADR 0012) and the prefix→member index (ADR 0010).
    """

    backend: str = "base"

    # --- sticky map ------------------------------------------------------
    async def sticky_get(self, pool: str, session: str) -> str | None:
        raise NotImplementedError

    async def sticky_set(self, pool: str, session: str, member: str) -> None:
        raise NotImplementedError

    async def sticky_len(self) -> int:
        raise NotImplementedError

    # --- counters --------------------------------------------------------
    async def incr(self, key: str, amount: int = 1, ttl: float | None = None) -> int:
        raise NotImplementedError

    async def get_counter(self, key: str) -> int:
        raise NotImplementedError

    async def window_incr(
        self, key: str, window_seconds: float, amount: int = 1, now: float | None = None
    ) -> int:
        raise NotImplementedError

    async def window_count(self, key: str, window_seconds: float, now: float | None = None) -> int:
        raise NotImplementedError

    # --- cooldowns -------------------------------------------------------
    async def record_failure(
        self,
        key: str,
        *,
        threshold: int,
        cooldown_seconds: float,
        now: float | None = None,
    ) -> CooldownRecord:
        raise NotImplementedError

    async def record_success(self, key: str) -> None:
        raise NotImplementedError

    async def get_cooldown(self, key: str, now: float | None = None) -> CooldownRecord:
        raise NotImplementedError

    async def try_half_open(self, key: str, ttl_seconds: float, now: float | None = None) -> bool:
        raise NotImplementedError

    # --- generic KV ------------------------------------------------------
    async def kv_get(self, key: str) -> bytes | None:
        raise NotImplementedError

    async def kv_set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        raise NotImplementedError

    async def kv_delete(self, key: str) -> None:
        raise NotImplementedError

    async def kv_get_json(self, key: str) -> Any | None:
        raw = await self.kv_get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def kv_set_json(self, key: str, value: Any, ttl: float | None = None) -> None:
        await self.kv_set(key, json.dumps(value).encode("utf-8"), ttl=ttl)

    # --- sorted-set LRU (ADR 0018) ---------------------------------------
    # A last-access-scored set used to size-bound the cache-aware prefix index
    # (ADR 0010) ACROSS replicas when backed by Redis: ``zlru_touch`` records an
    # access, ``zlru_trim`` evicts the oldest members beyond ``max_entries`` and
    # returns them so the caller can delete the associated KV entries.
    async def zlru_touch(
        self, index: str, member: str, score: float, ttl: float | None = None
    ) -> None:
        raise NotImplementedError

    async def zlru_trim(self, index: str, max_entries: int) -> list[str]:
        raise NotImplementedError

    async def zlru_size(self, index: str) -> int:
        raise NotImplementedError

    # --- lifecycle -------------------------------------------------------
    async def close(self) -> None:
        return None


class InProcessStateStore(StateStore):
    """Default store: plain in-memory structures, exact v0.4.0 semantics.

    Single event loop → each method runs to completion without awaiting between a
    read and its dependent write, so operations are effectively atomic. The
    sticky map is an ``OrderedDict`` with LRU eviction bounded by
    ``max_sticky_entries`` (0 disables the bound).
    """

    backend = "in_process"

    def __init__(self, max_sticky_entries: int = 10000) -> None:
        self.max_sticky_entries = max_sticky_entries
        self.sticky: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._counters: dict[str, tuple[int, float | None]] = {}
        self._windows: dict[str, list[float]] = {}
        self._cooldowns: dict[str, tuple[int, float]] = {}  # key -> (failures, until)
        self._halfopen: dict[str, float] = {}  # key -> trial-lock expiry ts
        self._kv: dict[str, tuple[bytes, float | None]] = {}
        self._zlru: dict[str, dict[str, float]] = {}  # index -> {member: score}

    # --- sticky map ---
    async def sticky_get(self, pool: str, session: str) -> str | None:
        return self.sticky.get((pool, session))

    async def sticky_set(self, pool: str, session: str, member: str) -> None:
        k = (pool, session)
        self.sticky[k] = member
        self.sticky.move_to_end(k)
        cap = self.max_sticky_entries
        while cap > 0 and len(self.sticky) > cap:
            self.sticky.popitem(last=False)

    async def sticky_len(self) -> int:
        return len(self.sticky)

    # --- counters ---
    def _counter_live(self, key: str, now: float) -> int:
        v = self._counters.get(key)
        if v is None:
            return 0
        value, expiry = v
        if expiry is not None and now >= expiry:
            self._counters.pop(key, None)
            return 0
        return value

    async def incr(self, key: str, amount: int = 1, ttl: float | None = None) -> int:
        now = time.time()
        cur = self._counter_live(key, now)
        existing = self._counters.get(key)
        expiry = existing[1] if existing is not None else None
        if cur == 0 and ttl is not None:  # (re)starting the window
            expiry = now + ttl
        new = cur + amount
        self._counters[key] = (new, expiry)
        return new

    async def get_counter(self, key: str) -> int:
        return self._counter_live(key, time.time())

    async def window_incr(
        self, key: str, window_seconds: float, amount: int = 1, now: float | None = None
    ) -> int:
        now = time.time() if now is None else now
        stamps = self._windows.setdefault(key, [])
        cutoff = now - window_seconds
        stamps[:] = [t for t in stamps if t > cutoff]
        stamps.extend([now] * max(0, amount))
        return len(stamps)

    async def window_count(self, key: str, window_seconds: float, now: float | None = None) -> int:
        now = time.time() if now is None else now
        stamps = self._windows.get(key)
        if not stamps:
            return 0
        cutoff = now - window_seconds
        stamps[:] = [t for t in stamps if t > cutoff]
        if not stamps:
            self._windows.pop(key, None)
        return len(stamps)

    # --- cooldowns ---
    async def record_failure(
        self,
        key: str,
        *,
        threshold: int,
        cooldown_seconds: float,
        now: float | None = None,
    ) -> CooldownRecord:
        now = time.time() if now is None else now
        failures, until = self._cooldowns.get(key, (0, 0.0))
        failures += 1
        if threshold > 0 and failures >= threshold:
            until = now + cooldown_seconds
        self._cooldowns[key] = (failures, until)
        return CooldownRecord(failures=failures, until=until, now=now)

    async def record_success(self, key: str) -> None:
        self._cooldowns.pop(key, None)
        self._halfopen.pop(key, None)

    async def get_cooldown(self, key: str, now: float | None = None) -> CooldownRecord:
        now = time.time() if now is None else now
        failures, until = self._cooldowns.get(key, (0, 0.0))
        return CooldownRecord(failures=failures, until=until, now=now)

    async def try_half_open(self, key: str, ttl_seconds: float, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        exp = self._halfopen.get(key)
        if exp is not None and now < exp:
            return False
        self._halfopen[key] = now + ttl_seconds
        return True

    # --- generic KV ---
    async def kv_get(self, key: str) -> bytes | None:
        v = self._kv.get(key)
        if v is None:
            return None
        value, expiry = v
        if expiry is not None and time.time() >= expiry:
            self._kv.pop(key, None)
            return None
        return value

    async def kv_set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        expiry = (time.time() + ttl) if ttl is not None else None
        self._kv[key] = (value, expiry)

    async def kv_delete(self, key: str) -> None:
        self._kv.pop(key, None)

    # --- sorted-set LRU ---
    async def zlru_touch(
        self, index: str, member: str, score: float, ttl: float | None = None
    ) -> None:
        self._zlru.setdefault(index, {})[member] = score

    async def zlru_trim(self, index: str, max_entries: int) -> list[str]:
        d = self._zlru.get(index)
        if not d or max_entries <= 0 or len(d) <= max_entries:
            return []
        # Oldest = lowest score; ties broken by insertion order (stable sort).
        ordered = sorted(d.items(), key=lambda kv: kv[1])
        evict = [m for m, _ in ordered[: len(d) - max_entries]]
        for m in evict:
            d.pop(m, None)
        return evict

    async def zlru_size(self, index: str) -> int:
        return len(self._zlru.get(index, {}))


class RedisStateStore(StateStore):
    """Shared store backed by Redis (optional dependency; multi-replica).

    Selected when ``BROKER_REDIS_URL`` is set. All keys are namespaced with
    ``BROKER_REDIS_PREFIX`` so one Redis can host several brokers. The client is
    a single shared ``redis.asyncio.Redis`` created at construction (its pool
    connects lazily on first command) and closed on shutdown — the pattern the
    redis-py async docs recommend over per-request clients.
    """

    backend = "redis"

    def __init__(
        self,
        url: str,
        prefix: str = "broker",
        *,
        client: Any | None = None,
        sticky_ttl_seconds: float = 0.0,
    ) -> None:
        self.prefix = prefix
        self.sticky_ttl_seconds = sticky_ttl_seconds
        if client is not None:
            self._r = client
        else:
            try:
                import redis.asyncio as aioredis
            except ImportError as exc:  # pragma: no cover - exercised via make_state_store
                raise ConfigError(
                    "BROKER_REDIS_URL is set but the 'redis' package is not installed",
                    remediation="pip install 'backend-agnostic-broker[redis]'",
                ) from exc
            # bytes in/out (decode_responses=False) so kv_* can store raw bytes;
            # string fields are decoded explicitly where needed.
            self._r = aioredis.from_url(url, decode_responses=False)

    def _k(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    @staticmethod
    def _ttl_int(ttl: float | None) -> int | None:
        if ttl is None or ttl <= 0:
            return None
        return max(1, int(math.ceil(ttl)))

    # --- sticky map ---
    async def sticky_get(self, pool: str, session: str) -> str | None:
        raw = await self._r.get(self._k("sticky", pool, session))
        return raw.decode("utf-8") if raw is not None else None

    async def sticky_set(self, pool: str, session: str, member: str) -> None:
        await self._r.set(
            self._k("sticky", pool, session),
            member.encode("utf-8"),
            ex=self._ttl_int(self.sticky_ttl_seconds),
        )

    async def sticky_len(self) -> int:
        # Best-effort SCAN count (ops/observability only). Bounded so a large
        # keyspace can't make /status pathological.
        count = 0
        match = self._k("sticky", "*")
        async for _ in self._r.scan_iter(match=match, count=500):
            count += 1
            if count >= 100000:
                break
        return count

    # --- counters ---
    async def incr(self, key: str, amount: int = 1, ttl: float | None = None) -> int:
        k = self._k("ctr", key)
        value = await self._r.incrby(k, amount)
        if value == amount and ttl is not None:  # first write in this window
            sec = self._ttl_int(ttl)
            if sec is not None:
                await self._r.expire(k, sec)
        return int(value)

    async def get_counter(self, key: str) -> int:
        raw = await self._r.get(self._k("ctr", key))
        return int(raw) if raw is not None else 0

    async def window_incr(
        self, key: str, window_seconds: float, amount: int = 1, now: float | None = None
    ) -> int:
        now = time.time() if now is None else now
        k = self._k("win", key)
        cutoff = now - window_seconds
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(k, 0, cutoff)
            for _ in range(max(1, amount)):
                member = f"{now:.6f}:{os.urandom(6).hex()}".encode()
                pipe.zadd(k, {member: now})
            pipe.zcard(k)
            pipe.expire(k, self._ttl_int(window_seconds) or 1)
            results = await pipe.execute()
        return int(results[-2])

    async def window_count(self, key: str, window_seconds: float, now: float | None = None) -> int:
        now = time.time() if now is None else now
        k = self._k("win", key)
        cutoff = now - window_seconds
        async with self._r.pipeline(transaction=True) as pipe:
            pipe.zremrangebyscore(k, 0, cutoff)
            pipe.zcard(k)
            results = await pipe.execute()
        return int(results[-1])

    # --- cooldowns ---
    async def record_failure(
        self,
        key: str,
        *,
        threshold: int,
        cooldown_seconds: float,
        now: float | None = None,
    ) -> CooldownRecord:
        now = time.time() if now is None else now
        cf = self._k("cf", key)
        failures = int(await self._r.incr(cf))
        # Keep the failure counter around at least as long as a cooldown window
        # so stale counts eventually self-clear.
        await self._r.expire(cf, self._ttl_int(cooldown_seconds) or 1)
        until = 0.0
        if threshold > 0 and failures >= threshold:
            until = now + cooldown_seconds
            await self._r.set(
                self._k("cd", key), str(until).encode(), ex=self._ttl_int(cooldown_seconds)
            )
        return CooldownRecord(failures=failures, until=until, now=now)

    async def record_success(self, key: str) -> None:
        await self._r.delete(self._k("cf", key), self._k("cd", key), self._k("ho", key))

    async def get_cooldown(self, key: str, now: float | None = None) -> CooldownRecord:
        now = time.time() if now is None else now
        raw_f = await self._r.get(self._k("cf", key))
        raw_u = await self._r.get(self._k("cd", key))
        failures = int(raw_f) if raw_f is not None else 0
        until = float(raw_u) if raw_u is not None else 0.0
        return CooldownRecord(failures=failures, until=until, now=now)

    async def try_half_open(self, key: str, ttl_seconds: float, now: float | None = None) -> bool:
        ok = await self._r.set(
            self._k("ho", key), b"1", nx=True, ex=self._ttl_int(ttl_seconds) or 1
        )
        return bool(ok)

    # --- generic KV ---
    async def kv_get(self, key: str) -> bytes | None:
        return await self._r.get(self._k("kv", key))

    async def kv_set(self, key: str, value: bytes, ttl: float | None = None) -> None:
        await self._r.set(self._k("kv", key), value, ex=self._ttl_int(ttl))

    async def kv_delete(self, key: str) -> None:
        await self._r.delete(self._k("kv", key))

    # --- sorted-set LRU ---
    async def zlru_touch(
        self, index: str, member: str, score: float, ttl: float | None = None
    ) -> None:
        k = self._k("zlru", index)
        await self._r.zadd(k, {member.encode("utf-8"): score})
        sec = self._ttl_int(ttl)
        if sec is not None:
            await self._r.expire(k, sec)

    async def zlru_trim(self, index: str, max_entries: int) -> list[str]:
        k = self._k("zlru", index)
        n = int(await self._r.zcard(k))
        if max_entries <= 0 or n <= max_entries:
            return []
        to_remove = n - max_entries
        # Lowest-scored (oldest) members, as a cross-replica LRU bound.
        members = await self._r.zrange(k, 0, to_remove - 1)
        if members:
            await self._r.zremrangebyrank(k, 0, to_remove - 1)
        return [m.decode("utf-8") if isinstance(m, bytes) else str(m) for m in members]

    async def zlru_size(self, index: str) -> int:
        return int(await self._r.zcard(self._k("zlru", index)))

    # --- lifecycle ---
    async def close(self) -> None:
        try:
            await self._r.aclose()
        except Exception:  # noqa: BLE001 - best effort on shutdown
            pass


def make_state_store(settings: Any) -> StateStore:
    """Build the configured store: Redis when ``redis_url`` is set, else in-proc.

    Raises :class:`ConfigError` if Redis is requested but the optional ``redis``
    package is not importable (fail-fast at startup, never a silent downgrade).
    """
    url = getattr(settings, "redis_url", None)
    if url:
        return RedisStateStore(
            url,
            getattr(settings, "redis_prefix", "broker"),
            sticky_ttl_seconds=getattr(settings, "route_sticky_ttl_seconds", 0.0),
        )
    return InProcessStateStore(
        max_sticky_entries=getattr(settings, "route_sticky_max_entries", 10000)
    )
