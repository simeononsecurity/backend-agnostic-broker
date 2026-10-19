"""Tests for /v1/completions, /v1/responses, and /usage (ADR 0006/0007)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def client(settings):
    settings.usage_log_path = ":memory:"  # ephemeral usage log for the test app
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


# --- /v1/completions ---------------------------------------------------------
def test_completions_nonstream(client):
    r = client.post("/v1/completions", headers=AUTH, json={"model": "echo", "prompt": "hello"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["object"] == "text_completion"
    assert d["model"] == "echo"
    assert d["choices"][0]["text"].startswith("echo:")
    assert d["usage"]["total_tokens"] >= 0


def test_completions_prompt_as_list(client):
    r = client.post("/v1/completions", headers=AUTH, json={"model": "echo", "prompt": ["a", "b"]})
    assert r.status_code == 200, r.text
    assert r.json()["object"] == "text_completion"


def test_completions_stream(client):
    r = client.post(
        "/v1/completions", headers=AUTH, json={"model": "echo", "prompt": "hi", "stream": True}
    )
    assert r.status_code == 200
    body = r.text
    assert "text_completion" in body
    assert "[DONE]" in body
    # the chat envelope must be fully translated away
    assert "chat.completion.chunk" not in body


def test_completions_requires_model(client):
    r = client.post("/v1/completions", headers=AUTH, json={"prompt": "x"})
    assert r.status_code == 400


def test_completions_requires_auth(client):
    r = client.post("/v1/completions", json={"model": "echo", "prompt": "x"})
    assert r.status_code == 401


# --- /v1/responses -----------------------------------------------------------
def test_responses_nonstream(client):
    r = client.post("/v1/responses", headers=AUTH, json={"model": "echo", "input": "hello"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["object"] == "response"
    assert d["status"] == "completed"
    assert d["output_text"].startswith("echo:")
    assert d["output"][0]["content"][0]["type"] == "output_text"
    assert "input_tokens" in d["usage"]


def test_responses_instructions_and_list_input(client):
    r = client.post(
        "/v1/responses",
        headers=AUTH,
        json={
            "model": "echo",
            "instructions": "be brief",
            "input": [{"role": "user", "content": "hi"}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["object"] == "response"


def test_responses_stream(client):
    r = client.post(
        "/v1/responses", headers=AUTH, json={"model": "echo", "input": "hi", "stream": True}
    )
    assert r.status_code == 200
    body = r.text
    assert "response.output_text.delta" in body
    assert "response.completed" in body
    assert "[DONE]" in body


def test_responses_requires_auth(client):
    r = client.post("/v1/responses", json={"model": "echo", "input": "x"})
    assert r.status_code == 401


def test_unknown_model_still_404_no_fallback(client):
    # the translation shims must preserve the no-silent-fallback guarantee
    r = client.post("/v1/completions", headers=AUTH, json={"model": "nope", "prompt": "x"})
    assert r.status_code == 404


# --- /usage ------------------------------------------------------------------
def test_usage_records_across_endpoints(client):
    client.post("/v1/completions", headers=AUTH, json={"model": "echo", "prompt": "hello"})
    client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    client.post("/v1/responses", headers=AUTH, json={"model": "echo", "input": "yo"})
    r = client.get("/usage", headers=AUTH)
    assert r.status_code == 200
    s = r.json()
    assert s["enabled"] is True
    assert s["requests"] >= 3
    endpoints = {e["endpoint"] for e in s["by_endpoint"]}
    assert {"completions", "chat", "responses"} <= endpoints


def test_usage_requires_auth(client):
    assert client.get("/usage").status_code == 401
