"""Cache-aware pool routing: approximate prefix affinity + load-aware scoring.

ADR 0010. This module turns a request's prompt into a chain of fixed-size block
hashes, keeps a bounded ``prefix-block -> member(s)`` index on the ADR 0009
:class:`~broker.state.StateStore` (so it is shared across replicas when Redis is
configured), and exposes the matched-prefix fraction per pool member. The manager
combines that fraction with a load term to pick a member (see
``ModelManager._select_cache_aware``).

Design (prior art, see ``docs/research/adr-0010-cache-aware-pool-routing.md``):

* **Chained block hashing** (vLLM automatic prefix caching): each block's hash
  folds in its parent's, so a single block hash identifies the *entire* prefix up
  to it. Longest-prefix match therefore reduces to "count the leading consecutive
  blocks a member holds". Only *full* blocks are hashed (a trailing partial block
  is ignored, exactly like vLLM).
* **Characters, not tokens** (SGLang approximate radix tree): the broker owns no
  tokenizer and must stay backend-agnostic, so blocks are sliced from the
  normalized prompt text. ``block_chars`` is the granularity.
* **Approximate index, router-maintained**: the index records what each member
  *served*, with a per-replica LRU size bound plus a TTL approximating KV-cache
  residency. Precise KV-event (ZMQ) indexing is deliberately out of scope.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from typing import Any

from .state import StateStore

#: Hard cap on prefix blocks considered per request, so a pathologically long
#: prompt cannot make a routing decision unbounded. ~MAX_PREFIX_BLOCKS*block_chars
#: characters of prefix participate in matching.
MAX_PREFIX_BLOCKS = 512

#: Max distinct members remembered per prefix block (most-recent wins). A popular
#: shared prefix (e.g. a system prompt) can legitimately be warm on several
#: members; this bounds the per-key fan-out.
MAX_MEMBERS_PER_BLOCK = 8


def _content_to_text(content: Any) -> str:
    """Flatten an OpenAI message ``content`` (str or list of parts) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                # text parts: {"type": "text", "text": "..."}; ignore non-text
                # (image) parts for prefix purposes.
                t = part.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(part, str):
                parts.append(part)
        return " ".join(parts)
    if content is None:
        return ""
    return str(content)


def normalize_prompt(payload: dict[str, Any]) -> str:
    """Build a canonical prompt-prefix string from any inference payload.

    Handles chat ``messages`` (system + turns, in order), completion ``prompt``,
    and responses/embeddings ``input``. Deterministic: the same logical prompt
    always yields the same string, so two requests sharing a system prompt +
    leading turns share a block prefix.
    """
    messages = payload.get("messages")
    if isinstance(messages, list):
        lines: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role", ""))
            lines.append(f"{role}:{_content_to_text(msg.get('content', ''))}")
        return "\n".join(lines)
    prompt = payload.get("prompt")
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        return "\n".join(str(p) for p in prompt)
    inp = payload.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        return "\n".join(str(p) for p in inp)
    return ""


def block_hashes(text: str, block_chars: int) -> list[str]:
    """Split ``text`` into fixed-size blocks and chain-hash them.

    ``h_i = sha256(h_{i-1} + block_i)`` truncated to 16 hex chars. Only full
    blocks are emitted (a trailing partial block is dropped, matching vLLM's
    "only cache full blocks"). An empty / sub-block prompt yields ``[]`` (no
    match, so routing falls back to least_busy).
    """
    if block_chars < 1 or not text:
        return []
    n_full = min(len(text) // block_chars, MAX_PREFIX_BLOCKS)
    hashes: list[str] = []
    parent = ""
    for i in range(n_full):
        chunk = text[i * block_chars : (i + 1) * block_chars]
        digest = hashlib.sha256((parent + chunk).encode("utf-8")).hexdigest()[:16]
        hashes.append(digest)
        parent = digest
    return hashes


class CacheRouter:
    """Maintains the prefix->member index and scores prefix affinity per member.

    One instance per :class:`~broker.manager.ModelManager`. All index I/O goes
    through the injected :class:`~broker.state.StateStore` so a Redis deployment
    shares the index across replicas. The LRU size bound is tracked per replica
    (only keys this replica wrote); the TTL is the authoritative cross-replica
    bound.
    """

    def __init__(
        self,
        state: StateStore,
        *,
        block_chars: int,
        beta: float,
        max_entries: int,
        ttl_seconds: float,
    ) -> None:
        self.state = state
        self.block_chars = block_chars
        self.beta = beta
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lru: OrderedDict[str, None] = OrderedDict()
        self._lookups = 0
        self._hits = 0  # lookups where >=1 member had a non-zero matched fraction

    # --- key helpers -----------------------------------------------------
    @staticmethod
    def _key(pool: str, block_hash: str) -> str:
        return f"prefix:{pool}:{block_hash}"

    def prefix_blocks(self, payload: dict[str, Any]) -> list[str]:
        """Chained block hashes for a payload's normalized prompt prefix."""
        return block_hashes(normalize_prompt(payload), self.block_chars)

    # --- lookup ----------------------------------------------------------
    async def _members(self, pool: str, block_hash: str) -> set[str]:
        raw = await self.state.kv_get_json(self._key(pool, block_hash))
        if not raw:
            return set()
        return set(raw)

    async def match_fractions(
        self, pool: str, blocks: list[str], aliases: list[str]
    ) -> dict[str, float]:
        """Return ``{alias: matched_fraction}`` for members with a non-zero match.

        For each alias, ``matched_fraction`` is the number of *leading consecutive*
        blocks that alias holds divided by the total block count. Because hashes
        are chained, a member holding block ``i`` implies it served blocks
        ``0..i`` as a contiguous prefix, so this is a true longest-prefix match.
        The scan reads each block at most once and stops as soon as every
        candidate has diverged.
        """
        self._lookups += 1
        n = len(blocks)
        if n == 0 or not aliases:
            return {}
        consec = dict.fromkeys(aliases, 0)
        still = set(aliases)
        for i, h in enumerate(blocks):
            if not still:
                break
            holders = await self._members(pool, h)
            for alias in list(still):
                if alias in holders:
                    consec[alias] = i + 1
                else:
                    still.discard(alias)
        fractions = {a: consec[a] / n for a in aliases if consec[a] > 0}
        if fractions:
            self._hits += 1
        return fractions

    # --- record ----------------------------------------------------------
    async def record(self, pool: str, blocks: list[str], member: str) -> None:
        """Record that ``member`` served ``blocks`` (called after a success)."""
        if not blocks:
            return
        ttl = self.ttl_seconds or None
        for h in blocks:
            key = self._key(pool, h)
            existing = await self.state.kv_get_json(key)
            members = list(existing) if isinstance(existing, list) else []
            if member in members:
                members.remove(member)
            members.append(member)
            if len(members) > MAX_MEMBERS_PER_BLOCK:
                members = members[-MAX_MEMBERS_PER_BLOCK:]
            await self.state.kv_set_json(key, members, ttl=ttl)
            self._lru[key] = None
            self._lru.move_to_end(key)
        while self.max_entries > 0 and len(self._lru) > self.max_entries:
            old, _ = self._lru.popitem(last=False)
            await self.state.kv_delete(old)

    # --- observability ---------------------------------------------------
    def stats(self) -> dict[str, Any]:
        rate = (self._hits / self._lookups) if self._lookups else 0.0
        return {
            "index_size": len(self._lru),
            "lookups": self._lookups,
            "hits": self._hits,
            "hit_rate": round(rate, 4),
            "block_chars": self.block_chars,
            "beta": self.beta,
        }
