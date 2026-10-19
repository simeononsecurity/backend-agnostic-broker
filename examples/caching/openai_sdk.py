"""Caching example with the official OpenAI Python SDK (ADR 0012 + ADR 0014).

Run the broker with examples/caching/env.semantic sourced, then:
    pip install openai
    BROKER_API_KEY=YOUR_API_KEY python examples/caching/openai_sdk.py

The broker is OpenAI-compatible, so point the SDK's base_url at it. Cache status
is returned in the `x-broker-cache` response header, surfaced here via the SDK's
raw-response accessor.
"""

from __future__ import annotations

import os

from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("BROKER_BASE", "http://127.0.0.1:8080") + "/v1",
    api_key=os.environ.get("BROKER_API_KEY", "YOUR_API_KEY"),
)

MESSAGES = [{"role": "user", "content": "What is the capital of France?"}]


def ask(**extra_headers):
    resp = client.chat.completions.with_raw_response.create(
        model="echo-chat", messages=MESSAGES, temperature=0, extra_headers=extra_headers or None
    )
    cache = resp.headers.get("x-broker-cache")
    sim = resp.headers.get("x-broker-cache-similarity")
    completion = resp.parse()
    print(f"x-broker-cache={cache}" + (f" similarity={sim}" if sim else ""))
    return completion


if __name__ == "__main__":
    print("1) first call:")
    ask()  # -> miss
    print("2) identical call:")
    ask()  # -> hit (exact)
    print("3) warm the embedding model so the semantic layer can engage:")
    client.embeddings.create(model="echo-embed", input="warm")
    print("4) forced bypass:")
    ask(**{"Cache-Control": "no-cache"})  # -> bypass
    print("5) per-request TTL + namespace:")
    ask(**{"x-broker-cache-ttl": "30", "x-broker-cache-namespace": "tenant-a"})
