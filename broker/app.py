"""FastAPI application: OpenAI-compatible routes + ops endpoints.

Routes
------
* ``GET  /v1/models``            — list registered models (aliases + caps)
* ``POST /v1/chat/completions``  — chat (streaming + non-streaming)
* ``POST /v1/completions``       — legacy text completions (translated to chat)
* ``POST /v1/responses``         — OpenAI Responses API (translated to chat)
* ``POST /v1/embeddings``        — embeddings (routed to an embedding backend)
* ``GET  /healthz``              — liveness (always 200 if process is up)
* ``GET  /readyz``               — readiness (config + registry loaded)
* ``GET  /status``               — per-model lifecycle + GPU allocation
* ``GET  /usage``                — aggregated request/token usage log
* ``GET  /metrics``              — Prometheus metrics
* ``GET  /api/tags``             — optional Ollama-style discovery (compat)

Cross-cutting: API-key auth, request-size limit, request IDs, structured error
envelopes, configurable concurrency/timeouts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import __version__
from .auth import check_auth
from .config import Settings, get_settings
from .errors import BrokerError, RequestTooLargeError, ValidationError
from .manager import ModelManager
from .metrics import INFLIGHT, LOADED_MODELS, REQUEST_LATENCY, REQUESTS
from .openai_compat import (
    chat_sse_to_responses_sse,
    chat_sse_to_text_sse,
    chat_to_response,
    chat_to_text_completion,
    prompt_to_messages,
    responses_input_to_messages,
)
from .registry import load_registry
from .security import reject_request_flags
from .usage import UsageLog

log = logging.getLogger("broker.app")


class ChatMessage(BaseModel):
    role: str
    content: Any = None
    name: str | None = None
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = {"extra": "allow"}  # pass through OpenAI params we don't model
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    tools: list[dict] | None = None
    tool_choice: Any | None = None


def _session_key(request: Request, body: dict) -> str:
    """Sticky-routing session key: X-Session-Id header, else OpenAI ``user``,
    else client IP, else ``anon`` — shared by all inference endpoints."""
    user_field = body.get("user")
    return (
        request.headers.get("x-session-id")
        or (user_field if isinstance(user_field, str) and user_field else None)
        or (request.client.host if request.client else None)
        or "anon"
    )


def _setup_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    if settings.log_format == "json":

        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return json.dumps(
                    {
                        "ts": record.created,
                        "level": record.levelname,
                        "logger": record.name,
                        "msg": record.getMessage(),
                    }
                )

        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _setup_logging(settings)
    settings.validate_runtime()
    registry = load_registry(settings.registry_path, set(settings.enabled_backends))
    manager = ModelManager(registry, settings)
    manager.validate_all()  # fail-fast; no models load here
    if (
        settings.default_model
        and settings.default_model not in manager.pools
        and settings.default_model not in manager.models
    ):
        raise ValidationError(f"default_model {settings.default_model!r} not in registry")

    usage_path = settings.usage_log_path or f"{settings.cache_dir.rstrip('/')}/usage.sqlite3"
    usage = UsageLog(usage_path, enabled=settings.usage_log_enabled)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await manager.start_sweeper()
        await manager.start_health_probe()
        log.info(
            "broker %s started; %d models registered (none loaded)",
            __version__,
            len(manager.models),
        )
        try:
            yield
        finally:
            await manager.shutdown()
            usage.close()

    app = FastAPI(title="backend-agnostic-broker", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.state.settings = settings
    app.state.ready = True
    app.state.usage = usage

    def err_response(exc: BrokerError, request_id: str) -> JSONResponse:
        payload = exc.to_payload()
        payload["error"]["request_id"] = request_id
        return JSONResponse(status_code=exc.http_status, content=payload)

    @app.middleware("http")
    async def context_mw(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id
        # Request-size limit (content-length fast path).
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > settings.max_request_bytes:
            return err_response(
                RequestTooLargeError(
                    f"request body exceeds {settings.max_request_bytes} bytes",
                    remediation="reduce request size or raise BROKER_MAX_REQUEST_BYTES",
                ),
                request_id,
            )
        try:
            resp = await call_next(request)
        except BrokerError as exc:
            return err_response(exc, request_id)
        resp.headers["x-request-id"] = request_id
        return resp

    def authed(authorization: str | None, x_api_key: str | None) -> None:
        check_auth(settings, authorization, x_api_key)

    # --- ops endpoints ---------------------------------------------------
    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    async def readyz(request: Request):
        if not getattr(app.state, "ready", False):
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {"status": "ready", "models_registered": len(manager.models)}

    @app.get("/healthz/backends")
    async def healthz_backends(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """Live, non-loading liveness of every backend member.

        Ollama daemons are probed via GET /api/tags; broker-managed runtimes are
        probed only while their process is running (unloaded = ``not_loaded``,
        never a spawn). Returns 200 with ``status: ok|degraded``.
        """
        authed(authorization, x_api_key)
        return await manager.backends_health()

    @app.get("/status")
    async def status(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        authed(authorization, x_api_key)
        LOADED_MODELS.set(manager.loaded_count())
        return await manager.status()

    @app.get("/metrics")
    async def metrics():
        if not settings.metrics_enabled:
            return PlainTextResponse("metrics disabled", status_code=404)
        LOADED_MODELS.set(manager.loaded_count())
        return PlainTextResponse(generate_latest().decode("utf-8"), media_type=CONTENT_TYPE_LATEST)

    # --- OpenAI endpoints ------------------------------------------------
    @app.get("/v1/models")
    async def list_models(
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        authed(authorization, x_api_key)
        return {"object": "list", "data": manager.public_models()}

    @app.get("/api/tags")
    async def ollama_tags(
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """Optional Ollama-style discovery compatibility endpoint."""
        authed(authorization, x_api_key)
        models = [
            {
                "name": d["id"],
                "model": d["id"],
                "details": {
                    "family": d["backend"],
                    "context_length": d["context_limit"],
                },
            }
            for d in manager.public_models()
        ]
        return {"models": models}

    @app.get("/api/ps")
    async def ollama_ps(
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """Passthrough of Ollama ``/api/ps`` across daemons — what is actually
        resident in memory, distinct from the broker's lifecycle state. Querying
        this never loads a model.
        """
        authed(authorization, x_api_key)
        return await manager.ollama_ps()

    @app.post("/v1/chat/completions")
    async def chat_completions(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        authed(authorization, x_api_key)
        key_id = UsageLog.key_fingerprint(x_api_key or authorization)
        request_id = request.state.request_id
        raw = await request.body()
        if len(raw) > settings.max_request_bytes:
            raise RequestTooLargeError(f"request body exceeds {settings.max_request_bytes} bytes")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON body: {exc}") from exc
        try:
            req = ChatCompletionRequest.model_validate(body)
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"invalid chat request: {exc}") from exc

        # Reject request-supplied flag-like/unsafe tokens in the model selector.
        reject_request_flags(req.model, "model")

        alias = req.model
        payload = req.model_dump(exclude_none=True)
        payload["messages"] = [m.model_dump(exclude_none=True) for m in req.messages]

        # Sticky-routing session key: explicit header wins, else the OpenAI
        # `user` field, else the client IP. Keeps a conversation pinned to one
        # pool member (warm cache) while distinct sessions still spread.
        user_field = body.get("user")
        session_key = (
            request.headers.get("x-session-id")
            or (user_field if isinstance(user_field, str) and user_field else None)
            or (request.client.host if request.client else None)
            or "anon"
        )

        start = time.monotonic()
        if req.stream:

            async def gen():
                INFLIGHT.inc()
                stream_status = "ok"
                try:
                    async for chunk in manager.stream_chat(
                        alias, payload, request_id, route_key=session_key
                    ):
                        yield chunk
                    REQUESTS.labels("chat_stream", alias, "ok").inc()
                except BrokerError as exc:
                    stream_status = "error"
                    payload_err = exc.to_payload()
                    payload_err["error"]["request_id"] = request_id
                    REQUESTS.labels("chat_stream", alias, "error").inc()
                    yield f"data: {json.dumps(payload_err)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                finally:
                    INFLIGHT.dec()
                    REQUEST_LATENCY.labels("chat_stream", alias).observe(time.monotonic() - start)
                    usage.record(
                        endpoint="chat_stream",
                        requested=alias,
                        model=alias,
                        request_id=request_id,
                        api_key_id=key_id,
                        latency_ms=(time.monotonic() - start) * 1000,
                        status=stream_status,
                        stream=True,
                    )

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={"x-request-id": request_id, "cache-control": "no-cache"},
            )

        INFLIGHT.inc()
        try:
            result = await manager.chat(alias, payload, request_id, route_key=session_key)
            REQUESTS.labels("chat", alias, "ok").inc()
            u = result.get("usage") or {}
            usage.record(
                endpoint="chat",
                requested=alias,
                model=result.get("model", alias),
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(content=result, headers={"x-request-id": request_id})
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("chat", alias).observe(time.monotonic() - start)

    @app.get("/usage")
    async def usage_summary(
        request: Request,
        limit: int = 20,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """Aggregated request/token usage from the usage log (see ADR 0007)."""
        authed(authorization, x_api_key)
        return usage.summary(limit=limit)

    @app.post("/v1/completions")
    async def completions(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """Legacy text completions, translated to the chat pipeline (ADR 0006)."""
        authed(authorization, x_api_key)
        key_id = UsageLog.key_fingerprint(x_api_key or authorization)
        request_id = request.state.request_id
        raw = await request.body()
        if len(raw) > settings.max_request_bytes:
            raise RequestTooLargeError(f"request body exceeds {settings.max_request_bytes} bytes")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON body: {exc}") from exc
        model = body.get("model")
        if not model or not isinstance(model, str):
            raise ValidationError("`model` is required")
        reject_request_flags(model, "model")
        stream = bool(body.get("stream"))
        chat_payload = {k: v for k, v in body.items() if k != "prompt"}
        chat_payload["model"] = model
        chat_payload["messages"] = prompt_to_messages(body.get("prompt"))
        session_key = _session_key(request, body)
        start = time.monotonic()

        if stream:

            async def gen():
                INFLIGHT.inc()
                st = "ok"
                try:
                    aiter = manager.stream_chat(
                        model, chat_payload, request_id, route_key=session_key
                    )
                    async for out in chat_sse_to_text_sse(aiter, model):
                        yield out
                    REQUESTS.labels("completions_stream", model, "ok").inc()
                except BrokerError as exc:
                    st = "error"
                    payload_err = exc.to_payload()
                    payload_err["error"]["request_id"] = request_id
                    REQUESTS.labels("completions_stream", model, "error").inc()
                    yield f"data: {json.dumps(payload_err)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                finally:
                    INFLIGHT.dec()
                    REQUEST_LATENCY.labels("completions_stream", model).observe(
                        time.monotonic() - start
                    )
                    usage.record(
                        endpoint="completions_stream",
                        requested=model,
                        model=model,
                        request_id=request_id,
                        api_key_id=key_id,
                        latency_ms=(time.monotonic() - start) * 1000,
                        status=st,
                        stream=True,
                    )

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={"x-request-id": request_id, "cache-control": "no-cache"},
            )

        INFLIGHT.inc()
        try:
            result = await manager.chat(model, chat_payload, request_id, route_key=session_key)
            out = chat_to_text_completion(result, model)
            REQUESTS.labels("completions", model, "ok").inc()
            u = result.get("usage") or {}
            usage.record(
                endpoint="completions",
                requested=model,
                model=result.get("model", model),
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(content=out, headers={"x-request-id": request_id})
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("completions", model).observe(time.monotonic() - start)

    @app.post("/v1/responses")
    async def responses(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """OpenAI Responses API, translated to the chat pipeline (ADR 0006)."""
        authed(authorization, x_api_key)
        key_id = UsageLog.key_fingerprint(x_api_key or authorization)
        request_id = request.state.request_id
        raw = await request.body()
        if len(raw) > settings.max_request_bytes:
            raise RequestTooLargeError(f"request body exceeds {settings.max_request_bytes} bytes")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON body: {exc}") from exc
        model = body.get("model")
        if not model or not isinstance(model, str):
            raise ValidationError("`model` is required")
        reject_request_flags(model, "model")
        stream = bool(body.get("stream"))
        passthrough = {
            k: v for k, v in body.items() if k not in ("input", "instructions", "stream")
        }
        if "max_output_tokens" in passthrough and "max_tokens" not in passthrough:
            passthrough["max_tokens"] = passthrough.pop("max_output_tokens")
        chat_payload = dict(passthrough)
        chat_payload["model"] = model
        chat_payload["messages"] = responses_input_to_messages(
            body.get("input"), body.get("instructions")
        )
        session_key = _session_key(request, body)
        start = time.monotonic()

        if stream:

            async def gen():
                INFLIGHT.inc()
                st = "ok"
                try:
                    aiter = manager.stream_chat(
                        model, chat_payload, request_id, route_key=session_key
                    )
                    async for out in chat_sse_to_responses_sse(aiter, model):
                        yield out
                    REQUESTS.labels("responses_stream", model, "ok").inc()
                except BrokerError as exc:
                    st = "error"
                    payload_err = exc.to_payload()
                    payload_err["error"]["request_id"] = request_id
                    REQUESTS.labels("responses_stream", model, "error").inc()
                    yield f"data: {json.dumps(payload_err)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
                finally:
                    INFLIGHT.dec()
                    REQUEST_LATENCY.labels("responses_stream", model).observe(
                        time.monotonic() - start
                    )
                    usage.record(
                        endpoint="responses_stream",
                        requested=model,
                        model=model,
                        request_id=request_id,
                        api_key_id=key_id,
                        latency_ms=(time.monotonic() - start) * 1000,
                        status=st,
                        stream=True,
                    )

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={"x-request-id": request_id, "cache-control": "no-cache"},
            )

        INFLIGHT.inc()
        try:
            result = await manager.chat(model, chat_payload, request_id, route_key=session_key)
            out = chat_to_response(result, model)
            REQUESTS.labels("responses", model, "ok").inc()
            u = result.get("usage") or {}
            usage.record(
                endpoint="responses",
                requested=model,
                model=result.get("model", model),
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=u.get("completion_tokens", 0),
                total_tokens=u.get("total_tokens", 0),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(content=out, headers={"x-request-id": request_id})
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("responses", model).observe(time.monotonic() - start)

    @app.post("/v1/embeddings")
    async def embeddings(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """OpenAI embeddings — routed to an embedding-capable backend (ADR 0006)."""
        authed(authorization, x_api_key)
        key_id = UsageLog.key_fingerprint(x_api_key or authorization)
        request_id = request.state.request_id
        raw = await request.body()
        if len(raw) > settings.max_request_bytes:
            raise RequestTooLargeError(f"request body exceeds {settings.max_request_bytes} bytes")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"invalid JSON body: {exc}") from exc
        model = body.get("model")
        if not model or not isinstance(model, str):
            raise ValidationError("`model` is required")
        reject_request_flags(model, "model")
        if body.get("input") is None:
            raise ValidationError("`input` is required (string or list of strings)")
        payload = {"input": body["input"]}
        for k in ("truncate", "options", "keep_alive", "encoding_format", "dimensions", "user"):
            if k in body:
                payload[k] = body[k]
        session_key = _session_key(request, body)
        start = time.monotonic()
        INFLIGHT.inc()
        try:
            result = await manager.embeddings(model, payload, request_id, route_key=session_key)
            REQUESTS.labels("embeddings", model, "ok").inc()
            u = result.get("usage") or {}
            usage.record(
                endpoint="embeddings",
                requested=model,
                model=result.get("model", model),
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=u.get("prompt_tokens", 0),
                completion_tokens=0,
                total_tokens=u.get("total_tokens", u.get("prompt_tokens", 0)),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(content=result, headers={"x-request-id": request_id})
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("embeddings", model).observe(time.monotonic() - start)

    @app.exception_handler(BrokerError)
    async def broker_error_handler(request: Request, exc: BrokerError):
        rid = getattr(request.state, "request_id", "unknown")
        return err_response(exc, rid)

    return app
