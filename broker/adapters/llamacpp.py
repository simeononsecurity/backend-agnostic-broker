"""llama.cpp backend adapter.

``llama-server`` (from llama.cpp) loads a single GGUF model at process startup
and serves an OpenAI-compatible ``/v1/chat/completions`` plus ``/health``. It has
no native lazy loading, so the broker starts it on first request and stops it
after idle. GPU offload (``-ngl``/``--n-gpu-layers``), ``--tensor-split``, and
context size (``-c``) are templated in the registry startup_command.

See docs/backends/llamacpp.md for confirmed behavior and references.
"""

from __future__ import annotations

from .httpbase import HTTPBackendAdapter


class LlamaCppAdapter(HTTPBackendAdapter):
    name = "llamacpp"
    chat_path = "/v1/chat/completions"
