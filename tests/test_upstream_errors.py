"""End-to-end upstream HTTP error typing + policy (ADR 0017 follow-up).

Unlike ``test_error_policy.py`` (which injects hand-built exceptions), these tests
drive the *real* adapter → manager → app path against a local
``ThreadingHTTPServer`` upstream, so they exercise exactly what a real backend
returning a 4xx/429/503 produces. They lock in the fixes that the per-error
policy never saw real upstream statuses:

* upstream 4xx (400/404/413/422) → the client gets that same 4xx with type
  ``upstream_client_error`` (never 502), the upstream is called exactly once, and
  no pool member is benched;
* upstream 429 / 503+Retry-After → immediate failover to a healthy member with
  the throttled member benched (no sleep while another member can serve);
* every Retry-After wait is bounded (single-member pool; and request_timeout 0 +
  a huge Retry-After both clamp to a finite cap);
* streaming pre-first-byte 4xx behaves the same as non-streaming;
* non-finite reliability knobs are rejected at config validation.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.errors import ConfigError


class _Upstream:
    """A tiny OpenAI-compatible upstream whose POST behavior is configurable.

    ``status`` 200 serves a canned chat completion; any other status returns an
    error body (plus ``Retry-After`` when set). Every POST increments ``hits``.
    """

    def __init__(self, status: int = 200, retry_after: str | None = None, body: object = None):
        self.status = status
        self.retry_after = retry_after
        self.body = body
        self.hits = 0
        up = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_GET(self):  # readiness / health probes
                self._send(200, {"data": [{"id": "x"}]})

            def do_POST(self):
                length = int(self.headers.get("content-length", 0))
                self.rfile.read(length)
                up.hits += 1
                if up.status == 200:
                    self._send(
                        200,
                        {
                            "id": "c",
                            "object": "chat.completion",
                            "model": "x",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 1,
                                "completion_tokens": 1,
                                "total_tokens": 2,
                            },
                        },
                    )
                else:
                    payload = up.body
                    if payload is None:
                        payload = {"error": {"message": "upstream error"}}
                    self._send(up.status, payload, up.retry_after)

            def _send(self, code, obj, retry_after=None):
                raw = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                if retry_after is not None:
                    self.send_header("retry-after", retry_after)
                self.end_headers()
                self.wfile.write(raw)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def close(self):
        self._server.shutdown()


def _registry(tmp_path: Path, members: list[tuple[str, int]], pool: str | None = "p") -> Path:
    lines = ["models:"]
    for alias, port in members:
        lines += [
            f"  - alias: {alias}",
            "    backend: vllm",
            "    runtime_model_id: x",
            f"    base_url: http://127.0.0.1:{port}",
        ]
        if pool:
            lines.append(f"    pool: {pool}")
    reg = tmp_path / "r.yaml"
    reg.write_text("\n".join(lines) + "\n")
    return reg


def _client(
    tmp_path: Path, members: list[tuple[str, int]], pool: str | None = "p", **over
) -> TestClient:
    reg = _registry(tmp_path, members, pool=pool)
    kwargs = dict(
        auth_enabled=False,
        registry_path=str(reg),
        enabled_backends=["vllm"],
        allow_loopback_backends=True,
        usage_log_path=":memory:",
        health_probe_enabled=False,
    )
    kwargs.update(over)
    s = Settings(**kwargs)
    app = create_app(s)
    return TestClient(app, raise_server_exceptions=False)


def _chat(content="hello"):
    return {"model": "p", "messages": [{"role": "user", "content": content}]}


# --- upstream 4xx → client 4xx, called once, no bench ---------------------


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_upstream_4xx_returns_client_4xx_called_once_no_bench(tmp_path, status):
    bad = _Upstream(status=status, body={"error": {"message": "maximum context length exceeded"}})
    good = _Upstream(status=200)
    try:
        c = _client(
            tmp_path,
            [("a", bad.port), ("b", good.port)],
            cooldown_failures=2,
            retry_max_attempts=2,
        )
        r = c.post("/v1/chat/completions", json=_chat(), headers={"X-Session-Id": "s1"})
        assert r.status_code == status  # NOT 502
        assert r.json()["error"]["type"] == "upstream_client_error"
        # Exactly one upstream call total: no failover across the pool.
        assert bad.hits + good.hits == 1
        # Neither member benched (a 4xx is the client's fault, not the member's).
        health = c.get("/healthz/backends").json()["backends"]
        assert all(not b["cooldown"]["in_cooldown"] for b in health)
    finally:
        bad.close()
        good.close()


def test_upstream_4xx_message_is_sanitized_and_truncated(tmp_path):
    huge = "secret\x00\x07" + ("A" * 5000)
    bad = _Upstream(status=400, body={"error": {"message": huge}})
    try:
        c = _client(tmp_path, [("a", bad.port)], pool=None)
        r = c.post("/v1/chat/completions", json={"model": "a", "messages": [{"role": "u"}]})
        assert r.status_code == 400
        msg = r.json()["error"]["message"]
        assert "\x00" not in msg and "\x07" not in msg  # control chars stripped
        assert len(msg) < 300  # truncated, never the full 5KB body
    finally:
        bad.close()


# --- upstream 429 / 503 Retry-After → failover + bench -------------------


@pytest.mark.parametrize("status", [429, 503])
def test_upstream_throttle_fails_over_and_benches(tmp_path, status):
    bad = _Upstream(status=status, retry_after="100000", body={"error": {"message": "busy"}})
    good = _Upstream(status=200)
    try:
        c = _client(
            tmp_path,
            [("a", bad.port), ("b", good.port)],
            cooldown_failures=2,
            retry_max_attempts=2,
            request_timeout_seconds=10,
        )
        t = time.monotonic()
        r = c.post("/v1/chat/completions", json=_chat(), headers={"X-Session-Id": "s1"})
        elapsed = time.monotonic() - t
        assert r.status_code == 200  # failed over to the healthy member
        assert elapsed < 2.0  # immediate failover — never slept the 100000s header
        assert bad.hits == 1 and good.hits == 1
        health = {b["alias"]: b["cooldown"] for b in c.get("/healthz/backends").json()["backends"]}
        assert health["a"]["in_cooldown"]  # throttled member benched
        assert not health["b"]["in_cooldown"]
    finally:
        bad.close()
        good.close()


def test_single_member_retry_after_sleep_is_bounded(tmp_path):
    bad = _Upstream(status=503, retry_after="100000", body={"error": {"message": "busy"}})
    try:
        c = _client(
            tmp_path,
            [("a", bad.port)],
            pool=None,
            retry_max_attempts=2,
            request_timeout_seconds=2,
        )
        t = time.monotonic()
        r = c.post("/v1/chat/completions", json={"model": "a", "messages": [{"role": "u"}]})
        elapsed = time.monotonic() - t
        assert r.status_code == 502  # exhausted 5xx → backend_unavailable
        # Honored Retry-After but clamped to the ~2s request deadline, not 100000s.
        assert elapsed <= 4.0
    finally:
        bad.close()


def test_unbounded_timeout_clamps_retry_after(tmp_path):
    bad = _Upstream(status=503, retry_after="100000", body={"error": {"message": "busy"}})
    try:
        c = _client(
            tmp_path,
            [("a", bad.port)],
            pool=None,
            retry_max_attempts=2,
            request_timeout_seconds=0,  # unbounded request timeout
            retry_after_max_seconds=1.0,  # finite safety cap
        )
        t = time.monotonic()
        r = c.post("/v1/chat/completions", json={"model": "a", "messages": [{"role": "u"}]})
        elapsed = time.monotonic() - t
        assert r.status_code == 502
        # With no request deadline, the Retry-After wait is still clamped to the
        # finite retry_after_max_seconds cap (never an unbounded 100000s sleep).
        assert elapsed <= 3.0
    finally:
        bad.close()


# --- streaming pre-first-byte 4xx ---------------------------------------


def test_stream_pre_first_byte_4xx_same_as_non_stream(tmp_path):
    bad = _Upstream(status=400, body={"error": {"message": "context too long"}})
    good = _Upstream(status=200)
    try:
        c = _client(
            tmp_path,
            [("a", bad.port), ("b", good.port)],
            cooldown_failures=2,
            retry_max_attempts=2,
        )
        body = {**_chat(), "stream": True}
        r = c.post("/v1/chat/completions", json=body, headers={"X-Session-Id": "s1"})
        # SSE transport is already 200; the error rides in the stream frame.
        assert r.status_code == 200
        text = r.text
        assert "upstream_client_error" in text
        assert bad.hits + good.hits == 1  # not retried/failed over
        health = c.get("/healthz/backends").json()["backends"]
        assert all(not b["cooldown"]["in_cooldown"] for b in health)
    finally:
        bad.close()
        good.close()


# --- embeddings path too ------------------------------------------------


def test_embeddings_upstream_4xx_returns_client_4xx(tmp_path):
    bad = _Upstream(status=422, body={"error": {"message": "bad input"}})
    try:
        c = _client(tmp_path, [("a", bad.port)], pool=None)
        r = c.post("/v1/embeddings", json={"model": "a", "input": "hi"})
        assert r.status_code == 422
        assert r.json()["error"]["type"] == "upstream_client_error"
        assert bad.hits == 1
    finally:
        bad.close()


# --- config validation: non-finite knobs rejected -----------------------


@pytest.mark.parametrize(
    "kw",
    [
        {"cooldown_backoff_multiplier": float("nan")},
        {"cooldown_max_seconds": float("inf")},
        {"cooldown_decay_seconds": float("nan")},
        {"cooldown_half_open_ttl_seconds": float("inf")},
        {"cooldown_seconds": float("nan")},
        {"retry_after_max_seconds": float("inf")},
    ],
)
def test_non_finite_reliability_knobs_rejected(tmp_path, kw):
    reg = _registry(tmp_path, [("a", 1)], pool=None)
    s = Settings(auth_enabled=False, registry_path=str(reg), enabled_backends=["vllm"], **kw)
    with pytest.raises(ConfigError):
        s.validate_runtime()
