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
# left untouched.
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand_env(text: str) -> str:
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

    # Per-model override of the global keep-alive (ollama) behavior.
    keep_alive: str | None = None

    @field_validator("alias", "runtime_model_id")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("must be non-empty")
        return v.strip()

    @field_validator("tensor_split", check_fields=False)
    @classmethod
    def _noop(cls, v):  # pragma: no cover - placeholder for forward-compat
        return v


class Registry(BaseModel):
    models: list[ModelEntry]

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
    text = _expand_env(p.read_text(encoding="utf-8"))
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
    try:
        reg = Registry.model_validate(data)
    except PydanticValidationError as exc:
        raise ConfigError(
            f"invalid registry: {exc}",
            remediation="fix the highlighted registry fields",
        ) from exc

    if enabled_backends is not None:
        reg = Registry(models=[m for m in reg.models if m.backend.value in enabled_backends])
    # Trigger duplicate-alias check eagerly.
    reg.by_alias()
    return reg
