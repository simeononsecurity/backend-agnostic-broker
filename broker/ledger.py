"""Runtime ledger: a durable record of broker-spawned runtimes — ADR 0021.

The broker spawns backend runtimes (vLLM, llama.cpp, Strata) as detached,
own-session children. If the broker dies uncleanly (SIGKILL / OOM / crash /
container restart) those children survive as **orphans**, holding VRAM and their
TCP port, with no in-memory record left behind. This ledger persists one record
per live runtime so a restarted broker can find, verify, and reconcile them.

Identity is ``(pid, start_time)`` plus the process-group id and an argv hash (see
:mod:`broker.runtime_identity`) — never a bare PID. Writes are atomic
(temp-file + ``fsync`` + ``os.replace``), ``0600``, and serialized with an
advisory ``flock`` on a sidecar lock file, reusing the proven pattern from
:mod:`broker.keys`. A torn or hand-mangled ledger is treated as **empty** with a
warning (fail-safe: the broker never kills anything on the strength of
unparsable data), never a crash.

Scope is **per host**: local child processes cannot be reconciled from another
machine, so Redis is intentionally not involved. Each broker replica writes to a
ledger under its own ``BROKER_CACHE_DIR`` and reconciles only its own records on
its own host (see ``docs/guides/restart-and-recovery.md``).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("broker.ledger")

#: On-disk schema version, so a future format change can be detected and ignored
#: rather than mis-parsed.
LEDGER_VERSION = 1


def argv_hash(argv: list[str] | None) -> str:
    """Stable hash of a runtime's exact argv, for detecting registry drift."""
    joined = "\x00".join(argv or [])
    return hashlib.sha256(joined.encode("utf-8", "surrogateescape")).hexdigest()


@dataclass
class RuntimeRecord:
    """One spawned runtime. ``start_time`` is the durable identity anchor.

    ``broker_pid`` / ``broker_start_time`` identify the broker process that
    spawned (and therefore owns) this runtime. Reconcile uses them as a
    per-record ownership gate: a record whose owning broker is still alive is
    never signalled or adopted by a *different* broker — only an orphan whose
    owner is dead may be acted on (ADR 0021, multi-replica safety).
    """

    alias: str
    pid: int
    pgid: int | None
    start_time: str | None
    port: int | None
    argv_sha256: str
    broker_instance_id: str
    base_url: str | None = None
    spawned_at: float = 0.0
    broker_pid: int | None = None
    broker_start_time: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RuntimeRecord:
        return cls(
            alias=str(d["alias"]),
            pid=int(d["pid"]),
            pgid=(int(d["pgid"]) if d.get("pgid") is not None else None),
            start_time=(str(d["start_time"]) if d.get("start_time") is not None else None),
            port=(int(d["port"]) if d.get("port") is not None else None),
            argv_sha256=str(d.get("argv_sha256", "")),
            broker_instance_id=str(d.get("broker_instance_id", "")),
            base_url=(str(d["base_url"]) if d.get("base_url") is not None else None),
            spawned_at=float(d.get("spawned_at", 0.0)),
            broker_pid=(int(d["broker_pid"]) if d.get("broker_pid") is not None else None),
            broker_start_time=(
                str(d["broker_start_time"]) if d.get("broker_start_time") is not None else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.spawned_at) if self.spawned_at else 0.0


class RuntimeLedger:
    """Atomic, locked, ``0600`` JSON ledger of spawned runtimes (keyed by alias).

    One record per alias (a model has at most one live broker-managed runtime at a
    time). All mutations take an exclusive advisory lock on ``<path>.lock`` and
    swap the data file atomically, so concurrent brokers/tools never interleave
    writes or observe a torn file.
    """

    def __init__(self, path: str | None) -> None:
        self.path = path

    # --- locking / atomic IO (mirrors broker.keys) -----------------------
    def _lock_path(self) -> str:
        return f"{self.path}.lock"

    @contextlib.contextmanager
    def _locked(self):
        assert self.path is not None
        lock_file = self._lock_path()
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

    def _read_raw(self) -> list[dict[str, Any]]:
        p = Path(self.path) if self.path else None
        if p is None or not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (ValueError, OSError) as exc:
            # Fail-safe: a corrupt ledger is ignored (empty), never acted on.
            log.warning("runtime ledger %r unparsable; ignoring it: %s", self.path, exc)
            return []
        if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
            if data:  # a non-empty but unrecognized shape is suspect — ignore it
                log.warning("runtime ledger %r has an unexpected shape; ignoring it", self.path)
            return []
        records = data.get("runtimes")
        return records if isinstance(records, list) else []

    def _write_raw(self, records: list[dict[str, Any]]) -> None:
        assert self.path is not None
        p = Path(self.path)
        directory = p.parent
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": LEDGER_VERSION, "runtimes": records}, indent=2).encode(
            "utf-8"
        )
        fd, tmp = tempfile.mkstemp(dir=str(directory), prefix=".runtime-ledger-", suffix=".tmp")
        try:
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            os.chmod(tmp, 0o600)
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

    # --- public API ------------------------------------------------------
    def load(self) -> list[RuntimeRecord]:
        """Return all records (deduped by alias, last wins). Never raises."""
        if not self.path:
            return []
        out: dict[str, RuntimeRecord] = {}
        for raw in self._read_raw():
            try:
                rec = RuntimeRecord.from_dict(raw)
            except (KeyError, ValueError, TypeError) as exc:
                log.warning("skipping malformed runtime ledger record: %s", exc)
                continue
            out[rec.alias] = rec
        return list(out.values())

    def upsert(self, record: RuntimeRecord) -> None:
        """Insert/replace the record for ``record.alias`` (atomic + locked)."""
        if not self.path:
            return
        with self._locked():
            records = [r for r in self._read_raw() if r.get("alias") != record.alias]
            records.append(record.to_dict())
            self._write_raw(records)

    def remove(self, alias: str) -> None:
        """Drop the record for ``alias`` on a clean stop (atomic + locked)."""
        if not self.path:
            return
        with self._locked():
            records = self._read_raw()
            kept = [r for r in records if r.get("alias") != alias]
            if len(kept) != len(records):
                self._write_raw(kept)

    def clear(self) -> None:
        if not self.path:
            return
        with self._locked():
            self._write_raw([])


@dataclass
class LockOwner:
    """The broker process that currently holds the ledger ownership lock."""

    pid: int
    start_time: str | None
    instance_id: str
    acquired_at: float = 0.0

    @classmethod
    def from_json(cls, data: bytes | str) -> LockOwner | None:
        try:
            d = json.loads(data or "{}")
            if not isinstance(d, dict) or "pid" not in d:
                return None
            return cls(
                pid=int(d["pid"]),
                start_time=(str(d["start_time"]) if d.get("start_time") is not None else None),
                instance_id=str(d.get("instance_id", "")),
                acquired_at=float(d.get("acquired_at", 0.0)),
            )
        except (ValueError, TypeError):
            return None


class LedgerOwnerLock:
    """Exclusive, broker-lifetime advisory lock on the runtime ledger (ADR 0021).

    Only one *live* broker per ``BROKER_CACHE_DIR`` may own the ledger. Ownership
    is a non-blocking ``flock(LOCK_EX | LOCK_NB)`` on a sidecar lock file held for
    the broker's entire life (the fd stays open until clean shutdown). Because an
    advisory ``flock`` is released by the kernel when the last fd referencing it
    is closed — including on process death — a crashed owner's lock frees itself,
    so a restart (or any stale lock) is taken over automatically with no manual
    liveness check. The lock file's body records the owner's PID + start time +
    instance id purely for diagnostics and the startup error message.
    """

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self, owner: LockOwner) -> LockOwner | None:
        """Try to take the lock non-blocking.

        Returns ``None`` on success (we now own it, fd held until
        :meth:`release`). If a live sibling broker already holds it, returns that
        owner (read from the lock file; a best-effort placeholder if unreadable).
        """
        if not self.path:
            return None
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Held by another process. flock frees on owner death, so reaching
            # here means that owner is alive. Read who it is for the message.
            existing = self._read_owner(fd)
            with contextlib.suppress(OSError):
                os.close(fd)
            return existing or LockOwner(pid=-1, start_time=None, instance_id="")
        except OSError:
            # flock genuinely failed (e.g. filesystem doesn't support it). Don't
            # misread this as contention; surface it to the caller to degrade.
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        # We hold it. Stamp our identity and keep the fd open for our lifetime.
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(asdict(owner)).encode("utf-8"))
            os.fsync(fd)
        except OSError as exc:  # pragma: no cover - stamping is best-effort
            log.warning("could not stamp ledger owner lock %r: %s", self.path, exc)
        self._fd = fd
        return None

    def _read_owner(self, fd: int) -> LockOwner | None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 65536)
        except OSError:
            return None
        return LockOwner.from_json(data)

    def release(self) -> None:
        """Release the lock (clean shutdown). Idempotent."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - best-effort
            pass
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None
