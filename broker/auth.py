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
        if hmac.compare_digest(presented, key):
            return
    raise AuthError(
        "invalid API key",
        remediation="use a configured API key",
    )
