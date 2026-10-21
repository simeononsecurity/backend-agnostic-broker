"""Durable runtime process identity (PID + start time) — ADR 0021.

A PID alone is unsafe as a durable handle: the kernel recycles PID numbers, so a
record written before an unclean broker death may, by the time the broker
restarts, point at an unrelated process. Signalling it would be cross-process
friendly fire. The fix is to pin identity to a value that is fixed for the life
of a process and is *not* reused with the number — the process **start time** —
and to never signal a PID whose re-read start time does not match the record.

Cross-platform, dependency-free, argv-only (no shell, no psutil):

* **Linux** — ``/proc/<pid>/stat`` field 22 (``starttime``, in ``USER_HZ``
  ticks) divided by ``sysconf(SC_CLK_TCK)`` and anchored to ``/proc/stat btime``
  → an absolute wall-clock start instant, comparable after a restart.
* **macOS / BSD** — ``ps -o lstart= -p <pid>`` (argv list, no shell): the full
  start timestamp, which changes when a PID is reused.

The normalized value is an opaque, platform-specific string: it is only ever
compared for *equality* against a value produced by the same reader on the same
host, so the broker never needs to interpret it.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

log = logging.getLogger("broker.runtime_identity")

_IS_LINUX = sys.platform.startswith("linux")


def clock_ticks() -> int:
    """``sysconf(_SC_CLK_TCK)`` — the USER_HZ divisor (100 on most Linux)."""
    try:
        val = os.sysconf("SC_CLK_TCK")
        if val and val > 0:
            return int(val)
    except (ValueError, OSError):  # pragma: no cover - platform without the name
        pass
    return 100


def parse_linux_boot_time(stat_text: str) -> float | None:
    """Return the ``btime`` (epoch seconds) line value from ``/proc/stat`` text."""
    for line in stat_text.splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def parse_linux_starttime(stat_line: str, btime: float, clk_tck: int) -> float | None:
    """Parse ``/proc/<pid>/stat`` field 22 into an absolute start instant.

    ``field 2`` (``comm``) is wrapped in parentheses and may itself contain
    spaces and ``)`` characters, so the robust parse is to split on the text
    *after the last* ``)``: the first token there is field 3 (``state``), making
    ``starttime`` (field 22) index ``22 - 3 == 19`` of the remainder. The value
    is in clock ticks since boot; ``btime + ticks / clk_tck`` is the absolute
    wall-clock start time in seconds.
    """
    rparen = stat_line.rfind(")")
    if rparen == -1:
        return None
    rest = stat_line[rparen + 1 :].split()
    # rest[0] == state (field 3); starttime is field 22 -> index 19.
    if len(rest) <= 19:
        return None
    try:
        ticks = int(rest[19])
    except ValueError:
        return None
    if clk_tck <= 0:
        return None
    return round(btime + ticks / clk_tck, 2)


def parse_macos_lstart(output: str) -> str | None:
    """Normalize ``ps -o lstart=`` output to a comparable token (collapsed WS)."""
    text = " ".join(output.split())
    return text or None


def _linux_start_time(pid: int) -> str | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
            stat_line = fh.read()
        with open("/proc/stat", encoding="utf-8", errors="replace") as fh:
            btime = parse_linux_boot_time(fh.read())
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None
    if btime is None:
        return None
    abs_start = parse_linux_starttime(stat_line, btime, clock_ticks())
    if abs_start is None:
        return None
    return f"{abs_start:.2f}"


def _macos_start_time(pid: int) -> str | None:
    try:
        out = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return parse_macos_lstart(out.stdout)


def process_start_time(pid: int) -> str | None:
    """Return this process's normalized start-time token, or ``None`` if gone.

    The token is opaque and platform-specific; only compare it for equality with
    another token produced by this same function on the same host.
    """
    if pid <= 0:
        return None
    if _IS_LINUX:
        return _linux_start_time(pid)
    return _macos_start_time(pid)


def pid_alive(pid: int) -> bool:
    """``kill(pid, 0)`` existence check — no signal sent (``kill(2)``).

    This alone proves only that *a* process by that number exists, never that it
    is ours — always pair it with :func:`identity_matches`.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user → not one of our child runtimes.
        return True
    except OSError:
        return False
    return True


def pgid_of(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, OSError):
        return None


def process_is_zombie(pid: int) -> bool:
    """True if ``pid`` is a zombie (dead but not yet reaped by its parent).

    A zombie still satisfies ``kill(pid, 0)`` (the PID is not recycled until the
    parent reaps it), so termination logic must treat it as already-dead rather
    than escalate to a pointless ``SIGKILL``.
    """
    if pid <= 0:
        return False
    if _IS_LINUX:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                stat_line = fh.read()
        except (FileNotFoundError, ProcessLookupError, OSError):
            return False
        rparen = stat_line.rfind(")")
        if rparen == -1:
            return False
        rest = stat_line[rparen + 1 :].split()
        return bool(rest) and rest[0] == "Z"
    try:
        out = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0 and out.stdout.strip().startswith("Z")


def _dead_or_zombie(pid: int) -> bool:
    return not pid_alive(pid) or process_is_zombie(pid)


def identity_matches(pid: int, recorded_start_time: str | None) -> bool:
    """True only if ``pid`` is alive AND its start time equals the record.

    Fail-closed: a missing recorded value, a process we cannot read a start time
    for, or any mismatch all return ``False`` — the broker then treats the record
    as stale and never signals the PID.
    """
    if not recorded_start_time:
        return False
    if not pid_alive(pid):
        return False
    live = process_start_time(pid)
    if live is None:
        return False
    return live == recorded_start_time


def terminate_process_group(
    pid: int,
    pgid: int | None,
    recorded_start_time: str | None,
    *,
    grace: float = 10.0,
) -> str:
    """Identity-gated ``SIGTERM`` → grace → ``SIGKILL`` of a process group.

    Refuses to signal anything whose ``(pid, start_time)`` does not match the
    record (returns ``"identity_mismatch"`` without sending a signal). On a match
    it signals the process **group** (the runtime leads its own session) so any
    helper children are reaped too, mirroring systemd's two-phase teardown.
    Returns a short status string for logging/reporting.
    """
    if not identity_matches(pid, recorded_start_time):
        return "identity_mismatch"
    target_pgid = pgid if pgid is not None else pgid_of(pid)
    if target_pgid is None:
        return "gone"
    try:
        os.killpg(target_pgid, signal.SIGTERM)
    except ProcessLookupError:
        return "already_gone"
    except (PermissionError, OSError) as exc:
        log.warning("SIGTERM of pgid %s failed: %s", target_pgid, exc)
        return "signal_error"
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        if _dead_or_zombie(pid):
            return "terminated"
        time.sleep(0.05)
    # Still alive after the grace window → escalate to SIGKILL of the group.
    if _dead_or_zombie(pid):
        return "terminated"
    try:
        os.killpg(target_pgid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except (PermissionError, OSError) as exc:  # pragma: no cover - rare
        log.warning("SIGKILL of pgid %s failed: %s", target_pgid, exc)
        return "signal_error"
    time.sleep(0.05)
    return "killed" if _dead_or_zombie(pid) else "kill_failed"


def port_from_url(url: str | None) -> int | None:
    """Extract the TCP port from a runtime ``base_url`` (default 80/443)."""
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"//{url}")
    except ValueError:
        return None
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    if parsed.scheme == "http":
        return 80
    return None


def host_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url if "://" in url else f"//{url}")
    except ValueError:
        return None
    return parsed.hostname


def port_in_use(host: str, port: int, *, timeout: float = 0.5) -> bool:
    """True if something is already listening on ``host:port``.

    A plain non-loading TCP connect probe: if a connection is accepted, a process
    (quite possibly an orphaned runtime) holds the port. Used to fail a spawn fast
    with a clear error instead of a confusing readiness timeout.
    """
    target = "127.0.0.1" if host in ("", "0.0.0.0", "::", "*") else host
    try:
        with socket.create_connection((target, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False
