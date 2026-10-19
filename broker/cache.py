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

Semantic (embedding-similarity) caching is implemented separately by
:class:`SemanticCache` below (ADR 0014).
"""

from __future__ import annotations

import hashlib
import json
import math
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

    async def store(self, key: str, value: dict, *, ttl_override: float | None = None) -> bool:
        """Store a successful result. Returns False if skipped (too large).

        Enforces ``max_bytes`` (per-entry cap) and ``max_entries`` (LRU bound on
        the in-process index). TTL comes from ``response_cache_ttl_seconds``
        unless ``ttl_override`` is given (per-request ``x-broker-cache-ttl``,
        ADR 0014): a non-negative override wins (0 = no TTL).
        """
        data = json.dumps(value, default=str).encode("utf-8")
        if self.max_bytes > 0 and len(data) > self.max_bytes:
            return False
        effective = self.ttl if ttl_override is None else ttl_override
        ttl = effective if effective > 0 else None
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


#: KV namespace prefix for the semantic index (under the StateStore ``kv`` ns).
_SEM_PREFIX = "semcache"

#: Request fields compared fuzzily (by embedding) rather than exactly. Everything
#: else in the body is part of the exact-match bucket key, so a semantic hit can
#: only differ in the prompt text.
_FUZZY_FIELDS = ("messages",)


class SemanticCache:
    """Embedding-similarity response cache (ADR 0014).

    A second cache layer consulted only after an exact-cache miss, for
    chat/completions/responses. It partitions stored responses into **buckets**
    keyed by every non-prompt request field (scope, resolved model, endpoint,
    sampling/tools/response_format/seed/..., plus the ADR 0012 scope) so that only
    the *prompt text* is fuzzy — a semantic hit can never cross a scope, model, or
    parameter boundary. Within a bucket the query embedding is compared to each
    stored embedding by cosine similarity (pure Python — no numpy); a hit fires
    when the best similarity clears ``threshold``.

    Embedding + residency live outside this class (the app supplies them via the
    manager's own ``/v1/embeddings`` path): this class is given a ready embedding
    vector and is otherwise a pure StateStore policy, keeping it unit-testable.
    """

    def __init__(self, state: StateStore, settings: Any) -> None:
        self.state = state
        self.enabled: bool = settings.semantic_cache_enabled
        self.model: str | None = settings.semantic_cache_model
        self.threshold: float = settings.semantic_cache_threshold
        self.ttl: float = settings.semantic_cache_ttl_seconds
        self.max_entries: int = settings.semantic_cache_max_entries
        self.require_resident: bool = settings.semantic_cache_require_resident
        self.hits = 0
        self.misses = 0
        self.stores = 0
        self.skipped_not_resident = 0

    # --- pure helpers (unit-testable) ------------------------------------
    @staticmethod
    def cosine(a: list[float], b: list[float]) -> float:
        """Cosine similarity of two equal-length vectors (0.0 on degenerate input)."""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = 0.0
        na = 0.0
        nb = 0.0
        for x, y in zip(a, b, strict=False):
            dot += x * y
            na += x * x
            nb += y * y
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return dot / (math.sqrt(na) * math.sqrt(nb))

    @staticmethod
    def bucket_key(*, scope: str, model: str, endpoint: str, payload: dict) -> str:
        """Exact-match bucket key: everything except the fuzzy prompt fields.

        Excludes the fuzzy fields (``messages``) and the routing-only fields
        (``model``, ``stream``, ``user``) that the exact cache also drops; the
        resolved model is pinned via the explicit ``model`` argument.
        """
        skip = set(_FUZZY_FIELDS) | {"model", "stream", "user"}
        body = {k: v for k, v in payload.items() if k not in skip}
        blob = json.dumps(
            {"scope": scope, "model": model, "endpoint": endpoint, "body": body},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def query_text(payload: dict) -> str:
        """Flatten the fuzzy prompt fields to a single string to embed."""
        parts: list[str] = []
        for msg in payload.get("messages") or []:
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content")
                if isinstance(content, list):
                    content = "".join(
                        str(p.get("text") or p.get("content") or "")
                        if isinstance(p, dict)
                        else str(p)
                        for p in content
                    )
                parts.append(f"{role}: {content if content is not None else ''}")
        return "\n".join(parts)

    # --- read / write ----------------------------------------------------
    def _k(self, bucket: str) -> str:
        return f"{_SEM_PREFIX}:{bucket}"

    async def lookup(self, bucket: str, embedding: list[float]) -> tuple[dict, float] | None:
        """Return ``(result, similarity)`` for the best stored match ≥ threshold.

        Updates hit/miss stats. ``None`` on a miss (empty bucket or no match).
        """
        raw = await self.state.kv_get_json(self._k(bucket))
        best_val: dict | None = None
        best_sim = -1.0
        for entry in raw or []:
            emb = entry.get("e")
            sim = self.cosine(embedding, emb if isinstance(emb, list) else [])
            if sim > best_sim:
                best_sim = sim
                best_val = entry.get("v")
        if best_val is not None and best_sim >= self.threshold:
            self.hits += 1
            return best_val, best_sim
        self.misses += 1
        return None

    async def store(
        self, bucket: str, embedding: list[float], value: dict, *, ttl_override: float | None = None
    ) -> None:
        """Append ``(embedding, value)`` to a bucket, LRU-bounded by max_entries."""
        raw = await self.state.kv_get_json(self._k(bucket))
        entries = list(raw) if isinstance(raw, list) else []
        entries.append({"e": embedding, "v": value})
        if self.max_entries > 0 and len(entries) > self.max_entries:
            entries = entries[-self.max_entries :]
        effective = self.ttl if ttl_override is None else ttl_override
        ttl = effective if effective > 0 else None
        await self.state.kv_set_json(self._k(bucket), entries, ttl=ttl)
        self.stores += 1

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {
            "enabled": self.enabled,
            "model": self.model,
            "threshold": self.threshold,
            "ttl_seconds": self.ttl,
            "max_entries": self.max_entries,
            "require_resident": self.require_resident,
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "skipped_not_resident": self.skipped_not_resident,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }
