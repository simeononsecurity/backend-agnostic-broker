"""Streaming cache-replay tests (ADR 0014): non-stream populates, stream replays."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings


def _settings(fake_registry_file, tmp_models_root, **over) -> Settings:
    base = dict(
        auth_enabled=True,
        api_keys=["key-a"],
        registry_path=str(fake_registry_file),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_models_root)],
        model_idle_timeout_seconds=60,
        idle_sweep_interval_seconds=5,
        max_loaded_models=2,
        response_cache_enabled=True,
    )
    base.update(over)
    return Settings(**base)


@pytest.fixture
def client(fake_registry_file, tmp_models_root):
    app = create_app(_settings(fake_registry_file, tmp_models_root))
    with TestClient(app) as c:
        yield c


H = {"Authorization": "Bearer key-a"}
CHAT = {"model": "echo", "messages": [{"role": "user", "content": "replay me"}], "temperature": 0}


def _collect_sse(client, path, body):
    events = []
    done = False
    with client.stream("POST", path, headers=H, json={**body, "stream": True}) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        cache_state = r.headers.get("x-broker-cache")
        for line in r.iter_lines():
            if not line:
                continue
            assert line.startswith("data: ")
            data = line[len("data: ") :]
            if data == "[DONE]":
                done = True
                continue
            events.append(json.loads(data))
    assert done, "stream must end with [DONE]"
    return cache_state, events


def test_chat_stream_replays_from_nonstream(client):
    # non-streaming request populates the cache
    r1 = client.post("/v1/chat/completions", headers=H, json=CHAT)
    assert r1.headers["x-broker-cache"] == "miss"
    body_text = r1.json()["choices"][0]["message"]["content"]
    # a subsequent streaming request replays it as SSE
    state, events = _collect_sse(client, "/v1/chat/completions", CHAT)
    assert state == "hit"
    assert all(e["object"] == "chat.completion.chunk" for e in events)
    streamed = "".join(e["choices"][0]["delta"].get("content", "") for e in events)
    assert streamed == body_text


def test_completions_stream_replays_as_text_completion(client):
    body = {"model": "echo", "prompt": "hi there", "temperature": 0}
    r1 = client.post("/v1/completions", headers=H, json=body)
    assert r1.headers["x-broker-cache"] == "miss"
    expected = r1.json()["choices"][0]["text"]
    state, events = _collect_sse(client, "/v1/completions", body)
    assert state == "hit"
    assert all(e["object"] == "text_completion" for e in events)
    streamed = "".join(e["choices"][0].get("text", "") for e in events)
    assert streamed == expected


def test_responses_stream_replays_as_response_events(client):
    body = {"model": "echo", "input": "say hi", "temperature": 0}
    r1 = client.post("/v1/responses", headers=H, json=body)
    assert r1.headers["x-broker-cache"] == "miss"
    expected = r1.json()["output_text"]
    state, events = _collect_sse(client, "/v1/responses", body)
    assert state == "hit"
    types = {e.get("type") for e in events}
    assert "response.output_text.delta" in types
    assert "response.completed" in types
    final = next(e for e in events if e.get("type") == "response.completed")
    assert final["response"]["output_text"] == expected


def test_streaming_miss_does_not_store(client):
    # a streaming request with no prior non-stream population is a miss and stores nothing
    fresh = {
        "model": "echo",
        "messages": [{"role": "user", "content": "never stored"}],
        "temperature": 0,
    }
    state, _ = _collect_sse(client, "/v1/chat/completions", fresh)
    assert state == "miss"
    assert client.app.state.response_cache.stores == 0
    # and a second streaming request is still a miss (streams never populate)
    state2, _ = _collect_sse(client, "/v1/chat/completions", fresh)
    assert state2 == "miss"


def test_semantic_stream_replay(fake_registry_file, tmp_models_root):
    s = _settings(
        fake_registry_file,
        tmp_models_root,
        response_cache_enabled=False,
        semantic_cache_enabled=True,
        semantic_cache_model="echo2",
        semantic_cache_require_resident=False,
    )
    app = create_app(s)
    with TestClient(app) as c:
        # non-stream populates the semantic bucket
        r1 = c.post("/v1/chat/completions", headers=H, json=CHAT)
        assert r1.headers["x-broker-cache"] == "miss"
        state, events = _collect_sse(c, "/v1/chat/completions", CHAT)
        assert state == "semantic-hit"
        assert all(e["object"] == "chat.completion.chunk" for e in events)
