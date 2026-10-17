"""Entrypoint: ``python -m broker``.

Loads a local ``.env`` into the process environment first (stdlib only, no
third-party dependency) so that *non-``BROKER_``-prefixed* variables referenced
by the registry — e.g. ``${OLLAMA_BASE_URL}`` pointing at a remote Ollama daemon
— are available to the registry expander. Variables already present in the
environment always win, so Docker/compose/secret injection overrides ``.env``.
"""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .app import create_app
from .config import get_settings


def _load_local_env(path: str = ".env") -> None:
    """Load ``KEY=VALUE`` pairs from a local ``.env`` into ``os.environ``.

    Minimal, dependency-free, and non-overriding: an existing environment
    variable is never clobbered. Supports ``#`` comments, blank lines, optional
    surrounding quotes, and an optional ``export`` prefix. Values are taken
    verbatim (no shell expansion) — a filled-in ``.env`` is never committed.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key and key not in os.environ:
            os.environ[key] = val


def main() -> None:
    _load_local_env()
    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
