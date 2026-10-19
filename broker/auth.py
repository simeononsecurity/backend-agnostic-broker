"""API-key authentication.

When ``auth_enabled`` is true, every API route except the liveness probe
(``/healthz``) and ``/metrics`` requires a valid key presented as either
``Authorization: Bearer <key>`` or ``X-API-Key: <key>``. Keys are compared in
constant time. Keys come from config/secrets and are never logged or committed.
"""

from __future__ import annotations

import hmac

from .config import Settings
from .errors import AuthError


def _extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    return None


def extract_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Public: pull the presented secret from Authorization/X-API-Key headers."""
    return _extract_key(authorization, x_api_key)


def check_auth(
    settings: Settings,
    authorization: str | None,
    x_api_key: str | None,
) -> None:
    if not settings.auth_enabled:
        return
    presented = _extract_key(authorization, x_api_key)
    if not presented:
        raise AuthError(
            "missing API key",
            remediation="send Authorization: Bearer <key> or X-API-Key",
        )
    for key in settings.api_keys:
        # Byte-wise compare: hmac.compare_digest raises TypeError on a non-ASCII
        # str, which on this unauthenticated-reachable path would surface as a 500
        # instead of a clean 401. Comparing UTF-8/surrogateescape bytes avoids it.
        try:
            if hmac.compare_digest(
                presented.encode("utf-8", "surrogateescape"),
                key.encode("utf-8", "surrogateescape"),
            ):
                return
        except Exception:  # noqa: BLE001 - encoding failure means "not a match"
            continue
    raise AuthError(
        "invalid API key",
        remediation="use a configured API key",
    )


def _key_match(presented: str, candidates) -> bool:
    """Byte-wise constant-time membership test for a presented secret.

    ``hmac.compare_digest`` raises ``TypeError`` on a non-ASCII ``str``; comparing
    UTF-8/surrogateescape **bytes** avoids surfacing that as a 500 on an
    unauthenticated-reachable path. A malformed key simply never matches.
    """
    pb = presented.encode("utf-8", "surrogateescape")
    for key in candidates:
        try:
            if hmac.compare_digest(pb, key.encode("utf-8", "surrogateescape")):
                return True
        except Exception:  # noqa: BLE001 - encoding failure means "not a match"
            continue
    return False


def admin_key_match(settings: Settings, presented: str) -> bool:
    """True if ``presented`` is one of the configured ``BROKER_ADMIN_API_KEYS``."""
    return _key_match(presented, settings.admin_api_keys)


def check_admin(
    settings: Settings,
    authorization: str | None,
    x_api_key: str | None,
    x_admin_key: str | None = None,
) -> None:
    """Authorize a privileged admin request against ``BROKER_ADMIN_API_KEYS``.

    Deliberately separate from :func:`check_auth`: admin actions (model unload/
    load, registry reload) are gated by a dedicated ``admin_api_keys`` list, not
    the normal API keys, so the control plane is isolated from the data plane.
    The integration layer (``require_admin`` in :mod:`broker.app`) also accepts a
    virtual key whose scope is ``admin`` (ADR 0008) before falling back here. The
    admin key may be presented as ``X-Admin-Key``, ``Authorization: Bearer`` or
    ``X-API-Key``. Comparison is byte-wise so a non-ASCII/over-long key fails
    closed with a clean 401 rather than a 500.
    """
    presented = x_admin_key.strip() if x_admin_key else _extract_key(authorization, x_api_key)
    if not presented:
        raise AuthError(
            "missing admin key",
            remediation="send X-Admin-Key (or Authorization: Bearer) with an admin API key",
        )
    if admin_key_match(settings, presented):
        return
    raise AuthError(
        "invalid admin key",
        remediation="use a configured BROKER_ADMIN_API_KEYS value",
    )
