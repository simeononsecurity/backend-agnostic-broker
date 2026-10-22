"""Entrypoint: ``python -m broker`` (serve) and ``python -m broker cleanup``.

``serve`` (the default, also the bare ``python -m broker``) runs the FastAPI app
under uvicorn. ``cleanup`` is a bounded, server-less one-shot that reaps stale
device leases (ADR 0026) — meant for a systemd ``ExecStopPost=`` so a forced stop
frees the device for a co-tenant within seconds instead of at the next start.

Both load a local ``.env`` into the process environment first (stdlib only, no
third-party dependency) so that *non-``BROKER_``-prefixed* variables referenced
by the registry — e.g. ``${OLLAMA_BASE_URL}`` pointing at a remote Ollama daemon
— are available to the registry expander. Variables already present in the
environment always win, so Docker/compose/secret injection overrides ``.env``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

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


def _serve() -> None:
    import uvicorn

    from .app import create_app

    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


def _cleanup(timeout: float, report: bool) -> int:
    """Reap stale device leases; print a JSON summary; return an exit code.

    Bounded by ``timeout`` (default 20 s), runs no server. A live orphan runtime
    (its broker is dead) follows ``BROKER_RUNTIME_RECONCILE``: ``stop`` kills the
    runtime (identity-gated) then releases, everything else keeps it and marks it
    ``orphaned``. ``--report`` forces keep-and-mark even in ``stop`` mode. Exit 0
    on success, 1 if any per-lease error occurred.
    """
    from .leases import DeviceLeaseStore, cleanup_leases

    settings = get_settings()
    if not settings.leases_enabled():
        print(json.dumps({"enabled": False, "reason": "BROKER_DEVICE_LEASES=off"}))
        return 0
    store = DeviceLeaseStore(settings.device_lease_dir_resolved())
    mode = settings.runtime_reconcile
    kill_orphans = (mode == "stop") and not report
    deadline = time.monotonic() + max(1.0, timeout)
    # Keep the identity-gated SIGTERM->SIGKILL window small so the whole run stays
    # well under the bound even for a stubborn runtime.
    kill_grace = min(settings.shutdown_timeout_seconds, 5.0)
    summary = cleanup_leases(
        store,
        kill_orphans=kill_orphans,
        release_command=settings.lease_release_command,
        allowed_exec_dirs=settings.allowed_exec_dirs,
        hook_timeout=settings.lease_hook_timeout_seconds,
        kill_grace=kill_grace,
        deadline=deadline,
    )
    summary["enabled"] = True
    summary["mode"] = mode
    summary["kill_orphans"] = kill_orphans
    print(json.dumps(summary))
    return 1 if summary.get("errors") else 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m broker")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run the broker HTTP server (default)")
    cp = sub.add_parser("cleanup", help="reap stale device leases and exit (ADR 0026)")
    cp.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="overall time budget in seconds (default 20)",
    )
    cp.add_argument(
        "--report",
        action="store_true",
        help="never kill an orphan runtime; keep and mark it orphaned",
    )
    args = parser.parse_args(argv)

    _load_local_env()
    if args.command == "cleanup":
        raise SystemExit(_cleanup(args.timeout, args.report))
    _serve()


if __name__ == "__main__":
    main(sys.argv[1:])
