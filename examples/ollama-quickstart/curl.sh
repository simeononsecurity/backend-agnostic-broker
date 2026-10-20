#!/usr/bin/env bash
# Ollama quickstart — curl walkthrough. Start the broker first:
#   set -a && . ./examples/ollama-quickstart/.env.example && set +a
#   python -m broker
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8080}
KEY=${KEY:-YOUR_API_KEY}            # must match a value in BROKER_API_KEYS

# Health (no auth):
curl -s "$BASE/healthz"             # -> {"status":"ok"}

# List models (auth required):
curl -s -H "Authorization: Bearer $KEY" "$BASE/v1/models"

# Chat (non-streaming) against the offline fake model:
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"Hello!"}]}' \
  "$BASE/v1/chat/completions"

# Once the real Ollama entry is enabled, just change the model name:
#   -d '{"model":"llama3","messages":[{"role":"user","content":"Hello!"}]}'
