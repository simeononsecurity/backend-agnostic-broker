"""Structured error types.

Every broker error identifies the backend, model alias, lifecycle state, and a
remediation hint so operators get actionable, machine-parseable failures. These
map to OpenAI-style error envelopes at the API layer (see ``app.py``).
"""

from __future__ import annotations

from typing import Any


class BrokerError(Exception):
    """Base class for all broker errors.

    Attributes mirror the OpenAI error object plus broker-specific context.
    """

    http_status: int = 500
    error_type: str = "broker_error"

    def __init__(
        self,
        message: str,
        *,
        backend: str | None = None,
        model: str | None = None,
        state: str | None = None,
        remediation: str | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.backend = backend
        self.model = model
        self.state = state
        self.remediation = remediation
        self.code = code or self.error_type
        # Optional HTTP response headers to attach to the error envelope (e.g.
        # Retry-After + x-ratelimit-* on a 429). Never carries key material.
        self.headers = headers or {}

    def to_payload(self) -> dict[str, Any]:
        """Render an OpenAI-compatible error envelope with broker context."""
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
                "param": None,
                "backend": self.backend,
                "model": self.model,
                "state": self.state,
                "remediation": self.remediation,
            }
        }


class ConfigError(BrokerError):
    """Invalid broker or registry configuration (fail-fast at startup)."""

    http_status = 500
    error_type = "config_error"


class ValidationError(BrokerError):
    """A request or value failed validation (rejected before any side effect)."""

    http_status = 400
    error_type = "invalid_request_error"


class SecurityError(BrokerError):
    """A request tripped a security boundary (SSRF, path traversal, injection)."""

    http_status = 400
    error_type = "security_error"


class AuthError(BrokerError):
    """Authentication failed or was missing."""

    http_status = 401
    error_type = "authentication_error"


class ModelNotFoundError(BrokerError):
    """Requested model alias is not in the registry (never falls back silently)."""

    http_status = 404
    error_type = "model_not_found"


class ModelLoadError(BrokerError):
    """The selected model failed to start or become ready."""

    http_status = 503
    error_type = "model_load_error"


class BackendUnavailableError(BrokerError):
    """The backend process/endpoint is unreachable.

    Reserved for *transport* failures (connect error, read timeout, DNS, a
    non-JSON body on an otherwise-2xx response): the broker could not get a valid
    HTTP reply from the backend at all. When the backend *did* reply with an HTTP
    error status, raise :class:`UpstreamHTTPError` instead so the per-error policy
    (``broker.reliability``) can classify it by that status.
    """

    http_status = 502
    error_type = "backend_unavailable"


class UpstreamHTTPError(BrokerError):
    """The backend RESPONDED with an HTTP error status >= 400 (ADR 0017).

    Distinct from :class:`BackendUnavailableError` (a transport failure): the
    backend was reachable and returned a status. Carrying the ``upstream_status``
    and the upstream ``Retry-After`` lets :func:`broker.reliability.classify_error`
    bucket the failure by *status* — ``408`` → timeout, ``429`` → backpressure,
    other ``4xx`` → client_error (never retried, never benched), ``5xx`` →
    server_error — instead of collapsing every upstream error into
    ``CONNECTION`` and retrying a client's own bad request across the pool.

    The client-facing ``http_status`` / ``error_type`` are derived from the
    upstream status so a caller sees an honest result:

    * ``429`` → ``429`` ``rate_limit_error`` (upstream backpressure)
    * ``408`` → ``504`` ``timeout_error``
    * other ``4xx`` → that same ``4xx`` with type ``upstream_client_error`` —
      the request was bad, so it is **never** reported as a ``502``
    * ``5xx`` / anything else → ``502`` ``backend_unavailable`` (the broker could
      not obtain a good answer from any pool member)

    The ``message`` must already be sanitized/truncated by the adapter; this class
    never stores raw upstream bodies or secrets.
    """

    error_type = "backend_unavailable"

    def __init__(
        self,
        message: str,
        *,
        upstream_status: int,
        backend: str | None = None,
        model: str | None = None,
        state: str | None = None,
        remediation: str | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        st = int(upstream_status)
        self.upstream_status = st
        if st == 429:
            client_status, etype = 429, "rate_limit_error"
        elif st == 408:
            client_status, etype = 504, "timeout_error"
        elif 400 <= st < 500:
            client_status, etype = st, "upstream_client_error"
        else:  # 5xx and any unexpected status
            client_status, etype = 502, "backend_unavailable"
        super().__init__(
            message,
            backend=backend,
            model=model,
            state=state,
            remediation=remediation,
            code=code or etype,
            headers=headers,
        )
        # Instance attributes override the class defaults for this envelope.
        self.http_status = client_status
        self.error_type = etype


class TimeoutError_(BrokerError):
    """A request or startup exceeded its configured timeout."""

    http_status = 504
    error_type = "timeout_error"


class ConcurrencyLimitError(BrokerError):
    """Too many concurrent or loaded models/requests for the configured limits."""

    http_status = 429
    error_type = "rate_limit_error"


class CapacityUnavailableError(ConcurrencyLimitError):
    """Memory-aware admission could not fit a sized model (ADR 0022).

    A *typed, distinct* capacity signal: the model declares ``memory_mib`` but the
    device budget (measured free memory when telemetry is on, else the declared
    ``BROKER_GPU_MEMORY_BUDGET_MIB``) cannot accommodate it even after evicting
    every idle model on its device(s). Carries the machine-readable ``reason`` and
    the ``holder`` (what currently occupies the memory) so an operator — and the
    ADR 0023 request queue — can tell "capacity unavailable" apart from a bare
    concurrency 429 and *wait* on it instead of failing. Subclasses
    :class:`ConcurrencyLimitError` so existing 429 handling is unchanged.
    """

    error_type = "capacity_unavailable"

    def __init__(
        self,
        message: str,
        *,
        reason: str | None = None,
        holder: str | None = None,
        backend: str | None = None,
        model: str | None = None,
        state: str | None = None,
        remediation: str | None = None,
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(
            message,
            backend=backend,
            model=model,
            state=state,
            remediation=remediation,
            code=code,
            headers=headers,
        )
        #: Machine-readable cause, e.g. "measured_insufficient" / "declared_insufficient".
        self.reason = reason
        #: What holds the memory (alias(es) or a measured-device description).
        self.holder = holder

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["error"]["reason"] = self.reason
        payload["error"]["holder"] = self.holder
        return payload


class ReleaseStuckError(BrokerError):
    """A broker-spawned runtime did not release its device after unload (ADR 0020).

    After evicting a model the broker waits (bounded by
    ``BROKER_RELEASE_TIMEOUT_SECONDS``) for the process group to exit AND — when
    telemetry is on — for the device to stop reporting that group's PIDs. If the
    memory never comes back, the next load fails with this **503** naming the stuck
    holder (PID, device, MiB still held) instead of starting into an OOM. The
    broker never force-resets the GPU or kills a process it did not start.
    """

    http_status = 503
    error_type = "release_stuck"


class GpuUnavailableError(BrokerError):
    """A ``gpu.required`` model could not be admitted to a GPU (ADR 0025).

    Raised by the shared admission path *before any spawn or eviction* when the
    resolved device inventory (measured telemetry devices, else the declared
    ``BROKER_GPU_DEVICES`` / ``BROKER_CUDA_VISIBLE_DEVICES`` list) cannot satisfy
    the entry: the model is ``gpu.required`` but the inventory is empty, or one of
    its ``gpu.device_ids`` is not in the inventory. Nothing is loaded and there is
    no CPU fallback. Maps to **503**; it fails over to a qualifying pool member but
    is never retried on the same member (the check is deterministic for a member).
    """

    http_status = 503
    error_type = "gpu_unavailable"


class GpuPlacementFailedError(BrokerError):
    """A ``gpu.required`` model loaded but was not confirmed on a GPU (ADR 0025).

    Raised by post-load verification: a broker-managed runtime whose process group
    held no memory on a required device within ``BROKER_GPU_VERIFY_SECONDS``
    (evidence ``measured``), or an Ollama model whose ``/api/ps`` reported
    ``size_vram == 0`` (evidence ``reported``). The model is unloaded before this
    is raised, so no request is ever served from a required model running on CPU.
    Maps to **503**; like :class:`GpuUnavailableError` it fails over to a
    qualifying member without being retried on the failing one.
    """

    http_status = 503
    error_type = "gpu_placement_failed"


class RequestTooLargeError(BrokerError):
    """Request body exceeded the configured size limit."""

    http_status = 413
    error_type = "request_too_large"


class ClientDisconnectedError(BrokerError):
    """The client went away before the (non-streaming) response was produced.

    Raised by the serve path's disconnect guard (ADR 0023) once the client has
    disconnected, so the held admission/concurrency slot and the inflight
    upstream request are cancelled and released immediately instead of leaking
    until the serve finishes. The response is never delivered (the socket is
    gone); the 499 status is for logs/metrics only, matching nginx's convention
    for a client-closed request.
    """

    http_status = 499
    error_type = "client_disconnected"


class PlacementDeniedError(BrokerError):
    """No model satisfies the request's placement/residency requirement.

    Fail-closed: the broker returns this rather than spilling to a model that
    does not carry every required placement label (ADR 0011).
    """

    http_status = 403
    error_type = "placement_denied"


class RateLimitExceededError(BrokerError):
    """A per-key request/token rate limit was exceeded (ADR 0008).

    Maps to OpenAI's ``429`` ``rate_limit_exceeded``. Carries ``Retry-After`` and
    ``x-ratelimit-*`` headers so OpenAI-compatible clients can self-pace.
    """

    http_status = 429
    error_type = "rate_limit_exceeded"


class InsufficientQuotaError(BrokerError):
    """A per-key token or cost budget was exhausted (ADR 0008).

    Maps to OpenAI's billing error class: HTTP ``429`` with ``error.type``
    ``insufficient_quota``. Unlike a transient rate limit, retrying does not help
    until the budget window resets or the cap is raised.
    """

    http_status = 429
    error_type = "insufficient_quota"


class AuthorizationError(BrokerError):
    """The authenticated key is not permitted to perform this action (ADR 0008).

    Used for a model the key's ``allowed_models`` list does not cover
    (``model_not_allowed``). Distinct from :class:`AuthError` (401), which is a
    failed/missing authentication.
    """

    http_status = 403
    error_type = "permission_error"


class ModelBusyError(BrokerError):
    """An admin action (unload/reload) was refused because the model is serving.

    Raised when ``POST /admin/models/{alias}/unload`` is called without drain on a
    model with in-flight requests, or when a drain exceeds its timeout. Distinct
    from the 429 concurrency limit: this is a conflict with current state, not a
    rate limit.
    """

    http_status = 409
    error_type = "model_busy"


class ConflictError(BrokerError):
    """A mutation was refused because it conflicts with current state (ADR 0016).

    Used by the admin key API when an operation is only valid on an *active* key —
    e.g. rotating a revoked or already-expired key would otherwise resurrect it.
    Maps to HTTP ``409`` with a specific ``code`` (e.g. ``key_not_active``).
    """

    http_status = 409
    error_type = "conflict"


class QueueFullError(ConcurrencyLimitError):
    """The admission queue is full; the request was not enqueued (ADR 0023).

    Returned when a newly arriving request would exceed the configured queue
    depth (global or per model). Maps to **429** ``queue_full`` — the client is
    contributing to overload and should back off — and carries ``Retry-After``.
    Subclasses :class:`ConcurrencyLimitError` so existing 429 handling (and the
    "a quota 429 is never queued" invariant) is unaffected. Distinct from a
    per-key quota 429 (``rate_limit_exceeded`` / ``insufficient_quota``): this is
    a *server* capacity signal, not a per-key limit, and it is never itself
    re-queued. Carries the ``trigger`` that caused the request to need queueing.
    """

    error_type = "queue_full"

    def __init__(
        self,
        message: str,
        *,
        trigger: str | None = None,
        depth: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.trigger = trigger
        self.depth = depth

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["error"]["trigger"] = self.trigger
        return payload


class QueueTimeoutError(BrokerError):
    """A queued request waited longer than ``BROKER_QUEUE_MAX_WAIT_SECONDS`` (ADR 0023).

    Maps to **503** ``queue_timeout``: the service was overloaded for the whole
    wait budget, independent of the caller (vLLM rejects an over-full admission
    queue with 503; Envoy overflows its pending-request queue with 503). Carries
    ``Retry-After`` and the ``trigger`` that kept the request waiting so an
    operator/client sees *why*. A timed-out request never started streaming, so
    the client can safely retry.
    """

    http_status = 503
    error_type = "queue_timeout"

    def __init__(
        self,
        message: str,
        *,
        trigger: str | None = None,
        waited_ms: float | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message, **kwargs)
        self.trigger = trigger
        self.waited_ms = waited_ms

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["error"]["trigger"] = self.trigger
        return payload
