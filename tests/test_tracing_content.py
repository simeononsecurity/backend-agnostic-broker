"""Opt-in span content-capture tests (ADR 0014).

The span assertions require the optional ``otel`` SDK and skip cleanly when it is
not installed (exercised green in a private ``[dev,otel]`` venv).
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings

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
        tracing_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def _exporter():
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
    return exporter


SECRET = "please-do-not-leak-unless-opted-in"
BODY = {"model": "echo", "messages": [{"role": "user", "content": SECRET}]}


@pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry SDK not installed (optional [otel])")
def test_content_not_captured_by_default(fake_registry_file, tmp_models_root):
    exporter = _exporter()
    app = create_app(_settings(fake_registry_file, tmp_models_root))  # capture off (default)
    with TestClient(app) as c:
        assert app.state.manager.tracer.capture_content is False
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key"}, json=BODY)
        assert r.status_code == 200
    for s in exporter.get_finished_spans():
        for k, v in (s.attributes or {}).items():
            assert "input.messages" not in k and "output.messages" not in k
            assert SECRET not in str(v)


@pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry SDK not installed (optional [otel])")
def test_content_captured_when_enabled(fake_registry_file, tmp_models_root):
    exporter = _exporter()
    app = create_app(_settings(fake_registry_file, tmp_models_root, tracing_capture_content=True))
    with TestClient(app) as c:
        assert app.state.manager.tracer.capture_content is True
        r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer key"}, json=BODY)
        assert r.status_code == 200
    inf = next(s for s in exporter.get_finished_spans() if s.name == "inference")
    inp = inf.attributes.get("gen_ai.input.messages")
    out = inf.attributes.get("gen_ai.output.messages")
    assert inp is not None and SECRET in inp
    # captured as a JSON string per the OTel spanOnly convention
    assert isinstance(inp, str) and json.loads(inp)[0]["role"] == "user"
    assert out is not None and "echo" in out


@pytest.mark.skipif(not HAS_OTEL_SDK, reason="opentelemetry SDK not installed (optional [otel])")
def test_content_capture_truncated(fake_registry_file, tmp_models_root):
    exporter = _exporter()
    app = create_app(
        _settings(
            fake_registry_file,
            tmp_models_root,
            tracing_capture_content=True,
            tracing_content_max_chars=10,
        )
    )
    long = {"model": "echo", "messages": [{"role": "user", "content": "x" * 500}]}
    with TestClient(app) as c:
        c.post("/v1/chat/completions", headers={"Authorization": "Bearer key"}, json=long)
    inf = next(s for s in exporter.get_finished_spans() if s.name == "inference")
    assert len(inf.attributes.get("gen_ai.input.messages")) <= 10
