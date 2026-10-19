"""Exact-match response cache (ADR 0012).

An opt-in cache that serves an identical request from a prior response instead of
re-running inference. It is a thin policy layer over the ADR 0009
:class:`~broker.state.StateStore` generic KV (``kv_*``): in-process by default,
or Redis when ``BROKER_REDIS_URL`` is set, so replicas share one cache.

Design (prior art: LiteLLM exact caching, RFC 9111):

* **Key = hash of the whole normalized request, scoped to the caller.** The key
  components are (caller scope, resolved public model, endpoint, normalized
  request body). The caller scope is the API-key fingerprint (plus optional
  tenant) — it is the FIRST component, so a cached answer can never cross a
  key/tenant boundary. The public model is the one chosen *after* ``auto``
  resolution, so two callers who both hit ``auto`` and land on the same concrete
  model share correctly.
* **Deterministic-only by default.** Caching a sampled (temperature>0, no seed)
  request could serve a "wrong" varied answer, so by default only deterministic
  requests (``temperature == 0`` or an explicit ``seed``) are cached. Embeddings
  are always deterministic. Configurable to cache everything.
* **Never cache errors or partial/streamed responses.** Only a fully-successful
  non-streaming result is stored.
* **Bypass is honored at the HTTP layer** (``Cache-Control: no-cache``/``no-store``
  or ``x-broker-cache: bypass``) — this module just reads/writes.

Semantic (embedding-similarity) caching is deferred — see ADR 0012.
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from typing import Any

from .state import StateStore

#: KV namespace prefix (sits under the StateStore's own ``kv`` namespace).
_PREFIX = "respcache"


class ResponseCache:
    """Policy wrapper over :class:`StateStore` for exact response caching."""

    def __init__(self, state: StateStore, settings: Any) -> None:
        self.state = state
        self.enabled: bool = settings.response_cache_enabled
        self.ttl: float = settings.response_cache_ttl_seconds
        self.max_entries: int = settings.response_cache_max_entries
        self.max_bytes: int = settings.response_cache_max_bytes
        self.deterministic_only: bool = settings.response_cache_deterministic_only
        # Per-process LRU index of live keys, used only to bound ``max_entries``
        # for the in-process store (best-effort under Redis, where TTL is the
        # real bound and the index is per-replica).
        self._index: OrderedDict[str, None] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.stores = 0

    # --- key / policy (pure, unit-testable) ------------------------------
    @staticmethod
    def make_key(*, scope: str, model: str, endpoint: str, payload: dict) -> str:
        """Stable cache key for a request.

        Deterministic across equal requests regardless of dict ordering: the
        body is serialized with ``sort_keys=True``. Routing-only fields that do
        not affect the response content (``model``, ``stream``, ``user``) are
        excluded; the model is pinned via the explicit ``model`` argument (the
        resolved public name) instead.
        """
        body = {k: v for k, v in payload.items() if k not in ("model", "stream", "user")}
        blob = json.dumps(
            {"scope": scope, "model": model, "endpoint": endpoint, "body": body},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def is_deterministic(payload: dict) -> bool:
        """True if the request is reproducible: explicit seed, or temperature 0.

        An unset temperature is treated as non-deterministic (backends default to
        a nonzero temperature), so it is NOT cached under the default policy.
        """
        if payload.get("seed") is not None:
            return True
        temp = payload.get("temperature")
        try:
            return temp is not None and float(temp) == 0.0
        except (TypeError, ValueError):
            return False

    def cacheable(self, payload: dict, *, always_deterministic: bool = False) -> bool:
        """Whether this request is eligible to be cached under the current policy."""
        if not self.enabled:
            return False
        if not self.deterministic_only:
            return True
        return always_deterministic or self.is_deterministic(payload)

    # --- read / write ----------------------------------------------------
    def _k(self, key: str) -> str:
        return f"{_PREFIX}:{key}"

    async def get(self, key: str) -> dict | None:
        """Return a cached result dict, or ``None`` on a miss. Updates stats."""
        raw = await self.state.kv_get(self._k(key))
        if raw is None:
            self.misses += 1
            return None
        self.hits += 1
        # Refresh LRU recency for the in-process bound.
        if key in self._index:
            self._index.move_to_end(key)
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    async def store(self, key: str, value: dict) -> bool:
        """Store a successful result. Returns False if skipped (too large).

        Enforces ``max_bytes`` (per-entry cap) and ``max_entries`` (LRU bound on
        the in-process index). TTL comes from ``response_cache_ttl_seconds``.
        """
        data = json.dumps(value, default=str).encode("utf-8")
        if self.max_bytes > 0 and len(data) > self.max_bytes:
            return False
        ttl = self.ttl if self.ttl > 0 else None
        await self.state.kv_set(self._k(key), data, ttl=ttl)
        self._index[key] = None
        self._index.move_to_end(key)
        self.stores += 1
        while self.max_entries > 0 and len(self._index) > self.max_entries:
            old, _ = self._index.popitem(last=False)
            await self.state.kv_delete(self._k(old))
        return True

    async def invalidate(self, key: str) -> None:
        await self.state.kv_delete(self._k(key))
        self._index.pop(key, None)

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "backend": self.state.backend,
            "ttl_seconds": self.ttl,
            "max_entries": self.max_entries,
            "deterministic_only": self.deterministic_only,
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
            "live_entries": len(self._index),
        }
