#!/usr/bin/env python3
"""POST /v1/rerank example (ADR 0013), stdlib-only (no SDK required).

Local trial with the fake backend (no GPU, no auth):

    BROKER_AUTH_ENABLED=false BROKER_ENABLED_BACKENDS=fake \\
    BROKER_REGISTRY_PATH=examples/rerank/registry.yaml \\
    python -m broker            # serves on :8080

    python examples/rerank/rerank.py

Override BROKER_BASE_URL / BROKER_API_KEY / RERANK_MODEL via env. The response is
Cohere/Jina-compatible: results sorted by relevance_score descending, `index` is
the original document ordinal.
"""

from __future__ import annotations

import json
import os
import urllib.request

BASE = os.environ.get("BROKER_BASE_URL", "http://127.0.0.1:8080")
KEY = os.environ.get("BROKER_API_KEY", "")
MODEL = os.environ.get("RERANK_MODEL", "rerank-fake")

DOCUMENTS = [
    "The capital of France is Paris.",
    "Bananas are a yellow tropical fruit rich in potassium.",
    "Berlin is the capital and largest city of Germany.",
]


def rerank(query: str, documents: list[str], top_n: int | None = None) -> dict:
    body = {"model": MODEL, "query": query, "documents": documents, "return_documents": True}
    if top_n is not None:
        body["top_n"] = top_n
    req = urllib.request.Request(
        f"{BASE}/v1/rerank",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    if KEY:
        req.add_header("Authorization", f"Bearer {KEY}")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - local broker URL
        return json.loads(resp.read())


if __name__ == "__main__":
    out = rerank("What is the capital of France?", DOCUMENTS, top_n=2)
    for r in out["results"]:
        doc = r.get("document", {}).get("text", "")
        print(f"#{r['index']}  score={r['relevance_score']:.4f}  {doc}")
