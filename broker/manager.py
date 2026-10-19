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
import logging
import time
from collections import OrderedDict
from collections.abc import AsyncIterator
from typing import Any

from .adapters import make_adapter
from .config import Settings
from .errors import (
    BackendUnavailableError,
    ConcurrencyLimitError,
    ModelLoadError,
    ModelNotFoundError,
    TimeoutError_,
)
from .lifecycle import LifecycleMachine, State
from .registry import ModelEntry, Registry
from .router import AutoRouter

log = logging.getLogger("broker.manager")

#: Exceptions that make a pool member a failover candidate (try another member).
_FAILOVER_ERRORS = (BackendUnavailableError, ModelLoadError, TimeoutError_)


class ManagedModel:
    def __init__(self, entry: ModelEntry, settings: Settings) -> None:
        self.entry = entry
        self.settings = settings
        self.adapter = make_adapter(entry, settings)
        self.machine = LifecycleMachine(alias=entry.alias)
        self.load_lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(settings.per_model_max_concurrency)
        # Routing / health state.
        self.public_name = entry.public_name
        self.healthy = True
        self._route_seq = 0
        self.last_health: dict[str, Any] | None = None

    async def ensure_ready(self) -> None:
        """Start + ready the model if needed, exactly once, under the load lock."""
        if self.machine.state in (State.READY, State.BUSY):
            return
        async with self.load_lock:
            if self.machine.state in (State.READY, State.BUSY):
                return
            if self.machine.state == State.FAILED:
                self.machine.to(State.UNLOADED)  # allow a fresh attempt
            self.machine.to(State.LOADING)
            try:
                pid = await self.adapter.start()
                self.machine.pid = pid
                await self.adapter.wait_until_ready(
                    self.entry.startup_timeout_seconds or self.settings.startup_timeout_seconds
                )
                self.machine.to(State.READY)
                self.machine.touch()
                self.healthy = True
                log.info("model %s ready (pid=%s)", self.entry.alias, pid)
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
                self.machine.to(State.UNLOADED)
                log.info("model %s unloaded", self.entry.alias)
            except Exception as exc:  # noqa: BLE001
                self.machine.to(State.FAILED, error=str(exc))

    async def stop(self) -> None:
        async with self.load_lock:
            if self.machine.state != State.UNLOADED:
                try:
                    self.machine.to(State.STOPPING)
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self.adapter.stop()
            finally:
                try:
                    self.machine.to(State.UNLOADED)
                except Exception:  # noqa: BLE001
                    self.machine.state = State.UNLOADED


class ModelManager:
    def __init__(self, registry: Registry, settings: Settings) -> None:
        self.settings = settings
        self.registry = registry
        self.models: dict[str, ManagedModel] = {
            e.alias: ManagedModel(e, settings) for e in registry.models
        }
        # Public name -> ordered list of member ManagedModels (a pool).
        self.pools: dict[str, list[ManagedModel]] = {}
        for e in registry.models:
            self.pools.setdefault(e.public_name, []).append(self.models[e.alias])
        self.global_sem = asyncio.Semaphore(settings.max_concurrent_requests)
        self._route_lock = asyncio.Lock()
        self._route_counter = itertools.count(1)
        # Sticky routing: (public_name, session_key) -> member alias. LRU-ordered.
        self._sticky: OrderedDict[tuple[str, str], str] = OrderedDict()
        # Auto-router ("auto" alias). Lazy-constructed; classification serialized.
        self.router: AutoRouter | None = None
        self._router_lock = asyncio.Lock()
        if settings.router_enabled and settings.router_backend != "off":
            self.router = AutoRouter(self, settings, registry.router)
        self._sweeper: asyncio.Task | None = None
        self._health_task: asyncio.Task | None = None
        self._stopping = False

    # --- validation ------------------------------------------------------
    def validate_all(self) -> None:
        """Validate every adapter config at startup (no model loads)."""
        for m in self.models.values():
            m.adapter.validate_config()

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

    def _members_for(self, name: str) -> list[ManagedModel]:
        members = self.pools.get(name)
        if members:
            return members
        if name in self.models:  # direct alias addressing of a non-pooled entry
            return [self.models[name]]
        raise ModelNotFoundError(
            f"unknown model: {name!r}",
            model=name,
            remediation=f"choose one of: {', '.join(sorted(self.pools))}",
        )

    def loaded_count(self) -> int:
        return sum(1 for m in self.models.values() if m.machine.is_active())

    async def _resolve_auto(self, name: str, payload: dict) -> str:
        """Resolve the ``auto`` alias to a concrete public model via the router.

        Classification is serialized (and offloaded to a thread so an in-process
        LLM classifier never blocks the event loop) and returns a normal public
        model name, which then flows through standard pool routing + failover.
        """
        if self.router is not None and name == self.settings.router_alias:
            async with self._router_lock:
                decision = await asyncio.to_thread(self.router.route, payload)
            return decision.chosen
        return name

    # --- eviction --------------------------------------------------------
    async def _maybe_evict(self, keep_alias: str) -> None:
        if self.loaded_count() < self.settings.max_loaded_models:
            return
        # Evict the LRU idle model (not the one we're about to load).
        candidates = [
            m
            for a, m in self.models.items()
            if a != keep_alias and m.machine.state == State.READY and m.machine.inflight == 0
        ]
        if not candidates:
            raise ConcurrencyLimitError(
                "max loaded models reached and no idle model to evict",
                remediation="raise BROKER_MAX_LOADED_MODELS or wait",
            )
        victim = min(candidates, key=lambda m: m.machine.last_used)
        log.info("evicting LRU model %s to make room for %s", victim.entry.alias, keep_alias)
        await victim.unload()

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

    def _remember_sticky(self, name: str, route_key: str, alias: str) -> None:
        """Record/refresh a session->member pairing with LRU eviction."""
        k = (name, route_key)
        self._sticky[k] = alias
        self._sticky.move_to_end(k)
        cap = self.settings.route_sticky_max_entries
        while cap > 0 and len(self._sticky) > cap:
            self._sticky.popitem(last=False)

    async def _select_member(
        self,
        name: str,
        members: list[ManagedModel],
        exclude: set[str],
        route_key: str | None = None,
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
            pool = [m for m in members if m.entry.alias not in exclude]
            if not pool:
                return None
            tier = [m for m in pool if m.healthy and m.machine.state != State.FAILED]
            if not tier:
                tier = [m for m in pool if m.machine.state != State.FAILED]
            if not tier:
                tier = pool  # last resort: retry a failed member (self-heal)

            strategy = self.settings.route_strategy
            chosen: ManagedModel | None = None
            if strategy == "sticky" and route_key is not None and len(members) > 1:
                assigned = self._sticky.get((name, route_key))
                if assigned is not None:
                    chosen = next((m for m in tier if m.entry.alias == assigned), None)
                if chosen is None:  # new session, or pinned member unavailable
                    chosen = self._assign_member(tier)
                self._remember_sticky(name, route_key, chosen.entry.alias)
            elif strategy == "round_robin":
                chosen = min(tier, key=lambda m: (m._route_seq,))
            else:  # least_busy
                chosen = self._least_busy(tier)

            chosen.machine.inflight += 1  # reserve the slot
            chosen._route_seq = next(self._route_counter)  # rotate tie-break
            return chosen

    # --- request path ----------------------------------------------------
    async def acquire(self, alias: str) -> ManagedModel:
        """Select + load a single model for a request (no routing/failover)."""
        m = self.get(alias)
        await self._maybe_evict(alias)
        await m.ensure_ready()
        return m

    async def chat(
        self, name: str, payload: dict, request_id: str, route_key: str | None = None
    ) -> dict:
        name = await self._resolve_auto(name, payload)
        members = self._members_for(name)
        exclude: set[str] = set()
        last: Exception | None = None
        for _ in range(len(members)):
            m = await self._select_member(name, members, exclude, route_key)
            if m is None:
                break
            try:
                return await self._serve_chat(m, name, payload, request_id)
            except _FAILOVER_ERRORS as exc:  # member unavailable → try another
                m.healthy = False
                exclude.add(m.entry.alias)
                last = exc
                log.warning(
                    "pool %s: member %s failed (%s); failing over", name, m.entry.alias, exc
                )
                continue
        if last is not None:
            raise last
        raise BackendUnavailableError(
            f"no healthy backend available for model {name!r}",
            model=name,
            remediation="check GET /healthz/backends",
        )

    async def _serve_chat(
        self, m: ManagedModel, public_name: str, payload: dict, request_id: str
    ) -> dict:
        # inflight was reserved by _select_member; release it in finally.
        try:
            await self._maybe_evict(m.entry.alias)
            await m.ensure_ready()
            async with self.global_sem:
                if m.sem.locked() and self.settings.load_concurrency_policy == "reject":
                    raise ConcurrencyLimitError(
                        f"per-model concurrency limit for {m.entry.alias}",
                        model=m.entry.alias,
                        remediation="retry later or raise per-model concurrency",
                    )
                async with m.sem:
                    if m.machine.state == State.READY:
                        m.machine.to(State.BUSY)
                    result = await asyncio.wait_for(
                        m.adapter.chat(payload, request_id),
                        timeout=m.entry.request_timeout_seconds,
                    )
                    m.healthy = True
                    # Present the public (requested) model name back to the caller.
                    if isinstance(result, dict):
                        result["model"] = public_name
                    return result
        finally:
            m.machine.inflight -= 1
            m.machine.touch()
            if m.machine.inflight == 0 and m.machine.state == State.BUSY:
                m.machine.to(State.READY)

    async def stream_chat(
        self, name: str, payload: dict, request_id: str, route_key: str | None = None
    ) -> AsyncIterator[bytes]:
        name = await self._resolve_auto(name, payload)
        members = self._members_for(name)
        exclude: set[str] = set()
        last: Exception | None = None
        chosen: ManagedModel | None = None
        # Select + ready a member, with failover *before* the stream starts.
        for _ in range(len(members)):
            m = await self._select_member(name, members, exclude, route_key)
            if m is None:
                break
            try:
                await self._maybe_evict(m.entry.alias)
                await m.ensure_ready()
                chosen = m
                break
            except _FAILOVER_ERRORS as exc:
                m.healthy = False
                m.machine.inflight -= 1  # release reservation; we won't serve here
                m.machine.touch()
                exclude.add(m.entry.alias)
                last = exc
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
        await self.global_sem.acquire()
        await m.sem.acquire()
        if m.machine.state == State.READY:
            m.machine.to(State.BUSY)
        m.healthy = True
        try:
            async for chunk in m.adapter.stream_chat(payload, request_id):
                yield chunk
        finally:
            m.machine.inflight -= 1
            m.machine.touch()
            if m.machine.inflight == 0 and m.machine.state == State.BUSY:
                m.machine.to(State.READY)
            m.sem.release()
            self.global_sem.release()

    async def embeddings(
        self, name: str, payload: dict, request_id: str, route_key: str | None = None
    ) -> dict:
        """Route an embeddings request through pool selection + failover.

        Unlike chat, ``auto`` does not apply (there is nothing to classify); the
        request is routed to the named model/pool. Shares the same member
        selection, concurrency gating, lazy-load, and failover as chat.
        """
        members = self._members_for(name)
        exclude: set[str] = set()
        last: Exception | None = None
        for _ in range(len(members)):
            m = await self._select_member(name, members, exclude, route_key)
            if m is None:
                break
            try:
                return await self._serve_embeddings(m, name, payload, request_id)
            except _FAILOVER_ERRORS as exc:
                m.healthy = False
                exclude.add(m.entry.alias)
                last = exc
                log.warning(
                    "pool %s: member %s failed (%s); failing over", name, m.entry.alias, exc
                )
                continue
        if last is not None:
            raise last
        raise BackendUnavailableError(
            f"no healthy backend available for model {name!r}",
            model=name,
            remediation="check GET /healthz/backends",
        )

    async def _serve_embeddings(
        self, m: ManagedModel, public_name: str, payload: dict, request_id: str
    ) -> dict:
        try:
            await self._maybe_evict(m.entry.alias)
            await m.ensure_ready()
            async with self.global_sem:
                if m.sem.locked() and self.settings.load_concurrency_policy == "reject":
                    raise ConcurrencyLimitError(
                        f"per-model concurrency limit for {m.entry.alias}",
                        model=m.entry.alias,
                        remediation="retry later or raise per-model concurrency",
                    )
                async with m.sem:
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

    # --- idle sweep ------------------------------------------------------
    async def _sweep_once(self) -> None:
        for m in list(self.models.values()):
            if m.machine.state != State.READY or m.machine.inflight > 0:
                continue
            timeout = m.entry.idle_timeout_seconds or self.settings.model_idle_timeout_seconds
            if timeout > 0 and m.machine.idle_seconds() >= timeout:
                log.info("idle unload: %s (idle %.1fs)", m.entry.alias, m.machine.idle_seconds())
                await m.unload()

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
        return {"status": overall, "backends": list(results)}

    async def _backend_health_entry(self, m: ManagedModel) -> dict[str, Any]:
        probe = await self._probe_member(m)
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

    async def shutdown(self) -> None:
        self._stopping = True
        for task in (self._sweeper, self._health_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        await asyncio.gather(*(m.stop() for m in self.models.values()), return_exceptions=True)

    # --- status ----------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        models = []
        for m in self.models.values():
            snap = m.machine.snapshot()
            snap["model"] = m.public_name
            snap["backend"] = m.adapter.name
            snap["healthy"] = m.healthy
            snap["last_health"] = m.last_health
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
            "sticky_sessions": len(self._sticky),
            "router": self.router.status() if self.router else {"enabled": False},
            "pools": {
                name: [mm.entry.alias for mm in members] for name, members in self.pools.items()
            },
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
