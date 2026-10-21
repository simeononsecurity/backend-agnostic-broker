"""Startup reconciliation of orphaned runtimes — ADR 0021.

When the broker restarts it reads the runtime ledger (:mod:`broker.ledger`) and,
for every recorded runtime, decides what to do under ``BROKER_RUNTIME_RECONCILE``:

* ``off``     — ignore the ledger entirely (the port pre-check at spawn time is
                the only remaining safety net).
* ``report``  — list still-alive, identity-matched runtimes as ``orphaned`` in
                ``/status``; never signal, never adopt (default).
* ``adopt``   — if the recorded process is alive with a matching identity, the
                registry argv still matches, and its port answers the readiness
                probe, mark the model READY **without spawning** (no new load).
* ``stop``    — ``SIGTERM`` → grace → ``SIGKILL`` the recorded process group,
                then clear the record.

The invariant across every mode: **no signal is ever sent to a PID whose recorded
start time does not match the live one.** A mismatch means the PID was reused; the
record is dropped (``stale``) without signalling. The broker also never touches a
process it did not itself record.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from . import runtime_identity as rid

if TYPE_CHECKING:  # pragma: no cover
    from .manager import ModelManager

log = logging.getLogger("broker.reconcile")

VALID_MODES = ("off", "report", "adopt", "stop")


async def reconcile_runtimes(manager: ModelManager) -> dict[str, Any]:
    """Reconcile ledgered runtimes at startup; return a summary for ``/status``.

    Safe to call unconditionally: with ``off`` or an empty ledger it does nothing
    but record an empty summary. Never raises into startup — any per-record error
    is logged and the record is left for the next pass.
    """
    settings = manager.settings
    mode = settings.runtime_reconcile
    summary: dict[str, Any] = {
        "mode": mode,
        "ran_at": time.time(),
        "checked": 0,
        "adopted": [],
        "stopped": [],
        "orphaned": [],
        "stale": [],
        "owned_by_live_broker": [],
    }
    ledger = manager.ledger
    if mode == "off" or ledger is None:
        return summary

    records = ledger.load()
    summary["checked"] = len(records)
    for rec in records:
        try:
            await _reconcile_one(manager, rec, mode, summary)
        except Exception as exc:  # noqa: BLE001 - one bad record never breaks startup
            log.warning("reconcile of runtime %r failed: %s", rec.alias, exc)

    if (
        summary["adopted"]
        or summary["stopped"]
        or summary["orphaned"]
        or summary["stale"]
        or summary["owned_by_live_broker"]
    ):
        log.warning(
            "runtime reconciliation (%s): %d adopted, %d stopped, %d orphaned, "
            "%d stale, %d owned-by-live-broker",
            mode,
            len(summary["adopted"]),
            len(summary["stopped"]),
            len(summary["orphaned"]),
            len(summary["stale"]),
            len(summary["owned_by_live_broker"]),
        )
    return summary


def _owner_broker_alive(manager: ModelManager, rec) -> bool:
    """True if the broker that spawned ``rec`` is a *different*, still-live broker.

    The per-record ownership gate (ADR 0021): reconcile may act only on a record
    whose owning broker is dead. A record whose owner is still alive belongs to a
    live sibling (two brokers sharing a cache dir) and must never be signalled,
    adopted, or dropped by us.

    Returns ``False`` for a record this broker instance wrote itself, and for a
    legacy record with no recorded owner identity — both fall through to the
    normal runtime-identity gate, preserving single-broker behavior.
    """
    if rec.broker_instance_id and rec.broker_instance_id == manager.broker_instance_id:
        return False
    if rec.broker_pid is None:
        return False
    return rid.identity_matches(rec.broker_pid, rec.broker_start_time)


async def _reconcile_one(manager: ModelManager, rec, mode: str, summary: dict[str, Any]) -> None:
    # Ownership gate FIRST: never touch a record whose owning broker is still
    # alive (a live sibling sharing this cache dir owns it). Defense in depth
    # alongside the broker-lifetime ownership lock.
    if _owner_broker_alive(manager, rec):
        log.info(
            "runtime record %r is owned by a live broker (pid=%s); leaving it alone",
            rec.alias,
            rec.broker_pid,
        )
        summary["owned_by_live_broker"].append(
            {"alias": rec.alias, "pid": rec.pid, "broker_pid": rec.broker_pid}
        )
        return

    # Identity gate: never act on a PID whose start time doesn't match.
    if not rid.identity_matches(rec.pid, rec.start_time):
        log.info(
            "runtime record %r (pid=%s) no longer matches its recorded identity; dropping it",
            rec.alias,
            rec.pid,
        )
        summary["stale"].append({"alias": rec.alias, "pid": rec.pid})
        if manager.ledger is not None:
            manager.ledger.remove(rec.alias)
        return

    if mode == "stop":
        status = rid.terminate_process_group(
            rec.pid, rec.pgid, rec.start_time, grace=manager.settings.shutdown_timeout_seconds
        )
        summary["stopped"].append({"alias": rec.alias, "pid": rec.pid, "status": status})
        if manager.ledger is not None:
            manager.ledger.remove(rec.alias)
        return

    if mode == "adopt":
        mm = manager.models.get(rec.alias)
        adopted = False
        if mm is not None:
            adopted = await mm.adopt(rec)
        if adopted:
            summary["adopted"].append(rec.alias)
            return
        # Could not adopt (unknown alias, argv drift, or probe failed): surface it
        # as an orphan rather than silently leaving it unaccounted for.
        summary["orphaned"].append(_orphan_entry(rec, adoptable=False))
        return

    # mode == "report": alive + identity-matched → list it, never signal.
    summary["orphaned"].append(_orphan_entry(rec, adoptable=None))


def _orphan_entry(rec, *, adoptable: bool | None) -> dict[str, Any]:
    entry = {
        "alias": rec.alias,
        "pid": rec.pid,
        "port": rec.port,
        "start_time": rec.start_time,
        "age_seconds": round(rec.age_seconds(), 1),
    }
    if adoptable is not None:
        entry["adoptable"] = adoptable
    return entry
