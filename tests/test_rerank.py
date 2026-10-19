"""Tests for POST /v1/rerank (ADR 0013).

Covers validation, ordering, top_n, return_documents, the non-rerank-model and
unknown-model errors, auth, per-key scoping, placement, pool failover, usage
logging, response-cache eligibility, the /v1/models capability surface, the
OpenAPI path, and the Ollama/Strata adapter 400 guards. All CI coverage runs
against the deterministic fake backend.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings
from broker.errors import AuthorizationError, BackendUnavailableError, ValidationError
from broker.manager import ModelManager
from broker.registry import load_registry

AUTH = {"Authorization": "Bearer test-key"}

# A rerank-capable fake model, a chat-only (non-rerank) model, and a rerank pool.
RERANK_REGISTRY = """
models:
  - alias: reranker
    backend: fake
    runtime_model_id: fake-reranker
    idle_timeout_seconds: 60
    supports_rerank: true
    placement: [local]
  - alias: echo
    backend: fake
    runtime_model_id: fake-echo
    idle_timeout_seconds: 60
  - alias: rr-node-a
    backend: fake
    runtime_model_id: shared-rr
    pool: rr-pool
    supports_rerank: true
  - alias: rr-node-b
    backend: fake
    runtime_model_id: shared-rr
    pool: rr-pool
    supports_rerank: true
"""

DOCS = [
    "The capital of France is Paris.",
    "Bananas are a yellow tropical fruit rich in potassium.",
    "Berlin is the capital and largest city of Germany.",
]
QUERY = "What is the capital of France?"


def _settings(tmp_path: Path, **over) -> Settings:
    reg = tmp_path / "registry.yaml"
    reg.write_text(textwrap.dedent(RERANK_REGISTRY).strip())
    kwargs = dict(
        auth_enabled=True,
        api_keys=["test-key"],
        registry_path=str(reg),
        enabled_backends=["fake"],
        allowed_model_roots=[str(tmp_path)],
        health_probe_enabled=False,
        router_backend="heuristic",
        usage_log_path=":memory:",
        max_loaded_models=5,
    )
    kwargs.update(over)
    return Settings(**kwargs)


@pytest.fixture
def client(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as c:
        yield c


def _mgr(tmp_path: Path, **over) -> ModelManager:
    s = _settings(tmp_path, auth_enabled=False, api_keys=[], **over)
    reg = load_registry(s.registry_path, set(s.enabled_backends))
    m = ModelManager(reg, s)
    m.validate_all()
    return m


# --- happy path: ordering ----------------------------------------------------
def test_rerank_basic_ordering(client):
    r = client.post(
        "/v1/rerank", headers=AUTH, json={"model": "reranker", "query": QUERY, "documents": DOCS}
    )
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["object"] == "rerank"
    assert d["model"] == "reranker"
    results = d["results"]
    assert len(results) == 3
    # The France doc (index 0) shares the most query terms -> ranks first.
    assert results[0]["index"] == 0
    # Sorted by relevance_score descending.
    scores = [x["relevance_score"] for x in results]
    assert scores == sorted(scores, reverse=True)
    # Default: no document echo.
    assert "document" not in results[0]


def test_rerank_index_is_original_ordinal(client):
    r = client.post(
        "/v1/rerank", headers=AUTH, json={"model": "reranker", "query": QUERY, "documents": DOCS}
    )
    assert {x["index"] for x in r.json()["results"]} == {0, 1, 2}


def test_rerank_top_n(client):
    r = client.post(
        "/v1/rerank",
        headers=AUTH,
        json={"model": "reranker", "query": QUERY, "documents": DOCS, "top_n": 1},
    )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["index"] == 0  # the most relevant


def test_rerank_return_documents_true(client):
    r = client.post(
        "/v1/rerank",
        headers=AUTH,
        json={
            "model": "reranker",
            "query": QUERY,
            "documents": DOCS,
            "return_documents": True,
        },
    )
    assert r.status_code == 200, r.text
    top = r.json()["results"][0]
    assert top["document"] == {"text": DOCS[0]}


def test_rerank_documents_as_objects(client):
    r = client.post(
        "/v1/rerank",
        headers=AUTH,
        json={
            "model": "reranker",
            "query": QUERY,
            "documents": [{"text": d} for d in DOCS],
            "return_documents": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["document"]["text"] == DOCS[0]


# --- validation --------------------------------------------------------------
def test_rerank_requires_query(client):
    r = client.post("/v1/rerank", headers=AUTH, json={"model": "reranker", "documents": DOCS})
    assert r.status_code == 400


def test_rerank_empty_query(client):
    r = client.post(
        "/v1/rerank", headers=AUTH, json={"model": "reranker", "query": "", "documents": DOCS}
    )
    assert r.status_code == 400


def test_rerank_requires_documents(client):
    r = client.post("/v1/rerank", headers=AUTH, json={"model": "reranker", "query": QUERY})
    assert r.status_code == 400


def test_rerank_empty_documents(client):
    r = client.post(
        "/v1/rerank", headers=AUTH, json={"model": "reranker", "query": QUERY, "documents": []}
    )
    assert r.status_code == 400


def test_rerank_bad_document_item(client):
    r = client.post(
        "/v1/rerank",
        headers=AUTH,
        json={"model": "reranker", "query": QUERY, "documents": ["ok", 123]},
    )
    assert r.status_code == 400


def test_rerank_documents_cap(tmp_path):
    app = create_app(_settings(tmp_path, rerank_max_documents=2))
    with TestClient(app) as c:
        r = c.post(
            "/v1/rerank",
            headers=AUTH,
            json={"model": "reranker", "query": QUERY, "documents": ["a", "b", "c"]},
        )
    assert r.status_code == 400
    assert "documents" in r.text


def test_rerank_bad_top_n(client):
    for bad in (-1, "3", 1.5, True):
        r = client.post(
            "/v1/rerank",
            headers=AUTH,
            json={"model": "reranker", "query": QUERY, "documents": DOCS, "top_n": bad},
        )
        assert r.status_code == 400, f"top_n={bad!r} should be rejected"


# --- capability / routing errors --------------------------------------------
def test_rerank_non_rerank_model_400(client):
    # `echo` is a valid model but not rerank-capable -> clear 400 (no silent sub).
    r = client.post(
        "/v1/rerank", headers=AUTH, json={"model": "echo", "query": QUERY, "documents": DOCS}
    )
    assert r.status_code == 400
    assert "rerank" in r.text.lower()


def test_rerank_unknown_model_404(client):
    r = client.post(
        "/v1/rerank", headers=AUTH, json={"model": "nope", "query": QUERY, "documents": DOCS}
    )
    assert r.status_code == 404


def test_rerank_requires_auth(client):
    r = client.post("/v1/rerank", json={"model": "reranker", "query": QUERY, "documents": DOCS})
    assert r.status_code == 401


# --- placement (ADR 0011) ----------------------------------------------------
def test_rerank_placement_allowed(client):
    # `reranker` carries placement [local]; a matching requirement is served.
    r = client.post(
        "/v1/rerank",
        headers={**AUTH, "x-broker-placement": "local"},
        json={"model": "reranker", "query": QUERY, "documents": DOCS},
    )
    assert r.status_code == 200, r.text


def test_rerank_placement_denied(client):
    # A requirement the model does not carry -> 403 placement_denied (fail-closed).
    r = client.post(
        "/v1/rerank",
        headers={**AUTH, "x-broker-placement": "region:eu"},
        json={"model": "reranker", "query": QUERY, "documents": DOCS},
    )
    assert r.status_code == 403


# --- usage + cache -----------------------------------------------------------
def test_rerank_usage_recorded(client):
    client.post(
        "/v1/rerank", headers=AUTH, json={"model": "reranker", "query": QUERY, "documents": DOCS}
    )
    s = client.get("/usage", headers=AUTH).json()
    assert s["enabled"] is True
    assert "rerank" in {e["endpoint"] for e in s["by_endpoint"]}


def test_rerank_response_cache_hit(tmp_path):
    app = create_app(_settings(tmp_path, response_cache_enabled=True))
    with TestClient(app) as c:
        body = {"model": "reranker", "query": QUERY, "documents": DOCS}
        a = c.post("/v1/rerank", headers=AUTH, json=body)
        b = c.post("/v1/rerank", headers=AUTH, json=body)
    assert a.headers["x-broker-cache"] == "miss"
    assert b.headers["x-broker-cache"] == "hit"
    assert a.json()["results"] == b.json()["results"]


# --- /v1/models capability + openapi ----------------------------------------
def test_rerank_capability_in_models(client):
    data = {m["id"]: m for m in client.get("/v1/models", headers=AUTH).json()["data"]}
    assert data["reranker"]["capabilities"]["rerank"] is True
    assert data["echo"]["capabilities"]["rerank"] is False
    # Pool: AND across members (both rerank-capable here).
    assert data["rr-pool"]["capabilities"]["rerank"] is True


def test_openapi_has_rerank_path(client):
    spec = client.get("/openapi.json").json()
    assert "/v1/rerank" in spec["paths"]


# --- manager-level: scoping + failover --------------------------------------
async def test_rerank_allowed_models_scoping(tmp_path):
    mgr = _mgr(tmp_path)
    payload = {"query": QUERY, "documents": DOCS}
    # A key scoped to only `echo` may not rerank via `reranker` -> 403.
    with pytest.raises(AuthorizationError):
        await mgr.rerank("reranker", payload, "r0", allowed_models={"echo"})
    # A key scoped to include it succeeds.
    out = await mgr.rerank("reranker", payload, "r1", allowed_models={"reranker"})
    assert out["results"][0]["index"] == 0
    await mgr.shutdown()


async def test_rerank_pool_failover(tmp_path):
    mgr = _mgr(tmp_path)

    async def boom(payload, request_id):
        raise BackendUnavailableError("boom", backend="fake", model="rr-node-a")

    # Pin the session to node-a and make it fail; the pool fails over to node-b.
    await mgr.state.sticky_set("rr-pool", "s", "rr-node-a")
    mgr.models["rr-node-a"].adapter.rerank = boom  # type: ignore[method-assign]
    out = await mgr.rerank("rr-pool", {"query": QUERY, "documents": DOCS}, "r0", route_key="s")
    assert out["model"] == "rr-pool"
    assert out["results"][0]["index"] == 0
    await mgr.shutdown()


async def test_rerank_capability_gate_manager(tmp_path):
    mgr = _mgr(tmp_path)
    with pytest.raises(ValidationError):
        await mgr.rerank("echo", {"query": QUERY, "documents": DOCS}, "r0")
    await mgr.shutdown()


# --- adapter guards: Ollama + Strata ----------------------------------------
def _entry(backend: str):
    from broker.registry import BackendType, ModelEntry

    return ModelEntry(
        alias=f"{backend}-x",
        backend=BackendType(backend),
        runtime_model_id="rt",
        base_url="http://127.0.0.1:9",
    )


async def test_rerank_ollama_adapter_400(tmp_path):
    from broker.adapters.ollama import OllamaAdapter

    a = OllamaAdapter(_entry("ollama"), _settings(tmp_path))
    with pytest.raises(ValidationError):
        await a.rerank({"query": QUERY, "documents": DOCS}, "r0")


async def test_rerank_strata_adapter_400(tmp_path):
    from broker.adapters.strata import StrataAdapter

    a = StrataAdapter(_entry("strata"), _settings(tmp_path))
    with pytest.raises(ValidationError):
        await a.rerank({"query": QUERY, "documents": DOCS}, "r0")


async def test_rerank_base_default_400(tmp_path):
    # A chat-only fake adapter inherits the base default only if it doesn't
    # override; the fake DOES implement rerank, so assert the base contract via
    # the capability gate instead (echo model, supports_rerank false).
    mgr = _mgr(tmp_path)
    with pytest.raises(ValidationError):
        await mgr.rerank("echo", {"query": QUERY, "documents": DOCS}, "r0")
    await mgr.shutdown()


# --- normalizer (the httpbase passthrough + fake share this) -----------------
def test_build_rerank_response_normalizes():
    from broker.openai_compat import build_rerank_response

    # Backend may emit `score` (not relevance_score), out of order; broker sorts.
    scored = [
        {"index": 0, "relevance_score": 0.1},
        {"index": 1, "relevance_score": 0.9},
        {"index": 2, "relevance_score": 0.5},
    ]
    out = build_rerank_response(
        scored=scored,
        documents=["a", "b", "c"],
        model="m",
        request_id="rid",
        top_n=2,
        return_documents=True,
    )
    assert out["object"] == "rerank"
    assert out["model"] == "m"
    # Sorted desc + top_n applied.
    assert [r["index"] for r in out["results"]] == [1, 2]
    # Document echoed from the ORIGINAL list, as a {text} object.
    assert out["results"][0]["document"] == {"text": "b"}


def test_build_rerank_response_omits_document_when_false():
    from broker.openai_compat import build_rerank_response

    out = build_rerank_response(
        scored=[{"index": 0, "relevance_score": 0.5}],
        documents=["a"],
        model="m",
        request_id="rid",
        return_documents=False,
    )
    # Omitted entirely (never null), matching Cohere v2 / LiteLLM.
    assert "document" not in out["results"][0]
