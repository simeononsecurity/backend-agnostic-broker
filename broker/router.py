"""Tiny router classifier for the ``auto`` model alias.

Only the virtual model id ``auto`` (``settings.router_alias``) is affected: a
request to ``auto`` is classified to exactly one concrete model the broker
currently exposes. Every other model id is addressed directly as before.

Pipeline (``AutoRouter.route``)::

    extract flags  ->  Stage-1 capability filter  ->  hard overrides (data)
                   ->  classifier (LLM or heuristic, constrained to candidates)
                   ->  validate choice  ->  deterministic fallback  ->  cache

The classifier never hard-codes model names: the candidate list is built live
from the registry at inference time, so adding/removing a model instantly changes
routing with no retrain. Three control knobs, all **data**: per-model ``tags`` /
``description`` / ``priority`` / ``cost_tier`` in the registry, the ``router``
policy block (default, policy prompt, weights), and hard ``overrides``.

Backends:
* ``HeuristicRouterBackend`` — pure-Python scorer (always available; the CI/no-GPU
  default and the fallback when the LLM is unavailable).
* ``LlamaCppRouterBackend`` — a small CPU GGUF via ``llama-cpp-python``, decoding
  constrained by a GBNF grammar to the exact candidate ids (output is 1-3 tokens;
  an invalid/empty answer is impossible). Loaded once and kept resident.
* ``CallableRouterBackend`` — wraps a function, used to simulate the LLM in tests.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ConfigError, ValidationError
from .registry import RouterConfig, RouterWeights

log = logging.getLogger("broker.router")


# --- request understanding ----------------------------------------------------


@dataclass(frozen=True)
class RequestFlags:
    has_images: bool = False
    has_tools: bool = False
    needs_json: bool = False
    approx_ctx_tokens: int = 0


@dataclass(frozen=True)
class Candidate:
    """A routable public model, with the metadata the classifier reasons over."""

    id: str
    tags: tuple[str, ...]
    description: str
    streaming: bool
    tools: bool
    vision: bool
    context_limit: int
    loaded: bool
    priority: int
    cost_tier: int


@dataclass
class RouteDecision:
    chosen: str
    via: str  # "override" | "llm" | "heuristic" | "fallback" | "cache"
    flags: RequestFlags
    candidates: list[str]
    latency_ms: float = 0.0


def _content_text(content: Any) -> str:
    """Flatten OpenAI message content (str, or list of parts) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text"):
                parts.append(str(p.get("text", "")))
        return " ".join(parts)
    return ""


_IMAGE_PART_TYPES = {"image_url", "input_image", "image"}


def extract_flags(payload: dict, max_chars: int = 2048) -> tuple[RequestFlags, str]:
    """Derive routing flags and the (truncated) last user message from a payload."""
    messages = payload.get("messages") or []
    has_images = False
    total_chars = 0
    for m in messages:
        c = m.get("content")
        total_chars += len(_content_text(c))
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") in _IMAGE_PART_TYPES:
                    has_images = True

    has_tools = bool(payload.get("tools")) or any(
        (m.get("role") == "tool") or m.get("tool_calls") for m in messages
    )
    rf = payload.get("response_format")
    needs_json = isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema")

    # Cheap char/4 token estimate plus requested output budget.
    approx = total_chars // 4 + int(payload.get("max_tokens") or 0)

    last_user = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user = _content_text(m.get("content"))
            break
    last_user = last_user[:max_chars]

    return (
        RequestFlags(
            has_images=has_images,
            has_tools=has_tools,
            needs_json=needs_json,
            approx_ctx_tokens=approx,
        ),
        last_user,
    )


# --- classifier backends ------------------------------------------------------


class RouterUnavailable(RuntimeError):
    """Raised by a backend that cannot classify (e.g. model/library missing)."""


class RouterBackend:
    name = "base"

    def choose(
        self, flags: RequestFlags, message: str, candidates: list[Candidate], cfg: RouterConfig
    ) -> str:
        raise NotImplementedError


# Tag -> keyword synonyms for intent matching. The registry's ``tags`` are the
# vocabulary; this just broadens matching for common classes (data, easy to edit).
_TAG_KEYWORDS: dict[str, list[str]] = {
    "code": [
        "code",
        "coding",
        "program",
        "function",
        "bug",
        "debug",
        "python",
        "javascript",
        "typescript",
        "java",
        "c++",
        "rust",
        "golang",
        "sql",
        "regex",
        "compile",
        "refactor",
        "stack trace",
        "traceback",
        "algorithm",
    ],
    "vision": ["image", "photo", "picture", "screenshot", "diagram", "chart", "vision"],
    "reasoning": ["reason", "prove", "theorem", "logic", "step by step", "deduce", "analyze"],
    "math": ["math", "calculate", "equation", "integral", "derivative", "algebra", "arithmetic"],
    "creative": ["poem", "story", "novel", "lyrics", "creative", "essay", "haiku", "write a"],
    "long-context": ["summarize", "summarise", "transcript", "whole document", "entire"],
    "json": ["json", "schema", "structured output"],
    "tools": ["tool", "function call", "api call", "call the"],
}


def _keywords_for(tag: str) -> list[str]:
    return _TAG_KEYWORDS.get(tag.lower(), [tag.lower()])


class HeuristicRouterBackend(RouterBackend):
    """Deterministic scorer: capability match dominates, then cost/loaded/priority."""

    name = "heuristic"

    def score(self, flags: RequestFlags, message: str, c: Candidate, w: RouterWeights) -> float:
        msg = message.lower()
        tags = {t.lower() for t in c.tags}
        s = 0.0
        # Intent match: reward tags whose keywords appear in the message.
        for tag in tags:
            if any(kw in msg for kw in _keywords_for(tag)):
                s += w.capability
        # Flag-driven capability boosts.
        if flags.has_images and "vision" in tags:
            s += w.capability
        if flags.needs_json and ("json" in tags or "tools" in tags):
            s += w.capability * 0.5
        if flags.has_tools and "tools" in tags:
            s += w.capability * 0.5
        if flags.approx_ctx_tokens > 8000 and "long-context" in tags:
            s += w.capability
        # Prefer cheaper, already-loaded, higher-priority models (tie-breakers).
        s += (5 - c.cost_tier) * w.cost
        if c.loaded:
            s += w.prefer_loaded
        s += c.priority * w.priority
        return s

    def choose(
        self, flags: RequestFlags, message: str, candidates: list[Candidate], cfg: RouterConfig
    ) -> str:
        w = cfg.weights
        # Deterministic: score desc, then cheaper, loaded, higher priority, id asc.
        ranked = sorted(
            candidates,
            key=lambda c: (
                -self.score(flags, message, c, w),
                c.cost_tier,
                0 if c.loaded else 1,
                -c.priority,
                c.id,
            ),
        )
        return ranked[0].id


class CallableRouterBackend(RouterBackend):
    """Wrap a function ``(flags, message, candidates) -> id`` (used in tests)."""

    name = "scripted"

    def __init__(self, fn: Callable[[RequestFlags, str, list[Candidate]], str]) -> None:
        self._fn = fn

    def choose(
        self, flags: RequestFlags, message: str, candidates: list[Candidate], cfg: RouterConfig
    ) -> str:
        return self._fn(flags, message, candidates)


_DEFAULT_POLICY = (
    "You are a routing classifier for an AI model broker. Given a user request, "
    "its derived flags, and a list of available models (id, tags, capabilities, "
    "loaded), choose the single best model id to handle the request. Prefer the "
    "cheapest model that is clearly capable; prefer an already-loaded model on a "
    "tie. Reply with ONLY the model id, nothing else."
)


def _gbnf_grammar(ids: list[str]) -> str:
    """GBNF restricting output to exactly one of the candidate ids."""

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    alts = " | ".join(f'"{esc(i)}"' for i in ids)
    return f"root ::= ({alts})\n"


def _render_candidates(candidates: list[Candidate]) -> str:
    rows = []
    for c in candidates:
        caps = ",".join(
            k for k, v in (("stream", c.streaming), ("tools", c.tools), ("vision", c.vision)) if v
        )
        rows.append(
            f"- {c.id} | tags: {', '.join(c.tags) or '-'} | caps: {caps or '-'} | "
            f"ctx: {c.context_limit} | cost: {c.cost_tier} | loaded: {str(c.loaded).lower()}"
            + (f" | {c.description}" if c.description else "")
        )
    return "\n".join(rows)


class LlamaCppRouterBackend(RouterBackend):
    """Small CPU GGUF classifier (llama-cpp-python) constrained by a GBNF grammar."""

    name = "llm"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._llm: Any = None  # lazily loaded, then kept resident

    @staticmethod
    def available(settings: Settings) -> bool:
        import importlib.util

        if not settings.router_model_path:
            return False
        if not Path(settings.router_model_path).exists():
            return False
        return importlib.util.find_spec("llama_cpp") is not None

    def _ensure_loaded(self) -> None:
        if self._llm is not None:
            return
        try:
            from llama_cpp import Llama
        except Exception as exc:  # noqa: BLE001
            raise RouterUnavailable(f"llama_cpp not importable: {exc}") from exc
        path = self.settings.router_model_path
        if not path or not Path(path).exists():
            raise RouterUnavailable(f"router model not found: {path!r}")
        kwargs: dict[str, Any] = {
            "model_path": path,
            "n_ctx": self.settings.router_model_ctx,
            "verbose": False,
        }
        if self.settings.router_threads:
            kwargs["n_threads"] = self.settings.router_threads
        self._llm = Llama(**kwargs)  # CPU; resident for the process lifetime
        log.info("router classifier loaded: %s", path)

    def choose(
        self, flags: RequestFlags, message: str, candidates: list[Candidate], cfg: RouterConfig
    ) -> str:
        self._ensure_loaded()
        from llama_cpp import LlamaGrammar

        ids = [c.id for c in candidates]
        grammar = LlamaGrammar.from_string(_gbnf_grammar(ids))
        system = cfg.policy or _DEFAULT_POLICY
        user = (
            f"Flags: has_images={flags.has_images}, has_tools={flags.has_tools}, "
            f"needs_json={flags.needs_json}, approx_tokens={flags.approx_ctx_tokens}\n"
            f"Available models:\n{_render_candidates(candidates)}\n\n"
            f"Request:\n{message}\n\nModel id:"
        )
        out = self._llm.create_chat_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            grammar=grammar,
            max_tokens=16,
            temperature=0.0,
        )
        return (out["choices"][0]["message"]["content"] or "").strip()


# --- orchestrator -------------------------------------------------------------


class AutoRouter:
    """Resolve the ``auto`` alias to a concrete model for a given request."""

    def __init__(
        self,
        manager: Any,
        settings: Settings,
        cfg: RouterConfig,
        backend: RouterBackend | None = None,
    ) -> None:
        self.manager = manager
        self.settings = settings
        self.cfg = cfg
        self.backend = backend or self._build_backend(settings)
        self._cache: OrderedDict[tuple, str] = OrderedDict()
        self.decisions: deque[dict] = deque(maxlen=50)

    @staticmethod
    def _build_backend(settings: Settings) -> RouterBackend:
        mode = settings.router_backend
        if mode == "heuristic":
            return HeuristicRouterBackend()
        if mode == "llm":
            if not LlamaCppRouterBackend.available(settings):
                raise ConfigError(
                    "router_backend='llm' but the classifier is unavailable",
                    remediation="install llama-cpp-python and set BROKER_ROUTER_MODEL_PATH, "
                    "or use router_backend=auto/heuristic",
                )
            return LlamaCppRouterBackend(settings)
        # "auto": prefer the LLM when present, else the heuristic.
        if LlamaCppRouterBackend.available(settings):
            return LlamaCppRouterBackend(settings)
        return HeuristicRouterBackend()

    # --- candidate building (Stage-1 capability filter) ------------------
    def build_candidates(self, flags: RequestFlags) -> list[Candidate]:
        out: list[Candidate] = []
        for name, members in self.manager.pools.items():
            if name == self.settings.router_alias:
                continue
            entries = [m.entry for m in members]
            if not all(e.router_eligible for e in entries):
                continue
            vision = all(e.supports_vision for e in entries)
            tools = all(e.supports_tools for e in entries)
            streaming = all(e.supports_streaming for e in entries)
            context_limit = min(e.context_limit for e in entries)
            # Stage-1: only models that can actually serve this request.
            if flags.has_images and not vision:
                continue
            if flags.has_tools and not tools:
                continue
            if flags.approx_ctx_tokens > context_limit:
                continue
            tags = tuple(sorted({t for e in entries for t in e.tags}))
            description = next((e.description for e in entries if e.description), "") or ""
            out.append(
                Candidate(
                    id=name,
                    tags=tags,
                    description=description,
                    streaming=streaming,
                    tools=tools,
                    vision=vision,
                    context_limit=context_limit,
                    loaded=any(m.machine.is_active() for m in members),
                    priority=max(e.priority for e in entries),
                    cost_tier=min(e.cost_tier for e in entries),
                )
            )
        return out

    # --- overrides -------------------------------------------------------
    @staticmethod
    def _override_matches(when, flags: RequestFlags, message: str) -> bool:
        if when.has_images is not None and when.has_images != flags.has_images:
            return False
        if when.has_tools is not None and when.has_tools != flags.has_tools:
            return False
        if when.needs_json is not None and when.needs_json != flags.needs_json:
            return False
        if when.min_ctx_tokens is not None and flags.approx_ctx_tokens < when.min_ctx_tokens:
            return False
        if when.regex is not None and not re.search(when.regex, message):
            return False
        return True

    def _default(self, candidates: list[Candidate], flags: RequestFlags, message: str) -> str:
        """Deterministic fallback: configured default if it's a valid candidate,
        else the heuristic's best (always capability-valid)."""
        ids = {c.id for c in candidates}
        preferred = self.settings.router_default_model or self.cfg.default
        if preferred and preferred in ids:
            return preferred
        return HeuristicRouterBackend().choose(flags, message, candidates, self.cfg)

    # --- main ------------------------------------------------------------
    def route(self, payload: dict) -> RouteDecision:
        t0 = time.monotonic()
        flags, message = extract_flags(payload, self.settings.router_max_input_chars)
        candidates = self.build_candidates(flags)
        if not candidates:
            raise ValidationError(
                "no registered model can satisfy this request",
                remediation="check required capabilities (images/tools/context) "
                "against your registry, or add a capable model",
            )
        cand_ids = [c.id for c in candidates]

        cache_key = (flags, tuple(sorted(cand_ids)), message[:256])
        if cache_key in self._cache:
            chosen = self._cache[cache_key]
            self._cache.move_to_end(cache_key)
            return self._record(chosen, "cache", flags, cand_ids, t0, cached=True)

        # Hard overrides (data) bypass the classifier when their target is capable.
        for ov in self.cfg.overrides:
            if self._override_matches(ov.when, flags, message) and ov.use in cand_ids:
                chosen = ov.use
                self._store(cache_key, chosen)
                return self._record(chosen, "override", flags, cand_ids, t0)

        via = self.backend.name
        try:
            chosen = self.backend.choose(flags, message, candidates, self.cfg)
        except RouterUnavailable as exc:
            log.warning("router backend unavailable (%s); using heuristic", exc)
            chosen = HeuristicRouterBackend().choose(flags, message, candidates, self.cfg)
            via = "heuristic"
        except Exception as exc:  # noqa: BLE001
            log.warning("router backend error (%s); using fallback default", exc)
            chosen = ""

        if chosen not in cand_ids:
            chosen = self._default(candidates, flags, message)
            via = "fallback"

        self._store(cache_key, chosen)
        return self._record(chosen, via, flags, cand_ids, t0)

    # --- bookkeeping -----------------------------------------------------
    def _store(self, key: tuple, chosen: str) -> None:
        self._cache[key] = chosen
        self._cache.move_to_end(key)
        cap = self.settings.router_cache_size
        while cap > 0 and len(self._cache) > cap:
            self._cache.popitem(last=False)

    def _record(
        self,
        chosen: str,
        via: str,
        flags: RequestFlags,
        cand_ids: list[str],
        t0: float,
        cached: bool = False,
    ) -> RouteDecision:
        latency = (time.monotonic() - t0) * 1000.0
        entry = {
            "chosen": chosen,
            "via": via,
            "flags": flags.__dict__,
            "candidates": cand_ids,
            "latency_ms": round(latency, 2),
            "cached": cached,
        }
        self.decisions.append(entry)
        log.info(
            "router: auto -> %s via %s (%d cands, %.1fms)", chosen, via, len(cand_ids), latency
        )
        return RouteDecision(
            chosen=chosen, via=via, flags=flags, candidates=cand_ids, latency_ms=latency
        )

    def status(self) -> dict:
        return {
            "enabled": True,
            "alias": self.settings.router_alias,
            "backend": self.backend.name,
            "cache_size": len(self._cache),
            "recent_decisions": list(self.decisions)[-10:],
        }


__all__ = [
    "AutoRouter",
    "Candidate",
    "CallableRouterBackend",
    "HeuristicRouterBackend",
    "LlamaCppRouterBackend",
    "RequestFlags",
    "RouteDecision",
    "RouterBackend",
    "RouterUnavailable",
    "extract_flags",
]
