"""Streaming tests."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app

AUTH = {"Authorization": "Bearer test-key"}


@pytest.fixture
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_streaming_sse(client):
    with client.stream(
        "POST",
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "model": "echo",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        chunks = []
        done = False
        for line in r.iter_lines():
            if not line:
                continue
            assert line.startswith("data: ")
            payload = line[len("data: ") :]
            if payload == "[DONE]":
                done = True
                break
            obj = json.loads(payload)
            assert obj["object"] == "chat.completion.chunk"
            chunks.append(obj)
    assert done
    assert len(chunks) >= 2
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "fake backend" in text
