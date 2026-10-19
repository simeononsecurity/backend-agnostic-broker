"""Optional OpenTelemetry tracing (ADR 0012).

Tracing is OFF by default and has **zero overhead** when disabled: no
OpenTelemetry import happens, and :meth:`Tracer.span` returns a cheap null
context manager. When ``BROKER_TRACING_ENABLED=true`` the OPTIONAL ``otel`` extra
must be installed (``pip install 'backend-agnostic-broker[otel]'``) or startup
fails fast with a :class:`ConfigError`.

Spans follow the OpenTelemetry **GenAI semantic conventions** (``gen_ai.*``
attributes). Per the spec, prompt/response **content is never captured** — only
metadata (model, operation, token counts, provider). The OTLP exporter is
configured entirely through the standard ``OTEL_*`` environment variables
(``OTEL_EXPORTER_OTLP_ENDPOINT``, ``OTEL_EXPORTER_OTLP_PROTOCOL``,
``OTEL_EXPORTER_OTLP_HEADERS``, ...); the broker configures nothing beyond the
service name, so operators use the ecosystem's documented knobs.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from .errors import ConfigError


class _NullSpan:
    """No-op span; satisfies the subset of the OTel span API the broker uses."""

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: D102
        return None

    def record_exception(self, exc: BaseException) -> None:  # noqa: D102
        return None

    def set_status(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


_NULL_SPAN = _NullSpan()


class Tracer:
    """Base tracer. The no-op implementation; real tracing subclasses it."""

    enabled: bool = False
    #: Opt-in span content capture (ADR 0014); always off on the no-op tracer.
    capture_content: bool = False
    content_max_chars: int = 0

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None):
        yield _NULL_SPAN

    def truncate(self, text: str) -> str:
        """Clip captured content to ``content_max_chars`` (0 = unlimited)."""
        if self.content_max_chars and len(text) > self.content_max_chars:
            return text[: self.content_max_chars]
        return text


class _OtelTracer(Tracer):
    """Wraps a real OpenTelemetry tracer, applying gen_ai.* attributes."""

    enabled = True

    def __init__(self, otel_tracer: Any) -> None:
        self._t = otel_tracer

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None):
        with self._t.start_as_current_span(name) as sp:
            if attributes:
                for k, v in attributes.items():
                    if v is not None:
                        sp.set_attribute(k, v)
            try:
                yield sp
            except Exception as exc:  # record + re-raise, per the conventions
                try:
                    sp.record_exception(exc)
                    sp.set_attribute("error.type", type(exc).__name__)
                except Exception:  # noqa: BLE001 - never let tracing mask the error
                    pass
                raise


def make_tracer(settings: Any) -> Tracer:
    """Build the configured tracer.

    * Disabled → a no-op :class:`Tracer` (no OpenTelemetry import).
    * Enabled but ``opentelemetry`` missing → :class:`ConfigError` (fail-fast).
    * Enabled with no TracerProvider yet configured → install an SDK provider that
      exports via OTLP using standard ``OTEL_*`` env vars.
    * Enabled when a provider already exists (e.g. a test's in-memory exporter, or
      an external SDK bootstrap) → reuse it without overriding.
    """
    if not getattr(settings, "tracing_enabled", False):
        return Tracer()
    try:
        from opentelemetry import trace
        from opentelemetry.trace import ProxyTracerProvider
    except ImportError as exc:  # pragma: no cover - guarded earlier in config
        raise ConfigError(
            "BROKER_TRACING_ENABLED=true but the 'opentelemetry' package is not installed",
            remediation="pip install 'backend-agnostic-broker[otel]'",
        ) from exc

    provider = trace.get_tracer_provider()
    if isinstance(provider, ProxyTracerProvider):
        # No real provider configured yet — stand one up that exports over OTLP
        # using the standard OTEL_* environment variables.
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError as exc:
            raise ConfigError(
                "tracing is enabled but the OpenTelemetry SDK/OTLP exporter is not installed",
                remediation="pip install 'backend-agnostic-broker[otel]'",
            ) from exc
        resource = Resource.create({"service.name": settings.tracing_service_name})
        tp = TracerProvider(resource=resource)
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tp)

    tracer = _OtelTracer(trace.get_tracer("broker"))
    tracer.capture_content = bool(getattr(settings, "tracing_capture_content", False))
    tracer.content_max_chars = int(getattr(settings, "tracing_content_max_chars", 0))
    return tracer
