"""Security boundary helpers.

This module centralizes every trust boundary the broker enforces:

* **Executable allow-listing** — runtime binaries must be explicitly allowed,
  absolute, and real files. Requests can never choose an arbitrary executable.
* **Model-path validation** — model paths must resolve *inside* configured
  allowed roots (defeats ``..`` path traversal and symlink escapes).
* **Request flag rejection** — request bodies may not inject CLI flags/args.
* **argv construction** — child processes are always built as an argv *list*
  (never a shell string), from a template with whitelisted placeholders only.
* **SSRF guard** — upstream URLs must match a configured allow-list of backend
  base URLs; link-local / cloud-metadata / unexpected hosts are rejected.

All functions raise :class:`SecurityError` or :class:`ValidationError` on
failure so callers never proceed with an unsafe value.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlparse

from .errors import SecurityError, ValidationError

# Placeholders we allow inside argv templates. Anything else is rejected so a
# template cannot smuggle in request-controlled data.
_PLACEHOLDER_RE = re.compile(r"^\{([a-z0-9_]+)\}$")

# Characters that must never appear in a value substituted into argv. Even
# though we never use a shell, we defensively reject shell metacharacters and
# NUL so a value can't be abused if argv is ever logged/replayed into a shell.
_UNSAFE_VALUE_RE = re.compile(r"[\x00\n\r;&|`$><]")

# Flag-looking tokens are rejected from request-derived values.
_FLAG_RE = re.compile(r"(^|\s)--?[a-zA-Z]")


def validate_executable(path: str, allowlist: Sequence[str]) -> str:
    """Return a safe absolute executable path or raise.

    ``path`` must be absolute, exist, be a regular file, be executable, and sit
    inside one of the allow-listed directories (or equal an allow-listed file).
    """
    if not path:
        raise SecurityError("empty executable path", remediation="set a runtime executable path")
    p = Path(path)
    if not p.is_absolute():
        raise SecurityError(
            f"executable path must be absolute: {path!r}",
            remediation="use an absolute path to the runtime binary",
        )
    rp = p.resolve()
    if not rp.exists() or not rp.is_file():
        raise SecurityError(
            f"executable not found: {path!r}",
            remediation="install the runtime or fix the configured path",
        )
    if not os.access(rp, os.X_OK):
        raise SecurityError(
            f"path is not executable: {path!r}",
            remediation="chmod +x the runtime binary",
        )
    allowed = False
    for entry in allowlist:
        ep = Path(entry).resolve()
        if rp == ep:
            allowed = True
            break
        try:
            rp.relative_to(ep)
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise SecurityError(
            f"executable {str(rp)!r} is not in the allow-list",
            remediation="add its directory to BROKER_ALLOWED_EXEC_DIRS",
        )
    return str(rp)


def validate_model_path(path: str, allowed_roots: Sequence[str]) -> str:
    """Return a safe absolute model path confined to an allowed root, or raise.

    Defeats path traversal (``..``) and symlink escapes by resolving the real
    path and requiring it to be *under* an allowed root.
    """
    if not path:
        raise ValidationError("empty model path", remediation="set the model path in the registry")
    if not allowed_roots:
        raise SecurityError(
            "no allowed model roots configured",
            remediation="set BROKER_ALLOWED_MODEL_ROOTS",
        )
    rp = Path(path).resolve()
    for root in allowed_roots:
        root_rp = Path(root).resolve()
        try:
            rp.relative_to(root_rp)
        except ValueError:
            continue
        if not rp.exists():
            raise ValidationError(
                f"model path does not exist: {path!r}",
                remediation="download the model or fix the registry path",
            )
        return str(rp)
    raise SecurityError(
        f"model path {path!r} escapes the allowed model roots",
        remediation="place the model under an allowed root or extend BROKER_ALLOWED_MODEL_ROOTS",
    )


def reject_request_flags(value: object, field: str = "value") -> None:
    """Reject request-supplied values that look like CLI flags/args.

    Called on any request field that could ever reach a child process (e.g. a
    model alias). Arbitrary command flags from requests must be rejected.
    """
    if value is None:
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            reject_request_flags(item, field)
        return
    s = str(value)
    if _FLAG_RE.search(s):
        raise SecurityError(
            f"request field {field!r} contains flag-like tokens: {s!r}",
            remediation="remove CLI-flag-style tokens from the request",
        )
    if _UNSAFE_VALUE_RE.search(s):
        raise SecurityError(
            f"request field {field!r} contains unsafe characters",
            remediation="remove shell metacharacters from the request",
        )


def build_argv(template: Sequence[str], substitutions: Mapping[str, str]) -> list[str]:
    """Build an argv list from a template, substituting whitelisted placeholders.

    * ``template`` is a list of tokens, e.g. ``["{exe}", "--model", "{model_path}"]``.
    * Only whole-token placeholders ``{name}`` are substituted, and only from
      ``substitutions``. Unknown placeholders raise.
    * Substituted values are checked for unsafe characters.
    * The result is a list — it is **never** joined into a shell string.
    """
    argv: list[str] = []
    for token in template:
        m = _PLACEHOLDER_RE.match(token)
        if m:
            key = m.group(1)
            if key not in substitutions:
                raise ConfigErrorLike(f"argv template references unknown placeholder {{{key}}}")
            val = str(substitutions[key])
            if _UNSAFE_VALUE_RE.search(val):
                raise SecurityError(
                    f"unsafe value for placeholder {{{key}}}",
                    remediation="remove shell metacharacters from the value",
                )
            argv.append(val)
        else:
            # Literal token. Must not itself contain a NUL.
            if "\x00" in token:
                raise SecurityError("NUL byte in argv template token")
            argv.append(token)
    if not argv:
        raise ValidationError("empty argv produced from template")
    return argv


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        # cloud metadata endpoint
        or str(ip) == "169.254.169.254"
    )


def validate_upstream_url(
    url: str,
    allowed_bases: Iterable[str],
    *,
    allow_loopback: bool = True,
) -> str:
    """SSRF guard for upstream backend URLs.

    An upstream URL is accepted only if it starts with one of the configured,
    trusted backend base URLs. We additionally resolve the host and reject
    link-local / multicast / reserved / cloud-metadata addresses. Loopback is
    allowed by default because local runtimes bind to 127.0.0.1, but callers may
    disable that.
    """
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SecurityError(
            f"upstream scheme not allowed: {parsed.scheme!r}",
            remediation="use http or https backend URLs",
        )
    base_ok = False
    for base in allowed_bases:
        base = base.rstrip("/")
        if url == base or url.startswith(base + "/"):
            base_ok = True
            break
    if not base_ok:
        raise SecurityError(
            f"upstream URL {url!r} is not in the backend allow-list",
            remediation="only configured backend base URLs may be contacted",
        )
    host = parsed.hostname
    if not host:
        raise SecurityError("upstream URL has no host")
    # Resolve and inspect every address the host maps to.
    try:
        infos = socket.getaddrinfo(host, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SecurityError(
            f"cannot resolve upstream host {host!r}: {exc}",
            remediation="check the backend host configuration",
        ) from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if ip.is_loopback and allow_loopback:
            continue
        if _is_blocked_ip(ip):
            raise SecurityError(
                f"upstream host {host!r} resolves to blocked address {addr}",
                remediation="backends must not resolve to metadata/link-local IPs",
            )
    return url


class ConfigErrorLike(SecurityError):
    """Raised for malformed argv templates (treated as a config/security fault)."""

    error_type = "config_error"
    http_status = 500
