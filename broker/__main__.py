"""Entrypoint: ``python -m broker``.

Loads a local ``.env`` into the process environment first (stdlib only, no
third-party dependency) so that *non-``BROKER_``-prefixed* variables referenced
by the registry — e.g. ``${OLLAMA_BASE_URL}`` pointing at a remote Ollama daemon
— are available to the registry expander. Variables already present in the
environment always win, so Docker/compose/secret injection overrides ``.env``.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import uvicorn

from .app import create_app
from .config import get_settings
from .teardown import set_child_subreaper


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


class _BoundedServer(uvicorn.Server):
    """uvicorn server that escalates a second stop signal to force-kill runtimes.

    The first SIGTERM/SIGINT begins a graceful, budget-bounded shutdown (ADR
    0024). A second one while already stopping tells the manager to SIGKILL every
    remaining runtime tree immediately instead of waiting out the grace.
    """

    def __init__(self, config: uvicorn.Config, app) -> None:
        super().__init__(config)
        self._broker_app = app

    def handle_exit(self, sig, frame) -> None:  # type: ignore[override]
        if self.should_exit:
            # Already stopping -> this is the second signal: force-kill runtimes.
            manager = getattr(self._broker_app.state, "manager", None)
            if manager is not None:
                manager.request_force_shutdown()
        super().handle_exit(sig, frame)


def main() -> None:
    _load_local_env()
    settings = get_settings()
    # ADR 0024: become a child subreaper on Linux (best effort, logged) BEFORE any
    # runtime is spawned, so an escaped descendant is re-parented to the broker and
    # can be found and reaped at teardown.
    set_child_subreaper()
    app = create_app(settings)
    # Bound uvicorn's graceful phase by the shutdown budget so an open request or
    # stream cannot hold up shutdown past the budget (ADR 0024).
    graceful = max(1, int(math.ceil(settings.shutdown_budget_seconds)))
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=graceful,
    )
    server = _BoundedServer(config, app)
    server.run()


if __name__ == "__main__":
    main()
