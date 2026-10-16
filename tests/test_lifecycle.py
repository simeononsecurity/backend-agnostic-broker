"""Lifecycle state-machine tests."""

from __future__ import annotations

import pytest

from broker.lifecycle import LifecycleError, LifecycleMachine, State


def test_happy_path():
    m = LifecycleMachine(alias="x")
    assert m.state == State.UNLOADED
    m.to(State.LOADING)
    m.to(State.READY)
    m.to(State.BUSY)
    m.to(State.READY)
    m.to(State.UNLOADING)
    m.to(State.UNLOADED)
    assert m.loaded_at is None


def test_illegal_transition_raises():
    m = LifecycleMachine(alias="x")
    with pytest.raises(LifecycleError):
        m.to(State.BUSY)  # can't go unloaded -> busy


def test_failed_then_reload():
    m = LifecycleMachine(alias="x")
    m.to(State.LOADING)
    m.to(State.FAILED, error="boom")
    assert m.last_error == "boom"
    m.to(State.LOADING)  # allowed to retry
    m.to(State.READY)
    assert m.state == State.READY


def test_idempotent_same_state():
    m = LifecycleMachine(alias="x")
    m.to(State.UNLOADED)  # no-op, no raise
    assert m.state == State.UNLOADED


def test_is_active_and_snapshot():
    m = LifecycleMachine(alias="x")
    assert not m.is_active()
    m.to(State.LOADING)
    assert m.is_active()
    snap = m.snapshot()
    assert snap["alias"] == "x" and snap["state"] == "loading"
