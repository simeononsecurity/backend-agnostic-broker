"""ADR 0026 real-process test: ``python -m broker cleanup`` reaps a real runtime.

Reproduces the post-forced-kill state: a lease file whose owning broker is dead
but whose runtime is a REAL, alive process (its own session leader). The actual
``python -m broker cleanup`` CLI is run as a subprocess:

* ``stop`` mode (systemd ``ExecStopPost=``) terminates the runtime identity-gated,
  removes the lease, and fires the release hook — all within 10 s;
* ``--report`` keeps the lease (marked ``orphaned``) and signals nothing.

Identity gating is proven by a *decoy* lease whose runtime PID is alive but whose
recorded start time does not match: cleanup must leave that process untouched.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from broker import runtime_identity as rid
from broker.leases import (
    MODE_SHARED,
    STATE_HELD,
    DeviceLease,
    DeviceLeaseStore,
    LeaseHolder,
)

REPO = Path(__file__).resolve().parents[1]
HOOK = Path(__file__).parent / "fixtures" / "lease_hook.sh"
DEAD_PID = 2_000_111  # a PID that is not alive (dead broker identity)


def _spawn_sleeper() -> subprocess.Popen:
    """A real process in its own session (pgid == pid) standing in for a runtime."""
    return subprocess.Popen(["sleep", "60"], start_new_session=True)


def _write_lease(store: DeviceLeaseStore, device: str, pid: int, start_time: str | None) -> None:
    holder = LeaseHolder(
        holder="m",
        state=STATE_HELD,
        broker_pid=DEAD_PID,  # dead broker → the lease is an orphan/stale candidate
        broker_start_time="dead",
        runtime_pid=pid,
        runtime_pgid=os.getpgid(pid),
        runtime_start_time=start_time,
        acquired_at=time.time(),
    )
    store.write(DeviceLease(device=device, mode=MODE_SHARED, holders=[holder]))


def _run_cleanup(lease_dir: Path, *, mode: str, report: bool, release_log: Path) -> dict:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(REPO),
            "BROKER_DEVICE_LEASES": "on",
            "BROKER_DEVICE_LEASE_DIR": str(lease_dir),
            "BROKER_CACHE_DIR": str(lease_dir.parent / "cache"),
            "BROKER_RUNTIME_RECONCILE": mode,
            "BROKER_AUTH_ENABLED": "false",
            "BROKER_ALLOWED_EXEC_DIRS": json.dumps([str(HOOK.parent)]),
            "BROKER_LEASE_RELEASE_COMMAND": f"{HOOK} {release_log} {{device}} {{mode}} {{holder}}",
        }
    )
    argv = [sys.executable, "-m", "broker", "cleanup", "--timeout", "15"]
    if report:
        argv.append("--report")
    out = subprocess.run(argv, cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30)
    assert out.returncode in (0, 1), out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_cleanup_stop_mode_reaps_runtime_and_lease_within_10s(tmp_path):
    lease_dir = tmp_path / "leases"
    store = DeviceLeaseStore(str(lease_dir))
    release_log = tmp_path / "release.log"
    proc = _spawn_sleeper()
    try:
        st = rid.process_start_time(proc.pid)
        assert st is not None
        _write_lease(store, "0", proc.pid, st)

        start = time.monotonic()
        summary = _run_cleanup(lease_dir, mode="stop", report=False, release_log=release_log)
        elapsed = time.monotonic() - start

        assert elapsed < 10.0, f"cleanup took {elapsed:.1f}s"
        # The runtime was terminated (identity-gated) ...
        _wait_dead(proc, 5.0)
        assert proc.poll() is not None
        # ... the lease file removed ...
        assert store.read("0") is None
        # ... the release hook fired once ...
        assert release_log.exists() and release_log.read_text().strip() == "0 shared m"
        # ... and the JSON summary records the kill + removal.
        assert summary["enabled"] is True
        assert summary["kill_orphans"] is True
        assert any(k["pid"] == proc.pid for k in summary["killed"])
        assert "0" in summary["removed"]
    finally:
        _kill(proc)


def test_cleanup_report_mode_keeps_orphan_and_signals_nothing(tmp_path):
    lease_dir = tmp_path / "leases"
    store = DeviceLeaseStore(str(lease_dir))
    release_log = tmp_path / "release.log"
    proc = _spawn_sleeper()
    try:
        st = rid.process_start_time(proc.pid)
        _write_lease(store, "0", proc.pid, st)

        summary = _run_cleanup(lease_dir, mode="report", report=False, release_log=release_log)

        # The runtime is untouched (never signalled) and still alive.
        time.sleep(0.5)
        assert proc.poll() is None
        # The lease is kept and marked orphaned; nothing was killed or removed.
        lease = store.read("0")
        assert lease is not None and lease.primary.state == "orphaned"
        assert summary["killed"] == []
        assert summary["removed"] == []
        assert not release_log.exists()
    finally:
        _kill(proc)


def test_cleanup_is_identity_gated_decoy_runtime_untouched(tmp_path):
    """A live PID whose recorded start time does not match is NEVER signalled."""
    lease_dir = tmp_path / "leases"
    store = DeviceLeaseStore(str(lease_dir))
    proc = _spawn_sleeper()
    try:
        # Record a WRONG start time → identity mismatch → must not be killed.
        _write_lease(store, "0", proc.pid, "not-the-real-start-time")
        summary = _run_cleanup(
            lease_dir, mode="stop", report=False, release_log=tmp_path / "rel.log"
        )
        time.sleep(0.5)
        # The decoy process is alive (identity mismatch → treated as gone, not killed).
        assert proc.poll() is None
        # The lease was dropped as stale (dead broker + unmatched runtime identity),
        # but no SIGTERM/SIGKILL was ever issued against the live PID.
        assert summary["killed"] == []
    finally:
        _kill(proc)


def _wait_dead(proc: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
