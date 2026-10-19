"""Per-error retry / cooldown classification (ADR 0017).

ADR 0009 shipped a single retryable error set (``_FAILOVER_ERRORS``) and benched a
member on any of them. Production gateways instead classify failures and apply a
*per-class* policy: LiteLLM's ``RetryPolicy`` sets ``num_retries`` per exception
type and ``AllowedFailsPolicy`` sets per-type cooldown thresholds; a 4xx client
error (auth, not-found, bad-request) is never retried. This module adds that
classification without changing default behavior.

Design
------
* :class:`ErrorClass` buckets a :class:`~broker.errors.BrokerError` into one of
  ``timeout`` / ``connection`` / ``server_error`` / ``backpressure`` /
  ``client_error`` / ``other``.
* :func:`classify_error` maps an exception to a class (subclass first, then HTTP
  status).
* :class:`RetryPolicy` reads the per-class flags off :class:`~broker.config.Settings`
  and answers three questions the manager's retry driver asks: *is this class
  retryable?*, *does it count toward a member's cooldown?*, and *is there a
  per-class attempt cap?* ``client_error`` and ``other`` are **hard-wired**
  non-retryable and never counted (4xx is never retried or benched, per the
  invariant), regardless of configuration.

Defaults reproduce ADR 0009 / v0.5.0 exactly: ``timeout``, ``connection`` and
``server_error`` are retried and count toward cooldown; ``backpressure`` (429)
and ``client_error`` (4xx) are not. Upstream ``Retry-After`` (RFC 9110 §10.2.3:
either delay-seconds or an HTTP-date) is honored within the retry budget when a
retried error carries it.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Any

from .errors import (
    BackendUnavailableError,
    ConcurrencyLimitError,
    InsufficientQuotaError,
    ModelLoadError,
    RateLimitExceededError,
    TimeoutError_,
)


class ErrorClass(str, Enum):
    """Coarse failure class a retry/cooldown policy keys on."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    SERVER_ERROR = "server_error"
    BACKPRESSURE = "backpressure"  # 429 rate-limit / concurrency backpressure
    CLIENT_ERROR = "client_error"  # 4xx (except 429): never retried or counted
    OTHER = "other"


#: Classes that can never be retried or benched, whatever the config says (the
#: "4xx client errors never retried or counted" invariant).
_NEVER = frozenset({ErrorClass.CLIENT_ERROR, ErrorClass.OTHER})

_VALID_CLASS_NAMES = frozenset(c.value for c in ErrorClass)


def classify_error(exc: BaseException) -> ErrorClass:
    """Bucket an exception into an :class:`ErrorClass`.

    Subclass identity wins (it carries intent); otherwise the HTTP status on a
    :class:`~broker.errors.BrokerError` decides (429 → backpressure, other 4xx →
    client_error, 5xx → server_error). Anything else is ``other``.
    """
    if isinstance(exc, TimeoutError_):
        return ErrorClass.TIMEOUT
    if isinstance(exc, ModelLoadError):
        return ErrorClass.SERVER_ERROR
    if isinstance(exc, BackendUnavailableError):
        return ErrorClass.CONNECTION
    if isinstance(exc, (ConcurrencyLimitError, RateLimitExceededError, InsufficientQuotaError)):
        return ErrorClass.BACKPRESSURE
    status = getattr(exc, "http_status", None)
    if isinstance(status, int):
        if status == 429:
            return ErrorClass.BACKPRESSURE
        if 400 <= status < 500:
            return ErrorClass.CLIENT_ERROR
        if status >= 500:
            return ErrorClass.SERVER_ERROR
    return ErrorClass.OTHER


def _retry_after_header(exc: BaseException) -> str | None:
    """Return the raw ``Retry-After`` value carried on an error, if any."""
    headers = getattr(exc, "headers", None) or {}
    for k, v in headers.items():
        if str(k).lower() == "retry-after":
            return str(v)
    ra = getattr(exc, "retry_after", None)
    return str(ra) if ra is not None else None


def retry_after_seconds(exc: BaseException, now: float | None = None) -> float | None:
    """Parse an error's ``Retry-After`` into seconds-to-wait (RFC 9110 §10.2.3).

    Accepts either a non-negative ``delay-seconds`` number or an HTTP-date; a
    date already in the past yields ``0``. Returns ``None`` when no parseable
    header is present.
    """
    raw = _retry_after_header(exc)
    if raw is None:
        return None
    s = raw.strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return max(0.0, float(s))
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(s)
        if dt is None:
            return None
        delta = dt.timestamp() - (time.time() if now is None else now)
        return max(0.0, delta)
    except (TypeError, ValueError, OverflowError):
        return None


class RetryPolicy:
    """Per-class retry/cooldown decisions derived from :class:`Settings`.

    Built once per manager (and rebuilt on registry reload). The defaults
    reproduce ADR 0009: timeout/connection/server_error are retried and counted;
    backpressure and client/other are not.
    """

    def __init__(self, settings: Any) -> None:
        self._retryable: dict[ErrorClass, bool] = {
            ErrorClass.TIMEOUT: bool(getattr(settings, "retry_on_timeout", True)),
            ErrorClass.CONNECTION: bool(getattr(settings, "retry_on_connection", True)),
            ErrorClass.SERVER_ERROR: bool(getattr(settings, "retry_on_server_error", True)),
            ErrorClass.BACKPRESSURE: bool(getattr(settings, "retry_on_backpressure", False)),
            ErrorClass.CLIENT_ERROR: False,
            ErrorClass.OTHER: False,
        }
        self._cooldown: dict[ErrorClass, bool] = {
            ErrorClass.TIMEOUT: bool(getattr(settings, "cooldown_on_timeout", True)),
            ErrorClass.CONNECTION: bool(getattr(settings, "cooldown_on_connection", True)),
            ErrorClass.SERVER_ERROR: bool(getattr(settings, "cooldown_on_server_error", True)),
            ErrorClass.BACKPRESSURE: bool(getattr(settings, "cooldown_on_backpressure", False)),
            ErrorClass.CLIENT_ERROR: False,
            ErrorClass.OTHER: False,
        }
        self._by_class: dict[ErrorClass, int] = {}
        for name, count in (getattr(settings, "retry_max_attempts_by_class", {}) or {}).items():
            if name in _VALID_CLASS_NAMES:
                self._by_class[ErrorClass(name)] = int(count)
        self.respect_retry_after: bool = bool(getattr(settings, "retry_respect_retry_after", True))

    def is_retryable(self, cls: ErrorClass) -> bool:
        if cls in _NEVER:
            return False
        return self._retryable.get(cls, False)

    def counts_toward_cooldown(self, cls: ErrorClass) -> bool:
        if cls in _NEVER:
            return False
        return self._cooldown.get(cls, False)

    def max_attempts_for(self, cls: ErrorClass) -> int | None:
        """Per-class attempt cap, or ``None`` to use the global attempt budget."""
        return self._by_class.get(cls)
