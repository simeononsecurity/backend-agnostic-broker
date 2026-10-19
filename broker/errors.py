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
    """The backend process/endpoint is unreachable."""

    http_status = 502
    error_type = "backend_unavailable"


class TimeoutError_(BrokerError):
    """A request or startup exceeded its configured timeout."""

    http_status = 504
    error_type = "timeout_error"


class ConcurrencyLimitError(BrokerError):
    """Too many concurrent or loaded models/requests for the configured limits."""

    http_status = 429
    error_type = "rate_limit_error"


class RequestTooLargeError(BrokerError):
    """Request body exceeded the configured size limit."""

    http_status = 413
    error_type = "request_too_large"


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
