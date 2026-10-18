"""API contract tests against the fake backend."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_healthz_no_auth(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readyz(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_list_models(client):
    r = client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()["data"]}
    assert ids == {"echo", "echo2", "auto"}
    m = next(x for x in r.json()["data"] if x["id"] == "echo")
    assert m["object"] == "model" and "capabilities" in m


def test_chat_completion(client):
    r = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model"] == "echo"
    assert data["choices"][0]["message"]["content"].startswith("echo:")
    assert "usage" in data
    assert "x-request-id" in r.headers


def test_unknown_model_404_no_fallback(client):
    r = client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "does-not-exist", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["type"] == "model_not_found"
    assert "remediation" in err


def test_status_shows_lifecycle(client):
    # trigger a load first
    client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
    )
    r = client.get("/status", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    aliases = {m["alias"]: m for m in body["models"]}
    assert aliases["echo"]["state"] in ("ready", "busy")


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "broker_requests_total" in r.text or "broker_loaded_models" in r.text


def test_ollama_tags_compat(client):
    r = client.get("/api/tags", headers=AUTH)
    assert r.status_code == 200
    names = {m["name"] for m in r.json()["models"]}
    assert "echo" in names


def test_request_too_large(client):
    big = "x" * 10
    r = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "content-length": "9999999999"},
        json={"model": "echo", "messages": [{"role": "user", "content": big}]},
    )
    assert r.status_code == 413


def test_invalid_json(client):
    r = client.post(
        "/v1/chat/completions",
        headers={**AUTH, "content-type": "application/json"},
        content=b"{not json",
    )
    assert r.status_code == 400
