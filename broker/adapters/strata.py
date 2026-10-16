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

from .httpbase import HTTPBackendAdapter


class StrataAdapter(HTTPBackendAdapter):
    name = "strata"
    # chat_path is overridable per entry via a future field; default to OpenAI.
    chat_path = "/v1/chat/completions"
