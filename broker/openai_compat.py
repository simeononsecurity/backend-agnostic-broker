"""OpenAI endpoint translation shims.

`/v1/completions` (legacy text completion) and `/v1/responses` (the Responses
API) are implemented by translating to/from the existing chat pipeline, so they
inherit all routing, pooling, lazy-load, failover, and the ``auto`` router without
any backend/adapter changes (see ADR 0006). These are pure helpers + SSE stream
transformers; the HTTP wiring lives in ``broker.app``.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

# --- request translation -----------------------------------------------------


def prompt_to_messages(prompt: Any) -> list[dict[str, Any]]:
    """Legacy completion ``prompt`` (str or list[str]) -> chat messages."""
    if isinstance(prompt, list):
        text = "\n".join(str(p) for p in prompt)
    else:
        text = "" if prompt is None else str(prompt)
    return [{"role": "user", "content": text}]


def responses_input_to_messages(
    input_: Any, instructions: str | None = None
) -> list[dict[str, Any]]:
    """Responses ``input`` (+ optional ``instructions``) -> chat messages.

    ``input`` may be a plain string or a list of message objects (each with a
    ``role`` and string/parts ``content``). ``instructions`` becomes a leading
    system message.
    """
    messages: list[dict[str, Any]] = []
    if instructions:
        messages.append({"role": "system", "content": str(instructions)})
    if isinstance(input_, str):
        messages.append({"role": "user", "content": input_})
    elif isinstance(input_, list):
        for item in input_:
            if isinstance(item, dict) and "role" in item:
                messages.append(
                    {
                        "role": item.get("role", "user"),
                        "content": _parts_to_text(item.get("content")),
                    }
                )
            else:
                messages.append({"role": "user", "content": _parts_to_text(item)})
    elif input_ is not None:
        messages.append({"role": "user", "content": str(input_)})
    if not messages or all(m["role"] == "system" for m in messages):
        messages.append({"role": "user", "content": ""})
    return messages


def _parts_to_text(content: Any) -> str:
    """Flatten OpenAI content parts to text (best-effort for the text path)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict):
                out.append(str(part.get("text") or part.get("content") or ""))
            else:
                out.append(str(part))
        return "".join(out)
    return str(content)


# --- response translation (non-streaming) ------------------------------------


def chat_to_text_completion(result: dict[str, Any], model: str) -> dict[str, Any]:
    """chat.completion -> text_completion object."""
    choices = []
    for ch in result.get("choices", []):
        msg = ch.get("message", {}) or {}
        choices.append(
            {
                "index": ch.get("index", 0),
                "text": msg.get("content") or "",
                "finish_reason": ch.get("finish_reason"),
                "logprobs": None,
            }
        )
    cid = (result.get("id") or f"cmpl-{uuid.uuid4().hex[:24]}").replace("chatcmpl", "cmpl")
    return {
        "id": cid,
        "object": "text_completion",
        "created": result.get("created", int(time.time())),
        "model": result.get("model", model),
        "choices": choices,
        "usage": result.get("usage"),
    }


def _responses_usage(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    if not usage:
        return None
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def chat_to_response(result: dict[str, Any], model: str) -> dict[str, Any]:
    """chat.completion -> Responses API ``response`` object."""
    text = ""
    for ch in result.get("choices", []):
        text = (ch.get("message", {}) or {}).get("content") or ""
        break
    rid = (result.get("id") or f"resp-{uuid.uuid4().hex[:24]}").replace("chatcmpl", "resp")
    return {
        "id": rid,
        "object": "response",
        "created_at": result.get("created", int(time.time())),
        "status": "completed",
        "model": result.get("model", model),
        "output": [
            {
                "type": "message",
                "id": f"msg-{uuid.uuid4().hex[:24]}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "output_text": text,
        "usage": _responses_usage(result.get("usage")),
    }


# --- response translation (streaming) ----------------------------------------


def _iter_sse_events(buf: str):
    """Split an SSE buffer into complete events; return (events, remainder)."""
    events = []
    while "\n\n" in buf:
        event, buf = buf.split("\n\n", 1)
        events.append(event)
    return events, buf


def _data_payloads(event: str):
    """Yield parsed ``data:`` payloads from one SSE event (str or dict)."""
    for line in event.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:") :].strip()
        if data == "[DONE]":
            yield "[DONE]"
        elif data:
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue


async def chat_sse_to_text_sse(aiter: AsyncIterator[bytes], model: str) -> AsyncIterator[bytes]:
    """Transform a chat.completion.chunk SSE stream into text_completion chunks."""
    buf = ""
    async for raw in aiter:
        buf += raw.decode("utf-8", "replace")
        events, buf = _iter_sse_events(buf)
        for event in events:
            for payload in _data_payloads(event):
                if payload == "[DONE]":
                    yield b"data: [DONE]\n\n"
                    continue
                if isinstance(payload, dict) and payload.get("error"):
                    yield f"data: {json.dumps(payload)}\n\n".encode()
                    continue
                choices = payload.get("choices", []) if isinstance(payload, dict) else []
                out_choices = []
                for ch in choices:
                    delta = ch.get("delta", {}) or {}
                    out_choices.append(
                        {
                            "index": ch.get("index", 0),
                            "text": delta.get("content") or "",
                            "finish_reason": ch.get("finish_reason"),
                            "logprobs": None,
                        }
                    )
                chunk = {
                    "id": (payload.get("id") or "").replace("chatcmpl", "cmpl"),
                    "object": "text_completion",
                    "created": payload.get("created", int(time.time())),
                    "model": payload.get("model", model),
                    "choices": out_choices,
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()


async def chat_sse_to_responses_sse(
    aiter: AsyncIterator[bytes], model: str
) -> AsyncIterator[bytes]:
    """Transform a chat SSE stream into Responses API streaming events.

    Emits ``response.output_text.delta`` per content delta and a terminal
    ``response.completed`` with the assembled response, then ``[DONE]``.
    """
    buf = ""
    text_acc = ""
    served = model
    async for raw in aiter:
        buf += raw.decode("utf-8", "replace")
        events, buf = _iter_sse_events(buf)
        for event in events:
            for payload in _data_payloads(event):
                if payload == "[DONE]":
                    continue
                if isinstance(payload, dict) and payload.get("error"):
                    yield f"data: {json.dumps(payload)}\n\n".encode()
                    continue
                served = payload.get("model", served) if isinstance(payload, dict) else served
                for ch in payload.get("choices", []) if isinstance(payload, dict) else []:
                    delta = (ch.get("delta", {}) or {}).get("content")
                    if delta:
                        text_acc += delta
                        yield (
                            "data: "
                            + json.dumps({"type": "response.output_text.delta", "delta": delta})
                            + "\n\n"
                        ).encode()
    final = {
        "type": "response.completed",
        "response": {
            "id": f"resp-{uuid.uuid4().hex[:24]}",
            "object": "response",
            "status": "completed",
            "model": served,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text_acc, "annotations": []}],
                }
            ],
            "output_text": text_acc,
        },
    }
    yield f"data: {json.dumps(final)}\n\n".encode()
    yield b"data: [DONE]\n\n"


# --- rerank normalization (ADR 0013) -----------------------------------------


class RerankContractError(ValueError):
    """An upstream rerank response violated the normalization contract.

    Raised by :func:`build_rerank_response` when a scored result is not a usable
    ``{index, relevance_score}`` pair (wrong type, out-of-range/duplicate index,
    or a non-finite score). Adapters translate this into a
    :class:`~broker.errors.BackendUnavailableError` (502) so a malformed upstream
    body is treated as a backend fault — never partially passed through, silently
    coerced, or written to the response cache.
    """


def _require_result_index(idx: Any, n_documents: int, seen: set[int]) -> int:
    """Validate a single rerank ``index`` against the request's document count.

    Must be a real ``int`` (JSON ``true``/``false`` are ``bool`` and rejected),
    within ``[0, n_documents)``, and not already seen. Returns the int or raises
    :class:`RerankContractError`.
    """
    if not isinstance(idx, int) or isinstance(idx, bool):
        raise RerankContractError(f"rerank index must be a non-bool int, got {idx!r}")
    if not (0 <= idx < n_documents):
        raise RerankContractError(f"rerank index {idx} out of range for {n_documents} document(s)")
    if idx in seen:
        raise RerankContractError(f"duplicate rerank index {idx}")
    return idx


def _require_result_score(score: Any) -> float:
    """Validate a single rerank ``relevance_score``.

    Must be a real, finite number (``bool`` and ``str`` are rejected; ``NaN`` and
    ``±Infinity`` are rejected so the response always JSON-serializes). Returns a
    float or raises :class:`RerankContractError`.
    """
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise RerankContractError(f"rerank score must be a number, got {score!r}")
    val = float(score)
    if not math.isfinite(val):
        raise RerankContractError(f"rerank score must be finite, got {score!r}")
    return val


def build_rerank_response(
    *,
    scored: list[dict[str, Any]],
    documents: list[str],
    model: str,
    request_id: str,
    top_n: Any = None,
    return_documents: bool = False,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a Cohere/Jina-compatible rerank response from scored results.

    ``scored`` is a list of ``{"index": int, "relevance_score": float}`` (any
    order). The broker is the normalization authority: results are sorted by
    ``relevance_score`` descending (``index`` ascending as a stable tie-break),
    ``top_n`` caps the count, and ``document`` (``{"text": ...}`` from the
    *request* ``documents``) is attached only when ``return_documents`` is true —
    omitted entirely (never ``null``) otherwise, matching Cohere v2 / LiteLLM.
    ``index`` is always the original document ordinal.

    Validation is strict and total: every scored item must be an object carrying
    a non-bool ``int`` ``index`` in ``[0, len(documents))`` that is unique across
    the response, and a finite real ``relevance_score``. Any violation raises
    :class:`RerankContractError` (adapters map this to a 502) — results are never
    partially emitted or silently coerced. Returning *fewer* results than
    documents (``top_n``) is allowed.
    """
    n = len(documents)
    seen: set[int] = set()
    norm: list[dict[str, Any]] = []
    for item in scored:
        if not isinstance(item, dict):
            raise RerankContractError(f"rerank result must be an object, got {item!r}")
        idx = _require_result_index(item.get("index"), n, seen)
        score = _require_result_score(item.get("relevance_score"))
        seen.add(idx)
        norm.append({"index": idx, "relevance_score": score})
    norm.sort(key=lambda r: (-r["relevance_score"], r["index"]))
    if isinstance(top_n, int) and not isinstance(top_n, bool) and top_n >= 0:
        norm = norm[:top_n]
    if return_documents:
        for r in norm:
            i = r["index"]
            if 0 <= i < len(documents):
                r["document"] = {"text": documents[i]}
    return {
        "id": f"rerank-{request_id}",
        "object": "rerank",
        "model": model,
        "results": norm,
        "usage": usage or {"total_tokens": 0},
    }
