"""Every example under examples/ is real, loadable, and runnable — not decorative.

Generic coverage (loops over every example dir):
  * each dir has a README and is listed in examples/README.md;
  * every registry*.yaml loads through the real loader;
  * every keys*.yaml parses through the real KeyStore;
  * every BROKER_* key in an env file is a real Settings field;
  * every docker-compose*.yml parses and references only allow-listed images.

Plus end-to-end runs of the NEW examples against the in-process fake backend
(ollama-quickstart, rag-pipeline, otel, docker-compose-redis). Examples that need a
real GPU backend are validated at the config level only; see the per-feature
modules (test_example_*.py, test_examples_*.py, test_ui.py, test_docs_examples.py)
for the pre-existing examples' deeper behavioral tests.
"""

from __future__ import annotations

import importlib.util
import os
import re
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.keys import KeyStore
from broker.registry import load_registry

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"

# Images an example docker-compose.yml may reference (prefix match). Anything else
# is a typo or an unexpected dependency and should fail the test.
ALLOWED_IMAGE_PREFIXES = (
    "ghcr.io/simeononsecurity/backend-agnostic-broker",
    "backend-agnostic-broker",
    "ollama/ollama",
    "redis:",
    "nginx:",
    "jaegertracing/all-in-one",
    "otel/opentelemetry-collector-contrib",
)

_ENV_VAR = re.compile(r"\$\{(\w+)(?::-[^}]*)?\}")


def _example_dirs() -> list[Path]:
    return sorted(p for p in EXAMPLES.iterdir() if p.is_dir())


def _set_placeholder_env(text: str) -> None:
    """Give every ${VAR} in a registry a harmless value so expansion is clean."""
    for var in _ENV_VAR.findall(text):
        os.environ.setdefault(var, "http://127.0.0.1:9")


def _registry_backends(data: object) -> set[str]:
    backends: set[str] = set()
    if isinstance(data, dict):
        for m in data.get("models", []) or []:
            if isinstance(m, dict) and m.get("backend"):
                backends.add(str(m["backend"]))
    return backends


# --------------------------------------------------------------------------- #
# Generic coverage
# --------------------------------------------------------------------------- #


def test_examples_dir_exists_with_index():
    assert EXAMPLES.is_dir()
    index = (EXAMPLES / "README.md").read_text()
    for d in _example_dirs():
        assert (d / "README.md").is_file(), f"{d.name}/README.md missing"
        assert d.name in index, f"{d.name} not listed in examples/README.md"


@pytest.mark.parametrize("d", _example_dirs(), ids=lambda p: p.name)
def test_every_registry_loads(d: Path):
    for reg in sorted(d.glob("registry*.y*ml")):
        text = reg.read_text()
        _set_placeholder_env(text)
        backends = _registry_backends(yaml.safe_load(text)) or None
        obj = load_registry(str(reg), backends)
        assert obj.models, f"{reg} produced no models"


@pytest.mark.parametrize("d", _example_dirs(), ids=lambda p: p.name)
def test_every_keys_file_parses(d: Path):
    for kf in sorted(d.glob("keys*.y*ml")):
        ks = KeyStore(str(kf))
        assert ks.records, f"{kf} produced no key records"


@pytest.mark.parametrize("d", _example_dirs(), ids=lambda p: p.name)
def test_env_files_reference_real_settings(d: Path):
    valid = set(Settings.model_fields)
    for env in [*d.glob(".env*"), *d.glob("env.*")]:
        for line in env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key = line.split("=", 1)[0].strip()
            if not key.startswith("BROKER_"):
                continue  # OTEL_*, OLLAMA_BASE_URL, etc. are not Settings fields
            assert key[len("BROKER_") :].lower() in valid, f"{env}: {key} not a Settings field"


@pytest.mark.parametrize("d", _example_dirs(), ids=lambda p: p.name)
def test_compose_files_parse_with_known_images(d: Path):
    for comp in sorted(d.glob("docker-compose*.y*ml")):
        data = yaml.safe_load(comp.read_text())
        assert "services" in data, f"{comp} has no services"
        for name, svc in data["services"].items():
            image = svc.get("image") if isinstance(svc, dict) else None
            if not image:
                continue
            assert any(image.startswith(p) for p in ALLOWED_IMAGE_PREFIXES), (
                f"{comp}: service {name} uses unexpected image {image!r}"
            )


def test_root_compose_files_parse():
    for comp in sorted(REPO.glob("docker-compose*.y*ml")):
        data = yaml.safe_load(comp.read_text())
        assert isinstance(data, dict)
        for svc in (data.get("services") or {}).values():
            image = svc.get("image") if isinstance(svc, dict) else None
            if image:
                assert any(image.startswith(p) for p in ALLOWED_IMAGE_PREFIXES), image


# --------------------------------------------------------------------------- #
# End-to-end runs of the NEW examples against the fake backend
# --------------------------------------------------------------------------- #


def _fake_client(registry: Path) -> TestClient:
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(registry),
        enabled_backends=["fake"],
        health_probe_enabled=False,
    )
    return TestClient(create_app(settings))


def test_ollama_quickstart_serves_fake_chat():
    reg = EXAMPLES / "ollama-quickstart" / "registry.example.yaml"
    with _fake_client(reg) as c:
        assert c.get("/healthz").json()["status"] == "ok"
        assert any(m["id"] == "echo-fake" for m in c.get("/v1/models").json()["data"])
        r = c.post(
            "/v1/chat/completions",
            json={"model": "echo-fake", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"] == "echo: echo-fake"


def test_rag_pipeline_embed_rerank_chat():
    reg = EXAMPLES / "rag-pipeline" / "registry.yaml"
    corpus = [
        "The capital of France is Paris.",
        "Berlin is the capital of Germany.",
        "The Eiffel Tower is in Paris, France.",
    ]
    with _fake_client(reg) as c:
        emb = c.post("/v1/embeddings", json={"model": "embed", "input": corpus})
        assert emb.status_code == 200, emb.text
        assert len(emb.json()["data"]) == len(corpus)

        rr = c.post(
            "/v1/rerank",
            json={
                "model": "rerank",
                "query": "What is the capital of France?",
                "documents": corpus,
                "top_n": 2,
                "return_documents": True,
            },
        )
        assert rr.status_code == 200, rr.text
        results = rr.json()["results"]
        # The France/Paris document wins on lexical overlap.
        assert results[0]["document"]["text"].startswith("The capital of France")

        chat = c.post(
            "/v1/chat/completions",
            json={"model": "chat", "messages": [{"role": "user", "content": "answer"}]},
        )
        assert chat.status_code == 200
        assert chat.json()["model"] == "chat"


def test_rag_pipeline_script_is_importable_python():
    # The example script must be valid, importable Python (syntax + openai import
    # guarded). We compile it rather than run it so no live server is needed.
    src = (EXAMPLES / "rag-pipeline" / "rag.py").read_text()
    compile(src, "rag.py", "exec")


@pytest.mark.skipif(
    importlib.util.find_spec("opentelemetry.sdk") is None,
    reason="otel SDK extra not installed",
)
def test_otel_example_boots_with_tracing_enabled():
    # Pre-install an in-memory provider so the broker reuses it instead of standing
    # up a live OTLP exporter (which would retry against a dead endpoint).
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

    reg = EXAMPLES / "otel" / "registry.example.yaml"
    settings = Settings(
        auth_enabled=False,
        registry_path=str(reg),
        enabled_backends=["fake"],
        health_probe_enabled=False,
        tracing_enabled=True,
        tracing_service_name="backend-agnostic-broker",
    )
    with TestClient(create_app(settings)) as c:
        assert c.get("/status").json()["tracing_enabled"] is True
        r = c.post(
            "/v1/chat/completions",
            json={"model": "echo-fake", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200, r.text
    # A metadata-only inference span was recorded (no prompt content).
    assert any(s.name == "inference" for s in exporter.get_finished_spans())


def test_redis_replica_example_forms_three_member_pool():
    reg = EXAMPLES / "docker-compose-redis" / "registry.example.yaml"
    obj = load_registry(str(reg), {"fake"})
    members = [m for m in obj.models if m.pool == "chat"]
    assert len(members) == 3
    # The example serves the pool against the fake backend (single-replica view).
    with _fake_client(reg) as c:
        r = c.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "alice"},
            json={"model": "chat", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["model"] == "chat"
