"""Optional static web UI / operator playground (ADR 0015).

Default-OFF. When ``BROKER_UI_ENABLED`` is true, a hardened, fully-offline
single-page app is served at ``/ui``. It is **static only**: vanilla HTML/CSS/JS
committed as package data under ``broker/ui/static/`` with no build step, no CDN,
and no external URLs. The UI calls the broker's **existing authenticated API**
with a key the operator enters at runtime (held in browser memory only, never
persisted). Registering the UI adds **no new unauthenticated data endpoints** —
only static-asset routes under ``/ui``.

Security headers on every UI response (including 404s):

* ``Content-Security-Policy: default-src 'self'; base-uri 'none';
  frame-ancestors 'none'; object-src 'none'; form-action 'self'`` — NO
  ``unsafe-inline``/``unsafe-eval`` and no external origins, so every script,
  style, font, image, and connection is same-origin.
* ``X-Content-Type-Options: nosniff``
* ``Referrer-Policy: no-referrer``
* ``X-Frame-Options: DENY`` (defense-in-depth with ``frame-ancestors``)
* ``Cache-Control: no-store``

Path traversal is defeated by resolving every requested path and requiring it to
stay under the static directory; only allow-listed asset extensions are served.

Robustness: ``_serve_asset`` **never raises** for any client-supplied path. Hostile
inputs (embedded NUL, control characters, backslashes, drive-like prefixes, or
over-long segments/paths) are rejected *before* the filesystem is touched, and the
remaining ``resolve``/``is_file``/``read_bytes`` calls are wrapped so a stray
``ValueError``/``OSError`` still degrades to the hardened 404 (security headers
attached) rather than a header-less 500.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import Response

from .config import Settings

# Directory holding the pre-built, plain static assets (package data).
_STATIC_DIR = (Path(__file__).parent / "ui" / "static").resolve()

# Allow-listed asset extensions -> Content-Type. Anything else 404s so the UI
# can never be used to read arbitrary package files.
_CONTENT_TYPES: dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}

# Strict CSP: everything is same-origin; no inline/eval; cannot be framed.
_CSP = (
    "default-src 'self'; "
    "base-uri 'none'; "
    "frame-ancestors 'none'; "
    "object-src 'none'; "
    "form-action 'self'"
)

_SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": _CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def _not_found() -> Response:
    return Response(
        status_code=404,
        content=b"not found",
        media_type="text/plain; charset=utf-8",
        headers=dict(_SECURITY_HEADERS),
    )


# Defensive caps applied before any filesystem access. ``NAME_MAX`` is 255 on the
# common filesystems; a generous whole-path cap guards against pathological inputs.
_MAX_SEGMENT_LEN = 255
_MAX_PATH_LEN = 1024


def _is_safe_rel(rel: str) -> bool:
    """Reject obviously hostile request paths *before* touching the filesystem.

    An embedded NUL makes ``Path.resolve`` raise ``ValueError`` and an over-long
    name makes it (or ``is_file``) raise ``OSError`` (``ENAMETOOLONG``); both would
    otherwise escape as a header-less 500. We refuse NUL/control characters,
    backslashes, Windows drive-like prefixes, and anything over the length caps so
    the request is answered with the hardened 404 instead.
    """
    if not rel or len(rel) > _MAX_PATH_LEN:
        return False
    for ch in rel:
        # NUL, C0 controls, and DEL have no place in an asset name.
        if ch == "\x00" or ord(ch) < 0x20 or ord(ch) == 0x7F:
            return False
    if "\\" in rel:
        return False
    # Drive-like prefix (``c:``) or any bare ``:`` — not a valid static asset name.
    if ":" in rel:
        return False
    for seg in rel.split("/"):
        if len(seg) > _MAX_SEGMENT_LEN:
            return False
    return True


def _serve_asset(rel: str) -> Response:
    """Serve a static UI asset confined to the static dir, or 404.

    ``rel`` is the request path under ``/ui``. Leading slashes are stripped and an
    empty path maps to ``index.html``. The candidate path is resolved and MUST
    remain under :data:`_STATIC_DIR` (defeats ``..`` traversal and symlink
    escapes); only allow-listed extensions are returned.

    This function never raises for client-supplied input: hostile paths are
    rejected up front by :func:`_is_safe_rel`, and the filesystem calls are wrapped
    so any residual ``ValueError``/``OSError`` falls through to the hardened 404.
    """
    rel = rel.lstrip("/")
    if not rel:
        rel = "index.html"
    if not _is_safe_rel(rel):
        return _not_found()
    try:
        candidate = (_STATIC_DIR / rel).resolve()
        candidate.relative_to(_STATIC_DIR)
        if not candidate.is_file():
            return _not_found()
        ctype = _CONTENT_TYPES.get(candidate.suffix.lower())
        if ctype is None:
            return _not_found()
        payload = candidate.read_bytes()
    except (ValueError, OSError):
        return _not_found()
    return Response(
        content=payload,
        media_type=ctype,
        headers=dict(_SECURITY_HEADERS),
    )


def register_ui(app: FastAPI, settings: Settings) -> None:
    """Register the ``/ui`` static routes **only** when enabled (ADR 0015).

    When ``BROKER_UI_ENABLED`` is false (the default), no route is registered and
    ``/ui`` returns the app's normal 404 — the UI surface does not exist.
    """
    if not settings.ui_enabled:
        return

    @app.get("/ui", include_in_schema=True)
    async def ui_index() -> Response:  # pragma: no cover - thin wrapper
        return _serve_asset("index.html")

    @app.get("/ui/", include_in_schema=False)
    async def ui_index_slash() -> Response:  # pragma: no cover - thin wrapper
        return _serve_asset("index.html")

    @app.get("/ui/{path:path}", include_in_schema=False)
    async def ui_asset(path: str) -> Response:  # pragma: no cover - thin wrapper
        return _serve_asset(path)
