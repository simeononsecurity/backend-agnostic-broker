"""Model manager: lazy loading, locking, concurrency, idle unload, routing.

The manager owns one :class:`ManagedModel` per registry alias. Guarantees:

* **No model loads at broker startup** — adapters are constructed but never
  started until a request selects a model.
* **A request loads only its selected model** — never a silent fallback.
* **Per-model load lock** — concurrent first-requests for the same model
  serialize on an ``asyncio.Lock`` so the runtime starts exactly once.
* **Configurable concurrency** — a global semaphore caps total in-flight
  requests; a per-model semaphore caps per-model concurrency.
* **Max loaded models** — loading a new model past the cap evicts the
  least-recently-used idle model.
* **Idle unload** — a background sweep unloads models idle beyond their timeout.
* **Failure cleanup** — a failed start transitions to FAILED and tears down the
  process.

Routing (pools)
---------------
A *public model name* maps to one or more member aliases (a load-balancing
**pool**). Routing is governed by ``route_strategy``:

* ``sticky`` (default) — pin each *session* to one member and keep using it,
  only switching members when that member is unhealthy/unavailable. A session is
  identified by an ``X-Session-Id`` header, else the OpenAI ``user`` field, else
  the client IP (derived in the API layer and passed in as ``route_key``). New
  sessions are assigned the least-busy member, so distinct sessions still spread
  across the pool, but a given conversation keeps hitting the same backend (warm
  cache / KV locality).
* ``least_busy`` — route every request to the member with the fewest in-flight
  requests (round-robin tie-break).
* ``round_robin`` — rotate members every request.

All strategies share **failover**: if a member's backend is unavailable the
request transparently retries on another member, and under ``sticky`` the
session is re-pinned to the new member. A name with a single member behaves
exactly as a standalone model.

Health probing (non-loading)
----------------------------
A background probe liveness-checks active models *without loading any*: external
daemons (Ollama) via ``GET /api/tags``; broker-managed runtimes only while their
process is already running. A READY member whose backend has gone away is marked
FAILED so the next request reloads/reroutes instead of erroring mid-flight.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import random
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from . import runtime_identity as rid
from .adapters import make_adapter
from .admission import (
    TRIGGER_BACKEND_BACKPRESSURE,
    TRIGGER_CAPACITY_MEMORY,
    TRIGGER_DEVICE_MEMORY,
    TRIGGER_GLOBAL_CONCURRENCY,
    TRIGGER_HOST_LOAD,
    TRIGGER_MAX_LOADED,
    TRIGGER_MODEL_CONCURRENCY,
    AdmissionQueue,
    Waiter,
)
from .cache_routing import CacheRouter
from .config import Settings
from .errors import (
    AuthorizationError,
    BackendUnavailableError,
    BrokerError,
    CapacityUnavailableError,
    ConcurrencyLimitError,
    ConfigError,
    ModelBusyError,
    ModelLoadError,
    ModelNotFoundError,
    PlacementDeniedError,
    QueueFullError,
    QueueTimeoutError,
    ReleaseStuckError,
    ShuttingDownError,
    TimeoutError_,
    ValidationError,
)
from .ledger import LedgerOwnerLock, LockOwner, RuntimeLedger, RuntimeRecord, argv_hash
from .lifecycle import LifecycleMachine, State
from .metrics import (
    QUEUE_ADMITTED,
    QUEUE_DEPTH,
    QUEUE_ENQUEUED,
    QUEUE_REJECTED,
    QUEUE_TIMEOUTS,
    QUEUE_WAIT_SECONDS,
)
from .registry import ModelEntry, Registry, load_registry
from .reliability import ErrorClass, RetryPolicy, classify_error, retry_after_seconds
from .resources import TelemetrySample, make_telemetry
from .router import AutoRouter, should_escalate
from .state import make_state_store
from .teardown import TeardownResult, reap_broker_zombies
from .tracing import make_tracer

log = logging.getLogger("broker.manager")


class _WouldBlock(Exception):  # noqa: N818 - internal control-flow signal, not an error
    """Internal: admission cannot proceed right now; the caller should queue.

    Never surfaces to a client — it is caught inside the admission loop and
    turned into a queue wait (or, if the queue is full/times out, a typed
    :class:`QueueFullError` / :class:`QueueTimeoutError`). Carries the overload
    ``trigger`` that blocked admission.
    """

    def __init__(self, trigger: str) -> None:
        super().__init__(trigger)
        self.trigger = trigger


#: Exceptions that make a pool member a failover candidate under the DEFAULT
#: per-error policy (ADR 0009). ADR 0017's ``RetryPolicy`` is authoritative — it
#: classifies every error and decides retry/cooldown per class — but this tuple
#: documents the default-retryable set (timeout / connection / server_error).
_FAILOVER_ERRORS = (BackendUnavailableError, ModelLoadError, TimeoutError_)


@dataclass
class ResolvedRoute:
    """The outcome of resolving a request's model selection (ADR 0011).

    ``hops`` is the ordered list of concrete public model names to try: one entry
    for a single-shot route, two for a cascade (``[primary, escalation]``).
    """

    hops: list[str]
    public_name: str  # the name the caller requested (e.g. "auto" or a model id)
    mode: str = "single"
    cascade_cfg: Any = None  # CascadeConfig when mode == "cascade"
    required_placement: frozenset[str] = field(default_factory=frozenset)


@dataclass
class StreamPlan:
    """How a streaming request will be served (ADR 0019).

    ``kind == "single"`` streams one concrete model (``concrete``); this is the
    default and the only mode for completions/responses. ``kind ==
    "cascade_buffer"`` is the opt-in buffered streaming cascade: ``hops`` holds
    ``[primary, escalation]`` and the chat endpoint buffers the cheap hop, scores
    it, then replays it or streams the stronger hop — one model's output per
    stream, never a mix. ``route_path`` is the ``x-broker-route`` header value.
    """

    route_path: str
    kind: str  # "single" | "cascade_buffer"
    public_name: str = ""
    concrete: str | None = None  # for kind == "single"
    hops: list[str] | None = None  # for kind == "cascade_buffer"
    cascade_cfg: Any = None
    required_placement: frozenset[str] = field(default_factory=frozenset)
    # Set by stream_cascade_buffer once the serving model is known, so the caller
    # can attribute the usage row to the model that actually served.
    served: str | None = None


def _result_to_sse_chunks(result: dict, model: str) -> list[bytes]:
    """Reconstruct OpenAI chat streaming SSE from a buffered completion dict.

    Used by the buffered streaming cascade (ADR 0019) to replay the cheap hop's
    already-generated answer as a stream. The whole answer is emitted as a single
    content chunk followed by a finish chunk and ``[DONE]`` — it is one model's
    output, never a mix of two.
    """
    choices = result.get("choices") or [{}]
    ch = choices[0] or {}
    msg = ch.get("message") or {}
    content = msg.get("content")
    content = content if isinstance(content, str) else ""
    finish = ch.get("finish_reason") or "stop"
    base = {
        "id": result.get("id") or "chatcmpl-cascade",
        "object": "chat.completion.chunk",
        "created": result.get("created") or int(time.time()),
        "model": model,
    }
    first = {
        **base,
        "choices": [
            {"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}
        ],
    }
    final: dict[str, Any] = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
    }
    return [
        f"data: {json.dumps(first)}\n\n".encode(),
        f"data: {json.dumps(final)}\n\n".encode(),
        b"data: [DONE]\n\n",
    ]


class ManagedModel:
    def __init__(
        self,
        entry: ModelEntry,
        settings: Settings,
        ledger: RuntimeLedger | None = None,
        broker_instance_id: str = "",
        broker_pid: int | None = None,
        broker_start_time: str | None = None,
    ) -> None:
        self.entry = entry
        self.settings = settings
        self.adapter = make_adapter(entry, settings)
        self.machine = LifecycleMachine(alias=entry.alias)
        self.load_lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(settings.per_model_max_concurrency)
        # Routing / health state.
        self.public_name = entry.public_name
        self.healthy = True
        # Runtime-identity / restart-reconciliation state (ADR 0021).
        self.ledger = ledger
        self.broker_instance_id = broker_instance_id
        #: The owning broker process's identity, stamped onto each ledger record.
        self.broker_pid = broker_pid if broker_pid is not None else os.getpid()
        self.broker_start_time = broker_start_time
        #: Normalized process start time of the current runtime (identity anchor).
        self.process_start_time: str | None = None
        #: True when the current runtime was ADOPTED at startup, not spawned.
        self.adopted = False
        # Set True while an admin unload/reload is draining this member: selection
        # skips it so in-flight requests finish without new ones piling on.
        self.draining = False
        self._route_seq = 0
        self.last_health: dict[str, Any] | None = None
        # Optional real load signal (vLLM /metrics num_requests_waiting) refreshed
        # by the non-loading health probe when cache-aware routing wants it; None
        # falls back to in-flight counts. See ADR 0010.
        self.last_load: float | None = None
        # Monotonic deadline until which this member is treated as under upstream
        # backpressure (ADR 0017 429 + Retry-After). The ADR 0023 admission queue
        # reads it as a backend-backpressure overload trigger. 0 = not throttled.
        self.backpressure_until: float = 0.0
        # ADR 0024: the asyncio task currently running a load, so shutdown can
        # preempt an in-progress load instead of waiting unbounded on load_lock.
        self._load_task: asyncio.Task | None = None
        # ADR 0024: flipped True once the broker starts stopping (first stop
        # signal) so a request waiting on a cold load fails fast with a typed 503
        # (shutting_down) and no NEW load is admitted, instead of hanging until
        # the shutdown budget expires. Set by ModelManager.begin_preemption /
        # shutdown; also set when a load is cancel_load()'d during teardown.
        self._shutting_down = False

    async def ensure_ready(self) -> None:
        """Start + ready the model if needed, exactly once, under the load lock."""
        if self.machine.state in (State.READY, State.BUSY):
            return
        if self._shutting_down:
            raise ShuttingDownError(
                f"broker is shutting down; not loading model {self.entry.alias}",
                backend=self.adapter.name,
                model=self.entry.alias,
            )
        async with self.load_lock:
            if self.machine.state in (State.READY, State.BUSY):
                return
            if self._shutting_down:
                raise ShuttingDownError(
                    f"broker is shutting down; not loading model {self.entry.alias}",
                    backend=self.adapter.name,
                    model=self.entry.alias,
                )
            if self.machine.state == State.FAILED:
                self.machine.to(State.UNLOADED)  # allow a fresh attempt
            self.machine.to(State.LOADING)
            self._load_task = asyncio.current_task()
            try:
                pid = await self.adapter.start()
                self.machine.pid = pid
                await self.adapter.wait_until_ready(
                    self.entry.startup_timeout_seconds or self.settings.startup_timeout_seconds
                )
                self.machine.to(State.READY)
                self.machine.touch()
                self.healthy = True
                self.adopted = False
                self._record_runtime(pid)
                log.info("model %s ready (pid=%s)", self.entry.alias, pid)
            except asyncio.CancelledError:
                # ADR 0024: the load was preempted. If the broker is stopping, the
                # partially-spawned runtime is torn down by the shutdown/stop path
                # (which signals every model), so here we only flip state out of
                # LOADING and convert the cancellation into a typed 503 so the
                # waiting client fails fast. A cancellation for any OTHER reason
                # (e.g. a client disconnect) is propagated unchanged.
                try:
                    self.machine.to(State.FAILED, error="load preempted by shutdown")
                except Exception:  # noqa: BLE001 - never mask the original cancel
                    pass
                self.healthy = False
                if self._shutting_down:
                    raise ShuttingDownError(
                        f"broker is shutting down; load of model {self.entry.alias} was preempted",
                        backend=self.adapter.name,
                        model=self.entry.alias,
                    ) from None
                raise
            except Exception as exc:  # noqa: BLE001
                self.machine.to(State.FAILED, error=str(exc))
                self.healthy = False
                # Clean up any partially-started process.
                try:
                    await self.adapter.stop()
                except Exception:  # noqa: BLE001
                    pass
                if isinstance(exc, ModelLoadError):
                    raise
                raise ModelLoadError(
                    f"failed to load model {self.entry.alias}: {exc}",
                    backend=self.adapter.name,
                    model=self.entry.alias,
                    state=self.machine.state.value,
                    remediation="check runtime logs and registry startup_command",
                ) from exc
            finally:
                self._load_task = None

    def _record_runtime(self, pid: int | None) -> None:
        """Persist a ledger record for a freshly-spawned broker-managed runtime.

        No-op for backends that do not spawn a broker-owned process (no pid, no
        startup_command, or no ledger configured) — e.g. the fake backend and
        external daemons like Ollama, so default/test behavior is unchanged.
        """
        if self.ledger is None or pid is None or not self.entry.startup_command:
            return
        start_time = rid.process_start_time(pid)
        self.process_start_time = start_time
        record = RuntimeRecord(
            alias=self.entry.alias,
            pid=pid,
            pgid=rid.pgid_of(pid),
            start_time=start_time,
            port=rid.port_from_url(self.entry.base_url),
            argv_sha256=argv_hash(self.adapter.runtime_argv()),
            broker_instance_id=self.broker_instance_id,
            base_url=self.entry.base_url,
            spawned_at=time.time(),
            broker_pid=self.broker_pid,
            broker_start_time=self.broker_start_time,
        )
        try:
            self.ledger.upsert(record)
        except Exception as exc:  # noqa: BLE001 - ledger failure must not break a load
            log.warning("failed to record runtime %s in ledger: %s", self.entry.alias, exc)

    def _forget_runtime(self) -> None:
        """Drop this model's ledger record on a clean stop/unload — but ONLY if
        this broker owns the runtime (it spawned or adopted it, so
        ``process_start_time`` is set). A record for a runtime this broker never
        owned — an orphan merely observed in ``report``/``off`` mode, or a failed
        load that never spawned a process — is left intact so a later
        ``adopt``/``stop`` restart can still reconcile it (ADR 0021: never touch a
        runtime this broker did not record)."""
        owned = self.process_start_time is not None
        self.process_start_time = None
        self.adopted = False
        if self.ledger is None or not owned:
            return
        try:
            self.ledger.remove(self.entry.alias)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to remove runtime %s from ledger: %s", self.entry.alias, exc)

    async def adopt(self, record: RuntimeRecord) -> bool:
        """Adopt a recorded, still-alive orphaned runtime (ADR 0021, adopt mode).

        Preconditions (ALL required, checked by the caller's identity gate plus
        here): the process is alive with a matching start time, the registry argv
        still matches the recorded hash (no drift), and the runtime's port answers
        the readiness probe. On success the model is marked READY **without
        spawning** and the ledger record is kept. Returns False (no adoption) if
        any check fails — the caller then surfaces it as an orphan instead.
        """
        # Registry drift check: the current spawn argv must hash to the record.
        planned = self.adapter.planned_argv()
        if planned is None or argv_hash(planned) != record.argv_sha256:
            log.info("cannot adopt %s: registry argv no longer matches record", self.entry.alias)
            return False
        # Probe readiness BEFORE taking ownership (the probe uses base_url, not the
        # process handle), so a failed probe leaves no adopted state to unwind.
        try:
            health = await self.adapter.health()
        except Exception as exc:  # noqa: BLE001
            log.info("cannot adopt %s: readiness probe errored: %s", self.entry.alias, exc)
            return False
        if not health.healthy:
            log.info("cannot adopt %s: port did not answer readiness probe", self.entry.alias)
            return False
        # Take ownership of the live process so a later stop() can terminate it.
        self.adapter.mark_adopted(record.pid, record.pgid, record.start_time)
        # Mark READY without spawning (UNLOADED -> LOADING -> READY).
        if self.machine.state == State.FAILED:
            self.machine.to(State.UNLOADED)
        if self.machine.state == State.UNLOADED:
            self.machine.to(State.LOADING)
        self.machine.to(State.READY)
        self.machine.pid = record.pid
        self.machine.touch()
        self.healthy = True
        self.adopted = True
        self.process_start_time = record.start_time
        log.info(
            "adopted orphaned runtime for model %s (pid=%s, port=%s)",
            self.entry.alias,
            record.pid,
            record.port,
        )
        return True

    async def unload(self) -> None:
        if not self.machine.is_active():
            return
        if self.machine.inflight > 0:
            return  # don't unload while serving
        async with self.load_lock:
            if self.machine.inflight > 0 or not self.machine.is_active():
                return
            self.machine.to(State.UNLOADING)
            try:
                await self.adapter.unload()
                self.machine.survived_kill = not self.adapter.last_release_ok()
                self.machine.to(State.UNLOADED)
                self._forget_runtime()
                log.info("model %s unloaded", self.entry.alias)
            except Exception as exc:  # noqa: BLE001
                self.machine.to(State.FAILED, error=str(exc))

    def cancel_load(self) -> asyncio.Task | None:
        """Preempt an in-progress load (ADR 0024 shutdown).

        Marks the model as shutting down (so the preempted load — and any request
        queued behind the load lock — fails fast with a typed 503 instead of a
        bare ``CancelledError``) and returns the cancelled load task (if any) so
        the caller can await its unwind before tearing down the spawning runtime.
        """
        self._shutting_down = True
        task = self._load_task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            return task
        return None

    async def stop(
        self,
        *,
        grace: float | None = None,
        hard_deadline: float | None = None,
        sigkill_only: bool = False,
        force_check: Callable[[], bool] | None = None,
    ) -> TeardownResult | None:
        """Stop the runtime within a deadline, preempting any in-progress load.

        ADR 0024: never waits unbounded on ``load_lock`` — an in-progress load is
        cancelled first (its spawning runtime is then torn down like any other),
        so a slow load can no longer hold shutdown open. The teardown result is
        recorded on the lifecycle machine (``survived_kill`` / ``release_info``)
        so a surviving runtime stays visible via ``/status`` ``release_stuck``.
        """
        task = self.cancel_load()
        if task is not None:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        if self.machine.state != State.UNLOADED:
            try:
                self.machine.to(State.STOPPING)
            except Exception:  # noqa: BLE001
                pass
        result: TeardownResult | None = None
        try:
            result = await self.adapter.stop(
                grace=grace,
                hard_deadline=hard_deadline,
                sigkill_only=sigkill_only,
                force_check=force_check,
            )
        finally:
            self._forget_runtime()
            try:
                self.machine.to(State.UNLOADED)
            except Exception:  # noqa: BLE001
                self.machine.state = State.UNLOADED
        if result is not None:
            self.machine.survived_kill = not result.ok()
            self.machine.release_info = result.to_dict()
            if result.survivors:
                log.error(
                    "teardown of %s left survivors: %s (outcome=%s, %.2fs)",
                    self.entry.alias,
                    result.survivors,
                    result.outcome,
                    result.duration,
                )
            else:
                log.info(
                    "teardown of %s: %s (%.2fs)",
                    self.entry.alias,
                    result.outcome,
                    result.duration,
                )
        return result


class ModelManager:
    def __init__(self, registry: Registry, settings: Settings) -> None:
        self.settings = settings
        self.registry = registry
        # Runtime ledger + this broker's instance id (ADR 0021). The ledger lives
        # under cache_dir and records every broker-spawned runtime so a restart can
        # reconcile orphans; a unique instance id tags which broker spawned each.
        self.broker_instance_id = secrets.token_hex(8)
        # This broker process's own durable identity (PID + start time), stamped
        # onto every ledger record and the ownership lock so reconcile can tell a
        # dead owner's orphans from a live sibling's runtimes (ADR 0021).
        self.broker_pid = os.getpid()
        self.broker_start_time = rid.process_start_time(self.broker_pid)
        self.ledger: RuntimeLedger | None = RuntimeLedger(self._ledger_path(settings))
        #: Broker-lifetime exclusive lock enforcing one live broker per cache dir.
        self._owner_lock = LedgerOwnerLock(self._owner_lock_path(settings))
        #: Set when the ledger was disabled because a live sibling owns it; shown
        #: in /status so an operator sees why reconciliation is inert here.
        self.reconcile_disabled_reason: str | None = None
        #: Summary of the last startup reconciliation, surfaced in /status.
        self.last_reconciliation: dict[str, Any] = {}
        self.models: dict[str, ManagedModel] = {
            e.alias: ManagedModel(
                e,
                settings,
                self.ledger,
                self.broker_instance_id,
                self.broker_pid,
                self.broker_start_time,
            )
            for e in registry.models
        }
        # Public name -> ordered list of member ManagedModels (a pool).
        self.pools: dict[str, list[ManagedModel]] = {}
        for e in registry.models:
            self.pools.setdefault(e.public_name, []).append(self.models[e.alias])
        self.global_sem = asyncio.Semaphore(settings.max_concurrent_requests)
        self._route_lock = asyncio.Lock()
        self._route_counter = itertools.count(1)
        # Shared routing/runtime state (ADR 0009): in-process by default, or
        # Redis when BROKER_REDIS_URL is set (multi-replica). Owns the sticky
        # session map, per-member cooldowns, counters, and a generic KV.
        self.state = make_state_store(settings)
        # Per-error retry/cooldown policy (ADR 0017). Classifies every failure and
        # decides retry + cooldown-counting per class; defaults reproduce ADR 0009.
        self.retry_policy = RetryPolicy(settings)
        # Optional OpenTelemetry tracer (ADR 0012): no-op unless tracing_enabled.
        # Constructed here so an enabled-but-not-installed otel extra fails fast
        # at app creation (ConfigError), never mid-request.
        self.tracer = make_tracer(settings)
        # Back-compat view of the sticky map for in-process deployments/tests:
        # (public_name, session_key) -> member alias, LRU-ordered. None under
        # Redis (the shared store is authoritative there).
        self._sticky = getattr(self.state, "sticky", None)
        # Cache-aware routing (ADR 0010): built only when selected, so the default
        # path constructs nothing. Owns the prefix->member index + affinity scoring.
        self.cache_router: CacheRouter | None = None
        if settings.route_strategy == "cache_aware":
            self.cache_router = CacheRouter(
                self.state,
                block_chars=settings.cache_aware_block_chars,
                beta=settings.cache_aware_beta,
                max_entries=settings.cache_aware_max_entries,
                ttl_seconds=settings.cache_aware_ttl_seconds,
            )
        # Auto-router ("auto" alias). Lazy-constructed; classification serialized.
        self.router: AutoRouter | None = None
        self._router_lock = asyncio.Lock()
        if settings.router_enabled and settings.router_backend != "off":
            self.router = AutoRouter(self, settings, registry.router)
        # Usage log (set by the app after construction); used for latency estimates
        # and to record intermediate cascade hops (ADR 0007 / 0011). Optional.
        self.usage: Any = None
        self._sweeper: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._stopping = False
        # ADR 0024: flipped True by a second SIGTERM/SIGINT during shutdown, so the
        # in-flight teardown escalates straight to SIGKILL of every runtime tree.
        self._force_shutdown = False
        # ADR 0024: the monotonic deadline for the WHOLE stop path, stamped once at
        # the first stop signal (begin_preemption) so the time uvicorn spends in
        # its graceful phase counts against the same budget the runtime teardown
        # draws from — the total from first signal to exit stays <= the budget.
        self._shutdown_deadline: float | None = None
        self._preempting = False
        # The running event loop, captured at startup so begin_preemption (called
        # from uvicorn's signal handler) can schedule preemption thread-safely.
        self._loop: asyncio.AbstractEventLoop | None = None
        # Resource telemetry (ADR 0020): a provider selected by BROKER_GPU_TELEMETRY
        # ("off" by default => disabled). Samples are cached for
        # gpu_telemetry_poll_seconds so a status read or admission check does not
        # re-shell the driver tool on every call.
        self.telemetry = make_telemetry(settings)
        self._telemetry_cache: TelemetrySample | None = None
        self._telemetry_cache_at: float = 0.0
        self._telemetry_lock = asyncio.Lock()
        # Serializes memory-aware admission (admit -> evict -> reserve) across ALL
        # models so two concurrent loads cannot both pass against the same free
        # reading and double-book a device (ADR 0022 follow-up). Held only for the
        # admission decision + eviction wait, never across a model's whole load.
        self._admission_lock = asyncio.Lock()
        # Recent memory-aware admission decisions (ADR 0022), newest last, for
        # /status visibility and debugging.
        self._admissions: deque[dict[str, Any]] = deque(maxlen=50)
        # Admission queue (ADR 0023): bounded/fair/timed waiting room in front of
        # model serving. A no-op passthrough unless BROKER_QUEUE_ENABLED, so the
        # default path keeps exact v0.6.0 concurrency behavior.
        self.admission = AdmissionQueue(settings)
        self._queue_pump_task: asyncio.Task | None = None
        # Cheap, cached host 1-min load-average-per-CPU for the host overload
        # trigger (os.getloadavg; refreshed at most once per poll interval).
        self._host_load_value: float | None = None
        self._host_load_at: float = 0.0

    # --- validation ------------------------------------------------------
    def validate_all(self) -> None:
        """Validate every adapter config at startup (no model loads)."""
        for m in self.models.values():
            m.adapter.validate_config()

    # --- runtime ledger / reconciliation (ADR 0021) ----------------------
    @staticmethod
    def _ledger_path(settings: Settings) -> str:
        """Host-local path of the runtime ledger under BROKER_CACHE_DIR."""
        return f"{settings.cache_dir.rstrip('/')}/runtime-ledger.json"

    @staticmethod
    def _owner_lock_path(settings: Settings) -> str:
        """Path of the broker-lifetime ledger ownership lock, next to the ledger."""
        return f"{settings.cache_dir.rstrip('/')}/runtime-ledger.owner.lock"

    def acquire_ledger_ownership(self) -> None:
        """Take the broker-lifetime ownership lock on the runtime ledger (ADR 0021).

        Enforces one live broker per ``BROKER_CACHE_DIR``. Called once from the app
        lifespan BEFORE :meth:`reconcile`. If a live sibling broker already owns the
        ledger (e.g. two local brokers accidentally sharing the default cache dir):

        * ``adopt`` / ``stop`` — refuse to start with a :class:`ConfigError` naming
          the cache dir and the owner PID. The operator opted into signalling or
          adopting runtimes, so acting on a sibling's runtimes is unsafe: fail
          closed.
        * ``report`` (default) / ``off`` — start normally but disable this
          instance's ledger and reconciliation, emitting one WARNING and surfacing
          ``reconciliation.disabled_reason`` in ``/status``. This preserves v0.6.0
          compatibility: two brokers sharing a dir must keep starting.

        A stale lock (previous owner dead) is taken over automatically — an
        advisory ``flock`` frees when the owning fd closes, including on process
        death — so no manual liveness check is needed here.
        """
        if self.ledger is None or not self.ledger.path:
            return
        owner = LockOwner(
            pid=self.broker_pid,
            start_time=self.broker_start_time,
            instance_id=self.broker_instance_id,
            acquired_at=time.time(),
        )
        try:
            held_by = self._owner_lock.acquire(owner)
        except OSError as exc:
            # The lock file could not be created/locked (e.g. an unwritable cache
            # dir, or a filesystem without flock). This is a local IO problem, not
            # a sibling: degrade to no-ledger (as the ledger itself does on write
            # failure) rather than failing startup — preserves v0.6.0 behavior.
            reason = f"runtime ledger unavailable ({exc}); running without a ledger"
            log.warning("%s", reason)
            self._disable_ledger(reason)
            return
        if held_by is None:
            return  # we own the ledger
        mode = self.settings.runtime_reconcile
        cache_dir = self.settings.cache_dir
        if mode in ("adopt", "stop"):
            raise ConfigError(
                f"another broker (pid {held_by.pid}) already owns the runtime "
                f"ledger under BROKER_CACHE_DIR {cache_dir!r}; refusing to start "
                f"with BROKER_RUNTIME_RECONCILE={mode!r} because reconciliation "
                f"would signal or adopt that live broker's runtimes",
                remediation="give each broker its own BROKER_CACHE_DIR",
            )
        reason = (
            f"another broker (pid {held_by.pid}) owns the runtime ledger under "
            f"BROKER_CACHE_DIR {cache_dir!r}; this instance runs without a ledger "
            f"(give each broker its own BROKER_CACHE_DIR)"
        )
        log.warning("runtime ledger ownership unavailable: %s", reason)
        self._disable_ledger(reason)

    def _disable_ledger(self, reason: str) -> None:
        """Drop the ledger for this instance (no writes, no reconciliation)."""
        self.reconcile_disabled_reason = reason
        self.ledger = None
        for m in self.models.values():
            m.ledger = None

    async def reconcile(self) -> dict[str, Any]:
        """Reconcile ledgered runtimes at startup per BROKER_RUNTIME_RECONCILE.

        Called once from the app lifespan BEFORE the sweeper/health loops start.
        Never loads a model (adoption marks an already-live runtime READY without
        spawning). The result is stored for /status.
        """
        from .reconcile import reconcile_runtimes

        self.last_reconciliation = await reconcile_runtimes(self)
        return self.last_reconciliation

    def get(self, alias: str) -> ManagedModel:
        m = self.models.get(alias)
        if m is None:
            raise ModelNotFoundError(
                f"unknown model alias: {alias!r}",
                model=alias,
                remediation=f"choose one of: {', '.join(sorted(self.models))}",
            )
        return m

    @property
    def public_names(self) -> set[str]:
        return set(self.pools)

    def _members_for(
        self, name: str, required_placement: frozenset[str] | None = None
    ) -> list[ManagedModel]:
        members = self.pools.get(name)
        if not members:
            if name in self.models:  # direct alias addressing of a non-pooled entry
                members = [self.models[name]]
            else:
                raise ModelNotFoundError(
                    f"unknown model: {name!r}",
                    model=name,
                    remediation=f"choose one of: {', '.join(sorted(self.pools))}",
                )
        if required_placement:
            # Fail-closed: keep only members carrying every required placement label.
            kept = [m for m in members if required_placement <= m.entry.placement_set]
            if not kept:
                raise PlacementDeniedError(
                    f"model {name!r} does not satisfy placement requirement "
                    f"{sorted(required_placement)}",
                    model=name,
                    remediation="request a model carrying the required placement "
                    "labels, or relax x-broker-placement / BROKER_DEFAULT_PLACEMENT",
                )
            return kept
        return members

    def loaded_count(self) -> int:
        return sum(1 for m in self.models.values() if m.machine.is_active())

    def is_resident(self, name: str) -> bool:
        """True if any member of ``name`` (alias or pool) is already loaded.

        Used by the semantic cache (ADR 0014) to honor the lazy-load invariant:
        the embedding model is embedded/consulted only when it is already
        resident, so a chat request never silently loads an extra model.
        """
        members = self.pools.get(name)
        if not members:
            m = self.models.get(name)
            members = [m] if m is not None else []
        return any(m.machine.is_active() for m in members)

    async def _resolve_route(
        self,
        name: str,
        payload: dict,
        required_placement: frozenset[str],
        latency_budget_ms: float | None,
    ) -> ResolvedRoute:
        """Resolve a request's model selection into an ordered list of hops.

        For the ``auto`` alias this runs the router (serialized, off-thread) and may
        return a two-hop cascade plan. For an explicit model/pool it validates the
        placement requirement fail-closed (before any load) and returns one hop.
        """
        req = frozenset(required_placement or ())
        if self.router is not None and name == self.settings.router_alias:
            with self.tracer.span(
                "auto_route", {"gen_ai.operation.name": "route", "gen_ai.request.model": name}
            ) as sp:
                async with self._router_lock:
                    decision = await asyncio.to_thread(
                        self.router.route, payload, req, latency_budget_ms
                    )
                sp.set_attribute("broker.route.chosen", decision.chosen)
            if decision.cascade and len(decision.cascade) > 1:
                return ResolvedRoute(
                    hops=list(decision.cascade),
                    public_name=name,
                    mode="cascade",
                    cascade_cfg=self.router.cfg.cascade,
                    required_placement=req,
                )
            return ResolvedRoute(hops=[decision.chosen], public_name=name, required_placement=req)
        # Explicit model/pool: enforce placement now so denial precedes any load.
        self._members_for(name, req)
        return ResolvedRoute(hops=[name], public_name=name, required_placement=req)

    async def resolve_stream(
        self,
        name: str,
        payload: dict,
        required_placement: frozenset[str] | None = None,
        latency_budget_ms: float | None = None,
    ) -> tuple[str, str]:
        """Resolve a streaming request to a single concrete model + route label.

        This is the ``fallback_single`` path (the default and the only mode for
        the completions/responses streaming endpoints): a cascade route falls
        back to the single strong pick, because a live stream cannot be cheaply
        re-scored. For the ``buffer`` streaming-cascade mode the chat endpoint
        uses :meth:`plan_stream` / :meth:`stream_cascade_buffer` instead.
        Returns ``(name, route_path)``.
        """
        req = frozenset(required_placement or ())
        if self.router is not None and name == self.settings.router_alias:
            async with self._router_lock:
                decision = await asyncio.to_thread(
                    self.router.route, payload, req, latency_budget_ms, True
                )
            if decision.cascade and len(decision.cascade) > 1:
                strong = decision.cascade[-1]
                self.router.record_cascade(
                    decision.cascade[0], strong, False, "stream_fallback_single"
                )
                return strong, f"{strong} (cascade->single: streaming)"
            return decision.chosen, decision.chosen
        self._members_for(name, req)  # fail-closed placement for explicit models
        return name, name

    async def plan_stream(
        self,
        name: str,
        payload: dict,
        required_placement: frozenset[str] | None = None,
        latency_budget_ms: float | None = None,
        *,
        allow_buffer: bool = False,
    ) -> StreamPlan:
        """Plan a streaming request, optionally as a buffered streaming cascade.

        When ``allow_buffer`` is set, ``router.mode: cascade`` is active, and
        ``router.cascade.stream_mode == "buffer"`` (all opt-in; ADR 0019), a
        two-hop cascade route is returned as a ``cascade_buffer`` plan: the chat
        endpoint buffers the cheap hop, scores it, and then either replays the
        buffered answer or streams the stronger model — never interleaving two
        models into one stream. Otherwise this collapses to the single
        ``fallback_single`` pick (identical to :meth:`resolve_stream`).
        """
        req = frozenset(required_placement or ())
        cfg = self.router.cfg if self.router is not None else None
        want_buffer = (
            allow_buffer
            and cfg is not None
            and cfg.mode == "cascade"
            and getattr(cfg.cascade, "stream_mode", "fallback_single") == "buffer"
        )
        if self.router is not None and name == self.settings.router_alias:
            async with self._router_lock:
                decision = await asyncio.to_thread(
                    self.router.route, payload, req, latency_budget_ms, True
                )
            if decision.cascade and len(decision.cascade) > 1:
                if want_buffer:
                    primary, strong = decision.cascade[0], decision.cascade[-1]
                    return StreamPlan(
                        route_path=f"{primary}->{strong} (cascade:buffer)",
                        kind="cascade_buffer",
                        hops=list(decision.cascade),
                        cascade_cfg=self.router.cfg.cascade,
                        public_name=name,
                        required_placement=req,
                    )
                strong = decision.cascade[-1]
                self.router.record_cascade(
                    decision.cascade[0], strong, False, "stream_fallback_single"
                )
                return StreamPlan(
                    route_path=f"{strong} (cascade->single: streaming)",
                    kind="single",
                    concrete=strong,
                    public_name=name,
                    required_placement=req,
                )
            return StreamPlan(
                route_path=decision.chosen,
                kind="single",
                concrete=decision.chosen,
                public_name=name,
                required_placement=req,
            )
        self._members_for(name, req)  # fail-closed placement for explicit models
        return StreamPlan(
            route_path=name, kind="single", concrete=name, public_name=name, required_placement=req
        )

    def _record_hop(
        self, endpoint: str, requested: str, result: dict, request_id: str, status: str
    ) -> None:
        """Record an intermediate cascade hop in the usage log (best-effort)."""
        if self.usage is None or not isinstance(result, dict):
            return
        try:
            u = result.get("usage") or {}
            self.usage.record(
                endpoint=endpoint,
                requested=requested,
                model=result.get("model"),
                request_id=request_id,
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
                status=status,
            )
        except Exception as exc:  # noqa: BLE001 - logging must never break a request
            log.warning("cascade hop usage record failed: %s", exc)

    async def resolve_public(self, name: str, payload: dict) -> str:
        """Resolve ``auto``/cascade to the model the response cache keys on.

        Runs the router (no serving) and returns the model the request will run
        under; for a cascade route this is the first (primary) hop. The final
        served model for a cascade is carried in the cached response body, per
        ADR 0012.
        """
        resolved = await self._resolve_route(name, payload, frozenset(), None)
        return resolved.hops[0]

    # --- eviction --------------------------------------------------------
    # --- telemetry (ADR 0020) -------------------------------------------
    def telemetry_enabled(self) -> bool:
        return getattr(self.telemetry, "name", "off") != "off"

    async def telemetry_sample(self, *, force: bool = False) -> TelemetrySample:
        """Return a cached telemetry sample, re-probing at most every poll window.

        The provider's blocking ``sample()`` runs in a thread so the event loop is
        never stalled by a driver-tool subprocess. Never raises — a failed probe
        yields an ``unavailable`` sample.
        """
        now = time.monotonic()
        ttl = self.settings.gpu_telemetry_poll_seconds
        if (
            not force
            and self._telemetry_cache is not None
            and (now - self._telemetry_cache_at) < ttl
        ):
            return self._telemetry_cache
        async with self._telemetry_lock:
            now = time.monotonic()
            if (
                not force
                and self._telemetry_cache is not None
                and (now - self._telemetry_cache_at) < ttl
            ):
                return self._telemetry_cache
            try:
                sample = await asyncio.to_thread(self.telemetry.sample)
            except Exception as exc:  # noqa: BLE001 - provider must never crash the broker
                log.warning("telemetry sample failed: %s", exc)
                sample = TelemetrySample(
                    provider=getattr(self.telemetry, "name", "unknown"),
                    status="unavailable",
                    reason=str(exc),
                )
            self._telemetry_cache = sample
            self._telemetry_cache_at = now
            return sample

    def _record_admission(self, alias: str, decision: str, detail: dict[str, Any]) -> None:
        self._admissions.append(
            {"alias": alias, "decision": decision, "detail": detail, "at": time.time()}
        )

    # --- admission + eviction (ADR 0020 measured release + ADR 0022) -----
    async def _maybe_evict(self, keep_alias: str) -> None:
        """Make room to load ``keep_alias``: memory-aware admission then count.

        The whole decision runs under a single global ``_admission_lock`` so two
        concurrent loads cannot both measure the same free memory and double-book a
        device. ADR 0022 memory admission runs first (a no-op for an unsized model
        or when neither telemetry nor a declared budget is configured), then the
        v0.6.0 count-based LRU limit. Every eviction is followed by the ADR 0020
        measured release wait. Finally the model is *reserved* (transitioned to
        LOADING) while the lock is still held, so a concurrent admission accounts
        for it. Models without ``memory_mib`` and with telemetry off keep exactly
        v0.6.0 behavior. The lock is released before the model's actual load.
        """
        m = self.get(keep_alias)
        async with self._admission_lock:
            await self._guard_release_stuck(m)
            await self._admit_memory(m)
            await self._evict_for_count(keep_alias)
            self._reserve(m)

    def _reserve(self, m: ManagedModel) -> None:
        """Mark a just-admitted model LOADING so a concurrent admission sees it.

        Done under the admission lock: the model's ``memory_mib`` then counts as an
        outstanding reservation (see :meth:`_outstanding_reservation`) before its
        driver memory is visible to telemetry. ``ensure_ready`` later finds it
        already LOADING and proceeds without re-transitioning.
        """
        st = m.machine.state
        if st in (State.READY, State.BUSY, State.LOADING):
            return
        if st in (State.FAILED, State.RELEASE_STUCK):
            m.machine.to(State.UNLOADED)
        if m.machine.state == State.UNLOADED:
            m.machine.to(State.LOADING)

    async def _evict_for_count(self, keep_alias: str) -> None:
        if self.loaded_count() < self.settings.max_loaded_models:
            return
        candidates = [
            mm
            for a, mm in self.models.items()
            if a != keep_alias and mm.machine.state == State.READY and mm.machine.inflight == 0
        ]
        if not candidates:
            raise ConcurrencyLimitError(
                "max loaded models reached and no idle model to evict",
                remediation="raise BROKER_MAX_LOADED_MODELS or wait",
            )
        victim = min(candidates, key=lambda mm: mm.machine.last_used)
        log.info("evicting LRU model %s to make room for %s", victim.entry.alias, keep_alias)
        await self._evict_model(victim)

    async def _guard_release_stuck(self, m: ManagedModel) -> None:
        """Clear or enforce release_stuck holders on ``m``'s device(s) before a load.

        Only active when telemetry is on. Each ``release_stuck`` model sharing a
        device (including ``m`` itself) is **re-checked** first: if its group PIDs
        are gone and the device's free memory has recovered, it is transitioned
        RELEASE_STUCK -> UNLOADED and cleared (the memory came back). Any holder
        still holding memory fails the load fast with a clear 503 — loading into
        the same device would race an OOM, and the stuck model itself must not
        reload while its old memory is still out.
        """
        if not self.telemetry_enabled():
            return
        keys = set(m.adapter.device_keys())
        # The model itself: a stuck model may only reload once its memory returns.
        if m.machine.state == State.RELEASE_STUCK and not await self._recheck_release_stuck(m):
            info = m.machine.release_info or {}
            raise ReleaseStuckError(
                f"model {m.entry.alias!r} has not released its previous device yet",
                model=m.entry.alias,
                state="release_stuck",
                remediation=(
                    f"its runtime (pid {info.get('pid')}) still holds "
                    f"~{info.get('mib_held')} MiB on device {info.get('device')}; "
                    "free it or wait for the memory to return"
                ),
            )
        for other in self.models.values():
            if other is m or other.machine.state != State.RELEASE_STUCK:
                continue
            if not (keys & set(other.adapter.device_keys())):
                continue
            if await self._recheck_release_stuck(other):
                continue  # recovered: memory came back, holder cleared
            info = other.machine.release_info or {}
            raise ReleaseStuckError(
                f"device still held by release-stuck model {other.entry.alias!r}",
                model=m.entry.alias,
                state="release_stuck",
                remediation=(
                    "the previous runtime did not release its device; inspect "
                    f"pid {info.get('pid')} on device {info.get('device')} "
                    "(it may be a zombie) then retry"
                ),
            )

    async def _recheck_release_stuck(self, m: ManagedModel) -> bool:
        """Re-test a release_stuck model; clear it to UNLOADED if memory returned.

        Returns True when the model is not (or no longer) stuck — either it was
        never stuck, or its group PIDs are now absent AND the device's free memory
        has recovered, so it is transitioned RELEASE_STUCK -> UNLOADED with
        ``survived_kill`` / ``release_info`` cleared. Returns False while the memory
        is still held. Needs telemetry to verify recovery; without it the model
        stays stuck (we cannot prove the memory came back).
        """
        if m.machine.state != State.RELEASE_STUCK:
            return True
        if not self.telemetry_enabled():
            return False
        info = m.machine.release_info or {}
        snapshot = info.get("snapshot") or {}
        group_pids = set(info.get("group_pids") or [])
        if not group_pids and info.get("pid") is not None:
            group_pids = {int(info["pid"])}
        sample = await self.telemetry_sample(force=True)
        released, _detail = self._release_state(sample, group_pids, snapshot)
        proc_gone = not await m.adapter.process_alive()
        if not (proc_gone and released):
            return False
        try:
            m.machine.to(State.UNLOADED)
        except Exception:  # noqa: BLE001 - never let a state guard strand recovery
            m.machine.state = State.UNLOADED
        m.machine.survived_kill = False
        m.machine.release_info = None
        log.info("model %s recovered from release_stuck: device memory returned", m.entry.alias)
        self._record_admission(m.entry.alias, "release_recovered", {"device": info.get("device")})
        return True

    async def _recheck_all_release_stuck(self) -> None:
        """Periodic pass (idle sweep): clear any release_stuck model that recovered."""
        if not self.telemetry_enabled():
            return
        for m in list(self.models.values()):
            if m.machine.state == State.RELEASE_STUCK:
                try:
                    await self._recheck_release_stuck(m)
                except Exception as exc:  # noqa: BLE001 - never let recovery break the sweep
                    log.warning("release_stuck recheck for %s failed: %s", m.entry.alias, exc)

    async def _evict_model(self, victim: ManagedModel) -> None:
        """Unload ``victim`` then wait for its device to be released (ADR 0020)."""
        pid = victim.adapter.process_pid()
        group_pids = set(victim.adapter.process_group_pids())
        keys = victim.adapter.device_keys()
        snapshot: dict[str, Any] = {}
        if self.telemetry_enabled():
            pre = await self.telemetry_sample(force=True)
            snapshot = self._snapshot_hold(pre, victim, group_pids, keys)
        await victim.unload()
        await self._await_release(victim, pid, group_pids, snapshot)

    def _snapshot_hold(
        self,
        sample: TelemetrySample,
        victim: ManagedModel,
        group_pids: set[int],
        keys: list[str],
    ) -> dict[str, Any]:
        """Snapshot what ``victim`` holds BEFORE it is stopped (ADR 0020 follow-up).

        Records, across the victim's relevant device(s): the held amount (the sum
        of its group PIDs' memory if the driver attributes any, else its declared
        ``memory_mib``, else ``None`` = unknown) and the device free memory at this
        instant. The release wait compares against this to confirm the memory came
        back — not just that the parent PID disappeared.
        """
        relevant = self._relevant_devices(sample, keys)
        holders: dict[int, int] = {}
        for d in relevant:
            holders.update(d.processes)
        present = [gp for gp in group_pids if gp in holders]
        if present:
            held: int | None = sum(holders[gp] for gp in present)
        elif victim.entry.memory_mib:
            held = victim.entry.memory_mib
        else:
            held = None
        frees = [d.free_mib for d in relevant if d.free_mib is not None]
        free_before = sum(frees) if frees else None
        dev_indices = [d.index for d in relevant]
        return {
            "devices": dev_indices,
            "device": dev_indices[0] if dev_indices else None,
            "held_mib": held,
            "free_before": free_before,
            "group_pids": sorted(group_pids),
        }

    def _release_state(
        self, sample: TelemetrySample, group_pids: set[int], snapshot: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        """Whether the evicted runtime has released its device memory (ADR 0020).

        Released ==  **none** of the runtime's group PIDs still appear as holders
        AND the device's free memory has recovered. Recovery uses the snapshot: if
        we know the held amount, free must have come back by at least
        ``gpu_release_recovery_fraction`` of it (tolerating another tenant taking a
        slice); if the amount is unknown (no PID attribution — e.g. a container
        where nvidia-smi lists no PIDs), any real increase over the pre-stop free
        counts, so memory that never comes back is correctly flagged stuck.
        """
        if not sample.ok:
            # No measurement right now: fall back to PID-only (best effort).
            return (True, {"reason": "telemetry_unavailable"})
        dev_indices = set(snapshot.get("devices") or [])
        relevant = [d for d in sample.devices if d.index in dev_indices] or list(sample.devices)
        holders: dict[int, int] = {}
        for d in relevant:
            holders.update(d.processes)
        present = any(gp in holders for gp in group_pids)
        frees = [d.free_mib for d in relevant if d.free_mib is not None]
        free_now = sum(frees) if frees else None
        held = snapshot.get("held_mib")
        free_before = snapshot.get("free_before")
        if present:
            recovered = False
        elif held is not None and held > 0:
            if free_before is None or free_now is None:
                recovered = True  # pids gone, cannot measure delta -> trust
            else:
                frac = self.settings.gpu_release_recovery_fraction
                recovered = free_now >= free_before + frac * held
        else:  # held unknown (no PID attribution)
            if free_before is None or free_now is None:
                recovered = True
            else:
                recovered = free_now > free_before
        released = (not present) and recovered
        info = {
            "device": snapshot.get("device"),
            "group_present": present,
            "free_now": free_now,
            "free_before": free_before,
            "held_mib": held,
        }
        return released, info

    async def _await_release(
        self,
        victim: ManagedModel,
        pid: int | None,
        group_pids: set[int],
        snapshot: dict[str, Any],
    ) -> None:
        """Wait until ``victim`` has released its device, or mark it release_stuck.

        Gated on telemetry being on (telemetry off keeps v0.6.0 timing; ``stop()``
        already reports a surviving process). Released == the process is gone AND
        :meth:`_release_state` confirms none of the runtime's **group** PIDs still
        hold memory AND the device free memory recovered, bounded by
        ``BROKER_RELEASE_TIMEOUT_SECONDS``. On timeout the model enters
        ``release_stuck`` and :class:`ReleaseStuckError` (503) is raised naming the
        held device/MiB. The broker never force-resets a GPU or kills a foreign PID.
        """
        if not self.telemetry_enabled():
            return
        timeout = self.settings.release_timeout_seconds
        if timeout <= 0:
            return
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        poll = max(0.05, min(self.settings.gpu_telemetry_poll_seconds, 1.0))
        last_info: dict[str, Any] = {}
        while loop.time() < deadline:
            proc_gone = not await victim.adapter.process_alive()
            sample = await self.telemetry_sample(force=True)
            released, last_info = self._release_state(sample, group_pids, snapshot)
            if proc_gone and released:
                return
            await asyncio.sleep(poll)
        # Timed out: surface a visible release_stuck state and fail the load 503.
        device = last_info.get("device") or snapshot.get("device")
        held_mib = snapshot.get("held_mib")
        info = {
            "pid": pid,
            "group_pids": sorted(group_pids),
            "device": device,
            "mib_held": held_mib,
            "free_before": snapshot.get("free_before"),
            "snapshot": snapshot,
            "timeout_seconds": timeout,
        }
        victim.machine.release_info = info
        victim.machine.survived_kill = True
        try:
            victim.machine.to(State.RELEASE_STUCK)
        except Exception:  # noqa: BLE001 - never let a state guard mask the 503
            victim.machine.state = State.RELEASE_STUCK
        log.error(
            "model %s is release_stuck: group %s still holds device %s "
            "(~%s MiB, free %s->%s) after %ss",
            victim.entry.alias,
            sorted(group_pids),
            device,
            held_mib,
            snapshot.get("free_before"),
            last_info.get("free_now"),
            timeout,
        )
        raise ReleaseStuckError(
            f"runtime for {victim.entry.alias!r} did not release device {device} within {timeout}s",
            model=victim.entry.alias,
            state="release_stuck",
            remediation=(
                f"group {sorted(group_pids)} still holds ~{held_mib} MiB (possible "
                "zombie CUDA/ROCm context, a lingering child, or a container PID "
                "namespace hiding the holder); free it or raise "
                "BROKER_RELEASE_TIMEOUT_SECONDS"
            ),
        )

    def _relevant_devices(self, sample: TelemetrySample, keys: list[str]) -> list[Any]:
        matched = [d for d in sample.devices if d.index in keys]
        return matched if matched else list(sample.devices)

    async def _admit_memory(self, m: ManagedModel) -> None:
        """Memory-aware admission for a sized model (ADR 0022 + follow-up).

        No-op unless the model declares ``memory_mib`` AND accounting is possible
        (telemetry on, or a declared budget configured). First a hard capacity
        check: if the need exceeds a device's total capacity it can NEVER fit, so
        fail fast with ``exceeds_device_capacity`` and evict nothing. Then evict
        idle models on the same device(s) — each with a measured release wait —
        until the model fits, but only while evicting the evictable candidates
        could actually make room; otherwise fail fast (no needless eviction). On
        no-fit raises :class:`CapacityUnavailableError` naming the holder.
        """
        need = m.entry.memory_mib
        if need is None:
            return
        measured = self.telemetry_enabled()
        if not measured and not self.settings.gpu_memory_budget_map():
            return  # sized but no budget/telemetry: fall back to count-only
        keys = m.adapter.device_keys()
        cap = await self._exceeds_capacity(m, need, keys, measured)
        if cap is not None:
            self._record_admission(m.entry.alias, "blocked", cap)
            raise CapacityUnavailableError(
                f"cannot load {m.entry.alias!r}: {cap.get('summary')}",
                reason=cap.get("reason"),
                holder=cap.get("holder"),
                model=m.entry.alias,
                remediation=(
                    "this model is larger than the device can ever hold; pin it to "
                    "a bigger device, lower its memory_mib, or raise the budget"
                ),
            )
        while True:
            fits, detail = await self._fits(m, need, keys, measured)
            if fits:
                self._record_admission(m.entry.alias, "fits", detail)
                return
            victim = self._pick_memory_victim(m, keys)
            if victim is None or not self._eviction_can_help(m, need, keys, measured):
                self._record_admission(m.entry.alias, "blocked", detail)
                raise CapacityUnavailableError(
                    f"not enough memory to load {m.entry.alias!r}: {detail.get('summary')}",
                    reason=detail.get("reason"),
                    holder=detail.get("holder"),
                    model=m.entry.alias,
                    remediation=(
                        "wait for an in-flight model to go idle, raise the device "
                        "budget/headroom, or pin the model to another device"
                    ),
                )
            log.info(
                "admission: evicting idle %s to fit %s (%s MiB)",
                victim.entry.alias,
                m.entry.alias,
                need,
            )
            await self._evict_model(victim)
            self._record_admission(m.entry.alias, f"evicted {victim.entry.alias}", detail)

    def _measured_capacity(self, key: str, sample: TelemetrySample) -> int | None:
        """Best relevant device's total capacity minus headroom (measured), or None."""
        devs = [d for d in sample.devices if d.index == key] or list(sample.devices)
        headroom = self.settings.gpu_memory_headroom_mib
        caps = [d.total_mib - headroom for d in devs if d.total_mib is not None]
        return max(caps) if caps else None

    async def _exceeds_capacity(
        self, m: ManagedModel, need: int, keys: list[str], measured: bool
    ) -> dict[str, Any] | None:
        """If ``need`` is larger than a device can EVER hold, return a block detail.

        Uses measured total (minus headroom) when telemetry is available, else the
        declared budget. This is the "impossible request" guard: it must run before
        any eviction so a 5000 MiB model never evicts the idle 600 MiB tenant of a
        1000 MiB device only to 429 anyway.
        """
        sample = await self.telemetry_sample() if measured else None
        use_measured = measured and sample is not None and sample.ok
        for key in keys:
            cap: int | None = None
            if use_measured and sample is not None:
                cap = self._measured_capacity(key, sample)
            if cap is None:
                cap = self.settings.budget_for_device(key)
            if cap is not None and need > cap:
                return {
                    "reason": "exceeds_device_capacity",
                    "device": key,
                    "capacity_mib": cap,
                    "need_mib": need,
                    "holder": f"device {key} (capacity {cap} MiB)",
                    "summary": (f"model needs {need} MiB but device {key} capacity is {cap} MiB"),
                }
        return None

    def _evictable_candidates(self, m: ManagedModel, keys: list[str]) -> list[ManagedModel]:
        """Idle, non-draining READY models sharing a device with ``m`` (never ``m``)."""
        keyset = set(keys)
        return [
            mm
            for mm in self.models.values()
            if mm is not m
            and mm.machine.state == State.READY
            and mm.machine.inflight == 0
            and not mm.draining
            and keyset & set(mm.adapter.device_keys())
        ]

    def _eviction_can_help(
        self, m: ManagedModel, need: int, keys: list[str], measured: bool
    ) -> bool:
        """Whether evicting the evictable candidates could actually make ``m`` fit.

        Prevents needless eviction: with no evictable candidate, or when even
        reclaiming all of them still leaves the model unable to fit (e.g. a busy
        non-evictable holder occupies the device), we fail fast instead of evicting
        for nothing. Precise for declared accounting; for measured accounting it is
        conservative — it only blocks when it is confident eviction cannot help
        (every candidate's footprint is known and still insufficient).
        """
        candidates = self._evictable_candidates(m, keys)
        if not candidates:
            return False
        if not measured:
            for key in keys:
                budget = self.settings.budget_for_device(key)
                if budget is None:
                    continue
                residents = self._resident_on(key, exclude=m)
                used = sum((r.entry.memory_mib or 0) for r in residents)
                reclaimable = sum(
                    (c.entry.memory_mib or 0)
                    for c in candidates
                    if key in set(c.adapter.device_keys())
                )
                if need + (used - reclaimable) > budget:
                    return False
            return True
        # Measured: only block when every candidate footprint is known and the
        # measured free plus the reclaimable total still cannot fit the need.
        cache = self._telemetry_cache
        if cache is None or not cache.ok:
            return True
        headroom = self.settings.gpu_memory_headroom_mib
        for key in keys:
            devs = [d for d in cache.devices if d.index == key] or list(cache.devices)
            avails = [d.free_mib - headroom for d in devs if d.free_mib is not None]
            if not avails:
                continue
            avail = max(avails)
            on_key = [c for c in candidates if key in set(c.adapter.device_keys())]
            if any(c.entry.memory_mib is None for c in on_key):
                continue  # unknown footprint: do not rule eviction out
            reclaimable = sum((c.entry.memory_mib or 0) for c in on_key)
            if avail + reclaimable < need:
                return False
        return True

    def _resident_on(self, key: str, exclude: ManagedModel) -> list[ManagedModel]:
        out = []
        for mm in self.models.values():
            if mm is exclude:
                continue
            if mm.machine.state not in (
                State.LOADING,
                State.READY,
                State.BUSY,
                State.UNLOADING,
            ):
                continue
            if key in mm.adapter.device_keys():
                out.append(mm)
        return out

    def _outstanding_reservation(self, key: str, exclude: ManagedModel) -> int:
        """Reserved MiB on ``key`` not yet reflected in measured free memory.

        A sized model counts while it is LOADING (its driver memory is not up yet),
        and for ``gpu_memory_reservation_seconds`` after it became READY (the
        telemetry poll may not have caught the allocation). This is what makes two
        concurrent admissions serialize correctly against a single free reading
        without double-booking the device.
        """
        now = time.monotonic()
        grace = self.settings.gpu_memory_reservation_seconds
        total = 0
        for mm in self.models.values():
            if mm is exclude:
                continue
            mib = mm.entry.memory_mib or 0
            if mib <= 0 or key not in mm.adapter.device_keys():
                continue
            st = mm.machine.state
            if st == State.LOADING:
                total += mib
            elif st in (State.READY, State.BUSY):
                la = mm.machine.loaded_at
                if la is not None and (now - la) < grace:
                    total += mib
        return total

    async def _fits(
        self, m: ManagedModel, need: int, keys: list[str], measured: bool
    ) -> tuple[bool, dict[str, Any]]:
        """Return (fits, detail). Measured uses telemetry free; declared uses budgets."""
        if measured:
            sample = await self.telemetry_sample()
            if not sample.ok:
                # Telemetry on but unavailable: fall back to declared accounting if
                # a budget exists, else do not block (honest: we cannot measure).
                if not self.settings.gpu_memory_budget_map():
                    return True, {"reason": "telemetry_unavailable", "summary": "no measurement"}
                measured = False
            else:
                headroom = self.settings.gpu_memory_headroom_mib
                worst: dict[str, Any] | None = None
                for key in keys:
                    devs = [d for d in sample.devices if d.index == key] or list(sample.devices)
                    # Subtract outstanding reservations (sized models in LOADING, or
                    # just-made-READY within the grace window) so two concurrent
                    # admissions cannot both spend the same free reading.
                    reserved = self._outstanding_reservation(key, exclude=m)
                    avails = [
                        (d.free_mib - headroom - reserved, d.index)
                        for d in devs
                        if d.free_mib is not None
                    ]
                    if not avails:
                        continue
                    # Pinned key: the matched device must fit. Unpinned: best device.
                    if any(d.index == key for d in sample.devices):
                        avail, dev = min(avails)
                    else:
                        avail, dev = max(avails)
                    if need > avail:
                        worst = {
                            "reason": "measured_insufficient",
                            "device": dev,
                            "free_minus_headroom_mib": avail,
                            "reserved_mib": reserved,
                            "need_mib": need,
                            "holder": f"device {dev}",
                            "summary": f"device {dev} has {avail} MiB free (headroom "
                            f"{headroom}, reserved {reserved}), need {need}",
                        }
                        break
                if worst is not None:
                    return False, worst
                return True, {
                    "reason": "measured_fits",
                    "need_mib": need,
                    "summary": f"{need} MiB fits measured free memory",
                }
        # Declared accounting (telemetry off or unavailable-with-budget).
        for key in keys:
            budget = self.settings.budget_for_device(key)
            if budget is None:
                continue
            residents = self._resident_on(key, exclude=m)
            used = sum((r.entry.memory_mib or 0) for r in residents)
            if need + used > budget:
                return False, {
                    "reason": "declared_insufficient",
                    "device": key,
                    "budget_mib": budget,
                    "used_mib": used,
                    "need_mib": need,
                    "holder": ", ".join(r.entry.alias for r in residents) or "none",
                    "summary": f"device {key}: budget {budget} MiB, used {used}, need {need}",
                }
        return True, {
            "reason": "declared_fits",
            "need_mib": need,
            "summary": f"{need} MiB fits the declared budget",
        }

    def _pick_memory_victim(self, m: ManagedModel, keys: list[str]) -> ManagedModel | None:
        """LRU idle model sharing a device with ``m`` (never ``m``, never busy)."""
        keyset = set(keys)
        candidates = [
            mm
            for mm in self.models.values()
            if mm is not m
            and mm.machine.state == State.READY
            and mm.machine.inflight == 0
            and not mm.draining
            and keyset & set(mm.adapter.device_keys())
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda mm: mm.machine.last_used)

    # --- routing ---------------------------------------------------------
    def _least_busy(self, tier: list[ManagedModel]) -> ManagedModel:
        """Fewest in-flight; tie-break prefers an already-active member, then RR."""
        return min(
            tier,
            key=lambda m: (m.machine.inflight, 0 if m.machine.is_active() else 1, m._route_seq),
        )

    def _assign_member(self, tier: list[ManagedModel]) -> ManagedModel:
        """Pick a member for a NEW sticky session: least in-flight, round-robin
        tie-break. Unlike ``_least_busy`` this does *not* prefer an already-active
        member, so distinct sessions spread across the pool instead of piling onto
        one warm member.
        """
        return min(tier, key=lambda m: (m.machine.inflight, m._route_seq))

    # --- cache-aware routing (ADR 0010) ----------------------------------
    def _prefix_blocks(self, payload: dict) -> list[str] | None:
        """Chained prefix-block hashes for a payload, or None when cache-aware
        routing is off (so the hot path computes nothing)."""
        if self.cache_router is None:
            return None
        return self.cache_router.prefix_blocks(payload)

    def _member_load(self, m: ManagedModel) -> float:
        """Load signal for scoring: the scraped vLLM metric when enabled and
        available, else the in-flight request count (ADR 0010)."""
        if self.settings.cache_aware_use_metrics and m.last_load is not None:
            return float(m.last_load)
        return float(m.machine.inflight)

    async def _cache_aware_fractions(
        self, name: str, tier: list[ManagedModel], prefix_blocks: list[str] | None
    ) -> dict[str, float] | None:
        """Fetch prefix-match fractions per member (async), or None to fall back.

        Returns None when cache-aware scoring should defer to ``least_busy``
        (single member, no prefix blocks, or no member holds any prefix).
        """
        assert self.cache_router is not None
        if len(tier) == 1 or not prefix_blocks:
            return None
        aliases = [m.entry.alias for m in tier]
        fractions = await self.cache_router.match_fractions(name, prefix_blocks, aliases)
        return fractions or None

    def _score_cache_aware(
        self, tier: list[ManagedModel], fractions: dict[str, float] | None
    ) -> ManagedModel:
        """Score members by prefix affinity minus relative load; argmax wins (sync).

        ``score = matched_fraction - beta * (load - mean) / max(1, mean)`` (vLLM
        production-stack LoadAwareRouter). With no fractions we fall back to
        ``least_busy``. Tie-breaks: highest score, then least load, then RR.
        """
        if not fractions:
            return self._least_busy(tier)
        loads = {m.entry.alias: self._member_load(m) for m in tier}
        mean = sum(loads.values()) / len(loads)
        beta = self.settings.cache_aware_beta

        def score(m: ManagedModel) -> float:
            frac = fractions.get(m.entry.alias, 0.0)
            rel = (loads[m.entry.alias] - mean) / max(1.0, mean)
            return frac - beta * rel

        return max(tier, key=lambda m: (score(m), -loads[m.entry.alias], -m._route_seq))

    def _attempt_budget(self, n_members: int) -> int:
        """How many serve attempts a request gets.

        Default (``retry_max_attempts == 0``) is legacy: one attempt per pool
        member (a single failover pass, no sleep). A positive value caps total
        attempts and enables re-trying members after a backoff delay.
        """
        n = self.settings.retry_max_attempts
        return n if n > 0 else n_members

    def _backoff_delay_s(self, attempt: int) -> float:
        """Full-jitter capped exponential backoff (AWS pattern), in seconds.

        ``delay = uniform(0, min(max_ms, base_ms * 2**attempt))``. Returns 0 when
        ``retry_backoff_base_ms`` is 0 (the default) so the legacy path never
        sleeps. ``attempt`` is 0-based across retries.
        """
        base = self.settings.retry_backoff_base_ms
        if base <= 0:
            return 0.0
        cap = self.settings.retry_backoff_max_ms
        hi = min(cap, base * (2**attempt))
        return random.uniform(0, hi) / 1000.0

    async def _note_success(self, m: ManagedModel) -> None:
        """Reset a member's cooldown after a successful serve (no-op if off).

        Passes ``cooldown_decay_seconds`` so the exponential ejection multiplier
        (ADR 0017) decays only after sustained success.
        """
        if self.settings.cooldown_failures > 0:
            await self.state.record_success(
                m.entry.alias, decay_seconds=self.settings.cooldown_decay_seconds
            )

    async def _note_failure(
        self, m: ManagedModel, error_class: ErrorClass, retry_after: float | None = None
    ) -> None:
        """Record a member failure and bench it (ADR 0017).

        No-op unless cooldowns are enabled (``cooldown_failures > 0``). When the
        failure carries an upstream ``Retry-After`` (a throttled member), bench it
        immediately for ``min(Retry-After, cooldown_max_seconds or
        retry_after_max_seconds)`` regardless of whether this class normally counts
        toward the threshold — the member told us exactly how long it is out. Other
        counted failures use the consecutive-failure threshold, whose window grows
        on repeated ejections via ``cooldown_backoff_multiplier`` (capped by
        ``cooldown_max_seconds``).
        """
        # ADR 0023: record an upstream-backpressure window on the member so the
        # admission queue's backend-backpressure trigger can hold new requests
        # until it clears. Independent of the cooldown machinery (works even with
        # cooldowns off), and only for an actual 429/Retry-After backpressure.
        if error_class is ErrorClass.BACKPRESSURE and retry_after is not None and retry_after > 0:
            cap = self.settings.retry_after_max_seconds
            window = min(retry_after, cap) if cap > 0 else retry_after
            m.backpressure_until = time.monotonic() + window
        if self.settings.cooldown_failures <= 0:
            return
        if retry_after is not None and retry_after > 0:
            cap = self.settings.cooldown_max_seconds or self.settings.retry_after_max_seconds
            window = min(retry_after, cap) if cap > 0 else retry_after
            if window > 0:
                await self.state.bench(m.entry.alias, window)
            return
        if self.retry_policy.counts_toward_cooldown(error_class):
            await self.state.record_failure(
                m.entry.alias,
                threshold=self.settings.cooldown_failures,
                cooldown_seconds=self.settings.cooldown_seconds,
                multiplier=self.settings.cooldown_backoff_multiplier,
                max_cooldown_seconds=self.settings.cooldown_max_seconds,
            )

    async def _select_member(
        self,
        name: str,
        members: list[ManagedModel],
        exclude: set[str],
        route_key: str | None = None,
        prefix_blocks: list[str] | None = None,
    ) -> ManagedModel | None:
        """Pick a pool member and reserve an in-flight slot, atomically.

        Candidates are filtered to healthy, non-failed members first (falling back
        to non-failed, then any, so a pool can self-heal). Selection within that
        tier depends on ``route_strategy``:

        * ``sticky`` — reuse the member this ``route_key`` is pinned to if it is
          still a candidate; otherwise assign the least-busy member and (re)pin
          the session to it. This is what keeps a conversation on one backend and
          only moves it when necessary (unhealthy/excluded-by-failover).
        * ``round_robin`` — rotate members.
        * ``least_busy`` — fewest in-flight.

        Reserving ``inflight`` here (under the route lock) is what makes
        concurrent requests balance across members for the non-sticky strategies
        and what lets sticky assign *new* sessions to the genuinely least-busy
        member.
        """
        async with self._route_lock:
            pool = [m for m in members if m.entry.alias not in exclude and not m.draining]
            if not pool:
                return None
            tier = [m for m in pool if m.healthy and m.machine.state != State.FAILED]
            if not tier:
                tier = [m for m in pool if m.machine.state != State.FAILED]
            if not tier:
                tier = pool  # last resort: retry a failed member (self-heal)

            # Member cooldowns (ADR 0009 + 0017): when enabled, cooldown state is
            # the authoritative benching signal (it supersedes the transient
            # `healthy` flag). Benched members are skipped; a member whose window
            # has elapsed rejoins as a half-open trial. The ejection-percentage
            # cap keeps the soonest-to-recover members eligible so a correlated
            # failure can never strand the whole pool; if EVERY member is benched
            # even after the cap, fall through to the healthy-based `tier`.
            recovering: set[str] = set()
            if self.settings.cooldown_failures > 0:
                now = time.time()
                records = {
                    m.entry.alias: await self.state.get_cooldown(m.entry.alias, now=now)
                    for m in members
                }
                benched = {a for a, r in records.items() if r.in_cooldown}
                benched = self._apply_ejection_cap(benched, records, len(members))
                recovering = {
                    m.entry.alias
                    for m in pool
                    if m.entry.alias not in benched
                    and records.get(m.entry.alias, None) is not None
                    and records[m.entry.alias].recovering
                }
                live = [
                    m
                    for m in pool
                    if m.entry.alias not in benched and m.machine.state != State.FAILED
                ]
                if not live:
                    live = [m for m in pool if m.entry.alias not in benched]
                if live:
                    tier = live

            strategy = self.settings.route_strategy
            # Pre-fetch the async inputs selection needs so the pick (and the
            # single-trial skip loop) can run synchronously without double writes.
            sticky_assigned: str | None = None
            if strategy == "sticky" and route_key is not None and len(members) > 1:
                sticky_assigned = await self.state.sticky_get(name, route_key)
            fractions: dict[str, float] | None = None
            if strategy == "cache_aware":
                fractions = await self._cache_aware_fractions(name, tier, prefix_blocks)

            chosen: ManagedModel | None = None
            skip: set[str] = set()
            single_trial = (
                self.settings.cooldown_failures > 0
                and self.settings.cooldown_half_open_single_trial
            )
            while True:
                cand = [m for m in tier if m.entry.alias not in skip]
                if not cand:
                    break
                pick = self._pick_member(
                    strategy, name, cand, route_key, members, sticky_assigned, fractions
                )
                # Single-trial half-open (ADR 0017): exactly ONE request probes a
                # recovering member; concurrent requests skip it until the trial
                # resolves. Claim the trial lock only for the member we select.
                if single_trial and pick.entry.alias in recovering:
                    ttl = (
                        self.settings.cooldown_half_open_ttl_seconds
                        or self.settings.request_timeout_seconds
                    )
                    if not await self.state.try_half_open(pick.entry.alias, ttl):
                        skip.add(pick.entry.alias)
                        continue
                chosen = pick
                break
            if chosen is None:
                return None

            if strategy == "sticky" and route_key is not None and len(members) > 1:
                await self.state.sticky_set(name, route_key, chosen.entry.alias)
            chosen.machine.inflight += 1  # reserve the slot
            chosen._route_seq = next(self._route_counter)  # rotate tie-break
            return chosen

    def _apply_ejection_cap(
        self,
        benched: set[str],
        records: dict[str, Any],
        pool_size: int,
    ) -> set[str]:
        """Cap the benched set at ``max_ejection_percent`` of the pool (ADR 0017).

        Envoy outlier-detection semantics: never eject more than the configured
        fraction of a cluster. 0 = no cap (v0.5.0). When more members are benched
        than the cap allows, keep the soonest-to-recover ones eligible so the pool
        is never stranded.
        """
        cap_pct = self.settings.max_ejection_percent
        if cap_pct <= 0 or not benched:
            return benched
        max_benched = (cap_pct * pool_size) // 100
        if len(benched) <= max_benched:
            return benched
        # Reinstate the soonest-to-recover members (smallest `until`) first.
        by_recovery = sorted(benched, key=lambda a: records[a].until)
        keep_benched = set(by_recovery[len(benched) - max_benched :])
        return keep_benched

    def _pick_member(
        self,
        strategy: str,
        name: str,
        cand: list[ManagedModel],
        route_key: str | None,
        members: list[ManagedModel],
        sticky_assigned: str | None,
        fractions: dict[str, float] | None,
    ) -> ManagedModel:
        """Select one member from ``cand`` per strategy (pure/synchronous)."""
        if strategy == "sticky" and route_key is not None and len(members) > 1:
            chosen: ManagedModel | None = None
            if sticky_assigned is not None:
                chosen = next((m for m in cand if m.entry.alias == sticky_assigned), None)
            if chosen is None:  # new session, or pinned member unavailable
                chosen = self._assign_member(cand)
            return chosen
        if strategy == "cache_aware":
            return self._score_cache_aware(cand, fractions)
        if strategy == "round_robin":
            return min(cand, key=lambda m: (m._route_seq,))
        return self._least_busy(cand)

    # --- request path ----------------------------------------------------
    async def acquire(self, alias: str) -> ManagedModel:
        """Select + load a single model for a request (no routing/failover)."""
        m = self.get(alias)
        await self._maybe_evict(alias)
        await m.ensure_ready()
        return m

    async def _route_and_serve(
        self,
        name: str,
        members: list[ManagedModel],
        route_key: str | None,
        serve,
        prefix_blocks: list[str] | None = None,
    ) -> dict:
        """Select a pool member, serve, and retry within the pool on failure.

        Shared driver for chat + embeddings. Retries stay WITHIN the requested
        public model's pool — never a different model (the no-silent-cross-model
        invariant). Each failure is classified (ADR 0017) and the per-error policy
        decides whether to retry and whether it benches the member; a 4xx client
        error is re-raised immediately (never retried or counted). Between retries
        it waits a full-jitter backoff (zero by default) or an upstream
        ``Retry-After`` when present, clamped to the request-timeout deadline. The
        attempt budget defaults to a single failover pass with no sleep (v0.5.0).
        """
        exclude: set[str] = set()
        last: Exception | None = None
        last_class: ErrorClass | None = None
        cap = self._attempt_budget(len(members))
        class_attempts: dict[ErrorClass, int] = {}
        deadline = self._retry_deadline()
        pending_retry_after: float | None = None
        attempt = 0
        while attempt < cap:
            m = await self._select_member(name, members, exclude, route_key, prefix_blocks)
            if m is not None:
                # A fresh member is available: failover is IMMEDIATE — an upstream
                # Retry-After is never slept while another member can serve. Only
                # the inter-attempt full-jitter backoff (0 by default) applies, and
                # only once past the first attempt (preserves v0.5.0).
                if attempt > 0 and not await self._retry_wait(attempt, None, deadline):
                    break
            else:
                # No un-excluded eligible member. Sleep-retry an excluded member
                # only if the last failure is sleep-retryable and the budget allows
                # (never for a lone backpressure member — that returns its
                # 429/Retry-After to the client). Here the Retry-After IS honored,
                # clamped to retry_after_max_seconds and the request deadline.
                if not (
                    self.settings.retry_max_attempts > 0
                    and exclude
                    and last_class is not None
                    and self.retry_policy.is_retryable(last_class)
                ):
                    break
                if not await self._retry_wait(attempt, pending_retry_after, deadline):
                    break
                pending_retry_after = None
                exclude = set()
                m = await self._select_member(name, members, exclude, route_key, prefix_blocks)
                if m is None:
                    break
            attempt += 1
            try:
                result = await serve(m)
                await self._note_success(m)
                if self.cache_router is not None and prefix_blocks:
                    await self.cache_router.record(name, prefix_blocks, m.entry.alias)
                return result
            except BrokerError as exc:
                error_class = classify_error(exc)
                if not self.retry_policy.fails_over(error_class):
                    # Client error (4xx) / other: the request itself is bad.
                    # Propagate without benching or trying another member — matches
                    # the "4xx never retried or counted" invariant and surfaces an
                    # honest upstream status to the caller (never a 502).
                    raise
                m.healthy = False
                ra = retry_after_seconds(exc) if self.retry_policy.respect_retry_after else None
                await self._note_failure(m, error_class, retry_after=ra)
                exclude.add(m.entry.alias)
                last = exc
                last_class = error_class
                log.warning(
                    "pool %s: member %s failed (%s: %s); retry/failover",
                    name,
                    m.entry.alias,
                    error_class.value,
                    exc,
                )
                class_attempts[error_class] = class_attempts.get(error_class, 0) + 1
                class_cap = self.retry_policy.max_attempts_for(error_class)
                if class_cap is not None and class_attempts[error_class] >= class_cap:
                    break
                # Carry a Retry-After only for sleep-retryable classes: it is used
                # when we run out of fresh members. A failoverable-but-not-sleep
                # class (backpressure) never sleeps the same member.
                pending_retry_after = ra if self.retry_policy.is_retryable(error_class) else None
                continue
        if last is not None:
            raise last
        raise BackendUnavailableError(
            f"no healthy backend available for model {name!r}",
            model=name,
            remediation="check GET /healthz/backends",
        )

    def _retry_deadline(self) -> float | None:
        """Absolute monotonic deadline for a request's retries, or None (legacy).

        With retries disabled (``retry_max_attempts == 0``) there is no deadline
        (one failover pass, no sleep). With retries enabled, the deadline is the
        request timeout; when that is 0 (unbounded) we still impose a finite
        ``retry_after_max_seconds`` ceiling so a huge upstream Retry-After can
        never become an unbounded sleep.
        """
        if self.settings.retry_max_attempts <= 0:
            return None
        rt = self.settings.request_timeout_seconds
        if rt and rt > 0:
            return time.monotonic() + rt
        return time.monotonic() + self.settings.retry_after_max_seconds

    async def _retry_wait(
        self, attempt: int, pending_retry_after: float | None, deadline: float | None
    ) -> bool:
        """Sleep before a retry; return False if the deadline is already blown.

        Uses an upstream ``Retry-After`` (seconds) when present, else the
        full-jitter backoff. Every Retry-After wait is clamped to BOTH
        ``retry_after_max_seconds`` and the remaining request budget, so neither a
        hostile header nor an unbounded request timeout can produce an unbounded
        sleep.
        """
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if pending_retry_after is not None:
            delay = min(pending_retry_after, self.settings.retry_after_max_seconds)
        else:
            delay = self._backoff_delay_s(attempt - 1)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            delay = min(delay, remaining)
        if delay > 0:
            await asyncio.sleep(delay)
        return True

    def _check_allowed(self, name: str, allowed_models: set[str] | None) -> None:
        """Fail-closed per-key model scoping (ADR 0008).

        ``allowed_models`` is the authenticated key's allow-list. ``None`` means no
        restriction (unlimited key). When set, the resolved public model name must
        be a member — this is applied to the model the ``auto`` router selects as
        well as a directly-requested model, so scoping cannot be bypassed via auto.
        """
        if allowed_models is None:
            return
        if name not in allowed_models:
            raise AuthorizationError(
                f"model {name!r} is not permitted for this API key",
                model=name,
                code="model_not_allowed",
                remediation="request a model in the key's allowed_models list",
            )

    # --- admission queue (ADR 0023) --------------------------------------
    @staticmethod
    def _sem_free(sem: asyncio.Semaphore) -> int:
        """Free permits on a semaphore (0 when fully taken). Read-only."""
        return max(0, getattr(sem, "_value", 0))

    def _model_free_slots(self, alias: str) -> int:
        m = self.models.get(alias)
        return self._sem_free(m.sem) if m is not None else 0

    def _pump_now(self) -> None:
        """Wake the best-eligible queued waiters (no-op when the queue is off)."""
        if not self.admission.enabled:
            return
        self.admission.pump(
            lambda: self._sem_free(self.global_sem),
            self._model_free_slots,
        )

    def _release_slots(self, m: ManagedModel) -> None:
        """Release the global + per-model concurrency slots and re-pump."""
        m.sem.release()
        self.global_sem.release()
        self._pump_now()

    def _queueable_capacity_trigger(self, exc: BrokerError) -> str | None:
        """Map a capacity/concurrency error to a queue trigger, or None (fail fast).

        A capacity error is *queueable* only if waiting could ever let it through:
        a concurrency/memory holder may go idle. ``exceeds_device_capacity``
        (ADR 0020/0022) can never fit even with everything evicted, so it is never
        queued — it fails fast. Honors ``queue_trigger_capacity``.
        """
        if not self.settings.queue_trigger_capacity:
            return None
        if isinstance(exc, CapacityUnavailableError):
            if getattr(exc, "reason", None) == "exceeds_device_capacity":
                return None
            return TRIGGER_CAPACITY_MEMORY
        if isinstance(exc, ConcurrencyLimitError):
            # _evict_for_count: "max loaded models reached and no idle model to evict".
            return TRIGGER_MAX_LOADED
        return None

    def _host_load_per_cpu(self) -> float | None:
        """1-minute load average per CPU, cached briefly. None if unavailable."""
        now = time.monotonic()
        ttl = max(0.2, self.settings.queue_pump_interval_seconds)
        if self._host_load_value is not None and (now - self._host_load_at) < ttl:
            return self._host_load_value
        try:
            avg1 = os.getloadavg()[0]
        except (OSError, AttributeError):  # not available on this platform (e.g. Windows)
            self._host_load_value = None
            self._host_load_at = now
            return None
        ncpu = os.cpu_count() or 1
        self._host_load_value = avg1 / ncpu
        self._host_load_at = now
        return self._host_load_value

    def _host_overloaded(self) -> bool:
        cap = self.settings.queue_host_load_per_cpu_max
        if cap <= 0:
            return False
        load = self._host_load_per_cpu()
        return load is not None and load > cap

    async def _device_overload(self, m: ManagedModel) -> str | None:
        """Measured device-memory pressure on the model's device(s) (telemetry on)."""
        s = self.settings
        sample = await self.telemetry_sample()
        if not sample.ok:
            return None
        keys = m.adapter.device_keys()
        for d in self._relevant_devices(sample, keys):
            if (
                s.queue_device_free_mib_min > 0
                and d.free_mib is not None
                and d.free_mib < s.queue_device_free_mib_min
            ):
                return TRIGGER_DEVICE_MEMORY
            if (
                s.queue_device_used_fraction_max > 0
                and d.total_mib
                and d.used_mib is not None
                and (d.used_mib / d.total_mib) > s.queue_device_used_fraction_max
            ):
                return TRIGGER_DEVICE_MEMORY
        return None

    def _backend_overload(self, m: ManagedModel) -> str | None:
        """Backend backpressure: upstream 429/Retry-After window or vLLM queue depth."""
        s = self.settings
        if m.backpressure_until and time.monotonic() < m.backpressure_until:
            return TRIGGER_BACKEND_BACKPRESSURE
        if (
            s.queue_backend_waiting_max > 0
            and m.last_load is not None
            and m.last_load > s.queue_backend_waiting_max
        ):
            return TRIGGER_BACKEND_BACKPRESSURE
        return None

    async def _overload_trigger(self, m: ManagedModel) -> str | None:
        """First active *soft* overload trigger for ``m``, or None.

        Soft triggers hold a request in the queue even when a concurrency slot is
        free. Telemetry-based device pressure is inactive unless telemetry is on.
        """
        s = self.settings
        if s.queue_trigger_device_memory and self.telemetry_enabled():
            t = await self._device_overload(m)
            if t is not None:
                return t
        if s.queue_trigger_host and self._host_overloaded():
            return TRIGGER_HOST_LOAD
        if s.queue_trigger_backend:
            t = self._backend_overload(m)
            if t is not None:
                return t
        return None

    async def _acquire_admission(
        self, m: ManagedModel, *, blocking: bool, honor_reject_policy: bool = True
    ) -> None:
        """Make room (evict), ready the model, and take the global + model slots.

        ``blocking=True`` reproduces v0.6.0 exactly (queue off): it may block on
        the semaphores and (when ``honor_reject_policy``) honors
        ``load_concurrency_policy='reject'``. The streaming path passes
        ``honor_reject_policy=False`` because v0.6.0 streaming always waited on the
        semaphores and never rejected. ``blocking=False`` (queue on) never blocks:
        it raises :class:`_WouldBlock` with the overload trigger whenever it cannot
        admit right now, so the caller can wait in the fair queue. On success BOTH
        slots are held and the caller MUST release them with :meth:`_release_slots`.
        """
        if not blocking:
            trig = await self._overload_trigger(m)
            if trig is not None:
                raise _WouldBlock(trig)
        try:
            await self._maybe_evict(m.entry.alias)
        except (CapacityUnavailableError, ConcurrencyLimitError) as exc:
            if not blocking:
                qt = self._queueable_capacity_trigger(exc)
                if qt is not None:
                    raise _WouldBlock(qt) from exc
            raise
        await m.ensure_ready()
        if blocking:
            await self.global_sem.acquire()
            try:
                if (
                    honor_reject_policy
                    and m.sem.locked()
                    and self.settings.load_concurrency_policy == "reject"
                ):
                    raise ConcurrencyLimitError(
                        f"per-model concurrency limit for {m.entry.alias}",
                        model=m.entry.alias,
                        remediation="retry later or raise per-model concurrency",
                    )
                await m.sem.acquire()
            except BaseException:
                self.global_sem.release()
                raise
            return
        # Non-blocking (queue on): both acquisitions complete synchronously when a
        # permit is free, so there is no interleaving window between the checks.
        if self._sem_free(self.global_sem) <= 0:
            raise _WouldBlock(TRIGGER_GLOBAL_CONCURRENCY)
        await self.global_sem.acquire()
        if m.sem.locked():
            self.global_sem.release()
            raise _WouldBlock(TRIGGER_MODEL_CONCURRENCY)
        await m.sem.acquire()

    def _queue_retry_after_headers(self) -> dict[str, str]:
        ra = max(0, int(round(self.settings.queue_retry_after_seconds)))
        return {"Retry-After": str(ra)}

    async def _wait_for_turn(self, waiter: Waiter, deadline: float | None) -> None:
        """Block until this waiter is woken, or raise :class:`QueueTimeoutError`."""
        if deadline is None:
            await waiter.event.wait()
            return
        remaining = deadline - self.admission.clock()
        if remaining <= 0:
            self._raise_queue_timeout(waiter)
        try:
            await asyncio.wait_for(waiter.event.wait(), timeout=remaining)
        except TimeoutError:
            self._raise_queue_timeout(waiter)

    def _raise_queue_timeout(self, waiter: Waiter) -> None:
        self.admission.record_timeout(waiter)
        QUEUE_TIMEOUTS.labels(waiter.trigger).inc()
        waited_ms = (self.admission.clock() - waiter.enqueued_at) * 1000.0
        raise QueueTimeoutError(
            f"request for {waiter.alias!r} waited "
            f"{self.settings.queue_max_wait_seconds}s in the admission queue "
            f"(overload: {waiter.trigger}) and timed out",
            trigger=waiter.trigger,
            waited_ms=waited_ms,
            model=waiter.alias,
            headers=self._queue_retry_after_headers(),
            remediation="retry after Retry-After; raise BROKER_QUEUE_MAX_WAIT_SECONDS or add capacity",
        )

    async def _queue_admit(
        self, m: ManagedModel, queue_key: str, queue_priority: int
    ) -> tuple[float, str | None]:
        """Admit ``m`` through the bounded fair queue. Returns (waited_ms, trigger).

        Fast path: if admission succeeds immediately, return (0, None). Otherwise
        the request joins the queue (or 429s if it is full) and waits its fair
        turn, retrying admission on each wake until it is served or times out.
        """
        # Fast path: only when NOBODY is already waiting, so a fresh arrival can
        # never jump ahead of the queue (FIFO/fairness). If anyone is queued, this
        # request takes its fair place at the back.
        if self.admission.depth() == 0:
            try:
                await self._acquire_admission(m, blocking=False)
                return 0.0, None
            except _WouldBlock as wb:
                first_trigger = wb.trigger
        else:
            first_trigger = await self._overload_trigger(m) or TRIGGER_MODEL_CONCURRENCY
        try:
            waiter = self.admission.enqueue(m.entry.alias, queue_key, queue_priority, first_trigger)
        except QueueFullError as qf:
            QUEUE_REJECTED.labels(qf.trigger or first_trigger).inc()
            qf.headers.setdefault("Retry-After", self._queue_retry_after_headers()["Retry-After"])
            raise
        QUEUE_ENQUEUED.labels(first_trigger).inc()
        QUEUE_DEPTH.set(self.admission.depth())
        max_wait = self.settings.queue_max_wait_seconds
        deadline = (waiter.enqueued_at + max_wait) if max_wait > 0 else None
        try:
            # A fresh waiter may be admissible already (a slot freed between the
            # fast-path miss and enqueue); pump once so it is considered.
            self._pump_now()
            while True:
                await self._wait_for_turn(waiter, deadline)
                try:
                    await self._acquire_admission(m, blocking=False)
                except _WouldBlock as wb:
                    # Still cannot admit (e.g. a soft overload has not cleared).
                    # Go back to waiting WITHOUT re-waking ourselves — the next
                    # wake comes from a slot release or the periodic pump tick, so
                    # this never becomes a busy spin.
                    waiter.trigger = wb.trigger
                    waiter.event.clear()
                    continue
                waited_ms = (self.admission.clock() - waiter.enqueued_at) * 1000.0
                waiter.waited_ms = waited_ms
                self.admission.record_admit(waiter)
                QUEUE_ADMITTED.inc()
                QUEUE_WAIT_SECONDS.observe(waited_ms / 1000.0)
                return waited_ms, first_trigger
        finally:
            self.admission.dequeue(waiter)
            QUEUE_DEPTH.set(self.admission.depth())
            self._pump_now()

    @asynccontextmanager
    async def _admit(
        self, m: ManagedModel, queue_key: str | None, queue_priority: int
    ) -> AsyncIterator[tuple[float, str | None]]:
        """Hold the admission slots for the serve body (queue-aware).

        Yields ``(queue_wait_ms, trigger)``: 0/None when admitted immediately.
        Releasing the slots on exit re-pumps the queue so the next waiter runs.
        """
        waited = await self._admit_enter(m, queue_key, queue_priority)
        try:
            yield waited
        finally:
            self._admit_exit(m)

    async def _admit_enter(
        self,
        m: ManagedModel,
        queue_key: str | None,
        queue_priority: int,
        *,
        honor_reject_policy: bool = True,
    ) -> tuple[float, str | None]:
        """Acquire admission slots (queue-aware). Returns (queue_wait_ms, trigger).

        When the queue is off this is exactly v0.6.0 (may block on the semaphores).
        The caller MUST pair every successful enter with :meth:`_admit_exit`.
        """
        if not self.admission.enabled:
            await self._acquire_admission(m, blocking=True, honor_reject_policy=honor_reject_policy)
            return 0.0, None
        return await self._queue_admit(m, queue_key or "anon", queue_priority)

    def _admit_exit(self, m: ManagedModel) -> None:
        """Release the admission slots and re-pump the queue."""
        self._release_slots(m)

    async def start_queue_pump(self) -> None:
        """Background ticker that re-drains the queue (soft-overload recovery)."""
        if not self.admission.enabled:
            return

        async def loop() -> None:
            interval = self.settings.queue_pump_interval_seconds
            while not self._stopping:
                await asyncio.sleep(interval)
                if self._stopping:
                    break
                try:
                    self._pump_now()
                except Exception as exc:  # noqa: BLE001 - never let the pump die
                    log.warning("queue pump error: %s", exc)

        self._queue_pump_task = asyncio.ensure_future(loop())

    async def chat(
        self,
        name: str,
        payload: dict,
        request_id: str,
        route_key: str | None = None,
        required_placement: frozenset[str] | None = None,
        latency_budget_ms: float | None = None,
        allowed_models: set[str] | None = None,
        model_guard: Callable[[str], Awaitable[None]] | None = None,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        req = frozenset(required_placement or ())
        resolved = await self._resolve_route(name, payload, req, latency_budget_ms)
        # ADR 0008 fail-closed per-key scoping applies to the (possibly auto-
        # chosen) resolved model, so scoping cannot be bypassed via `auto`.
        self._check_allowed(resolved.hops[0], allowed_models)
        # ADR 0016 per-model rpm/tpm limits on the resolved model (auto pick).
        if model_guard is not None:
            await model_guard(resolved.hops[0])
        if resolved.mode == "cascade" and len(resolved.hops) > 1:
            return await self._serve_cascade(
                resolved,
                payload,
                request_id,
                route_key,
                allowed_models,
                model_guard,
                queue_key=queue_key,
                queue_priority=queue_priority,
            )
        result = await self._serve_one(
            resolved.hops[0],
            payload,
            request_id,
            route_key,
            req,
            queue_key=queue_key,
            queue_priority=queue_priority,
        )
        if isinstance(result, dict):
            result["_broker_route"] = result.get("model", resolved.hops[0])
        return result

    async def _serve_one(
        self,
        name: str,
        payload: dict,
        request_id: str,
        route_key: str | None,
        required_placement: frozenset[str],
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        """Serve one concrete model/pool with member selection, retries + failover.

        Delegates to the shared ``_route_and_serve`` driver so cascade hops and
        single-model requests get identical ADR 0009 retries/cooldowns and the
        ADR 0010 pool-selection strategy. Placement is enforced fail-closed when
        resolving the pool members.
        """
        members = self._members_for(name, required_placement)
        return await self._route_and_serve(
            name,
            members,
            route_key,
            lambda m: self._serve_chat(
                m, name, payload, request_id, queue_key=queue_key, queue_priority=queue_priority
            ),
            prefix_blocks=self._prefix_blocks(payload),
        )

    async def _serve_cascade(
        self,
        resolved: ResolvedRoute,
        payload: dict,
        request_id: str,
        route_key: str | None,
        allowed_models: set[str] | None = None,
        model_guard: Callable[[str], Awaitable[None]] | None = None,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        """Serve the cheapest-capable hop; escalate once on a quality signal.

        Both hops are recorded (decision log + usage); the result carries the
        ``_broker_route`` path (``primary`` or ``primary->escalation``). The
        escalation hop is scope-checked (ADR 0008) before it is served, so a key
        that may not use the stronger model is denied fail-closed rather than
        silently escalated to it.
        """
        primary, escalation = resolved.hops[0], resolved.hops[1]
        req = resolved.required_placement
        result = await self._serve_one(
            primary,
            payload,
            request_id,
            route_key,
            req,
            queue_key=queue_key,
            queue_priority=queue_priority,
        )
        escalate, reason = should_escalate(result, resolved.cascade_cfg)
        primary_served = result.get("model", primary) if isinstance(result, dict) else primary
        if not escalate:
            if self.router is not None:
                self.router.record_cascade(primary_served, escalation, False, "")
            if isinstance(result, dict):
                result["_broker_route"] = primary_served
            return result
        # Escalate: fail-closed scoping on the stronger hop, record the primary
        # hop, then serve the stronger model.
        self._check_allowed(escalation, allowed_models)
        if model_guard is not None:
            await model_guard(escalation)
        self._record_hop("chat_cascade", resolved.public_name, result, request_id, "escalated")
        result2 = await self._serve_one(
            escalation,
            payload,
            request_id,
            route_key,
            req,
            queue_key=queue_key,
            queue_priority=queue_priority,
        )
        served = result2.get("model", escalation) if isinstance(result2, dict) else escalation
        if self.router is not None:
            self.router.record_cascade(primary_served, served, True, reason)
        if isinstance(result2, dict):
            result2["_broker_route"] = f"{primary_served}->{served}"
        return result2

    async def stream_cascade_buffer(
        self,
        plan: StreamPlan,
        payload: dict,
        request_id: str,
        route_key: str | None = None,
        allowed_models: set[str] | None = None,
        model_guard: Callable[[str], Awaitable[None]] | None = None,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> AsyncIterator[bytes]:
        """Serve a buffered streaming cascade (ADR 0019): buffer, score, then
        replay or stream the stronger model.

        The cheap hop is served **non-streamed** so its complete answer can be
        scored by the same :func:`should_escalate` signals as the non-stream
        cascade. If it passes, the buffered answer is **replayed** as a single
        stream. If a signal fires, the stronger hop is **streamed live**. Only
        one model's output is ever emitted into the stream — the invariant that a
        cascade never mixes two models' tokens holds for streaming too. Per-key
        scoping (ADR 0008) is enforced fail-closed on both hops.
        """
        hops = plan.hops or []
        primary, escalation = hops[0], hops[-1]
        req = plan.required_placement
        self._check_allowed(primary, allowed_models)
        if model_guard is not None:
            await model_guard(primary)
        # Serve the cheap hop fully (non-streamed) so we can inspect its answer.
        result = await self._serve_one(
            primary,
            payload,
            request_id,
            route_key,
            req,
            queue_key=queue_key,
            queue_priority=queue_priority,
        )
        escalate, reason = should_escalate(result, plan.cascade_cfg)
        primary_served = result.get("model", primary) if isinstance(result, dict) else primary
        if not escalate:
            if self.router is not None:
                self.router.record_cascade(primary_served, escalation, False, "")
            plan.served = primary_served
            for chunk in _result_to_sse_chunks(result, primary_served):
                yield chunk
            return
        # Escalate: fail-closed scope check on the stronger hop, record the
        # primary hop, then stream the stronger model live (its output only).
        self._check_allowed(escalation, allowed_models)
        if model_guard is not None:
            await model_guard(escalation)
        self._record_hop("chat_cascade", plan.public_name, result, request_id, "escalated")
        if self.router is not None:
            self.router.record_cascade(primary_served, escalation, True, reason)
        plan.served = escalation
        async for chunk in self.stream_chat(
            escalation,
            payload,
            request_id,
            route_key=route_key,
            required_placement=req,
            allowed_models=allowed_models,
            queue_key=queue_key,
            queue_priority=queue_priority,
        ):
            yield chunk

    async def _serve_chat(
        self,
        m: ManagedModel,
        public_name: str,
        payload: dict,
        request_id: str,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        # inflight was reserved by _select_member; release it in finally.
        try:
            # Admission (ADR 0023): evict/ready + concurrency slots, queue-aware.
            # With the queue off this is exactly v0.6.0 (may block on the sems).
            with self.tracer.span(
                "load_model",
                {"gen_ai.request.model": public_name, "broker.member.alias": m.entry.alias},
            ):
                queue_wait_ms, queue_trigger = await self._admit_enter(m, queue_key, queue_priority)
            try:
                if m.machine.state == State.READY:
                    m.machine.to(State.BUSY)
                with self.tracer.span(
                    "inference",
                    {
                        "gen_ai.operation.name": "chat",
                        "gen_ai.request.model": public_name,
                        "gen_ai.provider.name": m.adapter.name,
                        "broker.member.alias": m.entry.alias,
                    },
                ) as sp:
                    # OTel queue-wait span event (ADR 0023): no-op when tracing off.
                    if queue_wait_ms > 0:
                        sp.set_attribute("broker.queue.wait_ms", round(queue_wait_ms, 1))
                        if queue_trigger:
                            sp.set_attribute("broker.queue.trigger", queue_trigger)
                    result = await asyncio.wait_for(
                        m.adapter.chat(payload, request_id),
                        timeout=m.entry.request_timeout_seconds,
                    )
                    if isinstance(result, dict):
                        usage = result.get("usage") or {}
                        sp.set_attribute("gen_ai.response.model", public_name)
                        if usage.get("prompt_tokens") is not None:
                            sp.set_attribute("gen_ai.usage.input_tokens", usage["prompt_tokens"])
                        if usage.get("completion_tokens") is not None:
                            sp.set_attribute(
                                "gen_ai.usage.output_tokens", usage["completion_tokens"]
                            )
                    # Opt-in span content capture (ADR 0014): OFF by default.
                    # OTel GenAI `spanOnly` mode — truncated JSON strings; may
                    # contain PII, so never captured unless explicitly enabled.
                    if self.tracer.capture_content:
                        try:
                            sp.set_attribute(
                                "gen_ai.input.messages",
                                self.tracer.truncate(json.dumps(payload.get("messages", []))),
                            )
                            if isinstance(result, dict):
                                sp.set_attribute(
                                    "gen_ai.output.messages",
                                    self.tracer.truncate(
                                        json.dumps(
                                            [
                                                c.get("message", {})
                                                for c in result.get("choices", [])
                                            ]
                                        )
                                    ),
                                )
                        except Exception:  # noqa: BLE001 - tracing never breaks a request
                            pass
                m.healthy = True
                # Present the public (requested) model name back to the caller, and
                # expose the queue wait to the API layer (ADR 0023 response headers).
                if isinstance(result, dict):
                    result["model"] = public_name
                    if queue_wait_ms > 0:
                        result["_broker_queue_wait_ms"] = queue_wait_ms
                        if queue_trigger:
                            result["_broker_queue_trigger"] = queue_trigger
                return result
            finally:
                self._admit_exit(m)
        finally:
            m.machine.inflight -= 1
            m.machine.touch()
            if m.machine.inflight == 0 and m.machine.state == State.BUSY:
                m.machine.to(State.READY)

    async def stream_chat(
        self,
        name: str,
        payload: dict,
        request_id: str,
        route_key: str | None = None,
        required_placement: frozenset[str] | None = None,
        allowed_models: set[str] | None = None,
        model_guard: Callable[[str], Awaitable[None]] | None = None,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> AsyncIterator[bytes]:
        req = frozenset(required_placement or ())
        # ``name`` is already a concrete model (the API resolves streaming routes via
        # resolve_stream); placement is re-validated here when selecting members and
        # per-key scoping (ADR 0008) is re-checked fail-closed on the served model.
        self._check_allowed(name, allowed_models)
        if model_guard is not None:
            await model_guard(name)
        members = self._members_for(name, req)
        prefix_blocks = self._prefix_blocks(payload)
        exclude: set[str] = set()
        last: Exception | None = None
        last_class: ErrorClass | None = None
        chosen: ManagedModel | None = None
        # Select + ready a member, with retry/failover *before* the stream
        # starts. Once bytes are flowing we NEVER retry (a half-emitted stream
        # can't be transparently restarted). Retries stay within this pool.
        cap = self._attempt_budget(len(members))
        class_attempts: dict[ErrorClass, int] = {}
        deadline = self._retry_deadline()
        pending_retry_after: float | None = None
        attempt = 0
        queue_wait_ms: float = 0.0
        queue_trigger: str | None = None
        while attempt < cap:
            m = await self._select_member(name, members, exclude, route_key, prefix_blocks)
            if m is not None:
                # Fresh member: immediate failover (no Retry-After sleep); only the
                # inter-attempt backoff applies, and only past the first attempt.
                if attempt > 0 and not await self._retry_wait(attempt, None, deadline):
                    break
            else:
                if not (
                    self.settings.retry_max_attempts > 0
                    and exclude
                    and last_class is not None
                    and self.retry_policy.is_retryable(last_class)
                ):
                    break
                if not await self._retry_wait(attempt, pending_retry_after, deadline):
                    break
                pending_retry_after = None
                exclude = set()
                m = await self._select_member(name, members, exclude, route_key, prefix_blocks)
                if m is None:
                    break
            attempt += 1
            try:
                # Admission (ADR 0023) incl. the pre-first-byte queue wait. Holds
                # the concurrency slots on success; a queue-full/timeout verdict is
                # terminal (classified OTHER) and propagates instead of failing
                # over. honor_reject_policy=False keeps exact v0.6.0 stream timing.
                queue_wait_ms, queue_trigger = await self._admit_enter(
                    m, queue_key, queue_priority, honor_reject_policy=False
                )
                chosen = m
                break
            except BrokerError as exc:
                error_class = classify_error(exc)
                m.machine.inflight -= 1  # release reservation; we won't serve here
                m.machine.touch()
                if not self.retry_policy.fails_over(error_class):
                    raise
                m.healthy = False
                ra = retry_after_seconds(exc) if self.retry_policy.respect_retry_after else None
                await self._note_failure(m, error_class, retry_after=ra)
                exclude.add(m.entry.alias)
                last = exc
                last_class = error_class
                class_attempts[error_class] = class_attempts.get(error_class, 0) + 1
                class_cap = self.retry_policy.max_attempts_for(error_class)
                if class_cap is not None and class_attempts[error_class] >= class_cap:
                    break
                pending_retry_after = ra if self.retry_policy.is_retryable(error_class) else None
                continue
        if chosen is None:
            if last is not None:
                raise last
            raise BackendUnavailableError(
                f"no healthy backend available for model {name!r}",
                model=name,
                remediation="check GET /healthz/backends",
            )

        m = chosen
        # The concurrency slots were acquired by _admit_enter above (queue-aware).
        if m.machine.state == State.READY:
            m.machine.to(State.BUSY)
        m.healthy = True
        if queue_wait_ms > 0:
            log.debug(
                "stream %s queued %.1fms (trigger=%s) before first byte",
                m.entry.alias,
                queue_wait_ms,
                queue_trigger,
            )
        try:
            async for chunk in m.adapter.stream_chat(payload, request_id):
                yield chunk
            await self._note_success(m)  # stream completed cleanly → reset cooldown
            if self.cache_router is not None and prefix_blocks:
                await self.cache_router.record(name, prefix_blocks, m.entry.alias)
        finally:
            m.machine.inflight -= 1
            m.machine.touch()
            if m.machine.inflight == 0 and m.machine.state == State.BUSY:
                m.machine.to(State.READY)
            self._admit_exit(m)

    async def embeddings(
        self,
        name: str,
        payload: dict,
        request_id: str,
        route_key: str | None = None,
        required_placement: frozenset[str] | None = None,
        allowed_models: set[str] | None = None,
        model_guard: Callable[[str], Awaitable[None]] | None = None,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        """Route an embeddings request through pool selection + failover.

        Unlike chat, ``auto`` does not apply (there is nothing to classify); the
        request is routed to the named model/pool. Shares the same member
        selection, concurrency gating, lazy-load, and failover as chat. Placement
        pinning and per-key scoping are enforced fail-closed on the named model/pool.
        """
        req = frozenset(required_placement or ())
        self._check_allowed(name, allowed_models)
        if model_guard is not None:
            await model_guard(name)
        members = self._members_for(name, req)
        return await self._route_and_serve(
            name,
            members,
            route_key,
            lambda m: self._serve_embeddings(
                m, name, payload, request_id, queue_key=queue_key, queue_priority=queue_priority
            ),
            prefix_blocks=self._prefix_blocks(payload),
        )

    async def _serve_embeddings(
        self,
        m: ManagedModel,
        public_name: str,
        payload: dict,
        request_id: str,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        try:
            async with self._admit(m, queue_key, queue_priority):
                if m.machine.state == State.READY:
                    m.machine.to(State.BUSY)
                result = await asyncio.wait_for(
                    m.adapter.embeddings(payload, request_id),
                    timeout=m.entry.request_timeout_seconds,
                )
                m.healthy = True
                if isinstance(result, dict):
                    result["model"] = public_name
                return result
        finally:
            m.machine.inflight -= 1
            m.machine.touch()
            if m.machine.inflight == 0 and m.machine.state == State.BUSY:
                m.machine.to(State.READY)

    async def cancel(self, name: str, request_id: str) -> None:
        # The serving member holds the cancel event; signalling all members of
        # the pool is harmless (only the owner reacts).
        for m in self._members_for(name):
            await m.adapter.cancel(request_id)

    async def rerank(
        self,
        name: str,
        payload: dict,
        request_id: str,
        route_key: str | None = None,
        required_placement: frozenset[str] | None = None,
        allowed_models: set[str] | None = None,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        """Route a rerank request through pool selection + failover (ADR 0013).

        Like embeddings, ``auto`` does not apply (nothing to classify); the request
        is routed to the named model/pool through the same member selection,
        concurrency gating, lazy-load, and failover as chat. Placement pinning and
        per-key scoping are enforced fail-closed on the named model/pool. A
        fail-closed capability gate rejects a model/pool whose members are not all
        ``supports_rerank`` with 400 before any load.
        """
        req = frozenset(required_placement or ())
        self._check_allowed(name, allowed_models)
        members = self._members_for(name, req)
        if not all(m.entry.supports_rerank for m in members):
            raise ValidationError(
                f"model {name!r} does not support rerank",
                model=name,
                code="model_not_capable",
                remediation="request a rerank-capable model (supports_rerank: true)",
            )
        return await self._route_and_serve(
            name,
            members,
            route_key,
            lambda m: self._serve_rerank(
                m, name, payload, request_id, queue_key=queue_key, queue_priority=queue_priority
            ),
        )

    async def _serve_rerank(
        self,
        m: ManagedModel,
        public_name: str,
        payload: dict,
        request_id: str,
        queue_key: str | None = None,
        queue_priority: int = 0,
    ) -> dict:
        try:
            async with self._admit(m, queue_key, queue_priority):
                if m.machine.state == State.READY:
                    m.machine.to(State.BUSY)
                result = await asyncio.wait_for(
                    m.adapter.rerank(payload, request_id),
                    timeout=m.entry.request_timeout_seconds,
                )
                m.healthy = True
                if isinstance(result, dict):
                    result["model"] = public_name
                return result
        finally:
            m.machine.inflight -= 1
            m.machine.touch()
            if m.machine.inflight == 0 and m.machine.state == State.BUSY:
                m.machine.to(State.READY)

    # --- admin runtime control (ADR 0012) --------------------------------
    async def _drain(self, m: ManagedModel, timeout: float | None = None) -> bool:
        """Wait for a member's in-flight requests to reach zero (bounded).

        Returns True if drained, False on timeout. The caller sets ``m.draining``
        first so selection stops routing new work to the member.
        """
        timeout = self.settings.admin_drain_timeout_seconds if timeout is None else timeout
        deadline = time.monotonic() + max(0.0, timeout)
        while m.machine.inflight > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        return m.machine.inflight == 0

    def admin_list_models(self) -> list[dict[str, Any]]:
        """Per-model runtime snapshot for the admin API (never loads anything)."""
        out: list[dict[str, Any]] = []
        for alias, m in self.models.items():
            out.append(
                {
                    "alias": alias,
                    "model": m.public_name,
                    "backend": m.adapter.name,
                    "state": m.machine.state.value,
                    "inflight": m.machine.inflight,
                    "loaded": m.machine.is_active(),
                    "loaded_at": m.machine.loaded_at,
                    "idle_seconds": round(m.machine.idle_seconds(), 2),
                    "healthy": m.healthy,
                    "draining": m.draining,
                }
            )
        return out

    async def admin_unload(self, alias: str, *, drain: bool = False) -> dict[str, Any]:
        """Unload a model on demand (admin). Refuses or drains if it is serving.

        * ``drain=False`` (default): refuse with :class:`ModelBusyError` if there
          are in-flight requests; otherwise unload immediately.
        * ``drain=True``: stop selecting the member, wait for in-flight to reach
          zero (bounded by ``admin_drain_timeout_seconds``), then unload. If the
          drain times out, the model is left serving and ModelBusyError is raised.
        """
        m = self.get(alias)
        if not drain:
            if m.machine.inflight > 0:
                raise ModelBusyError(
                    f"model {alias!r} has {m.machine.inflight} in-flight request(s)",
                    model=alias,
                    remediation="retry with drain=true to wait for them to finish",
                )
            await m.unload()
            return {"alias": alias, "state": m.machine.state.value, "drained": False}
        m.draining = True
        try:
            drained = await self._drain(m)
            if not drained:
                raise ModelBusyError(
                    f"drain of {alias!r} timed out with {m.machine.inflight} in-flight",
                    model=alias,
                    remediation="raise BROKER_ADMIN_DRAIN_TIMEOUT_SECONDS or retry",
                )
            await m.unload()
            return {"alias": alias, "state": m.machine.state.value, "drained": True}
        finally:
            m.draining = False

    async def admin_load(self, alias: str) -> dict[str, Any]:
        """Explicitly preload a model (admin). The ONLY non-request load path.

        Preserves the lazy-load invariant for the normal data path: a model still
        never loads at startup or on a health check — only on an inference request
        or this explicit admin action.
        """
        m = self.get(alias)
        await self._maybe_evict(alias)
        await m.ensure_ready()
        return {"alias": alias, "state": m.machine.state.value, "loaded": m.machine.is_active()}

    async def admin_reload_registry(self) -> dict[str, Any]:
        """Re-read + validate the registry and apply the diff without a restart.

        Transactional: the new registry is fully loaded and every adapter config
        validated BEFORE anything mutates, so an invalid registry makes **no
        change** and surfaces a structured :class:`ConfigError`. Unchanged models
        keep their live instance (and loaded state); changed and removed models
        are drained then stopped; added models are registered (not loaded).
        """
        # 1. Load + validate the candidate registry (raises ConfigError, no change).
        new_reg = load_registry(self.settings.registry_path, set(self.settings.enabled_backends))
        new_by = {e.alias: e for e in new_reg.models}
        old_by = {a: m.entry for a, m in self.models.items()}

        added = [a for a in new_by if a not in old_by]
        removed = [a for a in old_by if a not in new_by]
        changed = [
            a for a in new_by if a in old_by and new_by[a].model_dump() != old_by[a].model_dump()
        ]
        unchanged = [a for a in new_by if a in old_by and a not in changed]

        # 2. Build + validate new ManagedModels for added/changed BEFORE mutating.
        fresh: dict[str, ManagedModel] = {}
        for a in added + changed:
            mm = ManagedModel(
                new_by[a],
                self.settings,
                self.ledger,
                self.broker_instance_id,
                self.broker_pid,
                self.broker_start_time,
            )
            mm.adapter.validate_config()  # raises → propagates, nothing mutated yet
            fresh[a] = mm

        # 3. Drain + stop the OLD instances of removed/changed models.
        for a in removed + changed:
            old = self.models.get(a)
            if old is None:
                continue
            old.draining = True
            await self._drain(old)
            await old.stop()

        # 4. Atomically swap in the new model set (unchanged keep their instance).
        new_models: dict[str, ManagedModel] = {}
        for a in new_by:
            new_models[a] = self.models[a] if a in unchanged else fresh[a]
        self.models = new_models
        self.registry = new_reg
        self.pools = {}
        for e in new_reg.models:
            self.pools.setdefault(e.public_name, []).append(self.models[e.alias])
        # Refresh the auto-router against the new registry policy.
        if self.settings.router_enabled and self.settings.router_backend != "off":
            self.router = AutoRouter(self, self.settings, new_reg.router)
        else:
            self.router = None

        log.info(
            "registry reloaded: +%d -%d ~%d (=%d)",
            len(added),
            len(removed),
            len(changed),
            len(unchanged),
        )
        return {
            "added": sorted(added),
            "removed": sorted(removed),
            "changed": sorted(changed),
            "unchanged": sorted(unchanged),
            "models": len(self.models),
        }

    # --- idle sweep ------------------------------------------------------
    async def _sweep_once(self) -> None:
        # Clear any release_stuck model whose device memory has come back (ADR 0020
        # follow-up): a stuck holder must not strand its device until a restart.
        await self._recheck_all_release_stuck()
        for m in list(self.models.values()):
            if m.machine.state != State.READY or m.machine.inflight > 0:
                continue
            timeout = m.entry.idle_timeout_seconds or self.settings.model_idle_timeout_seconds
            if timeout > 0 and m.machine.idle_seconds() >= timeout:
                log.info("idle unload: %s (idle %.1fs)", m.entry.alias, m.machine.idle_seconds())
                await m.unload()
        # ADR 0024: reap any re-parented descendant that became a zombie child of
        # the broker between teardowns (Linux subreaper), so a long-running broker
        # that churns such runtimes does not accumulate zombies. Never touches a
        # live runtime the broker spawned (those PIDs are skipped and reaped by
        # asyncio's child watcher). A no-op off Linux.
        await self._reap_reparented_zombies()

    async def _reap_reparented_zombies(self) -> None:
        """Periodically reap the broker's re-parented zombie children (ADR 0024)."""
        skip = {pid for m in self.models.values() if (pid := m.adapter.process_pid()) is not None}
        try:
            reaped = await asyncio.to_thread(reap_broker_zombies, os.getpid(), skip=skip)
        except Exception as exc:  # noqa: BLE001 - reaping must never break the sweep
            log.debug("descendant reap error: %s", exc)
            return
        if reaped:
            log.info("reaped %d re-parented runtime descendant(s): %s", len(reaped), reaped)

    async def start_sweeper(self) -> None:
        async def loop() -> None:
            while not self._stopping:
                try:
                    await self._sweep_once()
                except Exception as exc:  # noqa: BLE001
                    log.warning("idle sweep error: %s", exc)
                await asyncio.sleep(self.settings.idle_sweep_interval_seconds)

        self._sweeper = asyncio.ensure_future(loop())

    # --- health probing (non-loading) -----------------------------------
    async def _probe_member(self, m: ManagedModel) -> dict[str, Any]:
        """Liveness-probe one member WITHOUT loading a model.

        * external daemons (Ollama): probe regardless of broker state (daemon
          liveness via GET /api/tags);
        * broker-managed: probe only while the process is running; if the model
          is not loaded, report ``not_loaded`` (not a failure).
        """
        external = m.adapter.external_daemon
        active = m.machine.is_active()
        if not external and not active:
            m.last_health = {
                "status": "not_loaded",
                "detail": "model not loaded",
                "at": time.time(),
            }
            return m.last_health
        try:
            h = await asyncio.wait_for(
                m.adapter.health(), timeout=self.settings.health_probe_timeout_seconds
            )
            healthy, detail = h.healthy, h.detail
        except Exception as exc:  # noqa: BLE001
            healthy, detail = False, str(exc)
        m.last_health = {
            "status": "ok" if healthy else "unhealthy",
            "detail": detail,
            "at": time.time(),
        }
        # A READY member whose backend has gone away is marked FAILED so the next
        # request reloads/reroutes. Never fail a member that is mid-request.
        if m.machine.state == State.READY and m.machine.inflight == 0:
            if healthy:
                m.healthy = True
            else:
                try:
                    m.machine.to(State.FAILED, error=f"health probe failed: {detail}")
                except Exception:  # noqa: BLE001
                    pass
                m.healthy = False
                log.warning("health probe failed for %s: %s", m.entry.alias, detail)
        elif external:
            # Daemon liveness (independent of broker lifecycle) drives the flag.
            m.healthy = healthy
        # Optional real load signal for cache-aware routing (ADR 0010) and the
        # ADR 0023 admission queue's backend-backpressure trigger: scrape an
        # exposed vLLM /metrics endpoint WITHOUT loading a model (adapters return
        # None when their process is not running). Falls back to in-flight counts.
        want_load = (
            self.settings.route_strategy == "cache_aware" and self.settings.cache_aware_use_metrics
        ) or (self.settings.queue_enabled and self.settings.queue_trigger_backend)
        if want_load and (external or active):
            try:
                load = await m.adapter.collect_load()
                if load is not None:
                    m.last_load = load
            except Exception:  # noqa: BLE001 - best effort; never fail a probe
                pass
        return m.last_health

    async def _health_probe_once(self) -> None:
        await asyncio.gather(
            *(self._probe_member(m) for m in list(self.models.values())),
            return_exceptions=True,
        )

    async def start_health_probe(self) -> None:
        if not self.settings.health_probe_enabled:
            return

        async def loop() -> None:
            while not self._stopping:
                await asyncio.sleep(self.settings.health_probe_interval_seconds)
                if self._stopping:
                    break
                try:
                    await self._health_probe_once()
                except Exception as exc:  # noqa: BLE001
                    log.warning("health probe error: %s", exc)

        self._health_task = asyncio.ensure_future(loop())

    async def backends_health(self) -> dict[str, Any]:
        """On-demand, non-loading health of every backend member."""
        results = await asyncio.gather(
            *(self._backend_health_entry(m) for m in self.models.values())
        )
        overall = "degraded" if any(r["status"] == "unhealthy" for r in results) else "ok"
        return {
            "status": overall,
            "backends": list(results),
            "resources": await self.resources_block(),
        }

    async def _backend_health_entry(self, m: ManagedModel) -> dict[str, Any]:
        probe = await self._probe_member(m)
        cd = await self.state.get_cooldown(m.entry.alias)
        cd_dict = cd.to_dict()
        if self.settings.cooldown_failures > 0 and self.settings.cooldown_half_open_single_trial:
            cd_dict["half_open_trial"] = await self.state.half_open_active(m.entry.alias)
        return {
            "alias": m.entry.alias,
            "model": m.public_name,
            "backend": m.adapter.name,
            "state": m.machine.state.value,
            "external_daemon": m.adapter.external_daemon,
            "base_url": m.entry.base_url,
            "status": probe["status"],
            "detail": probe["detail"],
            "inflight": m.machine.inflight,
            "cooldown": cd_dict,
        }

    async def ollama_ps(self) -> dict[str, Any]:
        """Aggregate Ollama ``/api/ps`` (resident models) across daemons.

        Deduplicates by base_url so each daemon is queried once. Non-loading.
        """
        seen: set[str] = set()
        daemons: list[dict[str, Any]] = []
        for m in self.models.values():
            if m.adapter.name != "ollama":
                continue
            base = m.entry.base_url or ""
            if base in seen:
                continue
            seen.add(base)
            entry: dict[str, Any] = {"base_url": base}
            try:
                data = await m.adapter.ps()  # type: ignore[attr-defined]
                entry["models"] = data.get("models", data) if isinstance(data, dict) else data
            except Exception as exc:  # noqa: BLE001
                entry["error"] = str(exc)
            daemons.append(entry)
        return {"daemons": daemons}

    def note_event_loop(self) -> None:
        """Capture the running event loop at startup (ADR 0024).

        :meth:`begin_preemption` is invoked from uvicorn's signal handler, which
        runs in the main thread; it uses this loop reference to schedule the
        actual preemption thread-safely.
        """
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - not inside a loop
            self._loop = None

    def begin_preemption(self) -> None:
        """Begin stop preemption on the FIRST stop signal (ADR 0024).

        Called from uvicorn's signal handler, so it does no blocking work: it
        schedules the real preemption onto the event loop (thread-safely). This
        cancels in-progress loads and stops admitting new ones *concurrently with*
        uvicorn's (now bounded) graceful HTTP phase, so a client waiting on a cold
        load fails fast with a typed 503 instead of pinning shutdown open for the
        whole budget.
        """
        loop = self._loop
        if loop is None:
            self._do_begin_preemption()
            return
        try:
            loop.call_soon_threadsafe(self._do_begin_preemption)
        except RuntimeError:  # pragma: no cover - loop already closed
            self._do_begin_preemption()

    def _do_begin_preemption(self) -> None:
        """Flip into stopping, stamp the budget deadline, and cancel loads."""
        if self._preempting:
            return
        self._preempting = True
        self._stopping = True
        if self._shutdown_deadline is None:
            budget = self.settings.shutdown_budget_seconds
            self._shutdown_deadline = time.monotonic() + max(0.0, budget)
        for m in list(self.models.values()):
            try:
                m.cancel_load()
            except Exception:  # noqa: BLE001 - preemption is best effort
                pass

    def request_force_shutdown(self) -> None:
        """Escalate an in-progress shutdown to immediate SIGKILL (ADR 0024).

        Called when a second SIGTERM/SIGINT arrives while the broker is already
        stopping: every remaining runtime tree is SIGKILLed without waiting out
        the grace, and the overall budget is effectively zero from here on.
        """
        self._force_shutdown = True

    async def shutdown(self) -> None:
        self._stopping = True
        budget = self.settings.shutdown_budget_seconds
        # Draw the deadline from the first stop signal if preemption already ran,
        # so uvicorn's graceful phase and the runtime teardown share ONE budget
        # (total from first signal to exit stays <= budget). Fall back to a fresh
        # budget when shutdown() runs without a prior signal (e.g. a direct
        # lifespan exit in a test).
        deadline = self._shutdown_deadline
        if deadline is None:
            deadline = time.monotonic() + max(0.0, budget)
            self._shutdown_deadline = deadline
        for task in (self._sweeper, self._health_task, self._queue_pump_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        models = list(self.models.values())
        # Preempt every in-progress load up front so no stop waits on a load.
        for m in models:
            m.cancel_load()

        force_check = lambda: self._force_shutdown  # noqa: E731

        async def _stop_one(m: ManagedModel) -> Any:
            # Each runtime's SIGTERM grace is the smaller of shutdown_timeout and
            # the budget still remaining; the shared hard_deadline bounds the whole
            # set, and force_check cuts it short on a second signal.
            remaining = deadline - time.monotonic()
            if self._force_shutdown:
                return await m.stop(hard_deadline=deadline, sigkill_only=True)
            grace = min(self.settings.shutdown_timeout_seconds, max(0.0, remaining))
            return await m.stop(grace=grace, hard_deadline=deadline, force_check=force_check)

        results = await asyncio.gather(*(_stop_one(m) for m in models), return_exceptions=True)
        # Budget-expiry / survivor sweep: anything still alive after the budget (or
        # a forced shutdown) gets an unconditional SIGKILL of its whole tree.
        survivors = [
            m
            for m, r in zip(models, results, strict=False)
            if isinstance(r, TeardownResult) and r.survivors
        ]
        if survivors or self._force_shutdown or time.monotonic() >= deadline:
            for m in survivors:
                try:
                    await m.stop(sigkill_only=True)
                except Exception:  # noqa: BLE001
                    pass
        # Release the broker-lifetime ledger ownership lock (ADR 0021) so a
        # restart on the same cache dir takes it over cleanly.
        self._owner_lock.release()
        await self.state.close()

    # --- resources (ADR 0020 / 0022) ------------------------------------
    async def resources_block(self) -> dict[str, Any]:
        """The /status + /healthz/backends ``resources`` block.

        Measured memory (``source: measured``) when telemetry is on; a provider
        status with a reason when it degraded; per-model attribution; declared
        admission budgets and recent decisions; and any ``release_stuck`` holders.
        Ollama/external-daemon memory is reported separately, labeled ``reported``,
        and never mixed into the measured devices.
        """
        enabled = self.telemetry_enabled()
        budgets = self.settings.gpu_memory_budget_map()
        block: dict[str, Any] = {
            "telemetry": {
                "provider": self.settings.gpu_telemetry,
                "enabled": enabled,
                "poll_seconds": self.settings.gpu_telemetry_poll_seconds,
            },
            "release": {"timeout_seconds": self.settings.release_timeout_seconds},
            "admission": {
                "budget_mib": budgets or None,
                "headroom_mib": self.settings.gpu_memory_headroom_mib,
                "recent": list(self._admissions)[-10:],
            },
        }
        if enabled:
            sample = await self.telemetry_sample()
            block["source"] = "measured"
            block["sample"] = {
                "provider": sample.provider,
                "status": sample.status,
                "reason": sample.reason,
                "captured_at": sample.captured_at,
            }
            block["devices"] = [d.to_dict() for d in sample.devices]
        else:
            block["source"] = None
            block["sample"] = {"status": "off"}
            block["devices"] = []
        # Broker-owned models that are stuck or survived a kill (visible, not hidden).
        stuck = []
        for mm in self.models.values():
            if mm.machine.state == State.RELEASE_STUCK or mm.machine.survived_kill:
                stuck.append(
                    {
                        "alias": mm.entry.alias,
                        "state": mm.machine.state.value,
                        "survived_kill": mm.machine.survived_kill,
                        "release_info": mm.machine.release_info,
                    }
                )
        block["release_stuck"] = stuck
        # Ollama/external daemons: reported (not measured) VRAM, clearly labeled.
        reported = await self._reported_daemon_memory()
        if reported:
            block["reported"] = {"source": "reported", "daemons": reported}
        return block

    async def _reported_daemon_memory(self) -> list[dict[str, Any]]:
        """Per-daemon ``/api/ps`` size_vram for Ollama models, labeled ``reported``.

        Only runs when Ollama models exist (no network otherwise). Never raises.
        """
        if not any(m.adapter.name == "ollama" for m in self.models.values()):
            return []
        try:
            ps = await self.ollama_ps()
        except Exception:  # noqa: BLE001 - best effort; status must not fail
            return []
        out: list[dict[str, Any]] = []
        for daemon in ps.get("daemons", []):
            models = daemon.get("models") or []
            sized = []
            if isinstance(models, list):
                for entry in models:
                    if isinstance(entry, dict):
                        sized.append(
                            {
                                "name": entry.get("name") or entry.get("model"),
                                "size_vram": entry.get("size_vram"),
                            }
                        )
            out.append({"base_url": daemon.get("base_url"), "models": sized})
        return out

    # --- status ----------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        cooldowns_on = self.settings.cooldown_failures > 0
        models = []
        for m in self.models.values():
            snap = m.machine.snapshot()
            snap["model"] = m.public_name
            snap["backend"] = m.adapter.name
            snap["healthy"] = m.healthy
            snap["last_health"] = m.last_health
            # Per-model runtime process identity (ADR 0021).
            snap["process"] = {
                "pid": m.machine.pid,
                "start_time": m.process_start_time,
                "adopted": m.adopted,
            }
            if cooldowns_on:
                cd_dict = (await self.state.get_cooldown(m.entry.alias)).to_dict()
                if self.settings.cooldown_half_open_single_trial:
                    cd_dict["half_open_trial"] = await self.state.half_open_active(m.entry.alias)
                snap["cooldown"] = cd_dict
            try:
                snap["adapter"] = await m.adapter.collect_status()
            except Exception as exc:  # noqa: BLE001
                snap["adapter"] = {"error": str(exc)}
            models.append(snap)
        return {
            "loaded_count": self.loaded_count(),
            "max_loaded_models": self.settings.max_loaded_models,
            "max_concurrent_requests": self.settings.max_concurrent_requests,
            "route_strategy": self.settings.route_strategy,
            "sticky_sessions": await self.state.sticky_len(),
            "state_backend": self.state.backend,
            "retry_max_attempts": self.settings.retry_max_attempts,
            "cooldowns_enabled": cooldowns_on,
            "reliability": {
                "max_ejection_percent": self.settings.max_ejection_percent,
                "cooldown_backoff_multiplier": self.settings.cooldown_backoff_multiplier,
                "cooldown_max_seconds": self.settings.cooldown_max_seconds,
                "half_open_single_trial": self.settings.cooldown_half_open_single_trial,
                "retry_respect_retry_after": self.settings.retry_respect_retry_after,
                "retry_after_max_seconds": self.settings.retry_after_max_seconds,
                "redis_sticky_max_entries": self.settings.redis_sticky_max_entries,
            },
            "cache_aware": (
                {"enabled": True, **self.cache_router.stats()}
                if self.cache_router is not None
                else {"enabled": False}
            ),
            "tracing_enabled": self.tracer.enabled,
            "router": self.router.status() if self.router else {"enabled": False},
            "reconciliation": {
                "runtime_reconcile": self.settings.runtime_reconcile,
                "ledger_owned": self._owner_lock.held,
                "disabled_reason": self.reconcile_disabled_reason,
                "last": self.last_reconciliation,
            },
            "pools": {
                name: [mm.entry.alias for mm in members] for name, members in self.pools.items()
            },
            "resources": await self.resources_block(),
            "queue": self.admission.snapshot(),
            "models": models,
            "timestamp": time.time(),
        }

    def public_models(self) -> list[dict]:
        """One OpenAI ``/v1/models`` entry per public name (pools collapsed)."""
        out: list[dict] = []
        for name, members in self.pools.items():
            if len(members) == 1:
                d = members[0].adapter.describe_model()
                d["id"] = name
                d["pool"] = name
                d["pool_members"] = [members[0].entry.alias]
                d["pool_size"] = 1
                out.append(d)
                continue
            out.append(
                {
                    "id": name,
                    "object": "model",
                    "created": 0,
                    "owned_by": "broker:pool",
                    "backend": "pool",
                    "context_limit": min(mm.entry.context_limit for mm in members),
                    "output_limit": min(mm.entry.output_limit for mm in members),
                    "capabilities": {
                        # Advertise a capability only if EVERY member supports it,
                        # since a request may land on any member.
                        "streaming": all(mm.entry.supports_streaming for mm in members),
                        "tools": all(mm.entry.supports_tools for mm in members),
                        "vision": all(mm.entry.supports_vision for mm in members),
                        "rerank": all(mm.entry.supports_rerank for mm in members),
                    },
                    "pool": name,
                    "pool_size": len(members),
                    "pool_members": [mm.entry.alias for mm in members],
                    "backends": sorted({mm.adapter.name for mm in members}),
                }
            )
        if self.router is not None:
            out.append(self._auto_descriptor())
        return out

    def _auto_descriptor(self) -> dict:
        """Virtual ``auto`` model: advertises the union of eligible capabilities."""
        eligible = [
            members
            for name, members in self.pools.items()
            if name != self.settings.router_alias and all(m.entry.router_eligible for m in members)
        ]
        flat = [m for members in eligible for m in members]
        return {
            "id": self.settings.router_alias,
            "object": "model",
            "created": 0,
            "owned_by": "broker:router",
            "backend": "router",
            "description": "Auto-router: a tiny CPU classifier picks the best model per request.",
            "context_limit": max((m.entry.context_limit for m in flat), default=0),
            "output_limit": max((m.entry.output_limit for m in flat), default=0),
            "capabilities": {
                # The router can pick a capable model, so advertise a capability if
                # ANY eligible model supports it.
                "streaming": any(m.entry.supports_streaming for m in flat),
                "tools": any(m.entry.supports_tools for m in flat),
                "vision": any(m.entry.supports_vision for m in flat),
                "rerank": any(m.entry.supports_rerank for m in flat),
            },
            "router_candidates": sorted(
                {
                    name
                    for name, members in self.pools.items()
                    if name != self.settings.router_alias
                    and all(m.entry.router_eligible for m in members)
                }
            ),
        }

    def describe_models(self) -> list[dict]:
        """Back-compat: public model list (pools collapsed to one entry)."""
        return self.public_models()
