#!/usr/bin/env python3
"""Stand-in GPU runtime for ADR 0024 shutdown drills (no absolute paths).

A deterministic, stdlib-only HTTP server the broker spawns as a model runtime,
with env-selected misbehaviours that reproduce the production stop-path defects.
Select behaviours with ``DRILL_MODE`` (comma-separated):

* ``ignore_term`` — ignore ``SIGTERM`` (like a runtime busy tearing down a CUDA
  context); only ``SIGKILL`` stops it.
* ``slow_start``  — sleep 100 s before it begins listening (a load in progress).
* ``escape``      — fork a ``setsid`` grandchild that ignores ``SIGTERM`` and
  keeps the inherited stdout pipe open (an escaped GPU worker). Its PID is
  written to ``DRILL_ESCAPE_PIDFILE`` so a test can assert on it.

Endpoints mimic an OpenAI-compatible runtime: ``GET`` anything → 200 health /
models; ``POST`` → a chat completion, or a long SSE stream when ``stream`` is set.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_MODE = set(filter(None, os.environ.get("DRILL_MODE", "").split(",")))


def _port_from_argv() -> int:
    argv = sys.argv
    return int(argv[argv.index("--port") + 1])


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - silence default logging
        pass

    def do_GET(self):
        body = json.dumps({"status": "ok", "data": [{"id": "m"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("content-length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if payload.get("stream"):
            self._stream()
            return
        body = json.dumps(
            {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("transfer-encoding", "chunked")
        self.end_headers()
        for _ in range(400):  # ~200 s of stream unless cut short
            chunk = {
                "id": "x",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "delta": {"content": "."}, "finish_reason": None}],
            }
            data = f"data: {json.dumps(chunk)}\n\n".encode()
            try:
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()
            except OSError:
                return
            time.sleep(0.5)
        self.wfile.write(b"0\r\n\r\n")


def main() -> None:
    if "ignore_term" in _MODE:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if "escape" in _MODE and os.fork() == 0:
        # Grandchild: leave the runtime's session/group, ignore SIGTERM, and keep
        # the inherited stdout pipe open (writes nothing, like a GPU worker).
        os.setsid()
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pidfile = os.environ.get("DRILL_ESCAPE_PIDFILE")
        if pidfile:
            with open(pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
        while True:
            time.sleep(1)
    if "slow_start" in _MODE:
        time.sleep(100)
    print("server listening", flush=True)
    ThreadingHTTPServer(("127.0.0.1", _port_from_argv()), _Handler).serve_forever()


if __name__ == "__main__":
    main()
