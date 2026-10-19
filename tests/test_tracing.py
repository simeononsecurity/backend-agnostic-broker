"""Tracing tests (ADR 0012): no-op when off; gen_ai.* spans when otel present.

The span-assertion tests require the optional ``otel`` extra and are skipped when
OpenTelemetry is not installed. The no-op + fail-fast tests always run.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.errors import ConfigError
from broker.tracing import make_tracer

try:
    import opentelemetry  # noqa: F401

    HAS_OTEL = True
except ImportError:
    HAS_OTEL = False

try:
    import opentelemetry.sdk.trace  # noqa: F401

    HAS_OTEL_SDK = True
except ImportError:
    HAS_OTEL_SDK = False


def _settings(fake_registry_file, tmp_models_root, **over) -> Settings:
    base = dict(
        auth_enabled=True,
        api_keys=["key"],
        registry_path=str(fake_registry_file),
        enabled_backends=["fake", "ollama", "vllm", "llamacpp", "strata"],
        allowed_model_roots=[str(tmp_models_root)],
    )
    base.update(over)
    return Settings(**base)


# --- always-on: no-op + fail-fast ------------------------------------------
def test_tracer_noop_when_disabled(fake_registry_file, tmp_models_root):
    tracer = make_tracer(_settings(fake_registry_file, tmp_models_root, tracing_enabled=False))
    assert tracer.enabled is False
    # The null span satisfies the subset of the API the broker uses.
    with tracer.span("x", {"a": 1}) as sp:
        sp.set_attribute("k", "v")  # no-op, must not raise


def test_disabled_tracing_app_builds_and_serves(fake_registry_file, tmp_models_root):
    app = create_app(_settings(fake_registry_file, tmp_models_root, tracing_enabled=False))
    with TestClient(app) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key"},
            json={"model": "echo", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200
        assert app.state.manager.tracer.enabled is False


@pytest.mark.skipif(HAS_OTEL, reason="opentelemetry IS installed; ConfigError path not exercised")
def test_tracing_enabled_without_otel_raises(fake_registry_file, tmp_models_root):
    s = _settings(fake_registry_file, tmp_models_root, tracing_enabled=True)
    with pytest.raises(ConfigError):
        s.validate_runtime()


# --- otel present: assert gen_ai.* spans, assert NO content ----------------
@pytest.mark.skipif(
    not HAS_OTEL_SDK, reason="opentelemetry SDK not installed (optional [otel] extra)"
)
def test_spans_emitted_with_gen_ai_attributes(fake_registry_file, tmp_models_root):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    app = create_app(_settings(fake_registry_file, tmp_models_root, tracing_enabled=True))
    with TestClient(app) as c:
        assert app.state.manager.tracer.enabled is True
        secret_prompt = "please-do-not-leak-this-prompt"
        r = c.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer key"},
            json={"model": "echo", "messages": [{"role": "user", "content": secret_prompt}]},
        )
        assert r.status_code == 200

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "inference" in names
    assert "load_model" in names
    inf = next(s for s in spans if s.name == "inference")
    assert inf.attributes.get("gen_ai.request.model") == "echo"
    assert inf.attributes.get("gen_ai.operation.name") == "chat"

    # Hard requirement: NO prompt/response content in any span, ever.
    for s in spans:
        for k, v in (s.attributes or {}).items():
            assert "input.messages" not in k and "output.messages" not in k
            assert "system_instructions" not in k
            assert secret_prompt not in str(v)
