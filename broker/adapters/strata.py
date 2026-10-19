"""Strata backend adapter.

Strata is treated as a broker-managed, OpenAI-compatible HTTP runtime: the exact
server command, version, readiness path, unload mechanism, GPU placement,
tensor/layer split and MTP controls are **not assumed** — they are supplied via
the registry ``startup_command`` template and ``readiness`` probe, and must be
confirmed per deployment (see docs/backends/strata.md, which deliberately marks
unverified items as UNVERIFIED rather than inventing flags).

Because Strata's native lazy-loading/idle-shutdown behavior is not independently
confirmed here, the broker provides broker-managed lazy loading: start on first
request, stop after idle. If a specific Strata version offers native idle
shutdown, set a long ``idle_timeout_seconds`` and document it.
"""

from __future__ import annotations

from typing import Any

from ..errors import ValidationError
from .httpbase import HTTPBackendAdapter


class StrataAdapter(HTTPBackendAdapter):
    name = "strata"
    # chat_path is overridable per entry via a future field; default to OpenAI.
    chat_path = "/v1/chat/completions"

    async def rerank(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        """Strata rerank is unverified and disabled (ADR 0013).

        Strata's rerank support and wire shape are not independently confirmed
        (the broker never assumes Strata flags/endpoints), so we return a clear
        400 rather than proxy to an unverified path. If a confirmed Strata build
        exposes a Cohere/Jina-compatible ``/v1/rerank``, wire it as an
        ``llamacpp``/http entry pointed at that path.
        """
        raise ValidationError(
            "backend 'strata' does not support rerank: Strata's rerank endpoint "
            f"is unverified (model {self.entry.alias!r})",
            backend=self.name,
            model=self.entry.alias,
            remediation="use a vLLM or llama.cpp reranker model for /v1/rerank",
        )
