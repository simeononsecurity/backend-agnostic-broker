"""Typed broker configuration with fail-fast validation.

All behavior is configurable via environment variables (prefix ``BROKER_``), a
mounted registry file, or Docker Compose env injection. Defaults are safe:
authentication is *required* unless explicitly disabled, no backends contact the
network beyond their configured base URLs, and no model loads at startup.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

# Valid error-class names for BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS (mirrors
# broker.reliability.ErrorClass; kept as a literal here to avoid importing the
# reliability module at settings-construction time).
_ERROR_CLASS_NAMES = frozenset(
    {"timeout", "connection", "server_error", "backpressure", "client_error", "other"}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BROKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Server ---
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    log_format: str = "json"  # "json" | "text"

    # --- Auth ---
    # When auth_enabled, requests must present one of api_keys as a Bearer token
    # (or X-API-Key). Keys are provided via env/secret, never committed.
    auth_enabled: bool = True
    api_keys: list[str] = Field(default_factory=list)

    # --- Per-key governance (virtual keys, rpm/tpm, budgets; ADR 0008) ---
    # Opt-in: with no keys file and no per-key limits, behavior is identical to
    # v0.4.0. BROKER_KEYS_PATH points at a git-ignored keys.yaml holding HASHED
    # virtual keys (manage with `python -m broker.keys`); legacy BROKER_API_KEYS
    # keep working as unlimited user keys. The rate-limit window (seconds) applies
    # to both rpm (sliding log) and tpm (windowed counter).
    keys_path: str | None = None
    rate_limit_window_seconds: float = 60.0

    # --- Registry / models ---
    registry_path: str = "registry.yaml"
    default_model: str | None = None
    enabled_backends: list[str] = Field(
        default_factory=lambda: ["fake", "ollama", "vllm", "llamacpp", "strata"]
    )

    # --- Lifecycle / concurrency ---
    model_idle_timeout_seconds: float = 300.0
    max_loaded_models: int = 3
    max_concurrent_requests: int = 32
    per_model_max_concurrency: int = 4
    request_timeout_seconds: float = 300.0
    startup_timeout_seconds: float = 120.0
    shutdown_timeout_seconds: float = 30.0
    idle_sweep_interval_seconds: float = 5.0
    # concurrency policy when a model is loading: "queue" waits, "reject" 429s.
    load_concurrency_policy: str = "queue"

    # --- Routing (load balancing across pool members) ---
    # "sticky" (default): pin each session (X-Session-Id header, else the OpenAI
    #   `user` field, else client IP) to one member and keep using it; only switch
    #   members if that member is unhealthy/unavailable. New sessions are assigned
    #   the least-busy member, so distinct sessions still spread across the pool.
    # "least_busy": route every request to the member with the fewest in-flight
    #   requests (round-robin tie-break).
    # "round_robin": rotate members every request regardless of load.
    # "cache_aware": prefix-affinity + load-aware scoring (ADR 0010); route to the
    #   member that already holds the request's prompt prefix (warm KV cache),
    #   backing off to a less-loaded member once the warm one is saturated. Opt-in.
    route_strategy: str = "sticky"
    # Max remembered session->member pairings (LRU-evicted). Bounds memory for
    # sticky routing under many distinct sessions/clients.
    route_sticky_max_entries: int = 10000

    # --- Cache-aware pool routing (ADR 0010; route_strategy="cache_aware") ---
    # Approximate prefix-affinity routing: the broker hashes the request's prompt
    # into fixed-size CHAINED blocks, keeps a bounded prefix-block -> member(s)
    # index on the StateStore (Redis-shareable), and scores each member with
    #   score = matched_fraction - beta * relative_load
    #   relative_load = (load - mean_load) / max(1, mean_load)
    # routing to the argmax (vLLM production-stack LoadAwareRouter). No prefix
    # match for any member falls back to least_busy. All of this is opt-in and has
    # no effect unless route_strategy="cache_aware".
    #   beta: the single tunable exchange-rate between cache benefit and load.
    #     1.0 = "a member at 2x fleet-average load is docked one full cache hit's
    #     worth of preference"; 0.0 = pure prefix affinity (ignore load). Default
    #     0.25 was chosen from the scripts/bench_routing.py scenario x beta matrix
    #     (see ADR 0010 / docs/routing-and-health.md): it is the smallest beta that
    #     keeps the low-pressure pool balanced yet still concentrates prefixes
    #     enough to roughly double hit rate under cache pressure. beta=1.0
    #     over-penalized affinity (the approximate index makes the load term swamp
    #     a real cache hit) and never beat sticky.
    cache_aware_beta: float = 0.25
    # Characters per prefix block (approximate; ~4 chars/token). Larger = smaller
    # index but coarser prefix matching. Only full blocks are hashed.
    cache_aware_block_chars: int = 128
    # Max prefix-block index entries kept per replica (LRU-evicted). 0 disables
    # the size bound (TTL still applies).
    cache_aware_max_entries: int = 10000
    # TTL (seconds) on prefix-block index entries, approximating KV-cache
    # residency so stale affinities self-expire. 0 = no expiry.
    cache_aware_ttl_seconds: float = 1800.0
    # When true, the NON-LOADING health probe scrapes an exposed vLLM /metrics
    # endpoint (vllm:num_requests_waiting) for a real load signal; falls back to
    # in-flight request counts when unavailable. Never loads a model.
    cache_aware_use_metrics: bool = False

    # --- Background health probing (non-loading) ---
    # Periodically liveness-probe active models without loading any: Ollama via
    # GET /api/tags, broker-managed runtimes only while their process is running.
    # A READY model whose backend has gone away is marked FAILED so the next
    # request reloads/reroutes instead of erroring mid-flight.
    health_probe_enabled: bool = True
    health_probe_interval_seconds: float = 30.0
    health_probe_timeout_seconds: float = 5.0

    # --- Auto-router ("auto" alias: a tiny CPU classifier picks the model) ---
    # When enabled, a virtual model id (router_alias, default "auto") is exposed;
    # requests to it are classified to a concrete model. Only the "auto" alias is
    # affected — every other model is addressed directly as before.
    router_enabled: bool = True
    router_alias: str = "auto"
    # Backend selection: "auto" uses the llama.cpp classifier when its model +
    # library are available and falls back to the heuristic scorer otherwise;
    # "llm" forces the classifier (errors if unavailable); "heuristic" forces the
    # rule/score router; "off" disables the alias.
    router_backend: str = "auto"
    # GGUF model for the llama.cpp classifier (small, CPU, Q4). In the container
    # this defaults to the bundled model via BROKER_ROUTER_MODEL_PATH.
    router_model_path: str | None = None
    router_model_ctx: int = 2048
    router_threads: int | None = None  # CPU threads for the classifier (None=auto)
    # Max characters of the last user message fed to the classifier (~512 tokens).
    router_max_input_chars: int = 2048
    # Decision cache size (keyed by flags + candidate set + prompt prefix).
    router_cache_size: int = 512
    # Per-classification timeout (seconds) before falling back to the default.
    router_timeout_seconds: float = 10.0
    # Deterministic fallback model id (overrides registry router.default).
    router_default_model: str | None = None

    # --- Retries & backoff (ADR 0009) ---
    # Transparent retry of a failed request WITHIN the requested model's pool
    # (never a different model). Defaults reproduce v0.4.0 exactly: one pass over
    # the pool members (failover) with NO sleep.
    #   retry_max_attempts: 0 = legacy (one attempt per pool member). >0 caps the
    #     total attempts for a request and enables re-trying members after the
    #     backoff delay (useful for single-member pools / transient failures).
    #   backoff: full-jitter exponential delay between attempts (AWS pattern):
    #     delay = uniform(0, min(max_ms, base_ms * 2**(attempt-1))). base_ms=0
    #     disables sleeping (the default), so legacy behavior is byte-for-byte.
    retry_max_attempts: int = 0
    retry_backoff_base_ms: float = 0.0
    retry_backoff_max_ms: float = 2000.0

    # --- Member cooldowns / circuit breaker (ADR 0009) ---
    # After `cooldown_failures` consecutive failures a pool member is benched for
    # `cooldown_seconds`; selection skips benched members. Once the window
    # elapses the member is eligible again as a half-open trial: a success resets
    # it, another failure re-benches it. Default-off (0) preserves v0.4.0.
    cooldown_failures: int = 0
    cooldown_seconds: float = 30.0

    # --- Ejection cap + exponential cooldown growth (ADR 0017) ---
    # These refine the ADR 0009 circuit breaker; all default to v0.5.0 behavior.
    #   max_ejection_percent: Envoy-style cap on the fraction of a pool that may
    #     be benched at once. 0 = no cap (v0.5.0: any/all members may be benched,
    #     relying on the all-benched fall-through). >0 keeps at least
    #     ceil((100-pct)/100 * N) members serving: when more are benched, the
    #     soonest-to-recover ones are kept eligible so a correlated failure can
    #     never strand the whole pool.
    #   cooldown_backoff_multiplier: grow a member's cooldown window on repeated
    #     ejections (Envoy base_ejection_time x consecutive-ejections). The Nth
    #     consecutive ejection benches for cooldown_seconds * multiplier**(N-1).
    #     1.0 = fixed window (v0.5.0).
    #   cooldown_max_seconds: cap the grown window (Envoy max_ejection_time).
    #     0 = no cap beyond the multiplier.
    #   cooldown_decay_seconds: a successful serve resets the ejection multiplier
    #     only after the member has been out of cooldown at least this long
    #     (sustained success). 0 = reset on any success (punish only back-to-back
    #     trial failures).
    max_ejection_percent: int = 0
    cooldown_backoff_multiplier: float = 1.0
    cooldown_max_seconds: float = 0.0
    cooldown_decay_seconds: float = 0.0

    # --- Single-trial half-open probe (ADR 0017) ---
    # When true, a recovering member (cooldown window elapsed) admits exactly ONE
    # probe request via the StateStore try_half_open lock; concurrent requests
    # skip it until the trial resolves (success closes the breaker, failure
    # re-benches). Default off = ADR 0009 behavior (any post-expiry request may
    # probe). ttl 0 = derive the trial-lock lifetime from request_timeout_seconds.
    cooldown_half_open_single_trial: bool = False
    cooldown_half_open_ttl_seconds: float = 0.0

    # --- Per-error retry / cooldown policies (ADR 0017) ---
    # Classify each failure (timeout / connection / server_error / backpressure /
    # client_error) and decide per class whether it is retried and whether it
    # counts toward a member's cooldown. 4xx client errors are NEVER retried or
    # counted (hard-wired). Defaults reproduce ADR 0009's single failover set:
    # timeout+connection+server_error retry and bench; backpressure (429) does
    # not. Set retry_on_backpressure/cooldown_on_backpressure to opt a 429 in.
    retry_on_timeout: bool = True
    retry_on_connection: bool = True
    retry_on_server_error: bool = True
    retry_on_backpressure: bool = False
    cooldown_on_timeout: bool = True
    cooldown_on_connection: bool = True
    cooldown_on_server_error: bool = True
    cooldown_on_backpressure: bool = False
    # Honor an upstream Retry-After header (RFC 9110; delay-seconds or HTTP-date)
    # on a RETRIED error, clamped to the remaining retry budget. No effect on the
    # legacy single-pass default unless a retried error actually carries one.
    retry_respect_retry_after: bool = True
    # Optional per-class attempt cap (JSON, e.g. '{"timeout": 5, "server_error": 2}').
    # A class listed here caps attempts for its failures; unlisted classes use
    # retry_max_attempts. Keys must be ErrorClass names.
    retry_max_attempts_by_class: dict[str, int] = Field(default_factory=dict)

    # --- Optional shared state via Redis (ADR 0009) ---
    # When BROKER_REDIS_URL is set, the sticky map, cooldowns, counters, and
    # cached values live in Redis so multiple broker replicas share one view.
    # Absent the URL, state is in-process (exactly today's behavior, no new dep).
    # `redis` is an optional extra: pip install 'backend-agnostic-broker[redis]'.
    redis_url: str | None = None
    redis_prefix: str = "broker"
    # TTL (seconds) applied to Redis sticky keys (0 = no expiry). In-process
    # sticky is LRU-bounded by route_sticky_max_entries instead.
    route_sticky_ttl_seconds: float = 0.0
    # Global LRU bound on Redis sticky entries, shared across replicas via a
    # sorted-set index scored by last pin (ADR 0017). 0 = TTL-only (v0.5.0). >0
    # caps the total sticky pairings one Redis prefix holds; the least-recently
    # pinned entries are evicted (ZPOPMIN) once the bound is exceeded.
    redis_sticky_max_entries: int = 0

    # --- Auto-router: latency-budget routing (ADR 0011) ---
    # Default per-request latency budget in ms (the x-broker-latency-budget-ms
    # header, else registry router.latency_budget_ms, else this). None = no budget.
    router_latency_budget_ms: float | None = None
    # Rolling window (seconds) of the usage log used to estimate per-model latency.
    router_latency_window_seconds: float = 3600.0
    # Estimated extra latency (ms) added for a model that is not currently resident
    # (cold start), since lazy-load means the first call also pays load time.
    router_cold_start_ms: float = 1500.0

    # --- Placement / data-residency pinning (ADR 0011, fail-closed) ---
    # Baseline placement labels required of EVERY request (unioned with the
    # x-broker-placement header). Empty = no baseline requirement. A request is
    # only served by models carrying all required labels, else 403 placement_denied.
    default_placement: list[str] = Field(default_factory=list)

    # --- Limits ---
    max_request_bytes: int = 2_000_000

    # --- Response cache (ADR 0012) ---
    # Exact-match response cache, default OFF (defaults reproduce v0.4.0). When
    # enabled, identical requests to the same model/key scope are served from a
    # cache backed by the ADR 0009 StateStore (in-process or Redis), skipping
    # inference. The key hashes (key/tenant scope, resolved public model,
    # normalized messages/prompt/input, sampling + tools + response_format), so a
    # cached answer can NEVER cross a key/tenant boundary. Errors and partial
    # (streamed) responses are never cached; streaming requests bypass the cache.
    #   deterministic_only: by default only cache deterministic requests
    #     (temperature==0 or an explicit seed); set false to cache all requests.
    #   max_bytes: per-entry cap — a serialized result larger than this is not
    #     stored (0 = no cap). max_entries bounds the in-process index (LRU).
    response_cache_enabled: bool = False
    response_cache_ttl_seconds: float = 300.0
    response_cache_max_entries: int = 1000
    response_cache_max_bytes: int = 1_000_000
    response_cache_deterministic_only: bool = True

    # --- Admin runtime control (ADR 0012) ---
    # Privileged runtime-control surface (GET /admin/models, model unload/load,
    # registry hot-reload), default OFF. When enabled it is gated by a dedicated
    # admin-key list via the `require_admin` dependency — intentionally separate
    # from the normal API keys so a future virtual-key admin scope (ADR 0008) can
    # replace BROKER_ADMIN_API_KEYS without touching the route code.
    admin_enabled: bool = False
    admin_api_keys: list[str] = Field(default_factory=list)
    # Seconds to wait for in-flight requests to drain before an unload/reload
    # gives up (and reports the model still busy).
    admin_drain_timeout_seconds: float = 30.0

    # --- Web UI / playground (ADR 0012) --- DEFERRED (see ADR 0012). The flag is
    # reserved so a future static page at /ui is opt-in; it currently does nothing.
    ui_enabled: bool = False

    # --- Security allow-lists ---
    allowed_exec_dirs: list[str] = Field(
        default_factory=lambda: ["/usr/bin", "/usr/local/bin", "/opt"]
    )
    allowed_model_roots: list[str] = Field(default_factory=lambda: ["/models"])
    # Extra trusted backend base URLs for the SSRF allow-list (beyond those in
    # the registry entries' base_url).
    extra_allowed_backend_urls: list[str] = Field(default_factory=list)
    allow_loopback_backends: bool = True

    # --- GPU visibility ---
    gpu_visible: bool = False
    cuda_visible_devices: str | None = None  # e.g. "0,1"

    # --- Cache / tmp ---
    cache_dir: str = "/var/cache/broker"
    tmp_dir: str = "/tmp/broker"

    # --- Observability ---
    metrics_enabled: bool = True
    # Distributed tracing (ADR 0012): OFF by default, zero overhead when off.
    # When true, the OPTIONAL `otel` extra must be installed (pip install
    # 'backend-agnostic-broker[otel]') or startup fails fast with a ConfigError.
    # Spans carry gen_ai.* metadata only — NEVER prompt/response content. The
    # OTLP exporter is configured entirely via standard OTEL_* env vars
    # (OTEL_EXPORTER_OTLP_ENDPOINT, OTEL_EXPORTER_OTLP_PROTOCOL/HEADERS, ...).
    tracing_enabled: bool = False
    tracing_service_name: str = "backend-agnostic-broker"

    # --- Usage accounting (request + routing-decision log; see ADR 0007) ---
    # One row per completed request (metadata + token counts only, never content).
    # Substrate for cost reporting, budgets, and router evaluation. Path defaults
    # to <cache_dir>/usage.sqlite3; set ":memory:" for ephemeral, or disable.
    usage_log_enabled: bool = True
    usage_log_path: str | None = None

    # --- Docker / networking hints (documented, used by compose) ---
    docker_network_mode: str = "bridge"

    @field_validator("log_format")
    @classmethod
    def _logfmt(cls, v: str) -> str:
        if v not in ("json", "text"):
            raise ValueError("log_format must be 'json' or 'text'")
        return v

    @field_validator("load_concurrency_policy")
    @classmethod
    def _policy(cls, v: str) -> str:
        if v not in ("queue", "reject"):
            raise ValueError("load_concurrency_policy must be 'queue' or 'reject'")
        return v

    @field_validator("route_strategy")
    @classmethod
    def _route(cls, v: str) -> str:
        if v not in ("sticky", "least_busy", "round_robin", "cache_aware"):
            raise ValueError(
                "route_strategy must be 'sticky', 'least_busy', 'round_robin', or 'cache_aware'"
            )
        return v

    @field_validator("router_backend")
    @classmethod
    def _router_backend(cls, v: str) -> str:
        if v not in ("auto", "llm", "heuristic", "off"):
            raise ValueError("router_backend must be 'auto', 'llm', 'heuristic', or 'off'")
        return v

    def validate_runtime(self) -> None:
        """Cross-field, fail-fast checks run once at startup."""
        if self.auth_enabled and not self.api_keys and not self._has_keys_file():
            raise ConfigError(
                "auth is enabled but no API keys are configured",
                remediation=(
                    "set BROKER_API_KEYS, provide a BROKER_KEYS_PATH keys file, "
                    "or set BROKER_AUTH_ENABLED=false for local dev only"
                ),
            )
        if self.rate_limit_window_seconds <= 0:
            raise ConfigError("BROKER_RATE_LIMIT_WINDOW_SECONDS must be > 0")
        if self.port < 1 or self.port > 65535:
            raise ConfigError("BROKER_PORT out of range")
        if self.max_loaded_models < 1:
            raise ConfigError("BROKER_MAX_LOADED_MODELS must be >= 1")
        if self.max_concurrent_requests < 1:
            raise ConfigError("BROKER_MAX_CONCURRENT_REQUESTS must be >= 1")
        if self.retry_max_attempts < 0:
            raise ConfigError("BROKER_RETRY_MAX_ATTEMPTS must be >= 0 (0 = legacy one-pass)")
        if self.retry_backoff_base_ms < 0 or self.retry_backoff_max_ms < 0:
            raise ConfigError("BROKER_RETRY_BACKOFF_* must be >= 0")
        if self.retry_backoff_max_ms < self.retry_backoff_base_ms:
            raise ConfigError("BROKER_RETRY_BACKOFF_MAX_MS must be >= BROKER_RETRY_BACKOFF_BASE_MS")
        if self.cooldown_failures < 0:
            raise ConfigError("BROKER_COOLDOWN_FAILURES must be >= 0 (0 = disabled)")
        if self.cooldown_seconds < 0:
            raise ConfigError("BROKER_COOLDOWN_SECONDS must be >= 0")
        if self.max_ejection_percent < 0 or self.max_ejection_percent > 100:
            raise ConfigError("BROKER_MAX_EJECTION_PERCENT must be in 0..100 (0 = no cap)")
        if self.cooldown_backoff_multiplier < 1.0:
            raise ConfigError(
                "BROKER_COOLDOWN_BACKOFF_MULTIPLIER must be >= 1.0 (1.0 = fixed window)"
            )
        if self.cooldown_max_seconds < 0:
            raise ConfigError("BROKER_COOLDOWN_MAX_SECONDS must be >= 0 (0 = no cap)")
        if self.cooldown_decay_seconds < 0:
            raise ConfigError("BROKER_COOLDOWN_DECAY_SECONDS must be >= 0")
        if self.cooldown_half_open_ttl_seconds < 0:
            raise ConfigError("BROKER_COOLDOWN_HALF_OPEN_TTL_SECONDS must be >= 0 (0 = derive)")
        for _cls, _n in self.retry_max_attempts_by_class.items():
            if _cls not in _ERROR_CLASS_NAMES:
                raise ConfigError(
                    f"BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS has unknown error class {_cls!r}",
                    remediation=f"use one of: {', '.join(sorted(_ERROR_CLASS_NAMES))}",
                )
            if _n < 0:
                raise ConfigError("BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS counts must be >= 0")
        if self.redis_sticky_max_entries < 0:
            raise ConfigError("BROKER_REDIS_STICKY_MAX_ENTRIES must be >= 0 (0 = TTL-only)")
        if self.cache_aware_beta < 0:
            raise ConfigError("BROKER_CACHE_AWARE_BETA must be >= 0 (0 = ignore load)")
        if self.cache_aware_block_chars < 1:
            raise ConfigError("BROKER_CACHE_AWARE_BLOCK_CHARS must be >= 1")
        if self.cache_aware_max_entries < 0:
            raise ConfigError("BROKER_CACHE_AWARE_MAX_ENTRIES must be >= 0 (0 = unbounded)")
        if self.cache_aware_ttl_seconds < 0:
            raise ConfigError("BROKER_CACHE_AWARE_TTL_SECONDS must be >= 0 (0 = no expiry)")
        if self.redis_url is not None and not self.redis_url:
            raise ConfigError("BROKER_REDIS_URL must be a non-empty URL when set")
        if self.response_cache_enabled:
            if self.response_cache_ttl_seconds < 0:
                raise ConfigError("BROKER_RESPONSE_CACHE_TTL_SECONDS must be >= 0 (0 = no TTL)")
            if self.response_cache_max_entries < 0:
                raise ConfigError("BROKER_RESPONSE_CACHE_MAX_ENTRIES must be >= 0 (0 = unbounded)")
            if self.response_cache_max_bytes < 0:
                raise ConfigError("BROKER_RESPONSE_CACHE_MAX_BYTES must be >= 0 (0 = no cap)")
        if (
            self.admin_enabled
            and self.auth_enabled
            and not self.admin_api_keys
            and not self.keys_path
        ):
            raise ConfigError(
                "admin runtime control is enabled but no admin credentials are configured",
                remediation=(
                    "set BROKER_ADMIN_API_KEYS (separate from BROKER_API_KEYS), or configure "
                    "BROKER_KEYS_PATH with an admin-scope virtual key"
                ),
            )
        if self.tracing_enabled:
            try:
                import opentelemetry  # noqa: F401
            except ImportError as exc:
                raise ConfigError(
                    "BROKER_TRACING_ENABLED=true but the 'opentelemetry' package is not installed",
                    remediation="pip install 'backend-agnostic-broker[otel]'",
                ) from exc
        if not Path(self.registry_path).exists():
            raise ConfigError(
                f"registry not found: {self.registry_path!r}",
                remediation="mount a registry file and set BROKER_REGISTRY_PATH",
            )

    def _has_keys_file(self) -> bool:
        p = self.keys_path
        if not p:
            return False
        return Path(p).exists()

    def allowed_backend_bases(self, registry) -> list[str]:
        bases: list[str] = list(self.extra_allowed_backend_urls)
        for m in registry.models:
            if m.base_url:
                bases.append(m.base_url)
        return bases


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
