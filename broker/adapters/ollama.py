"""Ollama backend adapter.

Ollama runs as its own daemon (``ollama serve``) and has **native lazy loading**:
a model is loaded on first request and unloaded after ``keep_alive`` elapses.
The broker therefore does *not* spawn Ollama; it treats it as an external
network backend and layers its own idle tracking on top (calling Ollama's native
unload for determinism).

Routes used (see docs/backends/ollama.md for confirmed behavior):
* ``GET  /api/tags``               — model listing / readiness
* ``POST /v1/chat/completions``    — OpenAI-compatible chat (streaming + not)
* ``POST /api/generate`` w/ ``keep_alive: 0`` — explicit unload

GPU selection and daemon lifetime are controlled by Ollama's own env vars
(``OLLAMA_*``) on the daemon, documented rather than managed here.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import BackendUnavailableError, TimeoutError_, ValidationError
from .base import HealthResult
from .httpbase import HTTPBackendAdapter


class OllamaAdapter(HTTPBackendAdapter):
    name = "ollama"
    external_daemon = True
    chat_path = "/v1/chat/completions"

    def validate_config(self) -> None:
        # Ollama is external: require a base_url, never a startup_command.
        if self.entry.startup_command:
            from ..errors import ConfigError

            raise ConfigError(
                "ollama backend is external; remove startup_command",
                backend=self.name,
                model=self.entry.alias,
                remediation="Ollama manages its own process; set only base_url",
            )
        if not self.entry.base_url:
            from ..errors import ConfigError

            raise ConfigError(
                "ollama backend requires base_url",
                backend=self.name,
                model=self.entry.alias,
                remediation="set base_url to the Ollama daemon, e.g. http://127.0.0.1:11434",
            )
        # SSRF-validate the configured base URL.
        super().validate_config()

    async def start(self) -> int | None:
        # External daemon; nothing to spawn.
        return None

    async def wait_until_ready(self, timeout: float) -> None:
        import asyncio

        url = self._validated_url("/api/tags")
        client = self._client_or_new()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        last = ""
        while loop.time() < deadline:
            try:
                resp = await client.get(url, timeout=5)
                if resp.status_code == 200:
                    return
                last = f"status {resp.status_code}"
            except (httpx.HTTPError, OSError) as exc:
                last = str(exc)
            await asyncio.sleep(0.5)
        raise TimeoutError_(
            f"Ollama daemon not reachable at {self.entry.base_url} ({last})",
            backend=self.name,
            model=self.entry.alias,
            remediation="start `ollama serve` and check base_url",
        )

    async def health(self) -> HealthResult:
        try:
            url = self._validated_url("/api/tags")
            client = self._client_or_new()
            resp = await client.get(url, timeout=5)
            return HealthResult(healthy=resp.status_code == 200, detail=f"tags {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(healthy=False, detail=str(exc))

    async def unload(self) -> None:
        """Ask Ollama to unload the model natively via keep_alive: 0."""
        try:
            url = self._validated_url("/api/generate")
            client = self._client_or_new()
            await client.post(
                url,
                json={"model": self.entry.runtime_model_id, "keep_alive": 0},
                timeout=10,
            )
        except httpx.HTTPError:
            # Best-effort; the daemon will still unload after keep_alive.
            pass

    async def stop(self) -> None:
        # Don't kill the external daemon; just unload and close our client.
        await self.unload()
        if self._client:
            await self._client.aclose()
            self._client = None

    async def embeddings(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Translate OpenAI ``/v1/embeddings`` to Ollama's native ``/api/embed``.

        Ollama's ``/api/embed`` accepts ``input`` as a string or a list of strings
        and returns ``{"embeddings": [[...], ...], "prompt_eval_count": N}``. We map
        that to the OpenAI ``{"object": "list", "data": [{"embedding": [...]}]}``
        shape. The upstream model id is pinned from the registry entry; the request
        never selects a raw runtime id.
        """
        inp = payload.get("input")
        if inp is None or (isinstance(inp, (list, str)) and len(inp) == 0):
            raise ValidationError(
                "`input` is required and must be a non-empty string or list",
                model=self.entry.alias,
            )
        body: dict[str, Any] = {"model": self.entry.runtime_model_id, "input": inp}
        for k in ("truncate", "options", "keep_alive"):
            if k in payload:
                body[k] = payload[k]
        url = self._validated_url("/api/embed")
        client = self._client_or_new()
        try:
            resp = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"backend request failed: {exc}",
                backend=self.name,
                model=self.entry.alias,
                remediation="check the Ollama daemon/base_url",
            ) from exc
        if resp.status_code >= 400:
            self._raise_upstream(resp.status_code, resp.text, resp.headers)
        data = resp.json()
        vectors = data.get("embeddings") or []
        prompt_tokens = int(data.get("prompt_eval_count") or 0)
        return {
            "object": "list",
            "data": [
                {"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)
            ],
            "model": self.entry.alias,
            "usage": {"prompt_tokens": prompt_tokens, "total_tokens": prompt_tokens},
        }

    async def rerank(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Ollama has no native rerank endpoint (ADR 0013).

        Ollama exposes only the embedding layer, not a cross-encoder's
        classification head, so the only ways to "rerank" on Ollama are
        embedding-magnitude / yes-no-logprob hacks that are a silent quality
        substitute. We refuse them and return a clear 400 instead.
        """
        raise ValidationError(
            "backend 'ollama' does not support rerank: Ollama has no native "
            f"/api/rerank endpoint (model {self.entry.alias!r})",
            backend=self.name,
            model=self.entry.alias,
            remediation="use a vLLM or llama.cpp reranker model for /v1/rerank",
        )

    async def ps(self) -> dict[str, Any]:
        """Return Ollama's ``/api/ps`` — models currently resident in memory.

        This is a non-loading introspection call: it reports what the daemon
        already has in VRAM/RAM and never causes a model to load.
        """
        url = self._validated_url("/api/ps")
        client = self._client_or_new()
        resp = await client.get(url, timeout=5)
        resp.raise_for_status()
        return resp.json()

    async def collect_status(self) -> dict[str, Any]:
        status = await super().collect_status()
        status["external_daemon"] = True
        status["keep_alive"] = self.entry.keep_alive
        return status
