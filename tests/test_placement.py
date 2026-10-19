"""Placement / data-residency pinning (ADR 0011), fail-closed.

Covers auto-route filtering, explicit-model and pool denial (403 placement_denied),
pool member filtering, header parsing/validation, and the config default.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.errors import PlacementDeniedError
from broker.manager import ModelManager
from broker.registry import load_registry

# local-small carries {local, trusted}; cloud-big carries {region:us}. An image
# model with no labels is used to show unlabeled models satisfy only no-constraint.
PLACEMENT_REGISTRY = """
router:
  default: local-small
models:
  - alias: local-small
    backend: fake
    runtime_model_id: ls
    tags: [general, cheap]
    cost_tier: 1
    placement: [local, trusted]
  - alias: cloud-big
    backend: fake
    runtime_model_id: cb
    tags: [general, reasoning]
    cost_tier: 4
    placement: ["region:us"]
"""

POOL_REGISTRY = """
models:
  - alias: pool-local
    backend: fake
    runtime_model_id: pl
    pool: chat
    tags: [general]
    placement: [local]
  - alias: pool-cloud
    backend: fake
    runtime_model_id: pc
    pool: chat
    tags: [general]
    placement: ["region:us"]
"""


def _settings(tmp_path: Path, body: str = PLACEMENT_REGISTRY, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(body).strip())
    kwargs = dict(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        router_backend="heuristic",
    )
    kwargs.update(over)
    return Settings(**kwargs)


def _manager(tmp_path: Path, body: str = PLACEMENT_REGISTRY, **over) -> ModelManager:
    s = _settings(tmp_path, body, **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    return m


def _msg() -> dict:
    return {"messages": [{"role": "user", "content": "hello"}]}


# --- auto routing -------------------------------------------------------------


def test_auto_placement_filters_candidates(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", **_msg()},
            headers={"x-broker-placement": "local"},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "local-small"  # only the local model qualifies


def test_auto_placement_denied_when_none_qualify(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", **_msg()},
            headers={"x-broker-placement": "region:eu"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "placement_denied"


# --- explicit model -----------------------------------------------------------


def test_explicit_model_placement_allowed(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "local-small", **_msg()},
            headers={"x-broker-placement": "local"},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "local-small"


def test_explicit_model_placement_denied(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "cloud-big", **_msg()},
            headers={"x-broker-placement": "local"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "placement_denied"


def test_embeddings_placement_denied(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/embeddings",
            json={"model": "cloud-big", "input": "hi"},
            headers={"x-broker-placement": "local"},
        )
        assert r.status_code == 403
        assert r.json()["error"]["type"] == "placement_denied"


# --- pools --------------------------------------------------------------------


def test_pool_filters_to_satisfying_members(tmp_path):
    mgr = _manager(tmp_path, POOL_REGISTRY)
    members = mgr._members_for("chat", frozenset({"local"}))
    assert [m.entry.alias for m in members] == ["pool-local"]


def test_pool_denied_when_no_member_qualifies(tmp_path):
    mgr = _manager(tmp_path, POOL_REGISTRY)
    with pytest.raises(PlacementDeniedError):
        mgr._members_for("chat", frozenset({"region:eu"}))


# --- header parsing / validation ---------------------------------------------


def test_placement_header_invalid_label_rejected(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", **_msg()},
            headers={"x-broker-placement": "bad label!"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["type"] == "invalid_request_error"


def test_placement_header_multi_label_and_intersection(tmp_path):
    # Requiring both local AND trusted still matches local-small (carries both).
    with TestClient(create_app(_settings(tmp_path))) as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", **_msg()},
            headers={"x-broker-placement": "local, trusted"},
        )
        assert r.status_code == 200
        assert r.json()["model"] == "local-small"


def test_default_placement_setting_applies_without_header(tmp_path):
    settings = _settings(tmp_path, default_placement=["local"])
    with TestClient(create_app(settings)) as c:
        # No header, but the config default pins to local: cloud-big is denied.
        r = c.post("/v1/chat/completions", json={"model": "cloud-big", **_msg()})
        assert r.status_code == 403
        # ...and auto still serves the local model.
        r2 = c.post("/v1/chat/completions", json={"model": "auto", **_msg()})
        assert r2.status_code == 200
        assert r2.json()["model"] == "local-small"
