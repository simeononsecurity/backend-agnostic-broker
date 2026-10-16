"""Adapter registry: map a BackendType to its adapter class."""

from __future__ import annotations

from ..registry import BackendType
from .base import BackendAdapter
from .fake import FakeAdapter
from .llamacpp import LlamaCppAdapter
from .ollama import OllamaAdapter
from .strata import StrataAdapter
from .vllm import VLLMAdapter

ADAPTERS: dict[BackendType, type[BackendAdapter]] = {
    BackendType.FAKE: FakeAdapter,
    BackendType.OLLAMA: OllamaAdapter,
    BackendType.VLLM: VLLMAdapter,
    BackendType.LLAMACPP: LlamaCppAdapter,
    BackendType.STRATA: StrataAdapter,
}


def make_adapter(entry, settings) -> BackendAdapter:
    cls = ADAPTERS.get(entry.backend)
    if cls is None:  # pragma: no cover - guarded by registry validation
        from ..errors import ConfigError

        raise ConfigError(f"no adapter for backend {entry.backend!r}")
    return cls(entry, settings)


__all__ = ["ADAPTERS", "make_adapter", "BackendAdapter"]
