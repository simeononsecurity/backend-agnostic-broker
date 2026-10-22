"""Backend adapter contract.

Every backend implements the same interface so the manager treats them
uniformly. An adapter instance is bound to exactly one :class:`ModelEntry`.

Contract methods (required by the task):
    validate_config, describe_model, start, wait_until_ready, health,
    chat, stream_chat, cancel, unload, stop, collect_status

Design notes
------------
* ``start`` / ``stop`` manage a broker-owned child process for runtimes that can
  only load a model at process startup (vLLM, llama.cpp, strata). For network
  backends that manage their own lifetime (Ollama), ``start`` is a readiness
  no-op and ``unload`` uses the backend's native mechanism.
* Adapters never use a shell. They build argv lists via
  :func:`broker.security.build_argv` and validate executables/model paths.
* All HTTP egress goes through a URL validated by the SSRF guard.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..registry import ModelEntry


@dataclass
class HealthResult:
    healthy: bool
    detail: str = ""
    gpu: dict[str, Any] | None = None


class BackendAdapter(abc.ABC):
    """Abstract base for all backends."""

    #: Human-readable backend name (matches BackendType value).
    name: str = "base"
    #: True for backends that run as an independent daemon the broker does NOT
    #: own (e.g. Ollama). Such backends can be liveness-probed without loading a
    #: model, and their daemon liveness is independent of broker lifecycle state.
    external_daemon: bool = False

    def __init__(self, entry: ModelEntry, settings: Settings) -> None:
        self.entry = entry
        self.settings = settings

    # --- configuration ---------------------------------------------------
    @abc.abstractmethod
    def validate_config(self) -> None:
        """Validate this entry for this backend; raise ConfigError on problems.

        Called at broker startup for every registry entry (fail-fast), *without*
        starting any process — so this must not load a model.
        """

    def describe_model(self) -> dict[str, Any]:
        """Return an OpenAI ``/v1/models`` style object plus broker metadata."""
        e = self.entry
        return {
            "id": e.alias,
            "object": "model",
            "created": 0,
            "owned_by": f"broker:{self.name}",
            "backend": self.name,
            "runtime_model_id": e.runtime_model_id,
            "context_limit": e.context_limit,
            "output_limit": e.output_limit,
            "capabilities": {
                "streaming": e.supports_streaming,
                "tools": e.supports_tools,
                "vision": e.supports_vision,
                "rerank": e.supports_rerank,
            },
            "gpu": e.gpu.model_dump(),
            "mtp": e.mtp.model_dump(),
        }

    # --- lifecycle -------------------------------------------------------
    @abc.abstractmethod
    async def start(self) -> int | None:
        """Start the backend for this model. Return a child PID if broker-owned.

        Must *not* block until ready — call :meth:`wait_until_ready` for that.
        """

    @abc.abstractmethod
    async def wait_until_ready(self, timeout: float) -> None:
        """Block until the model can serve, or raise ModelLoadError/TimeoutError_."""

    @abc.abstractmethod
    async def health(self) -> HealthResult:
        """Return current health (used by /healthz aggregation and status)."""

    @abc.abstractmethod
    async def unload(self) -> None:
        """Release the loaded model (native unload or stop the child process)."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Forcefully stop and clean up any broker-owned process/resources."""

    # --- process identity / measured release (ADR 0020) ------------------
    # Default: the backend owns no broker-spawned process (external daemon). These
    # let the manager match a telemetry provider's per-device PIDs to a model and
    # wait for a measured release before loading the next model on the same device.
    def process_pid(self) -> int | None:
        """PID of the broker-spawned runtime, or None for external daemons."""
        return None

    async def process_alive(self) -> bool:
        """Whether a broker-spawned runtime process is still running."""
        return False

    def process_group_pids(self) -> set[int]:
        """PIDs belonging to this runtime's process group / descendants (ADR 0020).

        Default: just the main PID (external daemons own no group). Broker-spawned
        backends override this to include child processes — the GPU memory a
        runtime holds is frequently attributed to children, not the parent.
        """
        pid = self.process_pid()
        return {pid} if pid is not None else set()

    def last_release_ok(self) -> bool:
        """False if the most recent stop() left the process alive after SIGKILL."""
        return True

    def device_keys(self) -> list[str]:
        """Device keys this model is accounted against for memory admission.

        The pinned ``gpu.device_ids`` (as strings) when set, else the shared
        ``"default"`` bucket. Used by ADR 0022 declared accounting and to scope
        measured-release waits + eviction to the right device(s).
        """
        ids = self.entry.gpu.device_ids
        return [str(d) for d in ids] if ids else ["default"]

    # --- required-GPU enforcement (ADR 0025) -----------------------------
    def gpu_placement_evidence(self) -> str:
        """How a required GPU placement can be CONFIRMED for this backend.

        * ``measured`` — a broker-managed runtime; placement is verified against
          the telemetry provider's per-device PIDs after load (when telemetry is
          on).
        * ``reported`` — the backend reports residency itself (Ollama ``/api/ps``
          ``size_vram``); the broker trusts that report, labelled ``reported``.
        * ``external`` — an endpoint with no lifecycle the broker controls; the
          broker cannot verify placement at all.

        The default is ``external`` (the safe assumption); backends that spawn a
        runtime or report residency override it.
        """
        return "external"

    def _check_required_gpu_verifiable(self) -> None:
        """Startup guard: a ``gpu.required`` entry the broker cannot verify (ADR 0025).

        An ``external`` entry (no lifecycle, no residency report) cannot have its
        placement confirmed, so ``gpu.required: true`` there is a configuration
        error unless the operator explicitly sets ``gpu.verify: declared``. Called
        from each adapter's :meth:`validate_config` (fail-fast at startup).
        """
        from ..errors import ConfigError

        g = self.entry.gpu
        if not g.required:
            return
        if self.gpu_placement_evidence() == "external" and g.verify != "declared":
            raise ConfigError(
                f"model {self.entry.alias!r} sets gpu.required on backend "
                f"{self.name!r} with no runtime the broker can verify",
                backend=self.name,
                model=self.entry.alias,
                remediation=(
                    "the broker cannot confirm GPU placement for an external "
                    "endpoint; set gpu.verify: declared to assert it out-of-band, "
                    "or give the entry a broker-managed startup_command"
                ),
            )

    # --- inference -------------------------------------------------------
    @abc.abstractmethod
    async def chat(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Non-streaming chat completion. Returns an OpenAI-shaped response."""

    @abc.abstractmethod
    def stream_chat(self, payload: dict[str, Any], request_id: str) -> AsyncIterator[bytes]:
        """Yield raw SSE lines (``data: {...}\\n\\n``) for a streaming completion."""

    @abc.abstractmethod
    async def cancel(self, request_id: str) -> None:
        """Best-effort cancellation of an in-flight request."""

    # --- embeddings (optional capability) --------------------------------
    async def embeddings(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Return an OpenAI ``/v1/embeddings``-shaped response for ``input``.

        Default: the backend/model does not support embeddings. Backends that do
        (Ollama, OpenAI-compatible HTTP runtimes) override this. Kept non-abstract
        so chat-only backends need not implement it.
        """
        from ..errors import ValidationError

        raise ValidationError(
            f"backend {self.name!r} does not support embeddings for model {self.entry.alias!r}",
            model=self.entry.alias,
            remediation="request an embedding-capable model (e.g. Ollama nomic-embed-text)",
        )

    # --- rerank (optional capability; ADR 0013) --------------------------
    async def rerank(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Return a Cohere/Jina-compatible rerank response for ``query`` + ``documents``.

        Default: the backend/model does not support reranking. Backends that do
        (OpenAI/Cohere-compatible HTTP runtimes: vLLM, llama.cpp) override this.
        Kept non-abstract so chat-only backends need not implement it.
        """
        from ..errors import ValidationError

        raise ValidationError(
            f"backend {self.name!r} does not support rerank for model {self.entry.alias!r}",
            model=self.entry.alias,
            remediation="request a rerank-capable model (a vLLM or llama.cpp reranker)",
        )

    # --- status ----------------------------------------------------------
    @abc.abstractmethod
    async def collect_status(self) -> dict[str, Any]:
        """Return a status dict (pid, gpu allocation, endpoint, etc.)."""

    # --- runtime identity / adoption (ADR 0021) --------------------------
    def runtime_argv(self) -> list[str] | None:
        """The exact argv of the last spawned runtime, or None if not managed.

        Used by the runtime ledger to hash a spawned runtime's command. Backends
        that do not spawn a broker-owned process (external daemons, in-process
        fakes) return None.
        """
        return None

    def planned_argv(self) -> list[str] | None:
        """The argv this entry WOULD spawn, computed without starting anything.

        Used at reconciliation time to confirm a recorded runtime still matches
        the current registry (argv hash) before adopting it. Returns None for
        backends that do not spawn a broker-owned process.
        """
        return None

    def mark_adopted(self, pid: int, pgid: int | None, start_time: str | None) -> None:
        """Record that ``pid`` is an adopted (pre-existing) runtime for this model.

        The default is a no-op: only broker-managed HTTP runtimes support
        adoption. An adopted runtime has no :class:`asyncio.subprocess.Process`
        handle, so :meth:`stop` must terminate it by its process group instead.
        """
        return None

    # --- load signal (optional; cache-aware routing, ADR 0010) -----------
    async def collect_load(self) -> float | None:
        """Return a real load signal for routing, or None when unavailable.

        Default: no real signal (the manager falls back to in-flight counts).
        HTTP backends may override to scrape the runtime's ``/metrics``. This must
        NEVER load a model — only read from an already-running process.
        """
        return None
