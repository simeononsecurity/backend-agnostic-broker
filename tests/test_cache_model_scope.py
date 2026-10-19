"""GAP 3 (ADR 0014 follow-up): the cache keys on the model resolved BEFORE serving.

A cascade may escalate to a different model than ``resolve_public`` picked. The
served answer must never be stored under the primary model's key, or a later
request resolving to the primary would be served an answer from a different
model. These tests exercise both the escalation (skip) and the clean (store) path.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings

# tiny carries a shaping tag that trips a cascade signal → escalates to strong.
ESCALATE_REGISTRY = """
router:
  mode: cascade
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

# cheap answers cleanly → cascade stays on the primary (served == resolved).
CLEAN_REGISTRY = """
router:
  mode: cascade
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

CHAT = {"model": "auto", "messages": [{"role": "user", "content": "hello there"}], "temperature": 0}


def _settings(tmp_path: Path, body: str, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(body).strip())
    base = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        router_backend="heuristic",
        response_cache_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def test_no_store_when_cascade_escalates_to_other_model(tmp_path):
    app = create_app(_settings(tmp_path, ESCALATE_REGISTRY))
    with TestClient(app) as c:
        r1 = c.post("/v1/chat/completions", json=CHAT)
        assert r1.status_code == 200
        assert r1.json()["model"] == "strong"  # escalated away from the keyed primary
        assert r1.headers["x-broker-cache"] == "miss"
        # Not stored under the primary's key → a second identical request still misses.
        r2 = c.post("/v1/chat/completions", json=CHAT)
        assert r2.headers["x-broker-cache"] == "miss"
        assert r2.json()["model"] == "strong"
        stats = app.state.response_cache.stats()
        assert stats["skipped_model_mismatch"] >= 1
        assert stats["stores"] == 0


def test_store_when_cascade_stays_on_primary(tmp_path):
    app = create_app(_settings(tmp_path, CLEAN_REGISTRY))
    with TestClient(app) as c:
        r1 = c.post("/v1/chat/completions", json=CHAT)
        assert r1.json()["model"] == "cheap"  # served model == resolved model
        assert r1.headers["x-broker-cache"] == "miss"
        # served == keyed → stored → a second identical request hits.
        r2 = c.post("/v1/chat/completions", json=CHAT)
        assert r2.headers["x-broker-cache"] == "hit"
        stats = app.state.response_cache.stats()
        assert stats["stores"] == 1
        assert stats["skipped_model_mismatch"] == 0
