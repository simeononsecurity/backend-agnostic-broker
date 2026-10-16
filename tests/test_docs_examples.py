"""Documentation-example tests.

These assert that the request/response examples shown in docs/api-reference.md and
README.md actually work against the real API (fake backend). If the docs drift
from the implementation, this test fails. Run in CI as the "documentation
examples work against the real API" gate.
"""

from __future__ import annotations

import json
import textwrap

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings

KEY = "docs-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def docs_client(tmp_path):
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        textwrap.dedent(
            """
            models:
              - alias: echo-fake
                backend: fake
                runtime_model_id: fake-echo-v1
                idle_timeout_seconds: 60
            """
        ).strip()
    )
    settings = Settings(
        auth_enabled=True,
        api_keys=[KEY],
        registry_path=str(reg),
        enabled_backends=["fake"],
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_readme_list_models_example(docs_client):
    # Documented: GET /v1/models with a Bearer key returns the echo-fake model.
    r = docs_client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert any(m["id"] == "echo-fake" for m in body["data"])


def test_readme_chat_example(docs_client):
    # Documented curl body -> documented response shape.
    body = {
        "model": "echo-fake",
        "messages": [{"role": "user", "content": "Hello!"}],
    }
    r = docs_client.post("/v1/chat/completions", headers=AUTH, json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "echo-fake"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert "usage" in data


def test_readme_streaming_example(docs_client):
    body = {
        "model": "echo-fake",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": True,
    }
    with docs_client.stream("POST", "/v1/chat/completions", headers=AUTH, json=body) as r:
        assert r.status_code == 200
        saw_done = False
        for line in r.iter_lines():
            if line == "data: [DONE]":
                saw_done = True
            elif line.startswith("data: "):
                json.loads(line[6:])
    assert saw_done


def test_readme_health_examples(docs_client):
    assert docs_client.get("/healthz").json()["status"] == "ok"
    assert docs_client.get("/readyz").json()["status"] == "ready"
