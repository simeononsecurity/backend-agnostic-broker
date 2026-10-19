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
import random
import time
from collections.abc import AsyncIterator
from typing import Any

from .adapters import make_adapter
from .config import Settings
from .errors import (
    BackendUnavailableError,
    ConcurrencyLimitError,
    ModelBusyError,
    ModelLoadError,
    ModelNotFoundError,
    TimeoutError_,
)
from .lifecycle import LifecycleMachine, State
from .registry import ModelEntry, Registry, load_registry
from .router import AutoRouter
from .state import make_state_store
from .tracing import make_tracer

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
        # Set True while an admin unload/reload is draining this member: selection
        # skips it so in-flight requests finish without new ones piling on.
        self.draining = False
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
        # Shared routing/runtime state (ADR 0009): in-process by default, or
        # Redis when BROKER_REDIS_URL is set (multi-replica). Owns the sticky
        # session map, per-member cooldowns, counters, and a generic KV.
        self.state = make_state_store(settings)
        # Optional OpenTelemetry tracer (ADR 0012): no-op unless tracing_enabled.
        # Constructed here so an enabled-but-not-installed otel extra fails fast
        # at app creation (ConfigError), never mid-request.
        self.tracer = make_tracer(settings)
        # Back-compat view of the sticky map for in-process deployments/tests:
        # (public_name, session_key) -> member alias, LRU-ordered. None under
        # Redis (the shared store is authoritative there).
        self._sticky = getattr(self.state, "sticky", None)
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
            with self.tracer.span(
                "auto_route", {"gen_ai.operation.name": "route", "gen_ai.request.model": name}
            ) as sp:
                async with self._router_lock:
                    decision = await asyncio.to_thread(self.router.route, payload)
                sp.set_attribute("broker.route.chosen", decision.chosen)
            return decision.chosen
        return name

    async def resolve_public(self, name: str, payload: dict) -> str:
        """Public entry point for ``auto`` resolution (used by the response cache
        to key on the concrete model the request will actually run on)."""
        return await self._resolve_auto(name, payload)

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
        """Reset a member's cooldown after a successful serve (no-op if off)."""
        if self.settings.cooldown_failures > 0:
            await self.state.record_success(m.entry.alias)

    async def _note_failure(self, m: ManagedModel) -> None:
        """Record a member failure; bench it on reaching the threshold (off=no-op)."""
        if self.settings.cooldown_failures > 0:
            await self.state.record_failure(
                m.entry.alias,
                threshold=self.settings.cooldown_failures,
                cooldown_seconds=self.settings.cooldown_seconds,
            )

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
            pool = [m for m in members if m.entry.alias not in exclude and not m.draining]
            if not pool:
                return None
            tier = [m for m in pool if m.healthy and m.machine.state != State.FAILED]
            if not tier:
                tier = [m for m in pool if m.machine.state != State.FAILED]
            if not tier:
                tier = pool  # last resort: retry a failed member (self-heal)

            # Member cooldowns (ADR 0009): when enabled, cooldown state is the
            # authoritative benching signal (it supersedes the transient
            # `healthy` flag). Benched members are skipped; a member whose window
            # has elapsed rejoins as a half-open trial even if a prior failover
            # left healthy=False. If EVERY member is benched, fall through to the
            # healthy-based `tier` so the pool can still self-heal.
            if self.settings.cooldown_failures > 0:
                now = time.time()
                benched = set()
                for m in pool:
                    if (await self.state.get_cooldown(m.entry.alias, now=now)).in_cooldown:
                        benched.add(m.entry.alias)
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
            chosen: ManagedModel | None = None
            if strategy == "sticky" and route_key is not None and len(members) > 1:
                assigned = await self.state.sticky_get(name, route_key)
                if assigned is not None:
                    chosen = next((m for m in tier if m.entry.alias == assigned), None)
                if chosen is None:  # new session, or pinned member unavailable
                    chosen = self._assign_member(tier)
                await self.state.sticky_set(name, route_key, chosen.entry.alias)
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

    async def _route_and_serve(
        self,
        name: str,
        members: list[ManagedModel],
        route_key: str | None,
        serve,
    ) -> dict:
        """Select a pool member, serve, and retry within the pool on failure.

        Shared driver for chat + embeddings. Retries stay WITHIN the requested
        public model's pool — never a different model (the no-silent-cross-model
        invariant). Between attempts it waits a full-jitter backoff (zero by
        default). The attempt budget and member re-trying are gated by
        ``retry_max_attempts`` so the default is a single failover pass with no
        sleep, exactly as v0.4.0. Overall retrying also respects the request
        timeout as a deadline.
        """
        exclude: set[str] = set()
        last: Exception | None = None
        cap = self._attempt_budget(len(members))
        deadline: float | None = None
        if self.settings.retry_max_attempts > 0 and self.settings.request_timeout_seconds > 0:
            deadline = time.monotonic() + self.settings.request_timeout_seconds
        attempt = 0
        while attempt < cap:
            if attempt > 0:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                delay = self._backoff_delay_s(attempt - 1)
                if delay > 0:
                    await asyncio.sleep(delay)
            m = await self._select_member(name, members, exclude, route_key)
            if m is None:
                # Exhausted distinct members. With retries enabled, clear the
                # exclusion set and try members again (within the same pool).
                if self.settings.retry_max_attempts > 0 and exclude:
                    exclude = set()
                    m = await self._select_member(name, members, exclude, route_key)
                if m is None:
                    break
            attempt += 1
            try:
                result = await serve(m)
                await self._note_success(m)
                return result
            except _FAILOVER_ERRORS as exc:  # member unavailable → retry/failover
                m.healthy = False
                await self._note_failure(m)
                exclude.add(m.entry.alias)
                last = exc
                log.warning(
                    "pool %s: member %s failed (%s); retry/failover", name, m.entry.alias, exc
                )
                continue
        if last is not None:
            raise last
        raise BackendUnavailableError(
            f"no healthy backend available for model {name!r}",
            model=name,
            remediation="check GET /healthz/backends",
        )

    async def chat(
        self, name: str, payload: dict, request_id: str, route_key: str | None = None
    ) -> dict:
        name = await self._resolve_auto(name, payload)
        members = self._members_for(name)
        return await self._route_and_serve(
            name,
            members,
            route_key,
            lambda m: self._serve_chat(m, name, payload, request_id),
        )

    async def _serve_chat(
        self, m: ManagedModel, public_name: str, payload: dict, request_id: str
    ) -> dict:
        # inflight was reserved by _select_member; release it in finally.
        try:
            await self._maybe_evict(m.entry.alias)
            with self.tracer.span(
                "load_model",
                {"gen_ai.request.model": public_name, "broker.member.alias": m.entry.alias},
            ):
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
                    with self.tracer.span(
                        "inference",
                        {
                            "gen_ai.operation.name": "chat",
                            "gen_ai.request.model": public_name,
                            "gen_ai.provider.name": m.adapter.name,
                            "broker.member.alias": m.entry.alias,
                        },
                    ) as sp:
                        result = await asyncio.wait_for(
                            m.adapter.chat(payload, request_id),
                            timeout=m.entry.request_timeout_seconds,
                        )
                        if isinstance(result, dict):
                            usage = result.get("usage") or {}
                            sp.set_attribute("gen_ai.response.model", public_name)
                            if usage.get("prompt_tokens") is not None:
                                sp.set_attribute(
                                    "gen_ai.usage.input_tokens", usage["prompt_tokens"]
                                )
                            if usage.get("completion_tokens") is not None:
                                sp.set_attribute(
                                    "gen_ai.usage.output_tokens", usage["completion_tokens"]
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
        # Select + ready a member, with retry/failover *before* the stream
        # starts. Once bytes are flowing we NEVER retry (a half-emitted stream
        # can't be transparently restarted). Retries stay within this pool.
        cap = self._attempt_budget(len(members))
        deadline: float | None = None
        if self.settings.retry_max_attempts > 0 and self.settings.request_timeout_seconds > 0:
            deadline = time.monotonic() + self.settings.request_timeout_seconds
        attempt = 0
        while attempt < cap:
            if attempt > 0:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                delay = self._backoff_delay_s(attempt - 1)
                if delay > 0:
                    await asyncio.sleep(delay)
            m = await self._select_member(name, members, exclude, route_key)
            if m is None:
                if self.settings.retry_max_attempts > 0 and exclude:
                    exclude = set()
                    m = await self._select_member(name, members, exclude, route_key)
                if m is None:
                    break
            attempt += 1
            try:
                await self._maybe_evict(m.entry.alias)
                await m.ensure_ready()
                chosen = m
                break
            except _FAILOVER_ERRORS as exc:
                m.healthy = False
                m.machine.inflight -= 1  # release reservation; we won't serve here
                m.machine.touch()
                await self._note_failure(m)
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
            await self._note_success(m)  # stream completed cleanly → reset cooldown
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
        return await self._route_and_serve(
            name,
            members,
            route_key,
            lambda m: self._serve_embeddings(m, name, payload, request_id),
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
            mm = ManagedModel(new_by[a], self.settings)
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
        cd = await self.state.get_cooldown(m.entry.alias)
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
            "cooldown": cd.to_dict(),
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
        await self.state.close()

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
            if cooldowns_on:
                snap["cooldown"] = (await self.state.get_cooldown(m.entry.alias)).to_dict()
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
            "tracing_enabled": self.tracer.enabled,
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
