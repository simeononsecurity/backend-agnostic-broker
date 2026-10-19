"""Strict validation of upstream rerank responses (ADR 0013 follow-up).

A rerank-capable runtime (vLLM / llama.cpp) is proxied through
``HTTPBackendAdapter.rerank``. A *malformed* upstream body — an out-of-range,
negative, duplicate, non-int, or bool index; a non-numeric or non-finite score;
or a non-JSON body — is a backend contract violation, not a broker bug. The
broker must surface it as ``BackendUnavailableError`` (HTTP 502) so it (a) never
silently passes through or coerces a bad result, (b) is never written to the
response cache, and (c) participates in pool failover/cooldown like any other
backend error. The same non-JSON guard protects the chat/embeddings paths.

These tests drive the real adapter/app against a tiny in-process HTTP upstream
(loopback, allowed via ``BROKER_ALLOW_LOOPBACK_BACKENDS``) — no real runtime.
"""

from __future__ import annotations

import json
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from broker.adapters.vllm import VLLMAdapter
from broker.app import create_app
from broker.config import Settings
from broker.errors import BackendUnavailableError
from broker.registry import BackendType, ModelEntry

DOCS = ["a", "b", "c"]


class _Upstream:
    """A minimal mock OpenAI/Cohere-compatible upstream with a settable body.

    ``GET`` (readiness/health) always answers 200 JSON. ``POST`` echoes the
    currently-configured ``response`` (a dict → JSON-encoded, or raw ``bytes``
    for non-JSON / NaN / Infinity bodies). ``status`` is configurable so a
    member can also return an HTTP error.
    """

    def __init__(self) -> None:
        self.response: Any = {"results": []}
        self.status = 200
        handler = self._make_handler()
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _make_handler(self):
        parent = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                self.rfile.read(n)
                body = parent.response
                self.send_response(parent.status)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        return H

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def upstream():
    u = _Upstream()
    try:
        yield u
    finally:
        u.close()


def _settings(tmp_path: Path, registry: str) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(registry).strip())
    return Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["vllm"],
        allow_loopback_backends=True,
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        usage_log_path=":memory:",
    )


def _vllm_adapter(tmp_path: Path, base_url: str) -> VLLMAdapter:
    entry = ModelEntry(
        alias="rr",
        backend=BackendType.VLLM,
        runtime_model_id="up-reranker",
        base_url=base_url,
        supports_rerank=True,
    )
    return VLLMAdapter(entry, _settings(tmp_path, "models: []"))


# --- one case per table row: every malformed body -> BackendUnavailableError --
MALFORMED = {
    "index out of range": {
        "results": [{"index": 7, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
    },
    "negative index": {"results": [{"index": -1, "relevance_score": 0.9}]},
    "duplicate index": {
        "results": [{"index": 0, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.8}]
    },
    "float index": {"results": [{"index": 1.7, "relevance_score": 0.5}]},
    "bool index": {"results": [{"index": True, "relevance_score": 0.5}]},
    "string score": {"results": [{"index": 0, "relevance_score": "high"}]},
    "string index": {"results": [{"index": "zero", "relevance_score": 0.5}]},
    "NaN score": b'{"results":[{"index":0,"relevance_score":NaN}]}',
    "Infinity score": b'{"results":[{"index":0,"relevance_score":Infinity}]}',
    "non-JSON body": b"<html>oops</html>",
    "results not a list": {"results": {"index": 0}},
    "result not an object": {"results": ["nope"]},
}


@pytest.mark.parametrize("name", list(MALFORMED))
async def test_malformed_upstream_rerank_raises_502(tmp_path, upstream, name):
    upstream.response = MALFORMED[name]
    a = _vllm_adapter(tmp_path, upstream.base_url)
    try:
        with pytest.raises(BackendUnavailableError) as exc:
            await a.rerank({"query": "q", "documents": DOCS, "return_documents": True}, "r0")
        assert exc.value.http_status == 502
    finally:
        await a.stop()


async def test_wellformed_upstream_rerank_ok_and_id_is_broker_generated(tmp_path, upstream):
    # The only "normal" row: a valid body is normalized and returned.
    upstream.response = {
        "id": "upstream-should-not-leak",
        "results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.1},
            {"index": 2, "relevance_score": 0.5},
        ],
    }
    a = _vllm_adapter(tmp_path, upstream.base_url)
    try:
        out = await a.rerank({"query": "q", "documents": DOCS}, "req-123")
    finally:
        await a.stop()
    assert [r["index"] for r in out["results"]] == [1, 2, 0]
    # id stays broker-generated; the upstream id is never echoed verbatim.
    assert out["id"] == "rerank-req-123"


async def test_upstream_fewer_results_than_documents_allowed(tmp_path, upstream):
    # Returning fewer results than documents (top_n semantics) is valid.
    upstream.response = {"results": [{"index": 0, "relevance_score": 0.9}]}
    a = _vllm_adapter(tmp_path, upstream.base_url)
    try:
        out = await a.rerank({"query": "q", "documents": DOCS}, "r0")
    finally:
        await a.stop()
    assert [r["index"] for r in out["results"]] == [0]


# --- non-JSON guard on the chat/embeddings paths (same latent 500) -----------
async def test_chat_non_json_upstream_raises_502(tmp_path, upstream):
    upstream.response = b"<html>502 from proxy</html>"
    a = _vllm_adapter(tmp_path, upstream.base_url)
    try:
        with pytest.raises(BackendUnavailableError) as exc:
            await a.chat({"messages": [{"role": "user", "content": "hi"}]}, "r0")
        assert exc.value.http_status == 502
    finally:
        await a.stop()


async def test_embeddings_non_json_upstream_raises_502(tmp_path, upstream):
    upstream.response = b"not json at all"
    a = _vllm_adapter(tmp_path, upstream.base_url)
    try:
        with pytest.raises(BackendUnavailableError) as exc:
            await a.embeddings({"input": "hi"}, "r0")
        assert exc.value.http_status == 502
    finally:
        await a.stop()


# --- app level: cache is never poisoned by a malformed body ------------------
RR_REGISTRY = """
models:
  - alias: rr
    backend: vllm
    runtime_model_id: up-reranker
    base_url: {base}
    supports_rerank: true
"""


def test_malformed_response_never_cached(tmp_path, upstream):
    upstream.response = {"results": [{"index": 7, "relevance_score": 0.9}]}
    s = _settings(tmp_path, RR_REGISTRY.format(base=upstream.base_url))
    s = s.model_copy(update={"response_cache_enabled": True})
    body = {"model": "rr", "query": "q", "documents": DOCS}
    with TestClient(create_app(s), raise_server_exceptions=False) as c:
        bad = c.post("/v1/rerank", json=body)
        assert bad.status_code == 502, bad.text
        # Now the upstream recovers; the request must be a cache MISS (the 502
        # body was never stored), and the good result is returned.
        upstream.response = {"results": [{"index": 0, "relevance_score": 0.5}]}
        good = c.post("/v1/rerank", json=body)
        assert good.status_code == 200, good.text
        assert good.headers["x-broker-cache"] == "miss"
        assert good.json()["results"][0]["index"] == 0


# --- app level: pool failover past a member with a malformed body ------------
POOL_REGISTRY = """
models:
  - alias: rr-a
    backend: vllm
    runtime_model_id: up-reranker
    base_url: {base_a}
    pool: rr-pool
    supports_rerank: true
  - alias: rr-b
    backend: vllm
    runtime_model_id: up-reranker
    base_url: {base_b}
    pool: rr-pool
    supports_rerank: true
"""


def test_pool_failover_on_malformed_upstream(tmp_path):
    bad = _Upstream()
    good = _Upstream()
    bad.response = {"results": [{"index": 7, "relevance_score": 0.9}]}  # out of range
    good.response = {"results": [{"index": 0, "relevance_score": 0.5}]}
    try:
        s = _settings(
            tmp_path,
            POOL_REGISTRY.format(base_a=bad.base_url, base_b=good.base_url),
        )
        with TestClient(create_app(s), raise_server_exceptions=False) as c:
            ok = False
            # Whichever member is tried first, the pool must fail over to the
            # good one and return 200. Retry across a few route keys so we cover
            # the case where the balancer initially lands on the good member.
            for i in range(6):
                r = c.post(
                    "/v1/rerank",
                    headers={"x-session-id": f"s{i}"},
                    json={"model": "rr-pool", "query": "q", "documents": DOCS},
                )
                assert r.status_code == 200, r.text
                assert r.json()["results"][0]["index"] == 0
                ok = True
            assert ok
    finally:
        bad.close()
        good.close()
