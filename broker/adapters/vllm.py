"""vLLM backend adapter.

vLLM's OpenAI-compatible server (`vllm serve <model>` /
`python -m vllm.entrypoints.openai.api_server`) loads **one model at process
startup** and serves OpenAI-compatible routes. There is no native per-request
lazy loading, so the broker manages the process: it starts vLLM on first request
for a model and stops it after the idle timeout.

Confirmed-behavior references live in docs/backends/vllm.md. Flags are *templated*
in the registry (``startup_command``) and never taken from requests.
"""

from __future__ import annotations

from .httpbase import HTTPBackendAdapter


class VLLMAdapter(HTTPBackendAdapter):
    name = "vllm"
    chat_path = "/v1/chat/completions"
    # vLLM exposes /health (200 when ready).
