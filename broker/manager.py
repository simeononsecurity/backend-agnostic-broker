"""Model manager: lazy loading, locking, concurrency, idle unload.

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
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from .adapters import make_adapter
from .config import Settings
from .errors import (
    ConcurrencyLimitError,
    ModelLoadError,
    ModelNotFoundError,
)
from .lifecycle import LifecycleMachine, State
from .registry import ModelEntry, Registry

log = logging.getLogger("broker.manager")


class ManagedModel:
    def __init__(self, entry: ModelEntry, settings: Settings) -> None:
        self.entry = entry
        self.settings = settings
        self.adapter = make_adapter(entry, settings)
        self.machine = LifecycleMachine(alias=entry.alias)
        self.load_lock = asyncio.Lock()
        self.sem = asyncio.Semaphore(settings.per_model_max_concurrency)

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
                log.info("model %s ready (pid=%s)", self.entry.alias, pid)
            except Exception as exc:  # noqa: BLE001
                self.machine.to(State.FAILED, error=str(exc))
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
        self.global_sem = asyncio.Semaphore(settings.max_concurrent_requests)
        self._sweeper: asyncio.Task | None = None
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

    def loaded_count(self) -> int:
        return sum(1 for m in self.models.values() if m.machine.is_active())

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

    # --- request path ----------------------------------------------------
    async def acquire(self, alias: str) -> ManagedModel:
        """Select + load the model for a request. Never falls back."""
        m = self.get(alias)
        await self._maybe_evict(alias)
        await m.ensure_ready()
        return m

    async def chat(self, alias: str, payload: dict, request_id: str) -> dict:
        m = await self.acquire(alias)
        async with self.global_sem:
            if m.sem.locked() and self.settings.load_concurrency_policy == "reject":
                raise ConcurrencyLimitError(
                    f"per-model concurrency limit for {alias}",
                    model=alias,
                    remediation="retry later or raise per-model concurrency",
                )
            async with m.sem:
                m.machine.inflight += 1
                prev = m.machine.state
                if prev == State.READY:
                    m.machine.to(State.BUSY)
                try:
                    return await asyncio.wait_for(
                        m.adapter.chat(payload, request_id),
                        timeout=m.entry.request_timeout_seconds,
                    )
                finally:
                    m.machine.inflight -= 1
                    m.machine.touch()
                    if m.machine.inflight == 0 and m.machine.state == State.BUSY:
                        m.machine.to(State.READY)

    async def stream_chat(self, alias: str, payload: dict, request_id: str) -> AsyncIterator[bytes]:
        m = await self.acquire(alias)
        await self.global_sem.acquire()
        await m.sem.acquire()
        m.machine.inflight += 1
        if m.machine.state == State.READY:
            m.machine.to(State.BUSY)
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

    async def cancel(self, alias: str, request_id: str) -> None:
        m = self.get(alias)
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

    async def shutdown(self) -> None:
        self._stopping = True
        if self._sweeper:
            self._sweeper.cancel()
            try:
                await self._sweeper
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await asyncio.gather(*(m.stop() for m in self.models.values()), return_exceptions=True)

    # --- status ----------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        models = []
        for m in self.models.values():
            snap = m.machine.snapshot()
            try:
                snap["adapter"] = await m.adapter.collect_status()
            except Exception as exc:  # noqa: BLE001
                snap["adapter"] = {"error": str(exc)}
            models.append(snap)
        return {
            "loaded_count": self.loaded_count(),
            "max_loaded_models": self.settings.max_loaded_models,
            "max_concurrent_requests": self.settings.max_concurrent_requests,
            "models": models,
            "timestamp": time.time(),
        }

    def describe_models(self) -> list[dict]:
        return [m.adapter.describe_model() for m in self.models.values()]
