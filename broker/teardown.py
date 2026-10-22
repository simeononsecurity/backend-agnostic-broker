"""Deadline-driven, descendant-aware runtime teardown (ADR 0024).

Stopping the broker must stop everything it started, within a known time, on
every platform. Two problems made the v0.6.0 stop path unsafe:

* ``killpg`` only reaches the runtime's *own* process group. A GPU runtime that
  starts a worker in a new session (``setsid``) — common for CUDA wrappers —
  leaves that worker holding device memory after the broker exits.
* Nothing bounded the grace periods against the service manager's stop timeout,
  so a runtime that ignored ``SIGTERM`` (or a load still in progress) could hang
  the broker until the service manager force-killed it, skipping all cleanup.

This module gives a single, synchronous teardown routine that is always run off
the event loop (``asyncio.to_thread``) and is **identity-gated** (ADR 0021): it
never signals a PID it cannot prove the broker started. The kill set is the
runtime's process group **plus every recorded descendant**; each descendant was
recorded (PID + start time) while its parent was alive, so an escaped
``setsid`` child is still reachable after the parent dies.

Descendant tracking differs by platform, matching the ADR:

* **Linux** — the broker makes itself a child subreaper
  (:func:`set_child_subreaper` via ``prctl``) so re-parented descendants come to
  it, and descendants are enumerated from ``/proc`` parent-PID links.
* **Other OSes** — a periodic argv-only ``ps -A`` snapshot records descendants
  while the runtime runs (there is no subreaper / ``/proc``).
"""

from __future__ import annotations

import ctypes
import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import runtime_identity as rid
from .metrics import RUNTIME_TEARDOWN, RUNTIME_TEARDOWN_DURATION
from .resources import process_group_pids

log = logging.getLogger("broker.teardown")

_IS_LINUX = sys.platform.startswith("linux")

#: ``prctl`` option number for PR_SET_CHILD_SUBREAPER (Linux, from <sys/prctl.h>).
PR_SET_CHILD_SUBREAPER = 36


def set_child_subreaper() -> bool:
    """Best-effort: make this process a child subreaper on Linux.

    When set, descendants that outlive their immediate parent are re-parented to
    this process instead of PID 1, so they stay visible under ``/proc`` and can
    be reaped. A no-op (returns ``False``) on non-Linux or if ``prctl`` fails;
    the periodic ``ps`` snapshot covers those platforms. Always logged.
    """
    if not _IS_LINUX:
        return False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        rc = libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    except (OSError, AttributeError, ValueError) as exc:  # pragma: no cover - rare
        log.info("PR_SET_CHILD_SUBREAPER unavailable: %s", exc)
        return False
    if rc != 0:  # pragma: no cover - rare
        log.info("PR_SET_CHILD_SUBREAPER failed (rc=%s errno=%s)", rc, ctypes.get_errno())
        return False
    log.info("installed as child subreaper (descendant teardown is tree-wide)")
    return True


def _linux_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return None
    rparen = data.rfind(")")
    if rparen == -1:
        return None
    rest = data[rparen + 1 :].split()
    # After the "(comm)" token: state(0) ppid(1) pgrp(2) ...
    if len(rest) < 2:
        return None
    try:
        return int(rest[1])
    except ValueError:
        return None


def ppid_map() -> dict[int, int]:
    """Map every visible PID to its parent PID (argv-only, no shell).

    Linux reads ``/proc/<pid>/stat``; other platforms run ``ps -A -o pid=,ppid=``
    with a timeout. Returns an empty map on any failure (teardown then falls back
    to the process group alone).
    """
    out: dict[int, int] = {}
    if _IS_LINUX and os.path.isdir("/proc"):
        try:
            entries = os.listdir("/proc")
        except OSError:  # pragma: no cover - defensive
            return out
        for entry in entries:
            if not entry.isdigit():
                continue
            ppid = _linux_ppid(int(entry))
            if ppid is not None:
                out[int(entry)] = ppid
        return out
    try:
        res = subprocess.run(
            ["/bin/ps", "-A", "-o", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - rare
        return out
    if res.returncode != 0:
        return out
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            out[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return out


def enumerate_descendants(root_pid: int | None, ppids: dict[int, int] | None = None) -> set[int]:
    """Transitive set of live descendant PIDs of ``root_pid`` (excludes root).

    Walks parent-PID links, so it finds a ``setsid`` child (whose session/pgid
    changed but whose parent PID is still the runtime) while the runtime is alive.
    """
    if root_pid is None or root_pid <= 0:
        return set()
    if ppids is None:
        ppids = ppid_map()
    children: dict[int, list[int]] = {}
    for pid, ppid in ppids.items():
        children.setdefault(ppid, []).append(pid)
    seen: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in seen or pid == root_pid:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, []))
    return seen


def record_start_times(pids: set[int]) -> dict[int, str]:
    """Snapshot ``{pid: start_time}`` for the live PIDs we can read (identity)."""
    rec: dict[int, str] = {}
    for pid in pids:
        st = rid.process_start_time(pid)
        if st:
            rec[pid] = st
    return rec


@dataclass
class TeardownResult:
    """The honest outcome of one runtime stop (ADR 0024).

    ``outcome`` is one of: ``exited`` (nothing was left alive to signal),
    ``signalled`` (``SIGTERM`` reaped the tree within grace), ``killed``
    (``SIGKILL`` was needed), ``survivors`` (something outlived ``SIGKILL`` — a
    stuck device), ``identity_mismatch`` (the recorded runtime was gone/recycled
    and nothing we owned remained), ``noop`` (no process to stop).
    """

    alias: str = ""
    outcome: str = "noop"
    signalled: list[int] = field(default_factory=list)
    exited: list[int] = field(default_factory=list)
    survivors: list[int] = field(default_factory=list)
    duration: float = 0.0

    def ok(self) -> bool:
        """True when nothing survived the teardown."""
        return not self.survivors

    def to_dict(self) -> dict:
        return {
            "alias": self.alias,
            "outcome": self.outcome,
            "signalled": sorted(self.signalled),
            "exited": sorted(self.exited),
            "survivors": sorted(self.survivors),
            "duration": round(self.duration, 3),
        }


def record_metrics(result: TeardownResult) -> None:
    """Count the outcome and observe the duration (best effort)."""
    try:
        RUNTIME_TEARDOWN.labels(outcome=result.outcome).inc()
        RUNTIME_TEARDOWN_DURATION.observe(result.duration)
    except Exception:  # noqa: BLE001 - metrics must never break a stop
        pass


def _dead(pid: int) -> bool:
    return rid._dead_or_zombie(pid)


def _signal_pid(pid: int, sig: int) -> None:
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError as exc:  # pragma: no cover - rare
        log.warning("signal %s of pid %s failed: %s", sig, pid, exc)


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError as exc:  # pragma: no cover - rare
        log.warning("killpg %s of pgid %s failed: %s", sig, pgid, exc)


def terminate_tree(
    *,
    main_pid: int | None,
    main_start_time: str | None,
    pgid: int | None,
    descendants: dict[int, str] | None = None,
    grace: float,
    hard_deadline: float | None = None,
    sigkill_only: bool = False,
    force_check: Callable[[], bool] | None = None,
    alias: str = "",
    backend: str = "",
) -> TeardownResult:
    """Synchronously tear down a runtime's whole tree, identity-gated.

    The kill set is the runtime's process group plus every still-alive recorded
    (and freshly-enumerated) descendant. ``SIGTERM`` first, then ``SIGKILL`` once
    the per-runtime ``grace`` elapses, the overall ``hard_deadline`` (monotonic)
    passes, or ``force_check`` returns True (a second shutdown signal).
    ``sigkill_only`` skips straight to ``SIGKILL``. Always run via
    ``asyncio.to_thread`` — it sleeps, so it must not run on the event loop.
    """
    start = time.monotonic()
    descendants = dict(descendants or {})
    result = TeardownResult(alias=alias)

    # Identity gate the recorded main process: a mismatch means its PID was
    # recycled, so neither it nor its (possibly reused) process group is ours.
    main_ok = (
        main_pid is not None
        and main_start_time is not None
        and rid.identity_matches(main_pid, main_start_time)
    )
    if main_pid is not None and main_start_time is None:
        # No recorded identity (e.g. a fresh spawn handle we still hold): trust it.
        main_ok = rid.pid_alive(main_pid)

    # Build the target set.
    targets: set[int] = set()
    live_ppids = ppid_map()
    if main_ok and main_pid is not None:
        targets.add(main_pid)
        targets |= process_group_pids(pgid, main_pid)
        for pid in enumerate_descendants(main_pid, live_ppids):
            targets.add(pid)
            descendants.setdefault(pid, rid.process_start_time(pid) or "")
    elif main_pid is not None:
        result.outcome = "identity_mismatch"
    # Recorded descendants: keep only those still alive with a matching identity
    # (or no recorded start time, which we then re-verify as merely alive).
    for pid, st in descendants.items():
        if pid <= 0 or pid == main_pid:
            continue
        if st:
            if rid.identity_matches(pid, st):
                targets.add(pid)
        elif rid.pid_alive(pid) and not rid.process_is_zombie(pid):
            targets.add(pid)

    targets = {p for p in targets if p > 0}
    alive_targets = {p for p in targets if not _dead(p)}
    if not alive_targets:
        result.duration = time.monotonic() - start
        result.exited = sorted(targets)
        if result.outcome != "identity_mismatch":
            result.outcome = "exited" if targets else "noop"
        record_metrics(result)
        return result

    result.signalled = sorted(alive_targets)

    def _poll_until_gone(deadline: float) -> set[int]:
        while True:
            remaining = {p for p in alive_targets if not _dead(p)}
            if not remaining:
                return remaining
            now = time.monotonic()
            if now >= deadline:
                return remaining
            if hard_deadline is not None and now >= hard_deadline:
                return remaining
            if force_check is not None and force_check():
                return remaining
            time.sleep(0.05)

    if not sigkill_only:
        if pgid is not None and main_ok:
            _killpg(pgid, signal.SIGTERM)
        for pid in alive_targets:
            _signal_pid(pid, signal.SIGTERM)
        grace_deadline = start + max(0.0, grace)
        if hard_deadline is not None:
            grace_deadline = min(grace_deadline, hard_deadline)
        remaining = _poll_until_gone(grace_deadline)
        if not remaining:
            result.outcome = "signalled"
            result.exited = sorted(alive_targets)
            result.duration = time.monotonic() - start
            record_metrics(result)
            return result

    # Escalate to SIGKILL of the group and every remaining target.
    if pgid is not None and main_ok:
        _killpg(pgid, signal.SIGKILL)
    for pid in alive_targets:
        if not _dead(pid):
            _signal_pid(pid, signal.SIGKILL)
    kill_deadline = time.monotonic() + 2.0
    if hard_deadline is not None:
        kill_deadline = min(kill_deadline, max(hard_deadline, time.monotonic() + 0.2))
    survivors = _poll_until_gone(kill_deadline)
    result.survivors = sorted(survivors)
    result.exited = sorted(alive_targets - survivors)
    result.duration = time.monotonic() - start
    if survivors:
        result.outcome = "survivors"
        log.error(
            "runtime %s (backend=%s): %d process(es) survived SIGKILL: %s; "
            "device memory may not be released",
            alias or "?",
            backend or "?",
            len(survivors),
            sorted(survivors),
        )
    else:
        result.outcome = "killed"
    record_metrics(result)
    return result
