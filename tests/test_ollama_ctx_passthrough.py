"""Regression: the Ollama chat path forwards request params faithfully.

Context: a long-context benchmark against a remote Ollama (``<OLLAMA_HOST>``)
model showed ``prompt_tokens`` pinned at ~16386 for both a ~45k- and a
~100k-token prompt — the prompt was being silently truncated to ~16k. A prior
(separate) agent claimed the cause was *the broker's Ollama adapter rebuilding
each chat request and dropping the incoming ``options`` object (including
``num_ctx``)*.

That claim is false, and these tests pin the real behaviour so it can never
silently regress:

* The broker copies the request payload and only overrides ``model`` (to the
  registry-pinned runtime id); it does **not** strip ``options``/``num_ctx`` or
  any other unmodeled field. Whatever the client sends reaches the upstream.
* The Ollama adapter posts to Ollama's **OpenAI-compatible**
  ``POST /v1/chat/completions`` endpoint. That protocol has no context-window
  field, so ``options.num_ctx`` is simply passed along verbatim and ignored by
  Ollama there — the effective context is governed **server-side** on the Ollama
  host (per-model Modelfile ``num_ctx`` and/or the ``OLLAMA_CONTEXT_LENGTH`` default),
  never by the broker. The real-world fix is therefore an Ollama server-config
  change, not a code change; see docs/backends/ollama.md.

The tests drive the real :class:`OllamaAdapter` against a tiny in-process HTTP
upstream (loopback, allowed via ``allow_loopback_backends``) that records the
exact body it received — no real Ollama daemon.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from broker.adapters.ollama import OllamaAdapter
from broker.config import Settings
from broker.registry import BackendType, ModelEntry


class _RecordingUpstream:
    """Minimal Ollama-ish upstream that records the last POST path + JSON body.

    ``GET`` answers 200 (readiness). ``POST`` records the request and returns a
    valid OpenAI chat-completion envelope so ``OllamaAdapter.chat`` parses OK.
    """

    def __init__(self) -> None:
        # Every POST (chat, and the best-effort /api/generate unload on stop())
        # is recorded so a test can select the request it cares about.
        self.requests: list[tuple[str, dict[str, Any] | None]] = []
        self._srv = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _make_handler(self):
        parent = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence test server
                pass

            def do_GET(self):  # readiness / health (/api/tags)
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"models":[]}')

            def do_POST(self):
                n = int(self.headers.get("content-length", 0))
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    body = None
                parent.requests.append((self.path, body))
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(
                    json.dumps(
                        {
                            "id": "chatcmpl-x",
                            "object": "chat.completion",
                            "model": "ignored-by-broker",
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "ok"},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 7,
                                "completion_tokens": 1,
                                "total_tokens": 8,
                            },
                        }
                    ).encode()
                )

        return H

    def close(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture
def upstream():
    u = _RecordingUpstream()
    try:
        yield u
    finally:
        u.close()


def _ollama_adapter(tmp_path: Path, base_url: str) -> OllamaAdapter:
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(tmp_path / "registry.yaml"),
        enabled_backends=["ollama"],
        allow_loopback_backends=True,
        health_probe_enabled=False,
        usage_log_path=":memory:",
    )
    entry = ModelEntry(
        alias="public-alias",
        backend=BackendType.OLLAMA,
        runtime_model_id="gemma3:27b",
        base_url=base_url,
    )
    a = OllamaAdapter(entry, settings)
    a.validate_config()
    return a


async def test_ollama_chat_forwards_options_num_ctx_unchanged(tmp_path, upstream):
    """``options`` (incl. ``num_ctx``) and other unmodeled fields reach upstream."""
    a = _ollama_adapter(tmp_path, upstream.base_url)
    payload = {
        "model": "public-alias",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.0,
        "options": {"num_ctx": 131072, "seed": 42},
        "keep_alive": "30m",
    }
    try:
        out = await a.chat(payload, "req-ctx-1")
    finally:
        await a.stop()

    # Hits Ollama's OpenAI-compatible endpoint.
    chat_reqs = [(p, b) for p, b in upstream.requests if p == "/v1/chat/completions"]
    assert len(chat_reqs) == 1
    body = chat_reqs[0][1]
    assert body is not None
    # The broker does NOT strip options / num_ctx — the prior "drops options"
    # claim is false. Everything the client sent is forwarded verbatim.
    assert body["options"] == {"num_ctx": 131072, "seed": 42}
    assert body["keep_alive"] == "30m"
    assert body["temperature"] == 0.0
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    # Only `model` is overridden (pinned to the registry runtime id) and stream
    # is forced off for the non-streaming path.
    assert body["model"] == "gemma3:27b"
    assert body["stream"] is False
    # The public alias (never the raw runtime id) is presented back to the caller.
    assert out["model"] == "public-alias"


async def test_ollama_chat_does_not_mutate_caller_payload(tmp_path, upstream):
    """The adapter copies the payload; the caller's dict is left untouched."""
    a = _ollama_adapter(tmp_path, upstream.base_url)
    payload = {"model": "public-alias", "messages": [{"role": "user", "content": "hi"}]}
    try:
        await a.chat(payload, "req-ctx-2")
    finally:
        await a.stop()
    # No `stream` / rewritten `model` leaked back into the caller's object.
    assert payload["model"] == "public-alias"
    assert "stream" not in payload
