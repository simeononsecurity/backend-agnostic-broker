"""Declarative model registry.

The registry is a YAML (or JSON) file describing every model the broker can
serve. Each entry is validated by pydantic at load time so misconfiguration
fails fast with a clear message. The registry is the *only* source of truth for
what may be loaded — requests select a model by its public ``alias`` and can
never introduce a new backend, executable, or path.
"""

from __future__ import annotations

import json
import os
import re
from enum import Enum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from .errors import ConfigError

# ``${VAR}`` or ``${VAR:-default}`` references expanded from the environment at
# load time. This lets a committed registry stay free of secrets/host details
# (e.g. ``base_url: ${OLLAMA_BASE_URL}``) while the real endpoint is injected via
# env/.env/Docker secret. Only this exact syntax is expanded; a literal ``$`` is
# left untouched. Expansion is applied to parsed string *values* only — never to
# comments or keys — so documentation comments may safely mention ``${VAR}``.
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

# Allowed characters for a placement label (e.g. "local", "region:eu", "gpu-a").
_PLACEMENT_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _expand_env_str(text: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` references against ``os.environ``.

    Raises :class:`ConfigError` if a referenced variable is unset and no default
    is supplied, so misconfiguration fails fast instead of silently producing an
    empty/garbage URL.
    """

    def _sub(m: re.Match[str]) -> str:
        name, default = m.group(1), m.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ConfigError(
            f"registry references unset environment variable ${{{name}}}",
            remediation=(f"set {name} (e.g. in .env) or give it a default: ${{{name}:-...}}"),
        )

    return _ENV_REF_RE.sub(_sub, text)


def _expand_env(obj: object) -> object:
    """Recursively expand env references in string values of a parsed structure.

    Only string leaves are expanded; dict keys, comments (already dropped by the
    parser), and non-string scalars are returned unchanged.
    """
    if isinstance(obj, str):
        return _expand_env_str(obj)
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    return obj


class BackendType(str, Enum):
    FAKE = "fake"
    OLLAMA = "ollama"
    VLLM = "vllm"
    LLAMACPP = "llamacpp"
    STRATA = "strata"


class GPURequirements(BaseModel):
    """GPU placement controls for a model."""

    required: bool = False
    # Device ordinals, e.g. [0, 1]. Empty => backend default / CPU.
    device_ids: list[int] = Field(default_factory=list)
    # Per-GPU VRAM fraction for runtimes that accept it (e.g. vLLM).
    gpu_memory_fraction: float | None = None
    # Tensor split across GPUs (llama.cpp / strata), e.g. [0.5, 0.5].
    tensor_split: list[float] = Field(default_factory=list)
    # Number of layers to offload to GPU (llama.cpp n-gpu-layers), or "all".
    layer_split: str | None = None
    # Tensor-parallel / pipeline-parallel degrees (vLLM).
    tensor_parallel_size: int | None = None
    pipeline_parallel_size: int | None = None

    @field_validator("gpu_memory_fraction")
    @classmethod
    def _frac(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 < v <= 1.0):
            raise ValueError("gpu_memory_fraction must be in (0, 1]")
        return v


class MTPConfig(BaseModel):
    """Multi-Token-Prediction / speculative controls (backend-dependent)."""

    enabled: bool = False
    num_speculative_tokens: int | None = None
    draft_model: str | None = None


class ReadinessProbe(BaseModel):
    """How to detect that a broker-managed runtime is ready to serve."""

    # "http" polls path on the backend base URL; "log" waits for a log marker.
    kind: str = "http"
    path: str = "/health"
    expect_status: int = 200
    log_marker: str | None = None
    interval_seconds: float = 0.5
    timeout_seconds: float = 120.0


class ModelEntry(BaseModel):
    """A single registry entry. See docs/model-registry.md for field semantics."""

    model_config = {"extra": "forbid"}

    alias: str
    backend: BackendType
    runtime_model_id: str
    model_path: str | None = None
    # Optional load-balancing pool. When set, every entry sharing this pool name
    # is a member of one logical model that clients call by the POOL name; the
    # broker routes each request to one member (least-busy) with failover. When
    # unset, the entry stands alone and is addressed by its ``alias``.
    pool: str | None = None
    context_limit: int = 8192
    output_limit: int = 2048

    gpu: GPURequirements = Field(default_factory=GPURequirements)
    mtp: MTPConfig = Field(default_factory=MTPConfig)

    # Broker-managed process controls.
    base_url: str | None = None  # where the runtime serves (http backends)
    startup_command: list[str] = Field(default_factory=list)  # argv template
    readiness: ReadinessProbe = Field(default_factory=ReadinessProbe)
    unload_command: list[str] = Field(default_factory=list)  # optional argv template

    idle_timeout_seconds: float = 300.0
    request_timeout_seconds: float = 300.0
    startup_timeout_seconds: float = 120.0

    # Capability flags surfaced via /v1/models.
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_vision: bool = False
    # Whether this model can serve POST /v1/rerank (ADR 0013). A cross-encoder /
    # reranker runtime (vLLM or llama.cpp with a reranker model) sets this true;
    # requesting rerank on a model without it returns 400 (fail-closed).
    supports_rerank: bool = False

    # Per-model override of the global keep-alive (ollama) behavior.
    keep_alive: str | None = None

    # --- Auto-router metadata (used only by the "auto" alias) ---
    # Free-form class tags describing what this model is good at, e.g. "code",
    # "vision", "reasoning", "cheap", "fast", "long-context", "tools", "json".
    # The router matches request intent against these; they are the main control
    # surface (data, not code) and need no retrain to change.
    tags: list[str] = Field(default_factory=list)
    # One-line description shown to the router classifier's candidate table.
    description: str | None = None
    # Whether the "auto" router may ever select this model.
    router_eligible: bool = True
    # Relative preference for tie-breaks (higher = preferred). Also the user-facing
    # "priority" knob for auto routing.
    priority: int = 0
    # Relative cost/size: 1 (cheapest/smallest) .. 5 (most expensive). The router
    # prefers the cheapest model that still satisfies the request.
    cost_tier: int = 2

    # --- Placement / data-residency labels (ADR 0011, fail-closed) ---
    # Free-form labels describing where this model may process data, e.g.
    # "local", "trusted", "region:eu". A request that carries a placement
    # requirement (x-broker-placement header or BROKER_DEFAULT_PLACEMENT) is only
    # served by models/pool members that carry EVERY required label; if none
    # qualify the broker returns 403 placement_denied and NEVER spills to a
    # non-compliant model. Empty => the model carries no placement labels and so
    # satisfies only the empty (no-constraint) requirement.
    placement: list[str] = Field(default_factory=list)

    @field_validator("cost_tier")
    @classmethod
    def _cost_tier(cls, v: int) -> int:
        if not (1 <= v <= 5):
            raise ValueError("cost_tier must be between 1 and 5")
        return v

    @field_validator("placement")
    @classmethod
    def _placement(cls, v: list[str]) -> list[str]:
        out = []
        for label in v:
            label = str(label).strip()
            if not label or not _PLACEMENT_LABEL_RE.match(label):
                raise ValueError(
                    f"invalid placement label {label!r}: use letters, digits, "
                    "'.', '-', '_', ':' (e.g. 'local', 'region:eu')"
                )
            out.append(label)
        return out

    @property
    def placement_set(self) -> frozenset[str]:
        """Labels this model carries, as a set (for subset/superset checks)."""
        return frozenset(self.placement)

    @field_validator("alias", "runtime_model_id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v.strip()

    @field_validator("pool")
    @classmethod
    def _pool(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @property
    def public_name(self) -> str:
        """The name clients address this entry by (pool if pooled, else alias)."""
        return self.pool or self.alias

    @field_validator("tensor_split", check_fields=False)
    @classmethod
    def _noop(cls, v):  # pragma: no cover - placeholder for forward-compat
        return v


class RouterWhen(BaseModel):
    """Match conditions for a hard router override (all given conditions must hold)."""

    model_config = {"extra": "forbid"}

    has_images: bool | None = None
    has_tools: bool | None = None
    needs_json: bool | None = None
    min_ctx_tokens: int | None = None
    # Regex matched (search, case-insensitive handled by the pattern) against the
    # last user message.
    regex: str | None = None

    @field_validator("regex")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v is None:
            return None
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"invalid override regex {v!r}: {exc}") from exc
        return v


class RouterOverride(BaseModel):
    """A deterministic rule that bypasses the LLM classifier when it matches."""

    model_config = {"extra": "forbid"}

    when: RouterWhen
    use: str  # target public model id


class RouterWeights(BaseModel):
    """Scoring weights for the heuristic router (capability dominates by default)."""

    model_config = {"extra": "forbid"}

    capability: float = 3.0
    cost: float = 1.0
    prefer_loaded: float = 1.0
    priority: float = 1.0
    # When true (ADR 0019 opt-in), the cost term in the heuristic score is
    # normalized to [0, 1]·cost so one capability match always outweighs price.
    # Default is FALSE = the v0.5.0 un-normalized term ``(5 - cost_tier)·cost``,
    # because on the orchestrator's independent prompt set the normalized term
    # produced IDENTICAL accuracy (it only lifted the author's in-sample sets), so
    # the unjustified default change is reverted and the behavior is opt-in. See
    # docs/auto-router.md / ADR 0019.
    cost_normalized: bool = False


class ScorerConfig(BaseModel):
    """Calibrated reliability scorer for cascade escalation (ADR 0019, opt-in).

    When ``enabled``, the cheap hop's answer is reduced to a single reliability
    score in ``[0, 1]`` (higher = keep); the hop escalates when ``score <
    threshold``. The score has two parts:

    * **Graded features** (the tunable part): answer-length adequacy and, when the
      response carries token logprobs, mean token probability — a weighted mean.
    * **Hard safety gates** (parity with the boolean signals): a refusal match or
      a truncation (``finish_reason == "length"``) caps the score to 0 so it
      always escalates, exactly like v0.5.0's booleans. Each gate is toggleable.

    This gives operators one calibrated threshold to slide (how short / how
    unconfident is "too unreliable") instead of juggling several booleans, while
    keeping the hard refusal/truncation safety behavior. Default-off: the boolean
    signals remain the default, so v0.5.0 behavior is unchanged.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    # Escalate when the reliability score is strictly below this threshold.
    threshold: float = 0.5
    # Answer length (chars, stripped) at which the length feature saturates to 1.
    target_chars: int = 200
    # Non-negative weights for the graded features (length, logprob).
    weight_length: float = 1.0
    weight_logprob: float = 0.0
    # Hard safety gates that cap the score to 0 (always escalate) when they fire.
    gate_refusal: bool = True
    gate_truncation: bool = True

    @field_validator("threshold")
    @classmethod
    def _threshold(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("cascade.scorer.threshold must be in [0, 1]")
        return v

    @field_validator("target_chars")
    @classmethod
    def _target_chars(cls, v: int) -> int:
        if v < 1:
            raise ValueError("cascade.scorer.target_chars must be >= 1")
        return v

    @field_validator("weight_length", "weight_logprob")
    @classmethod
    def _weight(cls, v: float) -> float:
        if v < 0:
            raise ValueError("cascade.scorer weights must be >= 0")
        return v


class CascadeConfig(BaseModel):
    """Escalation signals for ``router.mode: cascade`` (ADR 0011).

    All signals are data-driven and pluggable; each is checked against the
    cheapest-capable model's *actual* response and, if any fires, the request
    escalates once to a stronger capable model.
    """

    model_config = {"extra": "forbid"}

    # Escalate when the first hop was truncated (OpenAI finish_reason == "length").
    escalate_on_length: bool = True
    # Escalate when the first hop produced an empty / too-short answer.
    escalate_on_empty: bool = True
    # The minimum acceptable answer length (characters, stripped); shorter => escalate.
    min_chars: int = 1
    # Escalate when the first hop looks like a refusal (any regex matches, searched
    # case-insensitively via the pattern) against the answer text.
    escalate_on_refusal: bool = True
    refusal_patterns: list[str] = Field(
        default_factory=lambda: [
            r"(?i)\bI\s+(?:cannot|can't|can not|am unable to|won'?t)\b",
            r"(?i)\bI'?m sorry,? but\b",
            r"(?i)\bas an AI\b",
        ]
    )

    # --- ADR 0019 ---
    # Streaming behavior for a cascade route:
    #   "fallback_single" (default, = v0.5.0): a streaming request is served by
    #     the single strong pick (a live stream can't be re-scored).
    #   "buffer": buffer the cheap hop fully, score it, then REPLAY it as a stream
    #     or STREAM the stronger hop — one model's output per stream, never mixed.
    #     Chat endpoint only; completions/responses stay fallback_single.
    stream_mode: str = "fallback_single"
    # Optional calibrated reliability scorer (opt-in) that replaces the boolean
    # escalate_on_* signals with one tunable threshold when scorer.enabled.
    scorer: ScorerConfig = Field(default_factory=ScorerConfig)

    @field_validator("stream_mode")
    @classmethod
    def _stream_mode(cls, v: str) -> str:
        if v not in ("fallback_single", "buffer"):
            raise ValueError("cascade.stream_mode must be 'fallback_single' or 'buffer'")
        return v

    @field_validator("refusal_patterns")
    @classmethod
    def _patterns(cls, v: list[str]) -> list[str]:
        for pat in v:
            try:
                re.compile(pat)
            except re.error as exc:
                raise ValueError(f"invalid refusal regex {pat!r}: {exc}") from exc
        return v


class RouterConfig(BaseModel):
    """Data-driven policy for the ``auto`` alias (all knobs are config, not code)."""

    model_config = {"extra": "forbid"}

    # Deterministic fallback model id used when the classifier returns an invalid
    # or empty choice. If unset, the router falls back to its best heuristic pick.
    default: str | None = None
    # Optional system-prompt / routing-policy override for the LLM classifier.
    policy: str | None = None
    # Hard overrides, evaluated in order; the first match bypasses the LLM.
    overrides: list[RouterOverride] = Field(default_factory=list)
    weights: RouterWeights = Field(default_factory=RouterWeights)

    # --- ADR 0011 ---
    # "single" (default): one-shot classification, unchanged v0.4.0 behavior.
    # "cascade": serve the cheapest capable model first, escalate once on a signal.
    mode: str = "single"
    cascade: CascadeConfig = Field(default_factory=CascadeConfig)
    # Default per-request latency budget (ms) for latency-aware routing; a request
    # header (x-broker-latency-budget-ms) or BROKER_ROUTER_LATENCY_BUDGET_MS wins.
    latency_budget_ms: float | None = None

    @field_validator("mode")
    @classmethod
    def _mode(cls, v: str) -> str:
        if v not in ("single", "cascade"):
            raise ValueError("router.mode must be 'single' or 'cascade'")
        return v

    @field_validator("latency_budget_ms")
    @classmethod
    def _budget(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("router.latency_budget_ms must be > 0")
        return v


class ModelPricing(BaseModel):
    """Per-model price, used to charge per-key cost budgets (ADR 0008).

    Prices are in **USD per million tokens**, split by input (prompt) and output
    (completion). Keyed by the *public* model name (pool or alias) in the registry
    ``pricing`` block. Models without an entry are treated as free (cost budgets
    are only enforced for priced models).
    """

    model_config = {"extra": "forbid"}

    input_per_million: float = 0.0
    output_per_million: float = 0.0

    @field_validator("input_per_million", "output_per_million")
    @classmethod
    def _nonneg(cls, v: float) -> float:
        if v < 0:
            raise ValueError("pricing must be >= 0")
        return v


class Registry(BaseModel):
    models: list[ModelEntry]
    # Optional policy for the "auto" router alias.
    router: RouterConfig = Field(default_factory=RouterConfig)
    # Optional per-model prices (USD / million tokens) for per-key cost budgets
    # (ADR 0008), keyed by public model name. Empty => cost budgets are inactive.
    pricing: dict[str, ModelPricing] = Field(default_factory=dict)

    def by_alias(self) -> dict[str, ModelEntry]:
        out: dict[str, ModelEntry] = {}
        for m in self.models:
            if m.alias in out:
                raise ConfigError(
                    f"duplicate model alias in registry: {m.alias!r}",
                    remediation="aliases must be unique",
                )
            out[m.alias] = m
        return out

    def public_groups(self) -> dict[str, list[ModelEntry]]:
        """Group entries by the public name clients address (pool or alias).

        A name with more than one member is a load-balancing pool; every member
        of such a group must declare ``pool`` explicitly, so a standalone
        ``alias`` can never silently collide with another entry's pool name.
        """
        groups: dict[str, list[ModelEntry]] = {}
        for m in self.models:
            groups.setdefault(m.public_name, []).append(m)
        for name, members in groups.items():
            if len(members) > 1 and any(not m.pool for m in members):
                raise ConfigError(
                    f"name {name!r} is used as both a standalone alias and a pool",
                    remediation=(
                        "give the standalone entry a unique alias, or set "
                        f"pool: {name} on every member of the pool"
                    ),
                )
        return groups


def load_registry(path: str, enabled_backends: set[str] | None = None) -> Registry:
    """Load and validate the registry file.

    ``enabled_backends`` (if given) filters out models whose backend is not
    enabled, so you can ship one registry and toggle backends via config.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(
            f"registry file not found: {path!r}",
            remediation="set BROKER_REGISTRY_PATH to a valid registry file",
        )
    text = p.read_text(encoding="utf-8")
    try:
        if p.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        elif p.suffix == ".json":
            data = json.loads(text)
        else:
            # Try YAML (superset of JSON).
            data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(
            f"failed to parse registry {path!r}: {exc}",
            remediation="fix the registry file syntax",
        ) from exc
    if not isinstance(data, dict) or "models" not in data:
        raise ConfigError(
            "registry must be a mapping with a top-level 'models' list",
            remediation="see registry.example.yaml",
        )
    # Expand ${VAR} references in string *values* only (never comments/keys).
    data = _expand_env(data)
    try:
        reg = Registry.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(
            f"invalid registry: {exc}",
            remediation="fix the highlighted registry fields",
        ) from exc

    if enabled_backends is not None:
        reg = Registry(
            models=[m for m in reg.models if m.backend.value in enabled_backends],
            router=reg.router,
            pricing=reg.pricing,
        )
    # Trigger duplicate-alias + pool-grouping checks eagerly.
    reg.by_alias()
    reg.public_groups()
    return reg
