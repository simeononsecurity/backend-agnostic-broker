"""Per-error retry / cooldown policy (ADR 0017).

Covers the classification matrix (timeout / connection / server_error /
backpressure / client_error), the hard rule that 4xx client errors are never
retried or benched, per-class retry counts, and honoring an upstream Retry-After
within the retry budget. Unit tests exercise ``broker.reliability`` directly;
integration tests drive the manager's retry loop with injected failures.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from broker.config import Settings
from broker.errors import (
    BackendUnavailableError,
    ConcurrencyLimitError,
    ModelLoadError,
    PlacementDeniedError,
    TimeoutError_,
    ValidationError,
)
from broker.manager import ModelManager
from broker.registry import load_registry
from broker.reliability import ErrorClass, RetryPolicy, classify_error, retry_after_seconds

REG = """
models:
  - alias: node-a
    backend: fake
    runtime_model_id: shared
    pool: chat
  - alias: node-b
    backend: fake
    runtime_model_id: shared
    pool: chat
"""


def _mgr(tmp_path: Path, **over) -> ModelManager:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(REG).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        max_loaded_models=5,
    )
    kwargs.update(over)
    s = Settings(**kwargs)
    reg_obj = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg_obj, s)
    m.validate_all()
    return m


def _msg() -> dict:
    return {"messages": [{"role": "user", "content": "x"}]}


def _instrument(mgr: ModelManager, raiser) -> dict[str, int]:
    """Replace each member's adapter.chat with a counting coroutine; ``raiser``
    returns an exception to raise for a given alias, or None to succeed."""
    calls: dict[str, int] = {a: 0 for a in mgr.models}

    def make(alias: str):
        async def chat(payload, request_id):
            calls[alias] += 1
            exc = raiser(alias, calls[alias])
            if exc is not None:
                raise exc
            return {
                "model": alias,
                "choices": [{"message": {"role": "assistant", "content": f"echo: {alias}"}}],
                "usage": {},
            }

        return chat

    for alias, mm in mgr.models.items():
        mm.adapter.chat = make(alias)  # type: ignore[method-assign]
    return calls


async def _noop_sleep(_delay):
    return None


# --- classification -----------------------------------------------------


def test_classify_matrix():
    assert classify_error(TimeoutError_("x")) is ErrorClass.TIMEOUT
    assert classify_error(BackendUnavailableError("x")) is ErrorClass.CONNECTION
    assert classify_error(ModelLoadError("x")) is ErrorClass.SERVER_ERROR
    assert classify_error(ConcurrencyLimitError("x")) is ErrorClass.BACKPRESSURE
    assert classify_error(ValidationError("x")) is ErrorClass.CLIENT_ERROR
    assert classify_error(PlacementDeniedError("x")) is ErrorClass.CLIENT_ERROR
    # Generic exception → other.
    assert classify_error(RuntimeError("x")) is ErrorClass.OTHER


def test_classify_by_http_status():
    class Err429(Exception):
        http_status = 429

    class Err500(Exception):
        http_status = 500

    class Err404(Exception):
        http_status = 404

    assert classify_error(Err429()) is ErrorClass.BACKPRESSURE
    assert classify_error(Err500()) is ErrorClass.SERVER_ERROR
    assert classify_error(Err404()) is ErrorClass.CLIENT_ERROR


# --- Retry-After parsing ------------------------------------------------


def test_retry_after_seconds_numeric():
    exc = ConcurrencyLimitError("x", headers={"Retry-After": "2"})
    assert retry_after_seconds(exc) == pytest.approx(2.0)


def test_retry_after_seconds_http_date():
    exc = ConcurrencyLimitError("x", headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    # A fixed 'now' before the date → positive delta; after → 0.
    before = retry_after_seconds(exc, now=1_700_000_000.0)
    assert before is not None and before > 0
    after = retry_after_seconds(exc, now=4_000_000_000.0)
    assert after == 0.0


def test_retry_after_seconds_absent_and_invalid():
    assert retry_after_seconds(BackendUnavailableError("x")) is None
    assert retry_after_seconds(ConcurrencyLimitError("x", headers={"Retry-After": "soon"})) is None


def test_retry_after_header_case_insensitive():
    exc = ConcurrencyLimitError("x", headers={"retry-after": "5"})
    assert retry_after_seconds(exc) == pytest.approx(5.0)


# --- RetryPolicy decisions ----------------------------------------------


def test_policy_defaults_reproduce_adr0009(tmp_path):
    p = RetryPolicy(Settings(_env_file=None))
    assert p.is_retryable(ErrorClass.TIMEOUT)
    assert p.is_retryable(ErrorClass.CONNECTION)
    assert p.is_retryable(ErrorClass.SERVER_ERROR)
    assert not p.is_retryable(ErrorClass.BACKPRESSURE)
    assert not p.is_retryable(ErrorClass.CLIENT_ERROR)
    assert not p.is_retryable(ErrorClass.OTHER)
    # Cooldown-counting mirrors the retryable failover set by default.
    assert p.counts_toward_cooldown(ErrorClass.TIMEOUT)
    assert not p.counts_toward_cooldown(ErrorClass.BACKPRESSURE)
    assert not p.counts_toward_cooldown(ErrorClass.CLIENT_ERROR)


def test_policy_client_error_never_retried_even_if_configured():
    # No knob can make a 4xx client error retryable/counted (hard invariant).
    s = Settings(_env_file=None, retry_max_attempts_by_class={"client_error": 9})
    p = RetryPolicy(s)
    assert not p.is_retryable(ErrorClass.CLIENT_ERROR)
    assert not p.counts_toward_cooldown(ErrorClass.CLIENT_ERROR)


def test_policy_backpressure_opt_in():
    s = Settings(_env_file=None, retry_on_backpressure=True, cooldown_on_backpressure=True)
    p = RetryPolicy(s)
    assert p.is_retryable(ErrorClass.BACKPRESSURE)
    assert p.counts_toward_cooldown(ErrorClass.BACKPRESSURE)


def test_policy_per_class_attempt_cap():
    s = Settings(_env_file=None, retry_max_attempts_by_class={"timeout": 5, "server_error": 2})
    p = RetryPolicy(s)
    assert p.max_attempts_for(ErrorClass.TIMEOUT) == 5
    assert p.max_attempts_for(ErrorClass.SERVER_ERROR) == 2
    assert p.max_attempts_for(ErrorClass.CONNECTION) is None


# --- manager integration ------------------------------------------------


async def test_client_error_not_retried_or_benched(tmp_path, monkeypatch):
    # A 4xx client error propagates immediately: one call, no failover, no bench.
    mgr = _mgr(tmp_path, cooldown_failures=1)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    calls = _instrument(
        mgr, lambda a, n: ValidationError("bad", model=a) if a == "node-a" else None
    )
    await mgr.state.sticky_set("chat", "s", "node-a")
    with pytest.raises(ValidationError):
        await mgr.chat("chat", _msg(), "r0", route_key="s")
    assert calls["node-a"] == 1
    assert calls["node-b"] == 0  # NOT failed over
    assert not (await mgr.state.get_cooldown("node-a")).active  # not benched
    await mgr.shutdown()


async def test_backpressure_fails_over_to_healthy_member(tmp_path, monkeypatch):
    # ADR 0017 (follow-up): upstream backpressure (429) always fails over to a
    # healthy member — immediately and without opt-in — and never sleep-retries
    # that member. node-a 429s, node-b serves.
    mgr = _mgr(tmp_path)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    calls = _instrument(
        mgr, lambda a, n: ConcurrencyLimitError("busy", model=a) if a == "node-a" else None
    )
    await mgr.state.sticky_set("chat", "s", "node-a")
    out = await mgr.chat("chat", _msg(), "r0", route_key="s")
    assert out["choices"][0]["message"]["content"] == "echo: node-b"
    assert calls["node-a"] == 1 and calls["node-b"] == 1
    await mgr.shutdown()


async def test_backpressure_lone_member_returns_429_no_sleep(tmp_path, monkeypatch):
    # With no healthy member to fail over to, backpressure is NOT sleep-retried by
    # default: the 429 is returned to the client (clients self-pace) and no sleep
    # happens. Both members 429 here.
    mgr = _mgr(tmp_path, retry_max_attempts=3, request_timeout_seconds=300)
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = _instrument(mgr, lambda a, n: ConcurrencyLimitError("busy", model=a))
    with pytest.raises(ConcurrencyLimitError):
        await mgr.chat("chat", _msg(), "r0")
    # One failover pass over the two members, then stop — no sleep-retry.
    assert calls["node-a"] == 1 and calls["node-b"] == 1
    assert slept == []
    await mgr.shutdown()


async def test_backpressure_retried_when_opted_in(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, retry_on_backpressure=True, retry_max_attempts=3)
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    # node-a 429s, node-b succeeds → failover now happens.
    calls = _instrument(
        mgr, lambda a, n: ConcurrencyLimitError("busy", model=a) if a == "node-a" else None
    )
    await mgr.state.sticky_set("chat", "s", "node-a")
    out = await mgr.chat("chat", _msg(), "r0", route_key="s")
    assert out["choices"][0]["message"]["content"] == "echo: node-b"
    assert calls["node-a"] == 1 and calls["node-b"] == 1
    await mgr.shutdown()


async def test_cooldown_counts_only_configured_classes(tmp_path, monkeypatch):
    # Retry both timeout+connection, but only count timeout toward cooldown.
    mgr = _mgr(
        tmp_path,
        cooldown_failures=1,
        retry_max_attempts=4,
        cooldown_on_connection=False,
        cooldown_on_timeout=True,
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # node-a always raises CONNECTION (not counted); node-b always TIMEOUT
    # (counted). The request exhausts its budget and raises.
    def raiser(a, n):
        if a == "node-a":
            return BackendUnavailableError("conn", model=a)
        return TimeoutError_("to", model=a)

    _instrument(mgr, raiser)
    with pytest.raises((BackendUnavailableError, TimeoutError_)):
        await mgr.chat("chat", _msg(), "r0")
    # node-a (connection) never benched; node-b (timeout) benched.
    assert not (await mgr.state.get_cooldown("node-a")).active
    assert (await mgr.state.get_cooldown("node-b")).in_cooldown
    await mgr.shutdown()


async def test_upstream_retry_after_immediate_failover_no_sleep(tmp_path, monkeypatch):
    # ADR 0017 (follow-up): when a healthy member is available, a Retry-After is
    # NOT slept — the broker fails over immediately and benches the throttled one.
    mgr = _mgr(tmp_path, cooldown_failures=1)
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    def raiser(a, n):
        if a == "node-a":
            return ConcurrencyLimitError("slow down", model=a, headers={"Retry-After": "2"})
        return None

    _instrument(mgr, raiser)
    await mgr.state.sticky_set("chat", "s", "node-a")
    out = await mgr.chat("chat", _msg(), "r0", route_key="s")
    assert out["choices"][0]["message"]["content"] == "echo: node-b"
    assert slept == []  # immediate failover, no sleep
    # The throttled member is benched for (clamped) Retry-After, not the client's.
    assert (await mgr.state.get_cooldown("node-a")).in_cooldown
    await mgr.shutdown()


async def test_upstream_retry_after_sleeps_only_when_no_other_member(tmp_path, monkeypatch):
    # With no healthy member available, a sleep-retryable backpressure waits the
    # upstream Retry-After (clamped) before retrying — bounded, never unbounded.
    mgr = _mgr(
        tmp_path,
        retry_on_backpressure=True,
        retry_max_attempts=3,
        request_timeout_seconds=300,
    )
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    # Both members 429 with Retry-After: 2 → one failover pass (no sleep), then a
    # sleep-retry round that waits the advertised 2s.
    _instrument(
        mgr,
        lambda a, n: ConcurrencyLimitError("slow down", model=a, headers={"Retry-After": "2"}),
    )
    with pytest.raises(ConcurrencyLimitError):
        await mgr.chat("chat", _msg(), "r0")
    assert slept  # at least one sleep happened
    assert all(d == pytest.approx(2.0) for d in slept)  # each clamped to Retry-After
    await mgr.shutdown()


async def test_per_class_attempt_cap_stops_retrying(tmp_path, monkeypatch):
    # timeout capped at 2 attempts even though the global budget is higher.
    mgr = _mgr(
        tmp_path,
        retry_max_attempts=10,
        retry_max_attempts_by_class={"timeout": 2},
    )
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    calls = _instrument(mgr, lambda a, n: TimeoutError_("to", model=a))  # all time out
    with pytest.raises(TimeoutError_):
        await mgr.chat("chat", _msg(), "r0")
    assert calls["node-a"] + calls["node-b"] == 2  # stopped at the per-class cap
    await mgr.shutdown()
