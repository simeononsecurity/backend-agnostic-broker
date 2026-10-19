"""Fake in-process backend.

Deterministic, dependency-free backend used by the test suite and for local
smoke tests. It simulates realistic lifecycle timing (configurable load delay),
streaming, cancellation, and failure injection — without any external runtime or
network egress. This lets every lifecycle, API, streaming, auth, and security
test run in CI with no GPU and no model downloads.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

from ..errors import BackendUnavailableError, ModelLoadError
from .base import BackendAdapter, HealthResult


class FakeAdapter(BackendAdapter):
    name = "fake"

    # Class-level knobs the tests flip to simulate conditions.
    load_delay: float = 0.0
    chat_delay: float = 0.0
    fail_on_start: bool = False
    fail_on_chat: bool = False

    def __init__(self, entry, settings) -> None:
        super().__init__(entry, settings)
        self._started = False
        self._ready = False
        self._pid: int | None = None
        self._cancelled: set[str] = set()
        self._load_count = 0
        # Simulated real load signal for cache-aware routing tests (ADR 0010);
        # None means "no real signal" so the manager falls back to in-flight.
        self.sim_load: float | None = None

    def validate_config(self) -> None:
        # Fake backend needs nothing beyond an alias + runtime_model_id.
        return None

    async def start(self) -> int | None:
        if self.fail_on_start:
            raise ModelLoadError(
                "fake backend configured to fail on start",
                backend=self.name,
                model=self.entry.alias,
                remediation="unset FakeAdapter.fail_on_start",
            )
        self._started = True
        self._ready = False
        self._load_count += 1
        self._pid = 100000 + self._load_count  # synthetic pid
        return self._pid

    async def wait_until_ready(self, timeout: float) -> None:
        if self.load_delay:
            await asyncio.wait_for(asyncio.sleep(self.load_delay), timeout=timeout)
        self._ready = True

    async def health(self) -> HealthResult:
        return HealthResult(
            healthy=self._ready,
            detail="ready" if self._ready else "not ready",
            gpu={"device_ids": self.entry.gpu.device_ids} if self.entry.gpu.required else None,
        )

    async def unload(self) -> None:
        self._ready = False
        self._started = False
        self._pid = None

    async def stop(self) -> None:
        await self.unload()

    def _completion(self, payload: dict[str, Any], text: str) -> dict[str, Any]:
        prompt = " ".join(str(m.get("content", "")) for m in payload.get("messages", []))
        # Deterministic response shaping for cascade tests: a model carrying one of
        # these tags produces an escalation-triggering response. Production models
        # never carry these tags, so default behavior is unchanged.
        finish = "stop"
        tags = set(self.entry.tags)
        if "truncate" in tags:
            finish = "length"
        elif "refuse" in tags:
            text = "I'm sorry, but I cannot help with that request."
        elif "empty" in tags:
            text = ""
        return {
            "id": f"chatcmpl-fake-{int(time.time() * 1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.entry.alias,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": finish,
                }
            ],
            "usage": {
                "prompt_tokens": len(prompt.split()),
                "completion_tokens": len(text.split()),
                "total_tokens": len(prompt.split()) + len(text.split()),
            },
        }

    async def chat(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if self.fail_on_chat:
            raise BackendUnavailableError(
                "fake backend configured to fail on chat",
                backend=self.name,
                model=self.entry.alias,
            )
        if not self._ready:
            raise BackendUnavailableError(
                "model not ready", backend=self.name, model=self.entry.alias
            )
        if self.chat_delay:
            await asyncio.sleep(self.chat_delay)
        return self._completion(payload, f"echo: {self.entry.alias}")

    async def stream_chat(  # type: ignore[override]
        self, payload: dict[str, Any], request_id: str
    ) -> AsyncIterator[bytes]:
        if not self._ready:
            raise BackendUnavailableError(
                "model not ready", backend=self.name, model=self.entry.alias
            )
        words = ["hello", "from", "the", "fake", "backend"]
        created = int(time.time())
        cid = f"chatcmpl-fake-{int(time.time() * 1000)}"
        for i, w in enumerate(words):
            if request_id in self._cancelled:
                self._cancelled.discard(request_id)
                break
            chunk = {
                "id": cid,
                "object": "chat.completion.chunk",
                "created": created,
                "model": self.entry.alias,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": (w if i == 0 else " " + w)},
                        "finish_reason": None,
                    }
                ],
            }
            import json

            yield f"data: {json.dumps(chunk)}\n\n".encode()
            await asyncio.sleep(0.01)
        final = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": self.entry.alias,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        import json

        yield f"data: {json.dumps(final)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def cancel(self, request_id: str) -> None:
        self._cancelled.add(request_id)

    async def embeddings(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not self._ready:
            raise BackendUnavailableError(
                "model not ready", backend=self.name, model=self.entry.alias
            )
        inp = payload.get("input")
        if inp is None or (isinstance(inp, (list, str)) and len(inp) == 0):
            from ..errors import ValidationError

            raise ValidationError(
                "`input` is required and must be a non-empty string or list",
                model=self.entry.alias,
            )
        texts = [inp] if isinstance(inp, str) else [str(x) for x in inp]
        dims = 16  # small, fixed, deterministic embedding dimension for tests
        data = []
        total_tokens = 0
        for i, text in enumerate(texts):
            total_tokens += len(text.split())
            # Deterministic pseudo-embedding: seed a hash per dimension.
            import hashlib

            vec = []
            for d in range(dims):
                h = hashlib.sha256(f"{text}:{d}".encode()).digest()
                # map first 4 bytes to a float in [-1, 1]
                n = int.from_bytes(h[:4], "big")
                vec.append((n / 0xFFFFFFFF) * 2 - 1)
            data.append({"object": "embedding", "index": i, "embedding": vec})
        return {
            "object": "list",
            "data": data,
            "model": self.entry.alias,
            "usage": {"prompt_tokens": total_tokens, "total_tokens": total_tokens},
        }

    async def collect_load(self) -> float | None:
        return self.sim_load

    async def collect_status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "alias": self.entry.alias,
            "pid": self._pid,
            "ready": self._ready,
            "load_count": self._load_count,
            "gpu": self.entry.gpu.model_dump() if self.entry.gpu.required else None,
        }
