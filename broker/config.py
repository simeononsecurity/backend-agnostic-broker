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
    route_strategy: str = "sticky"
    # Max remembered session->member pairings (LRU-evicted). Bounds memory for
    # sticky routing under many distinct sessions/clients.
    route_sticky_max_entries: int = 10000

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
        if v not in ("sticky", "least_busy", "round_robin"):
            raise ValueError("route_strategy must be 'sticky', 'least_busy', or 'round_robin'")
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
