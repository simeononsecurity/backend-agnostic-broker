#!/usr/bin/env bash
# POST /v1/rerank example (ADR 0013). Cohere/Jina-compatible.
#
# Local trial with the fake backend (no GPU, no auth):
#   BROKER_AUTH_ENABLED=false BROKER_ENABLED_BACKENDS=fake \
#   BROKER_REGISTRY_PATH=examples/rerank/registry.yaml \
#   python -m broker   # serves on :8080
#
# then run this script. Override BASE/KEY/MODEL via env.
set -euo pipefail
BASE="${BROKER_BASE_URL:-http://127.0.0.1:8080}"
KEY="${BROKER_API_KEY:-}"            # set for an auth-enabled broker
MODEL="${RERANK_MODEL:-rerank-fake}" # use "bge-reranker" against a real runtime

auth=()
[ -n "$KEY" ] && auth=(-H "Authorization: Bearer $KEY")

curl -s "${auth[@]}" -H 'content-type: application/json' \
  -d '{
        "model": "'"$MODEL"'",
        "query": "What is the capital of France?",
        "documents": [
          "The capital of France is Paris.",
          "Bananas are a yellow tropical fruit rich in potassium.",
          "Berlin is the capital and largest city of Germany."
        ],
        "top_n": 2,
        "return_documents": true
      }' \
  "$BASE/v1/rerank"
# -> results sorted by relevance_score desc; index 0 (Paris) ranks first.
