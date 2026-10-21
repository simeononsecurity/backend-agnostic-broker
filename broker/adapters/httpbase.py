"""Shared HTTP backend adapter.

Base class for backends that serve over HTTP. Two concerns are factored here:

1. **Broker-managed process lifecycle** — for runtimes that load a model only at
   process startup (vLLM, llama.cpp, Strata), the adapter spawns the runtime on
   first request (validated argv, no shell), waits for its readiness probe, and
   stops it on unload/idle.
2. **OpenAI-compatible proxying** — chat / streaming requests are forwarded to
   the runtime's ``/v1/chat/completions`` endpoint. Every outbound URL passes the
   SSRF allow-list check.

Network backends that manage their own lifetime (Ollama) subclass and override
the lifecycle hooks.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..errors import (
    BackendUnavailableError,
    ModelLoadError,
    TimeoutError_,
    UpstreamHTTPError,
)
from ..proc import ProcessSupervisor
from ..security import (
    build_argv,
    validate_executable,
    validate_model_path,
    validate_upstream_url,
)
from .base import BackendAdapter, HealthResult

#: Max characters of an upstream error body reflected back to the caller. Upstream
#: bodies are untrusted and may be large or echo request content, so we cap and
#: strip control characters before surfacing any of it.
_UPSTREAM_MSG_MAX = 200
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_upstream_message(status: int, body: str | None) -> str:
    """Build a short, safe client-facing message from an upstream error body.

    Prefers an OpenAI-style ``{"error": {"message": ...}}`` field, falling back to
    the raw body. The result is control-character-stripped and truncated to
    :data:`_UPSTREAM_MSG_MAX` so a huge or hostile upstream body can never be
    reflected verbatim (and we never echo request/secret material at length).
    """
    msg = ""
    if body:
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("type") or "")
            elif isinstance(err, str):
                msg = err
            if not msg:
                msg = str(data.get("message") or "")
        if not msg:
            msg = body
    msg = _CONTROL_CHARS.sub(" ", msg).strip()
    if len(msg) > _UPSTREAM_MSG_MAX:
        msg = msg[:_UPSTREAM_MSG_MAX].rstrip() + "…"
    base = f"upstream returned {status}"
    return f"{base}: {msg}" if msg else base


def _retry_after_headers(headers: Any) -> dict[str, str] | None:
    """Extract only the ``Retry-After`` header (if any) to carry on the error.

    Never forwards arbitrary upstream headers (which could leak upstream cookies /
    auth echoes) — only the one header the retry policy needs.
    """
    try:
        ra = headers.get("retry-after")
    except AttributeError:
        ra = None
    return {"retry-after": str(ra)} if ra else None


def _parse_waiting(metrics_text: str) -> float | None:
    """Sum ``vllm:num_requests_waiting`` samples from Prometheus text, or None.

    vLLM exposes queue depth as ``vllm:num_requests_waiting{...} <value>`` (one
    series per model/engine). We sum the sample values across series and ignore
    ``# HELP`` / ``# TYPE`` comment lines. Returns None when the metric is absent
    so the caller can fall back to in-flight counts.
    """
    total: float | None = None
    for line in metrics_text.splitlines():
        if not line.startswith("vllm:num_requests_waiting"):
            continue
        if line.startswith("#"):
            continue
        try:
            value = float(line.rsplit(" ", 1)[-1])
        except (ValueError, IndexError):
            continue
        total = value if total is None else total + value
    return total


class HTTPBackendAdapter(BackendAdapter):
    """OpenAI-compatible HTTP backend with optional broker-managed process."""

    name = "http"
    #: Path the runtime serves chat completions on.
    chat_path = "/v1/chat/completions"
    #: Path the runtime serves embeddings on (OpenAI-compatible).
    embeddings_path = "/v1/embeddings"
    #: Path the runtime serves reranking on (Cohere/Jina-compatible; vLLM +
    #: llama.cpp both expose this).
    rerank_path = "/v1/rerank"

    def __init__(self, entry, settings) -> None:
        super().__init__(entry, settings)
        self._supervisor: ProcessSupervisor | None = None
        self._client: httpx.AsyncClient | None = None
        self._inflight: dict[str, asyncio.Event] = {}
        # ADR 0020: PID/pgid of the last spawned runtime kept after stop() so the
        # measured-release wait can match telemetry holders; release-ok flag set
        # False when stop() left the process alive after SIGKILL.
        self._last_pid: int | None = None
        self._last_release_ok: bool = True

    # --- helpers ---------------------------------------------------------
    def _base_url(self) -> str:
        if not self.entry.base_url:
            raise ModelLoadError(
                "backend requires base_url",
                backend=self.name,
                model=self.entry.alias,
                remediation="set base_url in the registry entry",
            )
        return self.entry.base_url.rstrip("/")

    def _allowed_bases(self) -> list[str]:
        bases = list(self.settings.extra_allowed_backend_urls)
        if self.entry.base_url:
            bases.append(self.entry.base_url)
        return bases

    def _validated_url(self, path: str) -> str:
        url = self._base_url() + path
        return validate_upstream_url(
            url,
            self._allowed_bases(),
            allow_loopback=self.settings.allow_loopback_backends,
        )

    def _client_or_new(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.entry.request_timeout_seconds)
        return self._client

    def _json_or_unavailable(self, resp: httpx.Response, *, what: str) -> Any:
        """Parse an upstream response body as JSON, or raise 502.

        A runtime that returns a non-JSON body (HTML error page, truncated
        stream, proxy banner) is a backend fault, not a broker bug. Translate the
        decode failure into :class:`BackendUnavailableError` so it maps to a 502
        and participates in pool failover/cooldown instead of crashing with a 500.
        """
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
            raise BackendUnavailableError(
                f"backend returned a non-JSON {what} response",
                backend=self.name,
                model=self.entry.alias,
                remediation="check the runtime process/endpoint",
            ) from exc

    def _build_startup_argv(self) -> list[str]:
        exe = self.entry.startup_command[0]
        safe_exe = validate_executable(exe, self.settings.allowed_exec_dirs)
        subs: dict[str, str] = {"exe": safe_exe}
        if self.entry.model_path:
            subs["model_path"] = validate_model_path(
                self.entry.model_path, self.settings.allowed_model_roots
            )
        subs["runtime_model_id"] = self.entry.runtime_model_id
        subs["context_limit"] = str(self.entry.context_limit)
        subs["output_limit"] = str(self.entry.output_limit)
        if self.entry.gpu.tensor_split:
            subs["tensor_split"] = ",".join(str(x) for x in self.entry.gpu.tensor_split)
        if self.entry.gpu.layer_split:
            subs["layer_split"] = str(self.entry.gpu.layer_split)
        if self.entry.gpu.tensor_parallel_size:
            subs["tensor_parallel_size"] = str(self.entry.gpu.tensor_parallel_size)
        if self.entry.gpu.pipeline_parallel_size:
            subs["pipeline_parallel_size"] = str(self.entry.gpu.pipeline_parallel_size)
        if self.entry.gpu.gpu_memory_fraction:
            subs["gpu_memory_fraction"] = str(self.entry.gpu.gpu_memory_fraction)
        # Replace the first token (exe placeholder or literal) appropriately.
        template = list(self.entry.startup_command)
        template[0] = "{exe}"
        return build_argv(template, subs)

    def _device_env_var(self) -> str:
        """Device-isolation env var for the active telemetry provider (ADR 0020).

        Explicit ``BROKER_GPU_DEVICE_ENV_VAR`` wins; otherwise the AMD provider
        uses ``ROCR_VISIBLE_DEVICES`` (the ROCm runtime-level isolation var on
        Linux) and every other provider — including the default OFF — uses
        ``CUDA_VISIBLE_DEVICES`` (the portable default, unchanged from v0.6.0).
        """
        override = getattr(self.settings, "gpu_device_env_var", None)
        if override:
            return override
        if getattr(self.settings, "gpu_telemetry", "off") == "amd":
            return "ROCR_VISIBLE_DEVICES"
        return "CUDA_VISIBLE_DEVICES"

    def _process_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        var = self._device_env_var()
        if self.entry.gpu.device_ids:
            env[var] = ",".join(str(d) for d in self.entry.gpu.device_ids)
        elif self.settings.cuda_visible_devices is not None:
            env[var] = self.settings.cuda_visible_devices
        return env

    # --- configuration ---------------------------------------------------
    def validate_config(self) -> None:
        if self.entry.startup_command:
            # Validate executable + model path *without* starting anything.
            validate_executable(self.entry.startup_command[0], self.settings.allowed_exec_dirs)
            if self.entry.model_path:
                validate_model_path(self.entry.model_path, self.settings.allowed_model_roots)
        if self.entry.base_url:
            validate_upstream_url(
                self.entry.base_url,
                self._allowed_bases(),
                allow_loopback=self.settings.allow_loopback_backends,
            )

    # --- lifecycle -------------------------------------------------------
    async def start(self) -> int | None:
        if not self.entry.startup_command:
            # External server manages its own lifetime (e.g. a shared daemon).
            return None
        argv = self._build_startup_argv()
        self._supervisor = ProcessSupervisor(
            argv,
            env=self._process_env(),
            alias=self.entry.alias,
            backend=self.name,
        )
        pid = await self._supervisor.start()
        self._last_pid = pid
        self._last_release_ok = True
        return pid

    async def wait_until_ready(self, timeout: float) -> None:
        probe = self.entry.readiness
        if probe.kind == "log" and self._supervisor and probe.log_marker:
            await self._supervisor.wait_for_log(probe.log_marker, timeout)
            return
        # HTTP readiness polling.
        url = self._validated_url(probe.path)
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        client = self._client_or_new()
        last_err = ""
        while loop.time() < deadline:
            if self._supervisor and not self._supervisor.is_running():
                raise ModelLoadError(
                    f"runtime exited (code {self._supervisor.returncode}) during startup",
                    backend=self.name,
                    model=self.entry.alias,
                    remediation="inspect logs:\n" + self._supervisor.recent_log(10),
                )
            try:
                resp = await client.get(url, timeout=probe.interval_seconds + 2)
                if resp.status_code == probe.expect_status:
                    return
                last_err = f"status {resp.status_code}"
            except (httpx.HTTPError, OSError) as exc:
                last_err = str(exc)
            await asyncio.sleep(probe.interval_seconds)
        raise TimeoutError_(
            f"readiness probe timed out after {timeout}s ({last_err})",
            backend=self.name,
            model=self.entry.alias,
            remediation="increase startup_timeout or check the runtime",
        )

    async def health(self) -> HealthResult:
        if self._supervisor and not self._supervisor.is_running():
            return HealthResult(healthy=False, detail="process not running")
        try:
            url = self._validated_url(self.entry.readiness.path)
            client = self._client_or_new()
            resp = await client.get(url, timeout=5)
            ok = resp.status_code == self.entry.readiness.expect_status
            return HealthResult(healthy=ok, detail=f"probe {resp.status_code}")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(healthy=False, detail=str(exc))

    async def unload(self) -> None:
        # Broker-managed runtimes release the model by stopping the process.
        await self.stop()

    async def stop(self) -> None:
        if self._supervisor:
            released = await self._supervisor.stop(grace=self.settings.shutdown_timeout_seconds)
            self._last_release_ok = released
            self._supervisor = None
        if self._client:
            await self._client.aclose()
            self._client = None

    def process_pid(self) -> int | None:
        if self._supervisor is not None:
            return self._supervisor.pid
        return self._last_pid

    async def process_alive(self) -> bool:
        return self._supervisor is not None and self._supervisor.is_running()

    def last_release_ok(self) -> bool:
        return self._last_release_ok

    # --- inference -------------------------------------------------------
    def _request_body(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = dict(payload)
        # Pin the upstream model id; never trust request to pick a raw runtime id.
        body["model"] = self.entry.runtime_model_id
        return body

    def _raise_upstream(self, status: int, body: str | None, headers: Any) -> None:
        """Raise a typed :class:`UpstreamHTTPError` for an upstream error status.

        The backend replied with ``status >= 400``; we surface it as a typed error
        carrying the status + ``Retry-After`` (sanitized message) so the per-error
        policy classifies it by status instead of collapsing it to a 502/CONNECTION.
        """
        raise UpstreamHTTPError(
            _sanitize_upstream_message(status, body),
            upstream_status=status,
            backend=self.name,
            model=self.entry.alias,
            headers=_retry_after_headers(headers),
        )

    async def chat(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        url = self._validated_url(self.chat_path)
        client = self._client_or_new()
        body = self._request_body(payload)
        body["stream"] = False
        try:
            resp = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"backend request failed: {exc}",
                backend=self.name,
                model=self.entry.alias,
                remediation="check the runtime process/endpoint",
            ) from exc
        if resp.status_code >= 400:
            self._raise_upstream(resp.status_code, resp.text, resp.headers)
        data = self._json_or_unavailable(resp, what="chat")
        # Present the public alias back to the caller.
        data["model"] = self.entry.alias
        return data

    async def stream_chat(  # type: ignore[override]
        self, payload: dict[str, Any], request_id: str
    ) -> AsyncIterator[bytes]:
        url = self._validated_url(self.chat_path)
        client = self._client_or_new()
        body = self._request_body(payload)
        body["stream"] = True
        cancel_event = asyncio.Event()
        self._inflight[request_id] = cancel_event
        try:
            async with client.stream("POST", url, json=body) as resp:
                if resp.status_code >= 400:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    self._raise_upstream(resp.status_code, text, resp.headers)
                async for line in resp.aiter_lines():
                    if cancel_event.is_set():
                        break
                    if not line:
                        continue
                    # Upstream (e.g. Ollama's OpenAI endpoint) emits its own
                    # terminal `data: [DONE]`; drop it so we send exactly one,
                    # appended below, and never a duplicate sentinel.
                    if line.strip() == "data: [DONE]":
                        continue
                    yield (line + "\n\n").encode()
                if not cancel_event.is_set():
                    yield b"data: [DONE]\n\n"
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"backend stream failed: {exc}",
                backend=self.name,
                model=self.entry.alias,
            ) from exc
        finally:
            self._inflight.pop(request_id, None)

    async def cancel(self, request_id: str) -> None:
        ev = self._inflight.get(request_id)
        if ev:
            ev.set()

    async def embeddings(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Proxy to the runtime's OpenAI-compatible ``/v1/embeddings``.

        vLLM / llama.cpp serve an OpenAI-shaped embeddings response, so we pin the
        upstream model id and pass the body through unchanged.
        """
        url = self._validated_url(self.embeddings_path)
        client = self._client_or_new()
        body = self._request_body(payload)
        try:
            resp = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"backend request failed: {exc}",
                backend=self.name,
                model=self.entry.alias,
                remediation="check the runtime process/endpoint",
            ) from exc
        if resp.status_code >= 400:
            self._raise_upstream(resp.status_code, resp.text, resp.headers)
        data = self._json_or_unavailable(resp, what="embeddings")
        if isinstance(data, dict):
            data["model"] = self.entry.alias
        return data

    async def rerank(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Proxy to the runtime's Cohere/Jina-compatible ``/v1/rerank``.

        vLLM and llama.cpp both serve a Cohere/Jina-compatible rerank route, so we
        pin the upstream model id and forward ``query``/``documents``/``top_n``.
        The response is re-normalized by the broker (sort desc, honor ``top_n``,
        attach ``document`` from the request only when ``return_documents``) so
        every backend yields one consistent shape — the backend's own document
        echo is intentionally ignored.
        """
        query = payload.get("query")
        documents = [str(d) for d in (payload.get("documents") or [])]
        top_n = payload.get("top_n")
        return_documents = bool(payload.get("return_documents", False))
        body: dict[str, Any] = {
            "model": self.entry.runtime_model_id,
            "query": query,
            "documents": documents,
            # The broker attaches documents itself from the request list.
            "return_documents": False,
        }
        if top_n is not None:
            body["top_n"] = top_n
        for k in ("max_tokens_per_doc", "max_chunks_per_doc", "truncate"):
            if k in payload:
                body[k] = payload[k]
        url = self._validated_url(self.rerank_path)
        client = self._client_or_new()
        try:
            resp = await client.post(url, json=body)
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(
                f"backend request failed: {exc}",
                backend=self.name,
                model=self.entry.alias,
                remediation="check the runtime process/endpoint",
            ) from exc
        if resp.status_code >= 400:
            self._raise_upstream(resp.status_code, resp.text, resp.headers)
        from ..openai_compat import RerankContractError, build_rerank_response

        data = self._json_or_unavailable(resp, what="rerank")
        raw = data.get("results") if isinstance(data, dict) else None
        if raw is None and isinstance(data, dict) and isinstance(data.get("data"), list):
            raw = data["data"]
        if not isinstance(raw, list):
            raise BackendUnavailableError(
                "rerank backend returned no results array",
                backend=self.name,
                model=self.entry.alias,
            )
        # Normalize only the score key here (vLLM uses ``relevance_score``,
        # some runtimes use ``score``); every value is then strictly validated
        # by build_rerank_response. Do NOT drop/skip malformed items — a bad
        # upstream result is a backend contract violation (502), not something
        # to silently filter or coerce.
        scored: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise BackendUnavailableError(
                    "rerank backend returned a non-object result",
                    backend=self.name,
                    model=self.entry.alias,
                )
            scored.append(
                {
                    "index": item.get("index"),
                    "relevance_score": item.get("relevance_score", item.get("score")),
                }
            )
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            usage = None
        try:
            # ``id`` stays broker-generated (``rerank-<request_id>``); the
            # upstream id is never echoed verbatim.
            return build_rerank_response(
                scored=scored,
                documents=documents,
                model=self.entry.alias,
                request_id=request_id,
                top_n=top_n,
                return_documents=return_documents,
                usage=usage,
            )
        except RerankContractError as exc:
            raise BackendUnavailableError(
                f"rerank backend returned an invalid result: {exc}",
                backend=self.name,
                model=self.entry.alias,
                remediation="check the runtime process/endpoint",
            ) from exc

    async def collect_load(self) -> float | None:
        """Scrape ``vllm:num_requests_waiting`` from the runtime's /metrics.

        Returns None (fall back to in-flight counts) when the process is not
        running, the endpoint is absent/erroring, or the metric is missing. Never
        starts a process — a non-loading, best-effort read only.
        """
        if self._supervisor is not None and not self._supervisor.is_running():
            return None
        if not self.entry.base_url:
            return None
        try:
            url = self._validated_url("/metrics")
            client = self._client_or_new()
            resp = await client.get(url, timeout=5)
            if resp.status_code != 200:
                return None
            return _parse_waiting(resp.text)
        except Exception:  # noqa: BLE001 - best effort; never raise into the probe
            return None

    async def collect_status(self) -> dict[str, Any]:
        return {
            "backend": self.name,
            "alias": self.entry.alias,
            "base_url": self.entry.base_url,
            "pid": self._supervisor.pid if self._supervisor else None,
            "process_running": self._supervisor.is_running() if self._supervisor else None,
            "gpu": {
                "device_ids": self.entry.gpu.device_ids,
                "tensor_split": self.entry.gpu.tensor_split,
                "layer_split": self.entry.gpu.layer_split,
                "tensor_parallel_size": self.entry.gpu.tensor_parallel_size,
            },
        }
