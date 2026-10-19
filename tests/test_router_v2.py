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
from broker.router import (
    CallableRouterBackend,
    RequestFlags,
    code_strength,
    math_strength,
    reasoning_strength,
    reliability_score,
    should_escalate,
    structural_intents,
)
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


def test_streaming_cascade_escalation_emits_only_final_model_no_mixing(tmp_path):
    """End-to-end (ADR 0019 follow-up): on escalation the client stream carries
    ONLY the escalated model's tokens — never the buffered primary's — and the
    x-broker-route header reflects the FINAL served model.

    The cheap `tiny` hop is tagged `truncate`, so its buffered answer trips the
    cascade signal; the broker must discard it and stream `strong` live. We assert
    (1) every streamed chunk is from `strong`, (2) none of the primary's buffered
    content leaks into the stream, and (3) the route header names the final model.
    """
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
        # (1) only the escalated model's tokens appear.
        assert _stream_models(r.text) == {"strong"}
        # (2) the buffered primary's content never leaks into the client stream.
        assert "echo: tiny" not in r.text
        assert '"model": "tiny"' not in r.text and '"model":"tiny"' not in r.text
        # (3) x-broker-route reflects the final (escalated) model.
        route = r.headers["x-broker-route"]
        assert route == "tiny->strong (cascade:buffer)"
        assert route.split("->")[-1].startswith("strong")


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
# Heuristic accuracy: HONEST three-way evaluation (ADR 0019 follow-up)
#
# Three prompt sets, labeled by their independence:
#   TRAIN    — the bundled set the heuristic was tuned against (in-sample).
#   HELDOUT  — author-written in the SAME commit, same vocabulary. NOT an
#              independent measure; it saturates and must be read as such.
#   INDEP    — written by the orchestrator, disjoint vocabulary and phrasing;
#              the only set that measures generalization. Frozen, attributed.
# Keyword heuristics generalize poorly; the honest generalization numbers live
# on INDEP. For code/math/reasoning at scale prefer the LLM router image or the
# calibrated cascade. No "perfect"/1.0 claim is made about generalization.
# =====================================================================

INDEP = DATA / "router_eval_independent.jsonl"

# v0.5.0 per-task accuracy (measured on each set with the v0.5.0 heuristic), the
# floor the ADR 0019 follow-up must not drop below on the protected task types.
_V050_PROTECTED = ("chat", "creative", "json", "tools", "vision", "long-context")


def test_train_is_in_sample_and_not_saturated():
    # TRAIN is in-sample: accuracy is high but MUST stay strictly below the oracle
    # ceiling (PGR < 1.0). The overfit keyword set saturated it to 1.0; the honest
    # structural heuristic does not. This is the same guard the v0.5.0 back-compat
    # suite enforces via test_pgr_not_clamped.
    m = router_eval.run_eval(dataset_path=TRAIN)
    assert m["n"] >= 80
    assert m["accuracy"] < 1.0
    assert m["routellm"]["pgr_router"]["value"] < 1.0
    # Code/math on the in-sample set improved over the v0.5.0 baseline (0.64/0.09).
    assert m["by_task"]["code"]["accuracy"] >= 0.80
    assert m["by_task"]["math"]["accuracy"] >= 0.80


def test_heldout_is_author_written_not_independent():
    # HELDOUT shares author, commit and vocabulary with TRAIN, so a high number
    # here is NOT evidence of generalization. We assert it is high (it is tuned-
    # adjacent) AND document that it is not independent — the real measure is
    # test_independent_set_generalization below.
    m = router_eval.run_eval(dataset_path=HELDOUT)
    assert m["by_task"]["code"]["accuracy"] >= 0.80
    assert m["by_task"]["math"]["accuracy"] >= 0.80


def test_independent_set_generalization():
    """The honest generalization numbers, on the orchestrator's disjoint set.

    These are the acceptance targets: code >= 0.6, math >= 0.5, reasoning >= 0.3,
    and the protected task types (chat/creative) no lower than v0.5.0's 1.0. They
    are deliberately well below the in-sample numbers — keyword+structural
    heuristics generalize only partially, and that gap is the honest story.
    """
    m = router_eval.run_eval(dataset_path=INDEP)
    bt = m["by_task"]
    assert bt["code"]["accuracy"] >= 0.6
    assert bt["math"]["accuracy"] >= 0.5
    assert bt["reasoning"]["accuracy"] >= 0.3
    # Protected task types present in the independent set must match v0.5.0 (1.0).
    assert bt["chat"]["accuracy"] == 1.0
    assert bt["creative"]["accuracy"] == 1.0


def test_independent_set_is_frozen_and_disjoint():
    def prompts(p):
        return {json.loads(ln)["prompt"] for ln in p.read_text().splitlines() if ln.strip()}

    indep = prompts(INDEP)
    assert len(indep) >= 40
    # Disjoint from BOTH train and the author held-out split: no shared prompt.
    assert indep.isdisjoint(prompts(TRAIN))
    assert indep.isdisjoint(prompts(HELDOUT))
    tasks = {json.loads(ln)["task"] for ln in INDEP.read_text().splitlines() if ln.strip()}
    assert {"code", "math", "reasoning", "chat", "creative"} <= tasks


# =====================================================================
# Structural intent features (ADR 0019 follow-up): STRUCTURE, not vocabulary.
# Every prompt below is written fresh here and appears in NO eval dataset.
# =====================================================================


def test_code_structural_signals_fire_on_unseen_prompts():
    # Each prompt carries a DIFFERENT structural class; none shares a code keyword.
    assert code_strength("wrap the handler in a `try/except` block") > 0  # backticks
    assert code_strength("why does foo() return None here") > 0  # () punctuation
    assert code_strength("my service won't start, check main.rs line 42") > 0  # file ext
    assert code_strength("the pod is stuck, what does helm rollback do") > 0  # CLI tool
    assert code_strength("port this snippet from kotlin to swift") > 0  # language names
    assert code_strength("the useMemo hook keeps recomputing") > 0  # camelCase
    assert code_strength("hit a segfault after the second malloc") > 0  # error shape


def test_math_structural_signals_fire_on_unseen_prompts():
    assert math_strength("evaluate 3 * 7 + 2 and show the steps") > 0  # digit+operator
    assert math_strength("reduce the fraction 12/18") > 0  # fraction
    assert math_strength("what is 23 as a percent of 115") > 0  # percent phrasing
    assert math_strength("how many ways to arrange the letters in BANANA") > 0  # counting
    assert math_strength("is 97 a prime number") > 0  # 'prime'
    assert math_strength("solve y = 4x for y when x = 2") > 0  # equation operator


def test_reasoning_structural_signals_fire_on_unseen_prompts():
    assert reasoning_strength("why would a cache stampede happen under load") > 0  # why
    assert reasoning_strength("weigh the trade-offs of SQL vs NoSQL for this") > 0  # trade-off
    assert reasoning_strength("if all A are B and no B are C, what follows") > 0  # syllogism
    assert reasoning_strength("plan the rollout so nothing breaks midway") > 0  # plan
    assert (
        reasoning_strength(
            "there are three switches downstairs and one bulb upstairs, you may "
            "flip switches as much as you like but visit the bulb only once, how"
        )
        > 0
    )  # long multi-constraint puzzle, no cue word


def test_structural_features_do_not_misfire_on_plain_chat():
    # A casual prompt with no code/math/reasoning structure scores zero on all
    # three intents (so it routes to the cheap generalist, as it should).
    for p in ("what's a good name for a houseplant", "tell me about your day"):
        si = structural_intents(p)
        assert si["code"] == 0.0 and si["math"] == 0.0 and si["reasoning"] == 0.0


def test_structural_routes_unseen_code_math_without_keywords():
    # End-to-end through the eval registry: natural prompts sharing no keyword with
    # the tag lists still reach a capable model, purely on structure.
    mgr = router_eval._build_manager(None, "heuristic")
    router = mgr.router

    def route(text):
        return router.route({"messages": [{"role": "user", "content": text}]}).chosen

    assert route("the deploy fails with CrashLoopBackOff on every pod") in {"coder", "reasoner"}
    assert route("simplify (x^2 - 9)/(x - 3)") in {"coder", "reasoner"}
    assert route("why do we get a deadlock when both locks are taken") in {"coder", "reasoner"}


# =====================================================================
# Cost-normalization default: reverted to v0.5.0 (opt-in), ADR 0019 follow-up.
# =====================================================================


def test_cost_normalized_default_is_v050_unnormalized():
    from broker.registry import RouterWeights

    # The ADR 0019 normalized-cost term made NO difference on the independent set,
    # so its default was reverted to v0.5.0's un-normalized behavior and gated.
    assert RouterWeights().cost_normalized is False


def test_cost_normalized_is_opt_in_and_changes_scoring():
    from broker.registry import RouterWeights
    from broker.router import Candidate, HeuristicRouterBackend, RequestFlags

    h = HeuristicRouterBackend()
    flags = RequestFlags()
    cheap = Candidate(
        id="tiny",
        tags=("general",),
        description="",
        streaming=True,
        tools=False,
        vision=False,
        context_limit=8000,
        loaded=False,
        priority=0,
        cost_tier=1,
    )
    # On a plain prompt (no capability match) the only difference is the cost term:
    # un-normalized gives (5-1)=4·cost, normalized gives (5-1)/4=1·cost.
    msg = "hello there"
    s_unnorm = h.score(flags, msg, cheap, RouterWeights(cost_normalized=False))
    s_norm = h.score(flags, msg, cheap, RouterWeights(cost_normalized=True))
    assert s_unnorm == 4.0
    assert s_norm == 1.0
