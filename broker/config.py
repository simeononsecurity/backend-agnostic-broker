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
    #     worth of preference". 0.0 = pure prefix affinity (ignore load).
    cache_aware_beta: float = 1.0
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

    # --- Limits ---
    max_request_bytes: int = 2_000_000

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
    tracing_enabled: bool = False

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
        if self.auth_enabled and not self.api_keys:
            raise ConfigError(
                "auth is enabled but no API keys are configured",
                remediation="set BROKER_API_KEYS or BROKER_AUTH_ENABLED=false for local dev only",
            )
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
        if not Path(self.registry_path).exists():
            raise ConfigError(
                f"registry not found: {self.registry_path!r}",
                remediation="mount a registry file and set BROKER_REGISTRY_PATH",
            )

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
