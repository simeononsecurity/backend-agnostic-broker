"""Retry / backoff behavior (ADR 0009).

Covers: full-jitter backoff math; the default path is a single failover pass
with no sleep (byte-for-byte v0.4.0); retries stay WITHIN the requested pool and
never touch another model; the attempt cap is respected; a transient failure is
recovered; and a stream is never retried once bytes are flowing.
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path

import pytest

from broker.config import Settings
from broker.errors import BackendUnavailableError
from broker.manager import ModelManager
from broker.registry import load_registry

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
  - alias: node-c
    backend: fake
    runtime_model_id: other-model
    pool: other
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


async def _noop_sleep(_delay):
    """A real (non-recursing) replacement for asyncio.sleep in retry tests."""
    return None


def _instrument(mgr: ModelManager, fail_until: dict[str, int]) -> dict[str, int]:
    """Replace each member's adapter.chat with a counting (optionally failing)
    coroutine. ``fail_until[alias]`` = number of leading calls that raise."""
    calls: dict[str, int] = {a: 0 for a in mgr.models}

    def make(alias: str):
        async def chat(payload, request_id):
            calls[alias] += 1
            if calls[alias] <= fail_until.get(alias, 0):
                raise BackendUnavailableError("boom", backend="fake", model=alias)
            return {
                "model": alias,
                "choices": [{"message": {"role": "assistant", "content": f"echo: {alias}"}}],
                "usage": {},
            }

        return chat

    for alias, mm in mgr.models.items():
        mm.adapter.chat = make(alias)  # type: ignore[method-assign]
    return calls


# --- backoff math -------------------------------------------------------


def test_backoff_zero_by_default(tmp_path):
    mgr = _mgr(tmp_path)
    assert mgr._backoff_delay_s(0) == 0.0
    assert mgr._backoff_delay_s(5) == 0.0


def test_backoff_full_jitter_upper_bound(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, retry_backoff_base_ms=100, retry_backoff_max_ms=2000)
    # Force random.uniform(0, hi) to return hi so we can assert the ceiling.
    monkeypatch.setattr("broker.manager.random.uniform", lambda lo, hi: hi)
    assert mgr._backoff_delay_s(0) == pytest.approx(0.1)  # 100 * 2**0 = 100ms
    assert mgr._backoff_delay_s(3) == pytest.approx(0.8)  # 100 * 2**3 = 800ms
    assert mgr._backoff_delay_s(6) == pytest.approx(2.0)  # 100 * 2**6 capped at 2000ms


# --- default path = legacy ---------------------------------------------


async def test_default_single_pass_no_sleep(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path)
    calls = _instrument(mgr, {"node-a": 99, "node-b": 99})  # both always fail
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(BackendUnavailableError):
        await mgr.chat("chat", _msg(), "r0")
    # One pass over the two pool members, and NO backoff sleep.
    assert calls["node-a"] == 1 and calls["node-b"] == 1
    assert slept == []
    await mgr.shutdown()


# --- retries within the pool -------------------------------------------


async def test_retry_retries_within_pool_with_backoff(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, retry_max_attempts=4, retry_backoff_base_ms=10)
    calls = _instrument(mgr, {"node-a": 99, "node-b": 99})
    slept: list[float] = []

    async def fake_sleep(d):
        slept.append(d)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    with pytest.raises(BackendUnavailableError):
        await mgr.chat("chat", _msg(), "r0")
    # 4 total attempts (members reused after exclusion), 3 backoff sleeps between.
    assert calls["node-a"] + calls["node-b"] == 4
    assert len(slept) == 3 and all(d >= 0 for d in slept)
    await mgr.shutdown()


async def test_retry_never_crosses_pool(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, retry_max_attempts=3)
    calls = _instrument(mgr, {"node-c": 99})  # single-member 'other' pool, always fails
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    with pytest.raises(BackendUnavailableError):
        await mgr.chat("other", _msg(), "r0")
    assert calls["node-c"] == 3  # retried within its own pool
    # The 'chat' pool members were NEVER touched — no silent cross-model retry.
    assert calls["node-a"] == 0 and calls["node-b"] == 0
    await mgr.shutdown()


async def test_retry_recovers_transient_failure(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, retry_max_attempts=3)
    # node-c fails once then succeeds on the retry.
    calls = _instrument(mgr, {"node-c": 1})
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    res = await mgr.chat("other", _msg(), "r0")
    assert res["choices"][0]["message"]["content"] == "echo: node-c"
    assert calls["node-c"] == 2  # one failure + one success
    await mgr.shutdown()


async def test_retry_attempts_cap_respected(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, retry_max_attempts=2)
    calls = _instrument(mgr, {"node-c": 99})
    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    with pytest.raises(BackendUnavailableError):
        await mgr.chat("other", _msg(), "r0")
    assert calls["node-c"] == 2  # exactly the cap
    await mgr.shutdown()


# --- streaming: never retry mid-stream ---------------------------------


async def test_no_retry_once_stream_started(tmp_path):
    mgr = _mgr(tmp_path)
    streamed: dict[str, int] = {a: 0 for a in mgr.models}

    def make_stream(alias: str, fail_mid: bool):
        async def stream_chat(payload, request_id):
            streamed[alias] += 1
            yield b"data: chunk-1\n\n"
            if fail_mid:
                raise BackendUnavailableError("mid-stream boom", backend="fake", model=alias)
            yield b"data: [DONE]\n\n"

        return stream_chat

    # Pin the session to node-a and make it fail AFTER the first chunk.
    await mgr.state.sticky_set("chat", "s", "node-a")
    mgr.models["node-a"].adapter.stream_chat = make_stream("node-a", True)  # type: ignore[method-assign]
    mgr.models["node-b"].adapter.stream_chat = make_stream("node-b", False)  # type: ignore[method-assign]

    chunks: list[bytes] = []
    with pytest.raises(BackendUnavailableError):
        async for c in mgr.stream_chat("chat", _msg(), "r0", route_key="s"):
            chunks.append(c)

    assert chunks == [b"data: chunk-1\n\n"]  # the already-emitted chunk
    assert streamed["node-a"] == 1
    assert streamed["node-b"] == 0  # NEVER failed over after bytes started flowing
    await mgr.shutdown()
