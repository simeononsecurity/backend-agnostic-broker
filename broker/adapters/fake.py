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
from collections.abc import AsyncIterator, Callable
from typing import Any

from ..errors import BackendUnavailableError, ModelLoadError
from ..teardown import TeardownResult
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
        self._last_pid: int | None = None
        self._cancelled: set[str] = set()
        self._load_count = 0
        # ADR 0020 test knobs: simulate a runtime whose process survives SIGKILL
        # (telemetry-off path) and/or whose device memory lingers (telemetry path
        # scripts that via the fake telemetry provider, not here).
        self.simulate_survive_kill = False
        self._proc_alive = False
        self._release_ok = True
        self._last_teardown: TeardownResult | None = None
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
        self._last_pid = self._pid
        self._proc_alive = True
        self._release_ok = True
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
        if self.simulate_survive_kill:
            # Telemetry-off path: process refuses to die; stop() reports it.
            self._proc_alive = True
            self._release_ok = False
        else:
            self._proc_alive = False
            self._release_ok = True

    async def stop(
        self,
        *,
        grace: float | None = None,
        hard_deadline: float | None = None,
        sigkill_only: bool = False,
        force_check: Callable[[], bool] | None = None,
    ) -> TeardownResult | None:
        await self.unload()
        pid = self._pid if self._pid is not None else self._last_pid
        if self.simulate_survive_kill:
            self._last_teardown = TeardownResult(
                alias=self.entry.alias,
                outcome="survivors",
                survivors=[pid] if pid else [1],
            )
        else:
            self._last_teardown = TeardownResult(
                alias=self.entry.alias,
                outcome="signalled",
                exited=[pid] if pid else [],
            )
        return self._last_teardown

    def last_teardown(self) -> TeardownResult | None:
        return self._last_teardown

    def process_pid(self) -> int | None:
        return self._pid if self._pid is not None else self._last_pid

    async def process_alive(self) -> bool:
        return self._proc_alive

    def last_release_ok(self) -> bool:
        return self._release_ok

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

    async def rerank(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not self._ready:
            raise BackendUnavailableError(
                "model not ready", backend=self.name, model=self.entry.alias
            )
        query = payload.get("query")
        documents = payload.get("documents")
        if not query or not isinstance(query, str):
            from ..errors import ValidationError

            raise ValidationError(
                "`query` is required and must be a non-empty string", model=self.entry.alias
            )
        if not documents or not isinstance(documents, list):
            from ..errors import ValidationError

            raise ValidationError(
                "`documents` is required and must be a non-empty list", model=self.entry.alias
            )
        # Deterministic lexical-overlap (Jaccard) score so tests can assert
        # ordering: the document sharing the most query terms ranks first.
        q_tokens = set(query.lower().split())
        scored: list[dict[str, Any]] = []
        total_tokens = len(query.split())
        for i, doc in enumerate(documents):
            text = str(doc)
            total_tokens += len(text.split())
            d_tokens = set(text.lower().split())
            union = q_tokens | d_tokens
            score = (len(q_tokens & d_tokens) / len(union)) if union else 0.0
            scored.append({"index": i, "relevance_score": score})
        from ..openai_compat import build_rerank_response

        return build_rerank_response(
            scored=scored,
            documents=[str(d) for d in documents],
            model=self.entry.alias,
            request_id=request_id,
            top_n=payload.get("top_n"),
            return_documents=bool(payload.get("return_documents", False)),
            usage={"total_tokens": total_tokens},
        )

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
