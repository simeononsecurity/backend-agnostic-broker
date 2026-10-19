#!/usr/bin/env bash
# Caching example (ADR 0012 + ADR 0014). Assumes the broker is running on :8080
# with examples/caching/env.semantic sourced. Replace YOUR_API_KEY.
set -euo pipefail
BASE=${BROKER_BASE:-http://127.0.0.1:8080}
KEY=${BROKER_API_KEY:-YOUR_API_KEY}
AUTH="Authorization: Bearer ${KEY}"

echo "== 1) first request: MISS (served fresh, then stored) =="
curl -sS -D - -o /dev/null "$BASE/v1/chat/completions" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"model":"echo-chat","messages":[{"role":"user","content":"What is the capital of France?"}],"temperature":0}' \
  | grep -i '^x-broker-cache'

echo "== 2) identical request: exact HIT =="
curl -sS -D - -o /dev/null "$BASE/v1/chat/completions" -H "$AUTH" \
  -H 'Content-Type: application/json' \
  -d '{"model":"echo-chat","messages":[{"role":"user","content":"What is the capital of France?"}],"temperature":0}' \
  | grep -i '^x-broker-cache'

echo "== 3) warm the embedding model so the semantic cache can engage =="
curl -sS -o /dev/null "$BASE/v1/embeddings" -H "$AUTH" \
  -H 'Content-Type: application/json' -d '{"model":"echo-embed","input":"warm"}'

echo "== 4) force a fresh answer with a bypass =="
curl -sS -D - -o /dev/null "$BASE/v1/chat/completions" -H "$AUTH" \
  -H 'Cache-Control: no-cache' -H 'Content-Type: application/json' \
  -d '{"model":"echo-chat","messages":[{"role":"user","content":"What is the capital of France?"}],"temperature":0}' \
  | grep -i '^x-broker-cache'

echo "== 5) per-request controls: short TTL + a private namespace =="
curl -sS -D - -o /dev/null "$BASE/v1/chat/completions" -H "$AUTH" \
  -H 'x-broker-cache-ttl: 30' -H 'x-broker-cache-namespace: tenant-a' \
  -H 'Content-Type: application/json' \
  -d '{"model":"echo-chat","messages":[{"role":"user","content":"ping"}],"temperature":0}' \
  | grep -i '^x-broker-cache'
