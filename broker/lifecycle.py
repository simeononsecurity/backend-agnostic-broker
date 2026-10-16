"""Explicit model lifecycle state machine.

States:  unloaded -> loading -> ready -> busy -> unloading -> failed -> stopping

Legal transitions are declared in ``_TRANSITIONS``. Any illegal transition
raises :class:`LifecycleError` so bugs surface loudly instead of corrupting
state. The state object is pure (no I/O); the manager drives it.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field

from .errors import BrokerError


class LifecycleError(BrokerError):
    error_type = "lifecycle_error"
    http_status = 500


class State(str, enum.Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    BUSY = "busy"
    UNLOADING = "unloading"
    FAILED = "failed"
    STOPPING = "stopping"


# Allowed transitions. A state maps to the set of states it may move to.
_TRANSITIONS: dict[State, set[State]] = {
    State.UNLOADED: {State.LOADING, State.STOPPING},
    State.LOADING: {State.READY, State.FAILED, State.STOPPING},
    State.READY: {State.BUSY, State.UNLOADING, State.STOPPING, State.FAILED},
    State.BUSY: {State.READY, State.UNLOADING, State.STOPPING, State.FAILED},
    State.UNLOADING: {State.UNLOADED, State.FAILED, State.STOPPING},
    State.FAILED: {State.UNLOADED, State.LOADING, State.STOPPING},
    State.STOPPING: {State.UNLOADED, State.FAILED},
}


@dataclass
class LifecycleMachine:
    """Tracks the lifecycle state of one managed model."""

    alias: str
    state: State = State.UNLOADED
    last_used: float = field(default_factory=time.monotonic)
    last_error: str | None = None
    inflight: int = 0
    loaded_at: float | None = None
    pid: int | None = None
    history: list[tuple[str, float]] = field(default_factory=list)

    def can(self, target: State) -> bool:
        return target in _TRANSITIONS[self.state]

    def to(self, target: State, *, error: str | None = None) -> None:
        if target == self.state:
            return
        if not self.can(target):
            raise LifecycleError(
                f"illegal transition {self.state.value} -> {target.value}",
                model=self.alias,
                state=self.state.value,
                remediation="this is a broker bug; please report it",
            )
        self.state = target
        self.history.append((target.value, time.monotonic()))
        if len(self.history) > 50:
            self.history = self.history[-50:]
        if target == State.READY and self.loaded_at is None:
            self.loaded_at = time.monotonic()
        if target == State.UNLOADED:
            self.loaded_at = None
            self.pid = None
        if error is not None:
            self.last_error = error

    def touch(self) -> None:
        self.last_used = time.monotonic()

    def is_active(self) -> bool:
        """True if a backend process/model is (or is becoming) resident."""
        return self.state in {
            State.LOADING,
            State.READY,
            State.BUSY,
            State.UNLOADING,
        }

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    def snapshot(self) -> dict:
        return {
            "alias": self.alias,
            "state": self.state.value,
            "inflight": self.inflight,
            "idle_seconds": round(self.idle_seconds(), 2),
            "loaded_at": self.loaded_at,
            "pid": self.pid,
            "last_error": self.last_error,
        }
