"""The examples/auto-router/ registry loads and actually routes (ADR 0019).

This is the "runnable example is tested" gate for the auto-router example: the
registry validates (every router feature parses) and real requests route through
the fake backend, reading the x-broker-route header exactly as the README shows.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.registry import load_registry

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "auto-router" / "registry.yaml"


def test_example_registry_loads_with_all_router_features():
    reg = load_registry(str(EXAMPLE), {"fake"})
    r = reg.router
    assert r.mode == "cascade"
    assert r.cascade.scorer.enabled is True
    assert r.cascade.stream_mode == "buffer"
    assert r.latency_budget_ms == 4000
    # Placement labels + cost tiers parsed on the models.
    by = reg.by_alias()
    assert by["coder"].cost_tier == 3 and "code" in by["coder"].tags
    assert by["tiny"].placement == ["local"]


def _client():
    s = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(EXAMPLE),
        enabled_backends=["fake"],
        health_probe_enabled=False,
        router_backend="heuristic",
    )
    return TestClient(create_app(s))


def test_example_routes_code_prompt_and_sets_route_header():
    with _client() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [
                    {"role": "user", "content": "write a python function to reverse a linked list"}
                ],
            },
        )
        assert r.status_code == 200
        assert "x-broker-route" in r.headers
        # Cascade: the cheap hop's short fake answer trips the scorer, so the
        # request escalates to the code model; the route header shows the path.
        assert r.json()["model"] == "coder"
        assert "->" in r.headers["x-broker-route"]


def test_example_image_override_routes_to_vision_model():
    with _client() as c:
        r = c.post(
            "/v1/chat/completions",
            json={
                "model": "auto",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "what is in this image?"},
                            {"type": "image_url", "image_url": {"url": "x"}},
                        ],
                    }
                ],
            },
        )
        assert r.status_code == 200
        assert r.json()["model"] == "vizard"  # has_images override -> vision model
