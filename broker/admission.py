"""Bounded, fair, timed admission queue under overload (ADR 0023).

When ``BROKER_QUEUE_ENABLED`` is on, a request that cannot be admitted *right
now* — because of concurrency saturation, memory-aware capacity, a model mid
load/release, or a measured overload signal — waits in this queue instead of
blocking a concurrency semaphore unboundedly or failing immediately. The queue
is:

* **Bounded** — a global depth and a per-model depth. A newly arriving request
  that would exceed either bound is rejected with :class:`QueueFullError` (429 +
  Retry-After) and never joins the queue.
* **Timed** — a request waits at most ``queue_max_wait_seconds``; on timeout it
  fails with :class:`QueueTimeoutError` (503 + Retry-After) naming the trigger.
* **Fair** — FIFO within one API key, and round-robin *across* keys so one key
  flooding the queue cannot starve another (the Deficit-Round-Robin / fair-queue
  idea applied to admission: visit each backlogged key in turn). Equal-priority
  ties fall back to arrival order.
* **Prioritised with aging** — an optional per-key ``priority`` orders waiters,
  but a waiter's *effective* priority rises the longer it waits
  (``queue_priority_aging_seconds``) so a low-priority request still progresses
  (FQ-CoDel-style starvation prevention).

This module is the pure scheduler/bookkeeping: ordering, depth bounds, wake
selection and stats. The capacity probing and the actual semaphore acquisition
live in :class:`broker.manager.ModelManager`, which owns the overload signals.
Each replica runs its own local queue (Envoy's circuit-breaker counters are not
synchronised across processes either); see the guide for multi-replica notes.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .errors import QueueFullError

# --- overload / wait triggers (stable machine-readable strings) --------------
TRIGGER_GLOBAL_CONCURRENCY = "global_concurrency"
TRIGGER_MODEL_CONCURRENCY = "model_concurrency"
TRIGGER_MAX_LOADED = "max_loaded_models"
TRIGGER_CAPACITY_MEMORY = "capacity_memory"
TRIGGER_MODEL_LOADING = "model_loading"
TRIGGER_DEVICE_MEMORY = "device_memory"
TRIGGER_HOST_LOAD = "host_load"
TRIGGER_BACKEND_BACKPRESSURE = "backend_backpressure"

ALL_TRIGGERS = (
    TRIGGER_GLOBAL_CONCURRENCY,
    TRIGGER_MODEL_CONCURRENCY,
    TRIGGER_MAX_LOADED,
    TRIGGER_CAPACITY_MEMORY,
    TRIGGER_MODEL_LOADING,
    TRIGGER_DEVICE_MEMORY,
    TRIGGER_HOST_LOAD,
    TRIGGER_BACKEND_BACKPRESSURE,
)


@dataclass
class Waiter:
    """One request waiting for admission."""

    alias: str
    key: str
    base_priority: int
    seq: int
    enqueued_at: float
    trigger: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    #: Measured wait once admitted (ms), filled in by the manager.
    waited_ms: float = 0.0

    def effective_priority(self, now: float, aging_seconds: float) -> int:
        """Base priority plus an aging bump so a long wait cannot starve."""
        if aging_seconds <= 0:
            return self.base_priority
        waited = max(0.0, now - self.enqueued_at)
        return self.base_priority + int(waited // aging_seconds)


class AdmissionQueue:
    """Scheduler state for the admission queue (no I/O, no event loop assumptions).

    The manager drives it: ``enqueue`` on arrival, ``pump`` whenever a slot may
    have freed or on a periodic tick, ``record_admit`` when a waiter is finally
    served, and ``dequeue`` in a ``finally`` so a cancelled/disconnected request
    never leaks a slot.
    """

    def __init__(self, settings: Settings, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.clock = clock
        self._waiters: list[Waiter] = []
        self._per_model_depth: Counter[str] = Counter()
        self._per_key_depth: Counter[str] = Counter()
        # Round-robin fairness: the sequence number at which each key last had a
        # request admitted. A key that was served longer ago (or never, -1) ranks
        # ahead, so keys take turns regardless of how many each has queued.
        self._last_admit_seq: dict[str, int] = {}
        self._seq = itertools.count(1)
        # stats (cumulative, by reason) for /status + metrics.
        self.enqueued_total = 0
        self.admitted_total = 0
        self.rejections_by_reason: Counter[str] = Counter()
        self.timeouts_by_reason: Counter[str] = Counter()

    @property
    def enabled(self) -> bool:
        return self.settings.queue_enabled

    # --- bookkeeping -----------------------------------------------------
    def depth(self) -> int:
        return len(self._waiters)

    def depth_for(self, alias: str) -> int:
        return self._per_model_depth.get(alias, 0)

    def _next_seq(self) -> int:
        return next(self._seq)

    def enqueue(self, alias: str, key: str, priority: int, trigger: str) -> Waiter:
        """Add a waiter or raise :class:`QueueFullError` if a depth bound is hit."""
        gmax = self.settings.queue_max_depth
        mmax = self.settings.queue_max_depth_per_model
        if gmax > 0 and len(self._waiters) >= gmax:
            self.rejections_by_reason[trigger] += 1
            raise QueueFullError(
                f"admission queue full ({len(self._waiters)}/{gmax}) while overloaded ({trigger})",
                trigger=trigger,
                depth=len(self._waiters),
                model=alias,
                remediation="retry after the advertised Retry-After, or raise BROKER_QUEUE_MAX_DEPTH",
            )
        if mmax > 0 and self._per_model_depth.get(alias, 0) >= mmax:
            self.rejections_by_reason[trigger] += 1
            raise QueueFullError(
                f"per-model admission queue full for {alias!r} "
                f"({self._per_model_depth.get(alias, 0)}/{mmax}) while overloaded ({trigger})",
                trigger=trigger,
                depth=self._per_model_depth.get(alias, 0),
                model=alias,
                remediation="retry after Retry-After, or raise BROKER_QUEUE_MAX_DEPTH_PER_MODEL",
            )
        w = Waiter(
            alias=alias,
            key=key,
            base_priority=priority,
            seq=self._next_seq(),
            enqueued_at=self.clock(),
            trigger=trigger,
        )
        self._waiters.append(w)
        self._per_model_depth[alias] += 1
        self._per_key_depth[key] += 1
        self.enqueued_total += 1
        return w

    def dequeue(self, waiter: Waiter) -> None:
        """Remove a waiter (served, timed out, or cancelled). Idempotent."""
        try:
            self._waiters.remove(waiter)
        except ValueError:
            return
        if self._per_model_depth[waiter.alias] > 0:
            self._per_model_depth[waiter.alias] -= 1
        if self._per_model_depth[waiter.alias] == 0:
            del self._per_model_depth[waiter.alias]
        if self._per_key_depth[waiter.key] > 0:
            self._per_key_depth[waiter.key] -= 1
        if self._per_key_depth[waiter.key] == 0:
            del self._per_key_depth[waiter.key]

    def record_admit(self, waiter: Waiter) -> None:
        """Mark that ``waiter`` was admitted — advances its key's RR pointer."""
        self._last_admit_seq[waiter.key] = self._next_seq()
        self.admitted_total += 1

    def record_timeout(self, waiter: Waiter) -> None:
        self.timeouts_by_reason[waiter.trigger] += 1

    # --- ordering + wake -------------------------------------------------
    def _order_key(self, w: Waiter, now: float) -> tuple[int, int, int]:
        """Sort key (ascending = served first): priority desc, RR across keys, FIFO.

        * ``-effective_priority`` → higher (aged) priority first.
        * ``last_admit_seq`` → the key served least recently (or never, -1) first,
          which is the round-robin fairness term: one key cannot monopolise the
          head of line because admitting it pushes its pointer to the back.
        * ``seq`` → arrival order (FIFO) within a key and as the final tie-break.
        """
        eff = w.effective_priority(now, self.settings.queue_priority_aging_seconds)
        rr = self._last_admit_seq.get(w.key, -1)
        return (-eff, rr, w.seq)

    def ordered(self, now: float | None = None) -> list[Waiter]:
        now = self.clock() if now is None else now
        return sorted(self._waiters, key=lambda w: self._order_key(w, now))

    def pump(
        self,
        global_free: Callable[[], int],
        model_free: Callable[[str], int],
    ) -> int:
        """Wake the best-ordered waiters that can actually be admitted now.

        Wakes in fairness order, but never more than capacity allows: a waiter
        already woken (its event set, still queued because it has not yet acquired
        its slot) counts against both the global budget and its model's free
        slots, so repeated pumps never over-wake and let a lower-ranked waiter win
        the race. A woken waiter re-checks the full (async) overload state itself
        and re-queues if it still cannot proceed; the periodic pump then retries
        it, so a telemetry/host overload that clears always drains. Returns the
        number of waiters woken (for tests/metrics).
        """
        if not self._waiters:
            return 0
        pending_global = sum(1 for w in self._waiters if w.event.is_set())
        gfree = global_free() - pending_global
        if gfree <= 0:
            return 0
        pending_model: Counter[str] = Counter(w.alias for w in self._waiters if w.event.is_set())
        model_cap: dict[str, int] = {}
        woken = 0
        now = self.clock()
        for w in self.ordered(now):
            if gfree <= 0:
                break
            if w.event.is_set():
                continue
            if w.alias not in model_cap:
                model_cap[w.alias] = model_free(w.alias) - pending_model.get(w.alias, 0)
            if model_cap[w.alias] <= 0:
                continue
            w.event.set()
            model_cap[w.alias] -= 1
            gfree -= 1
            woken += 1
        return woken

    # --- visibility ------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        per_model: dict[str, int] = dict(self._per_model_depth)
        per_key: dict[str, int] = dict(self._per_key_depth)
        oldest_wait_ms = 0.0
        active_triggers: Counter[str] = Counter()
        for w in self._waiters:
            oldest_wait_ms = max(oldest_wait_ms, (now - w.enqueued_at) * 1000.0)
            active_triggers[w.trigger] += 1
        return {
            "enabled": self.enabled,
            "depth": len(self._waiters),
            "max_depth": self.settings.queue_max_depth,
            "max_depth_per_model": self.settings.queue_max_depth_per_model,
            "max_wait_seconds": self.settings.queue_max_wait_seconds,
            "per_model_depth": per_model,
            "per_key_depth": per_key,
            "oldest_wait_ms": round(oldest_wait_ms, 1),
            "waiting_triggers": dict(active_triggers),
            "enqueued_total": self.enqueued_total,
            "admitted_total": self.admitted_total,
            "rejections_by_reason": dict(self.rejections_by_reason),
            "timeouts_by_reason": dict(self.timeouts_by_reason),
        }
