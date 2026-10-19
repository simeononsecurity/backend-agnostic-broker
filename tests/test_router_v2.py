"""ADR 0019 — router v2: TTFT estimation, calibrated reliability scorer, buffered
streaming cascade, and the improved code/math heuristic with a frozen held-out
split. Complements tests/test_router_cascade.py / _latency.py / _eval.py.
"""

from __future__ import annotations

import json
import sqlite3
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import scripts.router_eval as router_eval
from broker.app import create_app
from broker.config import Settings
from broker.manager import ModelManager, _result_to_sse_chunks
from broker.registry import CascadeConfig, ScorerConfig, load_registry
from broker.router import CallableRouterBackend, RequestFlags, reliability_score, should_escalate
from broker.usage import UsageLog

DATA = Path(__file__).resolve().parent / "data"
TRAIN = DATA / "router_eval.jsonl"
HELDOUT = DATA / "router_eval_heldout.jsonl"


# =====================================================================
# TTFT estimation + usage schema migration
# =====================================================================


def test_new_usage_db_has_ttft_column(tmp_path):
    u = UsageLog(str(tmp_path / "usage.sqlite3"))
    cols = {r[1] for r in u._conn.execute("PRAGMA table_info(usage)").fetchall()}
    assert "ttft_ms" in cols
    u.close()


def test_usage_records_and_summarizes_ttft():
    u = UsageLog(":memory:")
    for _ in range(10):
        u.record(endpoint="chat_stream", model="m", latency_ms=900.0, ttft_ms=120.0, stream=True)
    ttft = u.latency_percentiles(metric="ttft")
    assert ttft["m"]["p90"] == pytest.approx(120.0)
    e2e = u.latency_percentiles(metric="latency")
    assert e2e["m"]["p90"] == pytest.approx(900.0)
    u.close()


def test_ttft_metric_excludes_nonstream_rows():
    u = UsageLog(":memory:")
    # Non-streaming rows carry NULL ttft_ms and must not appear in the ttft metric.
    for _ in range(5):
        u.record(endpoint="chat", model="m", latency_ms=300.0, stream=False)
    assert u.latency_percentiles(metric="ttft") == {}  # no streamed samples yet
    assert "m" in u.latency_percentiles(metric="latency")  # but end-to-end exists
    u.close()


def test_usage_migrates_old_schema_db(tmp_path):
    """An existing usage.sqlite3 written by a pre-ADR-0019 broker (no ttft_ms) is
    migrated in place: the column is added, old rows survive, new writes work."""
    path = tmp_path / "old_usage.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE usage (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, "
        "request_id TEXT, endpoint TEXT NOT NULL, requested TEXT, model TEXT, api_key_id TEXT, "
        "prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, "
        "total_tokens INTEGER DEFAULT 0, latency_ms REAL, status TEXT, stream INTEGER DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO usage (ts, endpoint, model, latency_ms, status) VALUES (1.0,'chat','old',50,'ok')"
    )
    conn.commit()
    conn.close()

    u = UsageLog(str(path))  # opening runs the migration
    cols = {r[1] for r in u._conn.execute("PRAGMA table_info(usage)").fetchall()}
    assert "ttft_ms" in cols
    # Old row is intact and carries NULL ttft; a new streamed row records ttft.
    assert u.summary()["requests"] == 1
    u.record(endpoint="chat_stream", model="new", latency_ms=200.0, ttft_ms=42.0, stream=True)
    assert u.latency_percentiles(metric="ttft")["new"]["p90"] == pytest.approx(42.0)
    u.close()


# =====================================================================
# Latency budget uses TTFT for streaming, end-to-end otherwise
# =====================================================================

_LAT_REGISTRY = """
models:
  - alias: fastttft
    backend: fake
    runtime_model_id: a
    tags: [general]
    cost_tier: 2
  - alias: faste2e
    backend: fake
    runtime_model_id: b
    tags: [general]
    cost_tier: 3
"""


def _lat_manager(tmp_path):
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(_LAT_REGISTRY).strip())
    s = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        router_backend="heuristic",
        router_cold_start_ms=0.0,
    )
    mgr = ModelManager(load_registry(s.registry_path, {"fake"}), s)
    mgr.validate_all()
    mgr.usage = UsageLog(":memory:")
    # fastttft: quick first token but slow overall; faste2e: the opposite.
    for _ in range(10):
        mgr.usage.record(
            endpoint="chat_stream", model="fastttft", latency_ms=5000.0, ttft_ms=100.0, stream=True
        )
        mgr.usage.record(
            endpoint="chat_stream", model="faste2e", latency_ms=200.0, ttft_ms=5000.0, stream=True
        )
    return mgr


def test_latency_budget_uses_ttft_for_stream(tmp_path):
    mgr = _lat_manager(tmp_path)
    mgr.router.backend = CallableRouterBackend(lambda f, m, c: "fastttft")
    # Streaming: judged on TTFT. fastttft (100ms) is within budget -> kept.
    assert (
        mgr.router.route(
            {"messages": [{"role": "user", "content": "hi"}]}, latency_budget_ms=1000.0, stream=True
        ).chosen
        == "fastttft"
    )
    # Non-streaming: judged on end-to-end. fastttft (5000ms) blows the budget, so
    # the router switches to the end-to-end-fast model.
    assert (
        mgr.router.route(
            {"messages": [{"role": "user", "content": "hi"}]},
            latency_budget_ms=1000.0,
            stream=False,
        ).chosen
        == "faste2e"
    )


def test_ttft_estimates_cover_streamed_models(tmp_path):
    mgr = _lat_manager(tmp_path)
    # Both models have streamed samples, so both carry a TTFT estimate the budget
    # can rank; a model without streamed samples would be None (see usage tests).
    cands = mgr.router.build_candidates(RequestFlags())
    ests = mgr.router._latency_estimates(cands, stream=True)
    assert set(ests) == {"fastttft", "faste2e"} and all(v is not None for v in ests.values())


# =====================================================================
# Calibrated reliability scorer (opt-in)
# =====================================================================


def _res(content="ok", finish="stop", logprobs=None):
    ch = {"message": {"role": "assistant", "content": content}, "finish_reason": finish}
    if logprobs is not None:
        ch["logprobs"] = logprobs
    return {"choices": [ch]}


def test_scorer_high_score_keeps_good_answer():
    cfg = CascadeConfig(scorer=ScorerConfig(enabled=True, threshold=0.5))
    good = _res("This is a thorough, complete answer. " * 10)
    score, feats = reliability_score(good, cfg)
    assert score >= 0.9
    assert should_escalate(good, cfg) == (False, "")


def test_scorer_low_score_escalates_short_or_refusal():
    cfg = CascadeConfig(scorer=ScorerConfig(enabled=True, threshold=0.5))
    assert should_escalate(_res(""), cfg)[0] is True  # empty -> length feature 0
    assert should_escalate(_res("I'm sorry, but I cannot help."), cfg)[0] is True  # refusal


def test_scorer_threshold_is_a_tunable_operating_point():
    """One answer; sliding the threshold up monotonically demands more certainty
    and escalates where a lower threshold would keep — the knob the fixed
    booleans cannot express."""
    cfg_lo = CascadeConfig(scorer=ScorerConfig(enabled=True, threshold=0.3))
    cfg_hi = CascadeConfig(scorer=ScorerConfig(enabled=True, threshold=0.95))
    answer = _res("A" * 100)  # mid-length (100/200 target), not a refusal/truncation
    score, _ = reliability_score(answer, cfg_lo)
    assert score == pytest.approx(0.5)
    assert should_escalate(answer, cfg_lo)[0] is False
    assert should_escalate(answer, cfg_hi)[0] is True


def test_scorer_disabled_preserves_boolean_signals():
    # Default (scorer off): a truncated but long answer still escalates on the
    # finish_reason=length boolean signal, exactly as v0.5.0.
    cfg = CascadeConfig()
    assert cfg.scorer.enabled is False
    assert should_escalate(_res("a long clean answer " * 20, finish="length"), cfg)[0] is True


def test_scorer_logprob_feature_and_absence():
    # No logprobs on the response -> the feature is skipped (score from others).
    cfg = CascadeConfig(scorer=ScorerConfig(enabled=True, weight_logprob=1.0))
    _, feats = reliability_score(_res("fine answer " * 10), cfg)
    assert feats["logprob"] is None
    # Low token logprobs (unconfident) pull the score down when weighted.
    lp = {"content": [{"token": "x", "logprob": -5.0}, {"token": "y", "logprob": -5.0}]}
    low = reliability_score(_res("fine answer " * 10, logprobs=lp), cfg)[0]
    hp = {"content": [{"token": "x", "logprob": -0.01}, {"token": "y", "logprob": -0.01}]}
    high = reliability_score(_res("fine answer " * 10, logprobs=hp), cfg)[0]
    assert low < high


def test_scorer_threshold_validation():
    with pytest.raises(ValidationError):
        ScorerConfig(threshold=1.5)


# =====================================================================
# Buffered streaming cascade (never mixes two models' output)
# =====================================================================

_CLEAN = """
router:
  mode: cascade
  cascade: {{stream_mode: {sm}}}
  default: cheap
models:
  - alias: cheap
    backend: fake
    runtime_model_id: c
    tags: [general, cheap]
    cost_tier: 1
  - alias: strong
    backend: fake
    runtime_model_id: s
    tags: [general, reasoning]
    cost_tier: 4
"""

_ESCALATE = """
router:
  mode: cascade
  cascade: {{stream_mode: {sm}}}
  default: tiny
models:
  - alias: tiny
    backend: fake
    runtime_model_id: t
    tags: [general, cheap, truncate]
    cost_tier: 1
  - alias: strong
    backend: fake
    runtime_model_id: s
    tags: [general, reasoning]
    cost_tier: 4
"""


def _settings(tmp_path, body):
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(body).strip())
    return Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        router_backend="heuristic",
    )


def _stream_models(text: str) -> set[str]:
    """Collect the `model` field of every content/data chunk in an SSE body."""
    models = set()
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            continue
        obj = json.loads(payload)
        if obj.get("object") == "chat.completion.chunk":
            models.add(obj.get("model"))
    return models


def test_buffer_cascade_kept_replays_cheap(tmp_path):
    with TestClient(create_app(_settings(tmp_path, _CLEAN.format(sm="buffer")))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hello there"}],
            },
        )
        assert r.status_code == 200
        models = _stream_models(r.text)
        assert models == {"cheap"}  # good cheap answer replayed; strong never runs
        assert "echo: cheap" in r.text


def test_buffer_cascade_escalates_and_streams_strong(tmp_path):
    with TestClient(create_app(_settings(tmp_path, _ESCALATE.format(sm="buffer")))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hello there"}],
            },
        )
        assert r.status_code == 200
        models = _stream_models(r.text)
        # The cheap hop was truncated -> escalate; ONLY the strong model's tokens
        # are emitted. Never a mix of two models in one stream.
        assert models == {"strong"}
        assert "(cascade:buffer)" in r.headers["x-broker-route"]


def test_buffer_cascade_default_is_fallback_single(tmp_path):
    # Without stream_mode: buffer, a streaming cascade falls back to single (v0.5.0).
    with TestClient(create_app(_settings(tmp_path, _ESCALATE.format(sm="fallback_single")))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "stream": True,
                "messages": [{"role": "user", "content": "hello there"}],
            },
        )
        assert r.status_code == 200
        assert "cascade->single: streaming" in r.headers["x-broker-route"]
        assert _stream_models(r.text) == {"strong"}


def test_completions_streaming_cascade_stays_fallback_single(tmp_path):
    # The buffered cascade is chat-only; /v1/completions streaming stays single.
    with TestClient(create_app(_settings(tmp_path, _ESCALATE.format(sm="buffer")))) as c:
        r = c.post(
            "/v1/completions", json={"model": "auto", "stream": True, "prompt": "hello there"}
        )
        assert r.status_code == 200
        assert "cascade->single: streaming" in r.headers["x-broker-route"]


def test_result_to_sse_chunks_is_single_model():
    result = {
        "id": "x",
        "created": 1,
        "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    }
    chunks = _result_to_sse_chunks(result, "cheap")
    bodies = [
        json.loads(ch.decode()[len("data:") :].strip()) for ch in chunks if b"[DONE]" not in ch
    ]
    assert {b["model"] for b in bodies} == {"cheap"}
    assert chunks[-1] == b"data: [DONE]\n\n"


def test_stream_mode_validation():
    with pytest.raises(ValidationError):
        CascadeConfig(stream_mode="bogus")


# =====================================================================
# Heuristic accuracy: pinned thresholds + frozen held-out split
# =====================================================================


def test_heuristic_accuracy_pinned_on_train():
    m = router_eval.run_eval(dataset_path=TRAIN)
    assert m["n"] >= 80
    assert m["accuracy"] >= 0.95
    assert m["by_task"]["code"]["accuracy"] >= 0.90  # was 0.64 before ADR 0019
    assert m["by_task"]["math"]["accuracy"] >= 0.90  # was 0.09 before ADR 0019


def test_heuristic_accuracy_pinned_on_heldout():
    m = router_eval.run_eval(dataset_path=HELDOUT)
    # Held-out (frozen, not tuned): code/math well above the pre-0019 baseline
    # (code 0.50, math 0.00) and no task type below its baseline floor.
    assert m["by_task"]["code"]["accuracy"] >= 0.80
    assert m["by_task"]["math"]["accuracy"] >= 0.80
    for task in ("chat", "vision", "tools", "json", "long-context", "creative", "reasoning"):
        assert m["by_task"][task]["accuracy"] >= 0.90, task


def test_heldout_split_is_frozen_and_disjoint_from_train():
    train = {json.loads(ln)["prompt"] for ln in TRAIN.read_text().splitlines() if ln.strip()}
    held = {json.loads(ln)["prompt"] for ln in HELDOUT.read_text().splitlines() if ln.strip()}
    assert len(held) >= 20
    assert train.isdisjoint(held)  # the held-out split shares no prompt with train
    tasks = {json.loads(ln)["task"] for ln in HELDOUT.read_text().splitlines() if ln.strip()}
    assert {"code", "math"} <= tasks
