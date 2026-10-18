"""Auto-router: flag extraction, Stage-1 filter, overrides, heuristic choice,
fallback, caching, and end-to-end routing through the manager + API."""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.manager import ModelManager
from broker.registry import RouterConfig, load_registry
from broker.router import (
    CallableRouterBackend,
    Candidate,
    HeuristicRouterBackend,
    RequestFlags,
    _gbnf_grammar,
    _render_candidates,
    extract_flags,
)

# A varied catalogue: a coder, a vision model, a cheap generalist, a long-context
# model, and a tool/json specialist — plus a data-driven router policy.
AUTO_REGISTRY = """
router:
  default: tiny
  overrides:
    - when: { regex: "(?i)translate" }
      use: tiny
    - when: { has_images: true }
      use: tiny          # deliberately incapable -> must be SKIPPED (no vision)
  weights: { capability: 3.0, cost: 1.0, prefer_loaded: 1.0, priority: 1.0 }
models:
  - alias: coder
    backend: fake
    runtime_model_id: c
    tags: [code]
    supports_tools: true
    cost_tier: 3
  - alias: vizard
    backend: fake
    runtime_model_id: v
    tags: [vision]
    supports_vision: true
    cost_tier: 4
  - alias: tiny
    backend: fake
    runtime_model_id: t
    tags: [cheap, fast, chat, general]
    cost_tier: 1
  - alias: longboi
    backend: fake
    runtime_model_id: l
    tags: [long-context]
    context_limit: 200000
    cost_tier: 4
  - alias: tooly
    backend: fake
    runtime_model_id: to
    tags: [tools, json]
    supports_tools: true
    cost_tier: 2
"""


def _settings(tmp_path: Path, body: str = AUTO_REGISTRY, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(body).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        model_idle_timeout_seconds=100,
        idle_sweep_interval_seconds=0.2,
        health_probe_enabled=False,
        router_backend="heuristic",  # deterministic; no llama_cpp in CI
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _manager(tmp_path: Path, body: str = AUTO_REGISTRY, **over) -> ModelManager:
    s = _settings(tmp_path, body, **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    return m


def _msg(text: str, **extra) -> dict:
    return {"messages": [{"role": "user", "content": text}], **extra}


# --- flag extraction ----------------------------------------------------------


def test_extract_flags_plain_text():
    flags, msg = extract_flags(_msg("hello world"))
    assert flags == RequestFlags(False, False, False, flags.approx_ctx_tokens)
    assert msg == "hello world"


def test_extract_flags_images():
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this?"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
    }
    flags, msg = extract_flags(payload)
    assert flags.has_images is True
    assert "what is this?" in msg


def test_extract_flags_tools_and_json():
    f1, _ = extract_flags(_msg("x", tools=[{"type": "function", "function": {"name": "f"}}]))
    assert f1.has_tools is True
    f2, _ = extract_flags(_msg("x", response_format={"type": "json_object"}))
    assert f2.needs_json is True


def test_extract_flags_approx_tokens_counts_output_budget():
    f, _ = extract_flags(_msg("word " * 100, max_tokens=5000))
    assert f.approx_ctx_tokens >= 5000


# --- Stage-1 capability filter ------------------------------------------------


def test_stage1_filters_to_vision_for_images(tmp_path):
    r = _manager(tmp_path).router
    flags = RequestFlags(has_images=True)
    ids = {c.id for c in r.build_candidates(flags)}
    assert ids == {"vizard"}  # only the vision model survives


def test_stage1_filters_to_tool_capable(tmp_path):
    r = _manager(tmp_path).router
    ids = {c.id for c in r.build_candidates(RequestFlags(has_tools=True))}
    assert ids == {"coder", "tooly"}  # only tools-capable models


def test_stage1_filters_by_context_window(tmp_path):
    r = _manager(tmp_path).router
    ids = {c.id for c in r.build_candidates(RequestFlags(approx_ctx_tokens=50000))}
    assert ids == {"longboi"}  # only the 200k-ctx model fits


def test_router_eligible_false_excluded(tmp_path):
    body = AUTO_REGISTRY.replace(
        "    tags: [code]\n", "    tags: [code]\n    router_eligible: false\n"
    )
    r = _manager(tmp_path, body).router
    ids = {c.id for c in r.build_candidates(RequestFlags())}
    assert "coder" not in ids


# --- heuristic: simulate tasks -> correct model -------------------------------


def _route(mgr, text, **extra):
    return mgr.router.route(_msg(text, **extra))


def test_route_code_task_to_coder(tmp_path):
    d = _route(_manager(tmp_path), "write a python function to sort a list")
    assert d.chosen == "coder" and d.via == "heuristic"


def test_route_vision_task_to_vision_model(tmp_path):
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            }
        ]
    }
    d = _manager(tmp_path).router.route(payload)
    assert d.chosen == "vizard"


def test_route_simple_task_to_cheapest(tmp_path):
    d = _route(_manager(tmp_path), "hello, how are you today?")
    assert d.chosen == "tiny"  # no intent match -> cheapest capable


def test_route_long_context_to_long_model(tmp_path):
    d = _route(_manager(tmp_path), "summarize this", max_tokens=50000)
    assert d.chosen == "longboi"


def test_route_tool_task_to_tool_model(tmp_path):
    d = _route(
        _manager(tmp_path),
        "call the weather tool for Paris",
        tools=[{"type": "function", "function": {"name": "weather"}}],
    )
    assert d.chosen == "tooly"


def test_route_json_task_to_json_model(tmp_path):
    d = _route(
        _manager(tmp_path), "return the result as json", response_format={"type": "json_object"}
    )
    assert d.chosen == "tooly"


# --- heuristic scorer unit (priority / loaded tie-breaks) ---------------------


def _cand(id, **kw):
    base = dict(
        tags=(),
        description="",
        streaming=True,
        tools=False,
        vision=False,
        context_limit=8192,
        loaded=False,
        priority=0,
        cost_tier=2,
    )
    base.update(kw)
    return Candidate(id=id, **base)


def test_heuristic_priority_breaks_ties():
    h = HeuristicRouterBackend()
    cands = [_cand("a", priority=0), _cand("b", priority=5)]
    assert h.choose(RequestFlags(), "hi", cands, RouterConfig()) == "b"


def test_heuristic_prefers_loaded_on_tie():
    h = HeuristicRouterBackend()
    cands = [_cand("cold", loaded=False), _cand("warm", loaded=True)]
    assert h.choose(RequestFlags(), "hi", cands, RouterConfig()) == "warm"


def test_heuristic_prefers_cheaper_on_tie():
    h = HeuristicRouterBackend()
    cands = [_cand("pricey", cost_tier=5), _cand("cheap", cost_tier=1)]
    assert h.choose(RequestFlags(), "hi", cands, RouterConfig()) == "cheap"


# --- overrides ----------------------------------------------------------------


def test_override_regex_bypasses_classifier(tmp_path):
    d = _route(_manager(tmp_path), "please translate this to French")
    assert d.chosen == "tiny" and d.via == "override"


def test_override_skipped_when_target_not_capable(tmp_path):
    # has_images override -> tiny, but tiny has no vision so it's not a candidate;
    # the override is skipped and the classifier picks the vision model.
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "x"}},
                ],
            }
        ]
    }
    d = _manager(tmp_path).router.route(payload)
    assert d.chosen == "vizard" and d.via != "override"


# --- fallback -----------------------------------------------------------------


def test_invalid_choice_falls_back_to_default(tmp_path):
    mgr = _manager(tmp_path)
    mgr.router.backend = CallableRouterBackend(lambda f, m, c: "does-not-exist")
    d = _route(mgr, "anything")
    assert d.chosen == "tiny" and d.via == "fallback"  # registry router.default


def test_backend_exception_falls_back(tmp_path):
    mgr = _manager(tmp_path)

    def boom(f, m, c):
        raise RuntimeError("model crashed")

    mgr.router.backend = CallableRouterBackend(boom)
    d = _route(mgr, "anything")
    assert d.chosen in {"tiny"} and d.via == "fallback"


# --- caching ------------------------------------------------------------------


def test_decisions_are_cached(tmp_path):
    mgr = _manager(tmp_path)
    d1 = _route(mgr, "write a python function")
    d2 = _route(mgr, "write a python function")
    assert d1.chosen == d2.chosen == "coder"
    assert d2.via == "cache"


# --- grammar + prompt builders (no model needed) ------------------------------


def test_gbnf_grammar_restricts_to_ids():
    g = _gbnf_grammar(["coder", "tiny"])
    assert g.strip() == 'root ::= ("coder" | "tiny")'


def test_render_candidates_has_rows():
    rows = _render_candidates([_cand("coder", tags=("code",), tools=True, cost_tier=3)])
    assert "coder" in rows and "code" in rows and "cost: 3" in rows


# --- end-to-end through the manager + API ------------------------------------


async def test_manager_chat_auto_routes_and_relabels(tmp_path):
    mgr = _manager(tmp_path)
    mgr.router.backend = CallableRouterBackend(lambda f, m, c: "coder")
    out = await mgr.chat("auto", _msg("hello"), "r1")
    assert out["model"] == "coder"
    assert out["choices"][0]["message"]["content"] == "echo: coder"
    await mgr.shutdown()


def test_api_auto_in_models_and_routes(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        models = {m["id"]: m for m in c.get("/v1/models").json()["data"]}
        assert "auto" in models and models["auto"]["backend"] == "router"

        # A code task sent to "auto" should be served by the coder model.
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [{"role": "user", "content": "debug my python code"}],
            },
        )
        assert r.status_code == 200
        assert r.json()["model"] == "coder"

        st = c.get("/status").json()
        assert st["router"]["enabled"] is True
        assert st["router"]["backend"] == "heuristic"
        assert any(dec["chosen"] == "coder" for dec in st["router"]["recent_decisions"])


def test_api_auto_disabled_is_not_a_model(tmp_path):
    with TestClient(create_app(_settings(tmp_path, router_backend="off"))) as c:
        assert "auto" not in {m["id"] for m in c.get("/v1/models").json()["data"]}
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 404


def test_auto_no_capable_model_errors(tmp_path):
    # Registry with only a text model; an image request has no capable candidate.
    body = """
    models:
      - alias: tiny
        backend: fake
        runtime_model_id: t
        tags: [general]
    """
    with TestClient(create_app(_settings(tmp_path, body))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
                ],
            },
        )
        assert r.status_code == 400  # ValidationError -> no capable model


def test_user_priority_override_wins(tmp_path):
    # Bump tiny's priority so it wins a no-intent tie even against equals — proves
    # the per-model priority knob is honored for "auto".
    body = AUTO_REGISTRY.replace(
        "    tags: [cheap, fast, chat, general]\n    cost_tier: 1",
        "    tags: [cheap, fast, chat, general]\n    cost_tier: 3\n    priority: 10",
    )
    d = _route(_manager(tmp_path, body), "hello there")
    assert d.chosen == "tiny"  # priority 10 overcomes its now-higher cost_tier
