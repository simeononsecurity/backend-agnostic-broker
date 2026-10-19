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
        if hmac.compare_digest(presented, key):
            return
    raise AuthError(
        "invalid API key",
        remediation="use a configured API key",
    )


def check_admin(
    settings: Settings,
    authorization: str | None,
    x_api_key: str | None,
    x_admin_key: str | None = None,
) -> None:
    """Authorize a privileged admin request (ADR 0012).

    Deliberately separate from :func:`check_auth`: admin actions (model unload/
    load, registry reload) are gated by a dedicated ``admin_api_keys`` list, not
    the normal API keys, so the control plane is isolated from the data plane. A
    future virtual-key admin scope (ADR 0008) can replace this one function
    without touching the routes. The admin key may be presented as
    ``X-Admin-Key``, ``Authorization: Bearer ...``, or ``X-API-Key``.
    """
    presented = x_admin_key.strip() if x_admin_key else _extract_key(authorization, x_api_key)
    if not presented:
        raise AuthError(
            "missing admin key",
            remediation="send X-Admin-Key (or Authorization: Bearer) with an admin API key",
        )
    for key in settings.admin_api_keys:
        if hmac.compare_digest(presented, key):
            return
    raise AuthError(
        "invalid admin key",
        remediation="use a configured BROKER_ADMIN_API_KEYS value",
    )
