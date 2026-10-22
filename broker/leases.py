"""Host-local device leases — ADR 0026.

GPUs on an inference host are frequently shared with co-tenant workloads. Those
co-tenants need a reliable, readable "the broker is using device N" signal, and
the broker needs to know when a device is taken (by another broker model or by
an external holder) so it can admit *exclusive* models safely and release the
device promptly after a stop — even a forced one.

This module is the opt-in (``BROKER_DEVICE_LEASES=off`` by default) lease layer:

* **One lease file per device** under ``BROKER_DEVICE_LEASE_DIR`` (default
  ``<cache_dir>/leases``). Writes are atomic (temp-file + ``fsync`` +
  ``os.replace``) and serialized with an advisory ``flock`` on a sidecar lock
  file — the exact pattern proven by :mod:`broker.ledger`. Lease files are
  **world-readable, owner-writable** (``0644``) so a co-tenant can read them; the
  lock file is ``0600``.
* **Versioned JSON schema** documented as a stable interface in
  ``docs/device-leases.md``. Each file records the device, the lease ``mode``
  (``exclusive`` | ``shared``) and the list of holders. A holder carries the
  owning broker's identity (PID + start time) and, for a broker-spawned runtime,
  that runtime's PID/pgid/start time — reusing :mod:`broker.runtime_identity` so
  a reused PID is never mistaken for a live holder. The flat top-level fields
  (``holder``, ``broker_pid``, ``runtime_pid`` …) mirror the first holder for the
  common single-holder case; ``holders`` carries the full set.
* **External holders** may write an ``external`` lease with the same protocol
  (``state: "external"``). The broker reads it, honours it for exclusive
  admission, and **never signals or evicts it**.

Identity is always ``(pid, start_time)`` — never a bare PID. A torn or
hand-mangled lease file is treated as absent with a warning (fail-safe: the
broker never kills or refuses on the strength of unparsable data), never a crash.
Scope is **per host**: device occupancy is a single-host fact, so there is no
network store.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import runtime_identity as rid
from .security import validate_executable

log = logging.getLogger("broker.leases")

#: On-disk schema version so a future format change can be detected and ignored
#: rather than mis-parsed.
LEASE_VERSION = 1

#: Holder lifecycle states.
STATE_HELD = "held"
STATE_RELEASING = "releasing"
STATE_STUCK = "stuck"
STATE_ORPHANED = "orphaned"
STATE_EXTERNAL = "external"

MODE_EXCLUSIVE = "exclusive"
MODE_SHARED = "shared"

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _safe_device(device: str) -> str:
    """Sanitize a device key into a filename component (``0`` / ``default`` …)."""
    s = _SAFE.sub("_", str(device)).strip("_.")
    return s or "device"


@dataclass
class LeaseHolder:
    """One holder of a device lease (a broker model, or an external process).

    ``broker_pid`` / ``broker_start_time`` identify the broker process that owns
    this lease so stale-cleanup can tell a dead broker's lease from a live one.
    ``runtime_*`` identify the broker-spawned runtime (if any) so a release is
    confirmed only when that process group is gone, and a ``stop``-mode cleanup
    can terminate it identity-gated. An ``external`` holder sets ``state`` to
    ``external`` and is never signalled.
    """

    holder: str
    state: str = STATE_HELD
    broker_pid: int | None = None
    broker_start_time: str | None = None
    runtime_pid: int | None = None
    runtime_pgid: int | None = None
    runtime_start_time: str | None = None
    acquired_at: float = 0.0
    heartbeat_at: float = 0.0

    @property
    def external(self) -> bool:
        return self.state == STATE_EXTERNAL

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LeaseHolder:
        def _int(v: Any) -> int | None:
            return int(v) if v is not None else None

        return cls(
            holder=str(d["holder"]),
            state=str(d.get("state", STATE_HELD)),
            broker_pid=_int(d.get("broker_pid")),
            broker_start_time=(
                str(d["broker_start_time"]) if d.get("broker_start_time") is not None else None
            ),
            runtime_pid=_int(d.get("runtime_pid")),
            runtime_pgid=_int(d.get("runtime_pgid")),
            runtime_start_time=(
                str(d["runtime_start_time"]) if d.get("runtime_start_time") is not None else None
            ),
            acquired_at=float(d.get("acquired_at", 0.0)),
            heartbeat_at=float(d.get("heartbeat_at", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DeviceLease:
    """A device's lease file: a ``mode`` and the list of current holders."""

    device: str
    mode: str = MODE_SHARED
    holders: list[LeaseHolder] = field(default_factory=list)
    version: int = LEASE_VERSION

    @property
    def primary(self) -> LeaseHolder | None:
        return self.holders[0] if self.holders else None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeviceLease | None:
        if not isinstance(d, dict) or d.get("version") != LEASE_VERSION:
            return None
        device = d.get("device")
        if not device:
            return None
        raw_holders = d.get("holders")
        holders: list[LeaseHolder] = []
        if isinstance(raw_holders, list):
            for h in raw_holders:
                if isinstance(h, dict) and h.get("holder"):
                    holders.append(LeaseHolder.from_dict(h))
        return cls(
            device=str(device),
            mode=str(d.get("mode", MODE_SHARED)),
            holders=holders,
            version=LEASE_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize with the documented flat fields mirroring the first holder."""
        p = self.primary
        out: dict[str, Any] = {
            "version": LEASE_VERSION,
            "device": self.device,
            "mode": self.mode,
            "holders": [h.to_dict() for h in self.holders],
        }
        if p is not None:
            out.update(
                {
                    "holder": p.holder,
                    "state": p.state,
                    "broker_pid": p.broker_pid,
                    "broker_start_time": p.broker_start_time,
                    "runtime_pid": p.runtime_pid,
                    "runtime_pgid": p.runtime_pgid,
                    "runtime_start_time": p.runtime_start_time,
                    "acquired_at": p.acquired_at,
                    "heartbeat_at": p.heartbeat_at,
                }
            )
        return out


class DeviceLeaseStore:
    """Atomic, flock-serialized store of per-device lease files under a directory.

    One file per device (``device-<key>.lease.json``), world-readable and
    owner-writable, each mutation guarded by an advisory lock on a sidecar
    ``.lock`` file. A ``None`` directory disables the store (leases off): every
    method is then a no-op and no directory is created.
    """

    def __init__(self, directory: str | None) -> None:
        self.directory = directory

    @property
    def enabled(self) -> bool:
        return bool(self.directory)

    def _path(self, device: str) -> str:
        return f"{self.directory.rstrip('/')}/device-{_safe_device(device)}.lease.json"  # type: ignore[union-attr]

    def _lock_path(self, device: str) -> str:
        return f"{self._path(device)}.lock"

    @contextlib.contextmanager
    def locked(self, device: str):
        """Hold the per-device advisory lock for a read-modify-write sequence."""
        assert self.directory is not None
        lock_file = self._lock_path(device)
        Path(lock_file).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def read(self, device: str) -> DeviceLease | None:
        if not self.directory:
            return None
        p = Path(self._path(device))
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError) as exc:
            log.warning("device lease %r unparsable; ignoring it: %s", str(p), exc)
            return None
        return DeviceLease.from_dict(data)

    def write(self, lease: DeviceLease) -> None:
        if not self.directory:
            return
        p = Path(self._path(lease.device))
        directory = p.parent
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(lease.to_dict(), indent=2).encode("utf-8")
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".device-lease-", suffix=".tmp")
        try:
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            os.chmod(tmp, 0o644)  # world-readable so co-tenants can read it
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        with contextlib.suppress(OSError):
            dir_fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def remove(self, device: str) -> bool:
        if not self.directory:
            return False
        removed = False
        with contextlib.suppress(OSError):
            os.unlink(self._path(device))
            removed = True
        return removed

    def devices(self) -> list[str]:
        if not self.directory:
            return []
        d = Path(self.directory)
        if not d.exists():
            return []
        out: list[str] = []
        for p in sorted(d.glob("device-*.lease.json")):
            lease = self._read_path(p)
            if lease is not None:
                out.append(lease.device)
        return out

    def _read_path(self, p: Path) -> DeviceLease | None:
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError) as exc:
            log.warning("device lease %r unparsable; ignoring it: %s", str(p), exc)
            return None
        return DeviceLease.from_dict(data)

    def load_all(self) -> list[DeviceLease]:
        if not self.directory:
            return []
        d = Path(self.directory)
        if not d.exists():
            return []
        out: list[DeviceLease] = []
        for p in sorted(d.glob("device-*.lease.json")):
            lease = self._read_path(p)
            if lease is not None:
                out.append(lease)
        return out


# --- pure classification + compatibility (unit-testable without a manager) -----


def holder_broker_alive(h: LeaseHolder) -> bool:
    """True if the broker that owns ``h`` is still alive (identity-matched)."""
    if h.broker_pid is None:
        return False
    return rid.identity_matches(h.broker_pid, h.broker_start_time)


def holder_runtime_alive(h: LeaseHolder) -> bool:
    """True if ``h``'s broker-spawned runtime is still alive (identity-matched)."""
    if h.runtime_pid is None:
        return False
    return rid.identity_matches(h.runtime_pid, h.runtime_start_time)


def holder_is_stale(h: LeaseHolder) -> bool:
    """Stale == a non-external holder whose broker is dead AND runtime is gone."""
    if h.external:
        return False
    return (not holder_broker_alive(h)) and (not holder_runtime_alive(h))


def holder_is_orphan(h: LeaseHolder) -> bool:
    """Orphan == a non-external holder whose broker is dead but runtime is alive."""
    if h.external:
        return False
    return (not holder_broker_alive(h)) and holder_runtime_alive(h)


def incompatible_holders(lease: DeviceLease | None, want_mode: str) -> list[LeaseHolder]:
    """Holders of ``lease`` that conflict with a new holder of ``want_mode``.

    * An ``exclusive`` request conflicts with *every* existing holder.
    * A ``shared`` request conflicts only with an ``exclusive`` holder.
    * ``shared`` + ``shared`` is compatible (co-residency), so no conflict.

    A holder that is the *same* alias re-acquiring is never a conflict (handled by
    the caller via ``exclude_alias``).
    """
    if lease is None or not lease.holders:
        return []
    if want_mode == MODE_EXCLUSIVE:
        return list(lease.holders)
    if lease.mode == MODE_EXCLUSIVE:
        return list(lease.holders)
    return []


# --- optional operator hooks ---------------------------------------------------


def run_lease_hook(
    command: str | None,
    *,
    device: str,
    mode: str,
    holder: str,
    allowed_exec_dirs: list[str],
    timeout: float,
) -> bool:
    """Run an acquire/release hook (argv-only, allow-listed, no shell, bounded).

    ``command`` is split on whitespace into an argv template; its first token
    (the executable) must resolve inside ``allowed_exec_dirs``. The placeholders
    ``{device}`` ``{mode}`` ``{holder}`` are substituted into the remaining
    tokens. Returns ``True`` on exit code 0, ``False`` on any failure (missing
    config is ``True`` — nothing to do). Never raises.
    """
    if not command or not str(command).strip():
        return True
    import shlex

    try:
        tokens = shlex.split(str(command))
        if not tokens:
            return True
        exe = validate_executable(tokens[0], allowed_exec_dirs)
        subs = {"device": str(device), "mode": str(mode), "holder": str(holder)}
        argv = [exe]
        for tok in tokens[1:]:
            argv.append(tok.format(**subs))
    except Exception as exc:  # noqa: BLE001 - a bad hook config must not crash a load path
        log.warning("lease hook %r invalid: %s", command, exc)
        return False
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(0.1, timeout),
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("lease hook %r failed to run: %s", argv[0], exc)
        return False
    if proc.returncode != 0:
        log.warning(
            "lease hook %r exited %s (device=%s mode=%s)",
            argv[0],
            proc.returncode,
            device,
            mode,
        )
        return False
    return True


# --- stale/orphan cleanup (shared by startup, the sweeper, and the CLI) ---------


def cleanup_leases(
    store: DeviceLeaseStore,
    *,
    kill_orphans: bool,
    release_command: str | None = None,
    allowed_exec_dirs: list[str] | None = None,
    hook_timeout: float = 10.0,
    kill_grace: float = 10.0,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Remove stale leases and (optionally) reap orphans; return a JSON summary.

    * **stale** holder (broker dead AND runtime gone) → dropped.
    * **orphan** holder (broker dead, runtime alive) → kept and marked
      ``orphaned`` unless ``kill_orphans`` (the ``stop``-mode / ExecStopPost
      path), which terminates the runtime **identity-gated** then drops it.
    * a holder whose **broker is alive** (a live broker owns the lease) is left
      untouched.
    * an **external** holder is never signalled and never dropped.

    When a lease file ends up with no holders it is removed and the release hook
    fires once (a freed device). Bounded by ``deadline`` (monotonic): any lease
    not reached in time is reported under ``skipped`` with nothing changed.
    """
    summary: dict[str, Any] = {
        "removed": [],
        "killed": [],
        "orphaned": [],
        "kept": [],
        "external": [],
        "skipped": [],
        "errors": [],
    }
    dirs = allowed_exec_dirs or []
    for lease in store.load_all():
        if deadline is not None and time.monotonic() >= deadline:
            summary["skipped"].append(lease.device)
            continue
        try:
            _cleanup_one(
                store,
                lease,
                kill_orphans=kill_orphans,
                release_command=release_command,
                allowed_exec_dirs=dirs,
                hook_timeout=hook_timeout,
                kill_grace=kill_grace,
                summary=summary,
            )
        except Exception as exc:  # noqa: BLE001 - one bad lease never aborts cleanup
            log.warning("cleanup of device lease %r failed: %s", lease.device, exc)
            summary["errors"].append({"device": lease.device, "error": str(exc)})
    return summary


def _cleanup_one(
    store: DeviceLeaseStore,
    lease: DeviceLease,
    *,
    kill_orphans: bool,
    release_command: str | None,
    allowed_exec_dirs: list[str],
    hook_timeout: float,
    kill_grace: float,
    summary: dict[str, Any],
) -> None:
    with store.locked(lease.device):
        fresh = store.read(lease.device)
        if fresh is None:
            return
        surviving: list[LeaseHolder] = []
        freed = False
        for h in fresh.holders:
            if h.external:
                summary["external"].append({"device": fresh.device, "holder": h.holder})
                surviving.append(h)
                continue
            if holder_broker_alive(h):
                summary["kept"].append({"device": fresh.device, "holder": h.holder})
                surviving.append(h)
                continue
            # Broker dead from here on.
            if holder_runtime_alive(h):
                if kill_orphans:
                    status = rid.terminate_process_group(
                        h.runtime_pid,  # type: ignore[arg-type]
                        h.runtime_pgid,
                        h.runtime_start_time,
                        grace=kill_grace,
                    )
                    summary["killed"].append(
                        {"device": fresh.device, "pid": h.runtime_pid, "status": status}
                    )
                    freed = True
                    # runtime terminated → drop the holder.
                else:
                    h.state = STATE_ORPHANED
                    surviving.append(h)
                    summary["orphaned"].append({"device": fresh.device, "pid": h.runtime_pid})
            else:
                # stale: broker dead + runtime gone → drop it.
                freed = True
        if not surviving:
            store.remove(fresh.device)
            summary["removed"].append(fresh.device)
            if freed:
                run_lease_hook(
                    release_command,
                    device=fresh.device,
                    mode=fresh.mode,
                    holder=(fresh.primary.holder if fresh.primary else ""),
                    allowed_exec_dirs=allowed_exec_dirs,
                    timeout=hook_timeout,
                )
        else:
            fresh.holders = surviving
            store.write(fresh)
