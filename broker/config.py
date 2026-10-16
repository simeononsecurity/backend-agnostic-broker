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
