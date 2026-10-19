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
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import __version__
from .auth import admin_key_match, extract_key
from .cache import ResponseCache
from .config import Settings, get_settings
from .errors import (
    AuthError,
    AuthorizationError,
    BrokerError,
    RequestTooLargeError,
    ValidationError,
)
from .governance import Governor
from .keys import KeyStore, Principal
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
from .ui import register_ui
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


# Placement label charset (mirrors broker/registry.py) for header validation.
_PLACEMENT_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _placement_requirement(request: Request, settings: Settings) -> frozenset[str]:
    """Required placement labels = BROKER_DEFAULT_PLACEMENT ∪ x-broker-placement.

    The header is a comma-separated list; the requirement is set server-side (a
    trusted caller/gateway), never downgradable by an end client below the config
    default. Invalid labels are rejected (400) before any routing (ADR 0011).
    """
    labels = set(settings.default_placement)
    raw = request.headers.get("x-broker-placement")
    if raw:
        for part in raw.split(","):
            label = part.strip()
            if not label:
                continue
            if not _PLACEMENT_LABEL_RE.match(label):
                raise ValidationError(
                    f"invalid x-broker-placement label {label!r}",
                    remediation="use letters/digits/'.'/'-'/'_'/':' (e.g. 'local', 'region:eu')",
                )
            labels.add(label)
    return frozenset(labels)


def _latency_budget_ms(request: Request) -> float | None:
    """Per-request latency budget from the x-broker-latency-budget-ms header.

    Returns ``None`` when absent so the router applies the registry
    ``router.latency_budget_ms`` then ``BROKER_ROUTER_LATENCY_BUDGET_MS`` default.
    """
    raw = request.headers.get("x-broker-latency-budget-ms")
    if raw is None:
        return None
    try:
        v = float(raw)
    except ValueError as exc:
        raise ValidationError(
            f"invalid x-broker-latency-budget-ms {raw!r}",
            remediation="send a positive number of milliseconds",
        ) from exc
    if v <= 0:
        raise ValidationError("x-broker-latency-budget-ms must be > 0")
    return v


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
    # Give the manager/router access to the usage log for latency estimates and for
    # recording intermediate cascade hops (ADR 0011).
    manager.usage = usage

    # Per-key governance (ADR 0008): virtual keys (hashed) + legacy flat keys,
    # and the governor that enforces rpm/tpm/concurrency/budgets on the shared
    # StateStore. Opt-in: with no keys file and no per-key limits this is inert.
    key_store = KeyStore(settings.keys_path, settings.api_keys)
    governor = Governor(
        manager.state,
        window_seconds=settings.rate_limit_window_seconds,
        pricing=registry.pricing,
    )

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
    app.state.key_store = key_store
    app.state.governor = governor
    # Exact response cache (ADR 0012), backed by the manager's StateStore. A no-op
    # pass-through unless BROKER_RESPONSE_CACHE_ENABLED.
    response_cache = ResponseCache(manager.state, settings)
    app.state.response_cache = response_cache
    admin_log = logging.getLogger("broker.admin")

    def _cache_bypass(request: Request) -> bool:
        """True if the caller asked to skip the cache (read AND write).

        Honors the HTTP-standard ``Cache-Control: no-cache``/``no-store`` request
        directives (treated as a full bypass — the broker has no revalidation
        protocol, so not-serving-stored is the safe superset) and the
        broker-specific ``x-broker-cache: bypass`` header.
        """
        cc = (request.headers.get("cache-control") or "").lower()
        xb = (request.headers.get("x-broker-cache") or "").lower()
        return "no-cache" in cc or "no-store" in cc or xb == "bypass"

    async def _cached_serve(
        *,
        request: Request,
        key_scope: str | None,
        endpoint: str,
        alias: str,
        keyed_payload: dict,
        serve,
        always_deterministic: bool = False,
    ) -> tuple[dict, str]:
        """Serve a request through the response cache. Returns (result, state).

        ``state`` is ``hit|miss|bypass`` (also set as the ``x-broker-cache``
        response header). ``serve(name)`` runs the real pipeline for the resolved
        public model and returns the final response dict. Errors propagate
        uncached; only a successful result is stored.
        """
        if not response_cache.enabled:
            return await serve(alias), "miss"
        if _cache_bypass(request):
            return await serve(alias), "bypass"
        resolved = await manager.resolve_public(alias, keyed_payload)
        if not response_cache.cacheable(keyed_payload, always_deterministic=always_deterministic):
            return await serve(alias), "miss"
        ckey = ResponseCache.make_key(
            scope=key_scope or "anon", model=resolved, endpoint=endpoint, payload=keyed_payload
        )
        with manager.tracer.span("cache_lookup", {"broker.cache.endpoint": endpoint}) as sp:
            hit = await response_cache.get(ckey)
            sp.set_attribute("broker.cache.hit", hit is not None)
        if hit is not None:
            return hit, "hit"
        # Serve through the ORIGINAL alias (not the pre-resolved model) so auto,
        # cascade, placement and per-key scoping all run in the manager. ``resolved``
        # is only the cache-key model; a cascade's final served model is in the body.
        result = await serve(alias)
        await response_cache.store(ckey, result)
        return result, "miss"

    def require_admin(
        authorization: str | None, x_api_key: str | None, x_admin_key: str | None
    ) -> None:
        """Gate for /admin/*: 400 ``admin_disabled`` when disabled, else require an
        admin credential.

        Admin is authorized by EITHER a virtual key whose scope is ``admin``
        (ADR 0008 key governance) OR a configured ``BROKER_ADMIN_API_KEYS`` value
        (ADR 0012 back-compat). Both credentials are compared byte-wise so a
        malformed (non-ASCII / over-long) key fails closed with a clean 401, and
        the admin surface stays locked even when data-plane auth is disabled.
        """
        if not settings.admin_enabled:
            raise ValidationError(
                "admin runtime control is disabled",
                remediation="set BROKER_ADMIN_ENABLED=true to enable /admin",
                code="admin_disabled",
            )
        presented = x_admin_key.strip() if x_admin_key else extract_key(authorization, x_api_key)
        if not presented:
            raise AuthError(
                "missing admin key",
                remediation="send X-Admin-Key (or Authorization/X-API-Key) with an admin key",
            )
        # ADR 0008 virtual-key admin scope takes precedence; fall back to the
        # dedicated BROKER_ADMIN_API_KEYS list (ADR 0012 back-compat).
        principal = key_store.resolve(presented)
        if principal is not None and principal.is_admin:
            return
        if admin_key_match(settings, presented):
            return
        raise AuthError(
            "invalid admin key",
            remediation="use an admin-scope virtual key or a configured BROKER_ADMIN_API_KEYS value",
        )

    def err_response(exc: BrokerError, request_id: str) -> JSONResponse:
        payload = exc.to_payload()
        payload["error"]["request_id"] = request_id
        headers = {"x-request-id": request_id, **getattr(exc, "headers", {})}
        return JSONResponse(status_code=exc.http_status, content=payload, headers=headers)

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

    def authed(authorization: str | None, x_api_key: str | None) -> Principal:
        """Authenticate a request and return the caller's Principal.

        With auth disabled every caller is an unlimited admin (identity-less);
        otherwise the presented secret is resolved against the key store (virtual
        keys + legacy flat keys). An unknown/invalid/revoked/expired key yields one
        generic 401 — no detail leaks that would let a client enumerate keys.
        """
        if not settings.auth_enabled:
            return Principal(usage_id=None, scope="admin", unlimited=True)
        principal = key_store.resolve(extract_key(authorization, x_api_key))
        if principal is None:
            raise AuthError(
                "invalid or missing API key",
                remediation="send Authorization: Bearer <key> or X-API-Key",
            )
        return principal

    def _allowed_models(principal: Principal) -> set[str] | None:
        models = principal.limits.allowed_models
        return set(models) if models else None

    def _cache_scope(key_id: str | None, placement) -> str:
        """Cache-key scope: the per-key/tenant id plus the placement requirement,
        so a cached response never crosses key or placement boundaries (ADR 0012)."""
        base = key_id or "anon"
        if placement:
            return f"{base}|placement={','.join(sorted(placement))}"
        return base

    def _precheck_model(principal: Principal, model: str) -> None:
        """Fast 403 for a directly-requested, disallowed model (before admission).

        ``auto`` is allowed through to routing; the model it selects is checked by
        the manager, so scoping still applies to auto's choice.
        """
        allowed = _allowed_models(principal)
        if allowed is not None and model != settings.router_alias and model not in allowed:
            raise AuthorizationError(
                f"model {model!r} is not permitted for this API key",
                model=model,
                code="model_not_allowed",
                remediation="request a model in the key's allowed_models list",
            )

    def _estimate_tokens(texts: list | str | None, max_tokens: int | None) -> int:
        """Cheap pre-dispatch token estimate (~4 chars/token) + requested output."""
        if texts is None:
            chars = 0
        elif isinstance(texts, str):
            chars = len(texts)
        else:
            chars = sum(len(str(t)) for t in texts)
        return chars // 4 + int(max_tokens or 0)

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
        snapshot = await manager.status()
        snapshot["response_cache"] = response_cache.stats()
        return snapshot

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
        principal = authed(authorization, x_api_key)
        key_id = principal.usage_id
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

        # Placement requirement + latency budget (ADR 0011). Parsed/validated up
        # front so a bad header or a placement denial fails before any routing.
        placement = _placement_requirement(request, settings)
        budget = _latency_budget_ms(request)

        alias = req.model
        _precheck_model(principal, alias)
        allowed = _allowed_models(principal)
        payload = req.model_dump(exclude_none=True)
        payload["messages"] = [m.model_dump(exclude_none=True) for m in req.messages]
        est_tokens = _estimate_tokens(
            [m.get("content") for m in payload["messages"]], req.max_tokens
        )
        admission = await governor.admit(principal, est_tokens)

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
            # Resolve the (possibly auto) route to one concrete model before streaming
            # so the route is known for the header and placement is enforced up front.
            concrete, route_path = await manager.resolve_stream(
                alias, payload, required_placement=placement, latency_budget_ms=budget
            )

            async def gen():
                INFLIGHT.inc()
                stream_status = "ok"
                try:
                    async for chunk in manager.stream_chat(
                        concrete,
                        payload,
                        request_id,
                        route_key=session_key,
                        required_placement=placement,
                        allowed_models=allowed,
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
                    await governor.reconcile(principal, admission.reservation, model=alias)
                    usage.record(
                        endpoint="chat_stream",
                        requested=alias,
                        model=concrete,
                        request_id=request_id,
                        api_key_id=key_id,
                        latency_ms=(time.monotonic() - start) * 1000,
                        status=stream_status,
                        stream=True,
                    )

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={
                    "x-request-id": request_id,
                    "cache-control": "no-cache",
                    "x-broker-route": route_path,
                    **admission.headers,
                },
            )

        INFLIGHT.inc()
        prompt_tokens = completion_tokens = 0
        served_model = alias
        try:

            async def _serve(name: str) -> dict:
                return await manager.chat(
                    name,
                    payload,
                    request_id,
                    route_key=session_key,
                    required_placement=placement,
                    latency_budget_ms=budget,
                    allowed_models=allowed,
                )

            result, cache_state = await _cached_serve(
                request=request,
                key_scope=_cache_scope(key_id, placement),
                endpoint="chat",
                alias=alias,
                keyed_payload=payload,
                serve=_serve,
            )
            route_path = result.pop("_broker_route", result.get("model", alias))
            REQUESTS.labels("chat", alias, "ok").inc()
            u = result.get("usage") or {}
            prompt_tokens = u.get("prompt_tokens", 0)
            completion_tokens = u.get("completion_tokens", 0)
            served_model = result.get("model", alias)
            usage.record(
                endpoint="chat",
                requested=alias,
                model=served_model,
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=u.get("total_tokens", 0),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(
                content=result,
                headers={
                    "x-request-id": request_id,
                    "x-broker-route": route_path,
                    "x-broker-cache": cache_state,
                    **admission.headers,
                },
            )
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("chat", alias).observe(time.monotonic() - start)
            await governor.reconcile(
                principal,
                admission.reservation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model=served_model,
            )

    @app.get("/usage")
    async def usage_summary(
        request: Request,
        limit: int = 20,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """Aggregated request/token usage from the usage log (see ADR 0007).

        Scoped per ADR 0008: a ``user``-scope key sees only its own usage; an
        ``admin``-scope key sees all keys plus a per-key spend breakdown.
        """
        principal = authed(authorization, x_api_key)
        if principal.is_admin:
            return usage.summary(limit=limit, include_keys=True)
        return usage.summary(limit=limit, api_key_id=principal.usage_id)

    @app.post("/v1/completions")
    async def completions(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """Legacy text completions, translated to the chat pipeline (ADR 0006)."""
        principal = authed(authorization, x_api_key)
        key_id = principal.usage_id
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
        placement = _placement_requirement(request, settings)
        budget = _latency_budget_ms(request)
        _precheck_model(principal, model)
        allowed = _allowed_models(principal)
        stream = bool(body.get("stream"))
        chat_payload = {k: v for k, v in body.items() if k != "prompt"}
        chat_payload["model"] = model
        chat_payload["messages"] = prompt_to_messages(body.get("prompt"))
        session_key = _session_key(request, body)
        est_tokens = _estimate_tokens(body.get("prompt"), body.get("max_tokens"))
        admission = await governor.admit(principal, est_tokens)
        start = time.monotonic()

        if stream:
            concrete, route_path = await manager.resolve_stream(
                model, chat_payload, required_placement=placement, latency_budget_ms=budget
            )

            async def gen():
                INFLIGHT.inc()
                st = "ok"
                try:
                    aiter = manager.stream_chat(
                        concrete,
                        chat_payload,
                        request_id,
                        route_key=session_key,
                        required_placement=placement,
                        allowed_models=allowed,
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
                    await governor.reconcile(principal, admission.reservation, model=model)
                    usage.record(
                        endpoint="completions_stream",
                        requested=model,
                        model=concrete,
                        request_id=request_id,
                        api_key_id=key_id,
                        latency_ms=(time.monotonic() - start) * 1000,
                        status=st,
                        stream=True,
                    )

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={
                    "x-request-id": request_id,
                    "cache-control": "no-cache",
                    "x-broker-route": route_path,
                    **admission.headers,
                },
            )

        INFLIGHT.inc()
        prompt_tokens = completion_tokens = 0
        served_model = model
        try:

            async def _serve(name: str) -> dict:
                return await manager.chat(
                    name,
                    chat_payload,
                    request_id,
                    route_key=session_key,
                    required_placement=placement,
                    latency_budget_ms=budget,
                    allowed_models=allowed,
                )

            result, cache_state = await _cached_serve(
                request=request,
                key_scope=_cache_scope(key_id, placement),
                endpoint="completions",
                alias=model,
                keyed_payload=chat_payload,
                serve=_serve,
            )
            route_path = result.pop("_broker_route", result.get("model", model))
            out = chat_to_text_completion(result, model)
            REQUESTS.labels("completions", model, "ok").inc()
            u = result.get("usage") or {}
            prompt_tokens = u.get("prompt_tokens", 0)
            completion_tokens = u.get("completion_tokens", 0)
            served_model = result.get("model", model)
            usage.record(
                endpoint="completions",
                requested=model,
                model=served_model,
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=u.get("total_tokens", 0),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(
                content=out,
                headers={
                    "x-request-id": request_id,
                    "x-broker-route": route_path,
                    "x-broker-cache": cache_state,
                    **admission.headers,
                },
            )
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("completions", model).observe(time.monotonic() - start)
            await governor.reconcile(
                principal,
                admission.reservation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model=served_model,
            )

    @app.post("/v1/responses")
    async def responses(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """OpenAI Responses API, translated to the chat pipeline (ADR 0006)."""
        principal = authed(authorization, x_api_key)
        key_id = principal.usage_id
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
        placement = _placement_requirement(request, settings)
        budget = _latency_budget_ms(request)
        _precheck_model(principal, model)
        allowed = _allowed_models(principal)
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
        est_tokens = _estimate_tokens(
            [body.get("input"), body.get("instructions")], passthrough.get("max_tokens")
        )
        admission = await governor.admit(principal, est_tokens)
        start = time.monotonic()

        if stream:
            concrete, route_path = await manager.resolve_stream(
                model, chat_payload, required_placement=placement, latency_budget_ms=budget
            )

            async def gen():
                INFLIGHT.inc()
                st = "ok"
                try:
                    aiter = manager.stream_chat(
                        concrete,
                        chat_payload,
                        request_id,
                        route_key=session_key,
                        required_placement=placement,
                        allowed_models=allowed,
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
                    await governor.reconcile(principal, admission.reservation, model=model)
                    usage.record(
                        endpoint="responses_stream",
                        requested=model,
                        model=concrete,
                        request_id=request_id,
                        api_key_id=key_id,
                        latency_ms=(time.monotonic() - start) * 1000,
                        status=st,
                        stream=True,
                    )

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={
                    "x-request-id": request_id,
                    "cache-control": "no-cache",
                    "x-broker-route": route_path,
                    **admission.headers,
                },
            )

        INFLIGHT.inc()
        prompt_tokens = completion_tokens = 0
        served_model = model
        try:

            async def _serve(name: str) -> dict:
                return await manager.chat(
                    name,
                    chat_payload,
                    request_id,
                    route_key=session_key,
                    required_placement=placement,
                    latency_budget_ms=budget,
                    allowed_models=allowed,
                )

            result, cache_state = await _cached_serve(
                request=request,
                key_scope=_cache_scope(key_id, placement),
                endpoint="responses",
                alias=model,
                keyed_payload=chat_payload,
                serve=_serve,
            )
            route_path = result.pop("_broker_route", result.get("model", model))
            out = chat_to_response(result, model)
            REQUESTS.labels("responses", model, "ok").inc()
            u = result.get("usage") or {}
            prompt_tokens = u.get("prompt_tokens", 0)
            completion_tokens = u.get("completion_tokens", 0)
            served_model = result.get("model", model)
            usage.record(
                endpoint="responses",
                requested=model,
                model=served_model,
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=u.get("total_tokens", 0),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(
                content=out,
                headers={
                    "x-request-id": request_id,
                    "x-broker-route": route_path,
                    "x-broker-cache": cache_state,
                    **admission.headers,
                },
            )
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("responses", model).observe(time.monotonic() - start)
            await governor.reconcile(
                principal,
                admission.reservation,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                model=served_model,
            )

    @app.post("/v1/embeddings")
    async def embeddings(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
    ):
        """OpenAI embeddings — routed to an embedding-capable backend (ADR 0006)."""
        principal = authed(authorization, x_api_key)
        key_id = principal.usage_id
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
        placement = _placement_requirement(request, settings)
        _precheck_model(principal, model)
        allowed = _allowed_models(principal)
        if body.get("input") is None:
            raise ValidationError("`input` is required (string or list of strings)")
        payload = {"input": body["input"]}
        for k in ("truncate", "options", "keep_alive", "encoding_format", "dimensions", "user"):
            if k in body:
                payload[k] = body[k]
        session_key = _session_key(request, body)
        est_tokens = _estimate_tokens(body.get("input"), None)
        admission = await governor.admit(principal, est_tokens)
        start = time.monotonic()
        INFLIGHT.inc()
        prompt_tokens = 0
        served_model = model
        try:

            async def _serve(name: str) -> dict:
                return await manager.embeddings(
                    name,
                    payload,
                    request_id,
                    route_key=session_key,
                    required_placement=placement,
                    allowed_models=allowed,
                )

            result, cache_state = await _cached_serve(
                request=request,
                key_scope=_cache_scope(key_id, placement),
                endpoint="embeddings",
                alias=model,
                keyed_payload=payload,
                serve=_serve,
                always_deterministic=True,
            )
            REQUESTS.labels("embeddings", model, "ok").inc()
            u = result.get("usage") or {}
            prompt_tokens = u.get("prompt_tokens", 0)
            served_model = result.get("model", model)
            usage.record(
                endpoint="embeddings",
                requested=model,
                model=served_model,
                request_id=request_id,
                api_key_id=key_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                total_tokens=u.get("total_tokens", u.get("prompt_tokens", 0)),
                latency_ms=(time.monotonic() - start) * 1000,
                status="ok",
            )
            return JSONResponse(
                content=result,
                headers={
                    "x-request-id": request_id,
                    "x-broker-cache": cache_state,
                    **admission.headers,
                },
            )
        finally:
            INFLIGHT.dec()
            REQUEST_LATENCY.labels("embeddings", model).observe(time.monotonic() - start)
            await governor.reconcile(
                principal,
                admission.reservation,
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                model=served_model,
            )

    # --- admin runtime control (ADR 0012) --------------------------------
    def _admin_actor(authorization, x_api_key, x_admin_key) -> str:
        return UsageLog.key_fingerprint(x_admin_key or x_api_key or authorization) or "unknown"

    @app.get("/admin/models")
    async def admin_models(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
        x_admin_key: str | None = Header(None),
    ):
        """Runtime snapshot of every model: state, inflight, last-used (admin)."""
        require_admin(authorization, x_api_key, x_admin_key)
        return {"models": manager.admin_list_models()}

    @app.post("/admin/models/{alias}/unload")
    async def admin_unload(
        alias: str,
        request: Request,
        drain: bool = False,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
        x_admin_key: str | None = Header(None),
    ):
        """Unload a model (admin). Refuses on in-flight unless ``?drain=true``."""
        require_admin(authorization, x_api_key, x_admin_key)
        actor = _admin_actor(authorization, x_api_key, x_admin_key)
        admin_log.info("admin action", extra={"action": "unload", "target": alias, "actor": actor})
        result = await manager.admin_unload(alias, drain=drain)
        admin_log.info("admin unload result: %s", result)
        return result

    @app.post("/admin/models/{alias}/load")
    async def admin_load(
        alias: str,
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
        x_admin_key: str | None = Header(None),
    ):
        """Explicitly preload a model (admin) — the only non-request load path."""
        require_admin(authorization, x_api_key, x_admin_key)
        actor = _admin_actor(authorization, x_api_key, x_admin_key)
        admin_log.info("admin action", extra={"action": "load", "target": alias, "actor": actor})
        result = await manager.admin_load(alias)
        admin_log.info("admin load result: %s", result)
        return result

    @app.post("/admin/registry/reload")
    async def admin_reload(
        request: Request,
        authorization: str | None = Header(None),
        x_api_key: str | None = Header(None),
        x_admin_key: str | None = Header(None),
    ):
        """Re-read + validate the registry and apply the diff with no restart.

        Transactional: an invalid registry makes no change and returns a
        structured error; in-flight requests on changed/removed models drain
        before replacement; unchanged models keep serving.
        """
        require_admin(authorization, x_api_key, x_admin_key)
        actor = _admin_actor(authorization, x_api_key, x_admin_key)
        admin_log.info("admin action", extra={"action": "registry_reload", "actor": actor})
        result = await manager.admin_reload_registry()
        admin_log.info("admin registry reload result: %s", result)
        return result

    @app.exception_handler(BrokerError)
    async def broker_error_handler(request: Request, exc: BrokerError):
        rid = getattr(request.state, "request_id", "unknown")
        return err_response(exc, rid)

    # --- Optional static web UI / playground (ADR 0015) ------------------
    # Registered LAST and only when BROKER_UI_ENABLED. Serves hardened static
    # assets at /ui with a strict CSP; adds no new unauthenticated data routes.
    # Disabled (the default) -> /ui 404s.
    register_ui(app, settings)

    return app
