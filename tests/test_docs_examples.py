"""Documentation-example tests.

These assert that the request/response examples shown in docs/api-reference.md and
README.md actually work against the real API (fake backend). If the docs drift
from the implementation, this test fails. Run in CI as the "documentation
examples work against the real API" gate.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from broker.app import create_app
from broker.config import Settings

KEY = "docs-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def docs_client(tmp_path):
    reg = tmp_path / "registry.yaml"
    reg.write_text(
        textwrap.dedent(
            """
            models:
              - alias: echo-fake
                backend: fake
                runtime_model_id: fake-echo-v1
                idle_timeout_seconds: 60
            """
        ).strip()
    )
    settings = Settings(
        auth_enabled=True,
        api_keys=[KEY],
        registry_path=str(reg),
        enabled_backends=["fake"],
    )
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_readme_list_models_example(docs_client):
    # Documented: GET /v1/models with a Bearer key returns the echo-fake model.
    r = docs_client.get("/v1/models", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert any(m["id"] == "echo-fake" for m in body["data"])


def test_readme_chat_example(docs_client):
    # Documented curl body -> documented response shape.
    body = {
        "model": "echo-fake",
        "messages": [{"role": "user", "content": "Hello!"}],
    }
    r = docs_client.post("/v1/chat/completions", headers=AUTH, json=body)
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "chat.completion"
    assert data["model"] == "echo-fake"
    assert data["choices"][0]["message"]["role"] == "assistant"
    assert "usage" in data


def test_readme_streaming_example(docs_client):
    body = {
        "model": "echo-fake",
        "messages": [{"role": "user", "content": "Hello!"}],
        "stream": True,
    }
    with docs_client.stream("POST", "/v1/chat/completions", headers=AUTH, json=body) as r:
        assert r.status_code == 200
        saw_done = False
        for line in r.iter_lines():
            if line == "data: [DONE]":
                saw_done = True
            elif line.startswith("data: "):
                json.loads(line[6:])
    assert saw_done


def test_readme_health_examples(docs_client):
    assert docs_client.get("/healthz").json()["status"] == "ok"
    assert docs_client.get("/readyz").json()["status"] == "ready"


def _runnable_bash_blocks(md_text: str) -> list[str]:
    """Fenced ```bash blocks immediately preceded by a <!-- runnable --> marker."""
    pat = re.compile(r"<!--\s*runnable\s*-->\s*\n```bash\n(.*?)\n```", re.DOTALL)
    return [m.group(1) for m in pat.finditer(md_text)]


def _parse_curls(block: str):
    """Yield (method, path, json_body) for each curl in a (continuation-joined) block."""
    joined = block.replace("\\\n", " ")
    for seg in re.split(r"(?=\bcurl\b)", joined):
        seg = seg.strip()
        if not seg.startswith("curl"):
            continue
        murl = re.search(r"\$BASE(\S*)", seg)
        if not murl:
            continue
        path = murl.group(1)
        mbody = re.search(r"-d '(\{.*?\})'", seg)
        body = json.loads(mbody.group(1)) if mbody else None
        yield ("POST" if body is not None else "GET", path, body)


def test_getting_started_track_c_runnable_blocks(tmp_path):
    """Execute every <!-- runnable --> block in docs/getting-started.md (Track C).

    The heredoc block defines the fake registry; the curl blocks are replayed as
    real requests against that registry on the in-process fake backend.
    """
    from broker.registry import load_registry

    md = (Path(__file__).resolve().parent.parent / "docs" / "getting-started.md").read_text()
    blocks = _runnable_bash_blocks(md)
    assert blocks, "no runnable blocks found in getting-started.md"

    # 1. The heredoc block carries the documented registry — load it for real.
    reg_yaml = None
    for b in blocks:
        m = re.search(r"<<'YAML'\n(.*?)\nYAML", b, re.DOTALL)
        if m:
            reg_yaml = m.group(1)
            break
    assert reg_yaml, "registry heredoc not found in a runnable block"
    reg = tmp_path / "registry.yaml"
    reg.write_text(reg_yaml)
    obj = load_registry(str(reg), {"fake"})
    aliases = {mm.alias for mm in obj.models}
    assert {"echo-fake", "echo-embed"} <= aliases

    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg),
        enabled_backends=["fake"],
        health_probe_enabled=False,
    )
    ran = 0
    with TestClient(create_app(settings)) as c:
        for b in blocks:
            for method, path, body in _parse_curls(b):
                r = c.request(method, path, json=body)
                assert r.status_code == 200, f"{method} {path} -> {r.status_code}: {r.text}"
                ran += 1
                if path == "/v1/models":
                    assert any(m["id"] == "echo-fake" for m in r.json()["data"])
                b_ = body or {}
                if path == "/v1/chat/completions" and b_.get("model") == "echo-fake":
                    if b_.get("stream"):
                        assert "data: [DONE]" in r.text
                    else:
                        assert "echo: echo-fake" in r.text
    assert ran >= 5, f"expected to execute several documented requests, ran {ran}"


def test_examples_rerank_registry_and_snippet(tmp_path):
    """examples/rerank/ loads as a registry and its /v1/rerank snippet runs (ADR 0013)."""
    repo_root = Path(__file__).resolve().parent.parent
    reg_path = repo_root / "examples" / "rerank" / "registry.yaml"
    assert reg_path.exists(), "examples/rerank/registry.yaml missing"
    settings = Settings(
        auth_enabled=False,
        api_keys=[],
        registry_path=str(reg_path),
        enabled_backends=["fake"],
    )
    app = create_app(settings)
    with TestClient(app) as c:
        # The fake reranker is advertised as rerank-capable.
        models = {m["id"]: m for m in c.get("/v1/models").json()["data"]}
        assert models["rerank-fake"]["capabilities"]["rerank"] is True
        # The documented request body (from rerank.sh / rerank.py) works.
        r = c.post(
            "/v1/rerank",
            json={
                "model": "rerank-fake",
                "query": "What is the capital of France?",
                "documents": [
                    "The capital of France is Paris.",
                    "Bananas are a yellow tropical fruit rich in potassium.",
                    "Berlin is the capital and largest city of Germany.",
                ],
                "top_n": 2,
                "return_documents": True,
            },
        )
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) == 2
    assert results[0]["index"] == 0  # the Paris document ranks first
    assert results[0]["document"]["text"].startswith("The capital of France")
