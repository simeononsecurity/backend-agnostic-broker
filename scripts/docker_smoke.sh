#!/usr/bin/env bash
# Build-independent container smoke test: run the image with the fake backend,
# hit the API, assert expected responses, then tear down.
set -euo pipefail

IMAGE="${1:-backend-agnostic-broker:0.1.0}"
NAME="broker-smoke-$$"
PORT="${SMOKE_PORT:-8099}"
KEY="smoke-key"

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

echo "[smoke] starting container from $IMAGE"
docker run -d --name "$NAME" \
  -p "${PORT}:8080" \
  -e BROKER_AUTH_ENABLED=true \
  -e "BROKER_API_KEYS=[\"${KEY}\"]" \
  -e BROKER_ENABLED_BACKENDS='["fake"]' \
  -e BROKER_REGISTRY_PATH=/config/registry.yaml \
  -v "$(pwd)/registry.example.yaml:/config/registry.yaml:ro" \
  --read-only --tmpfs /tmp/broker --tmpfs /var/cache/broker \
  --cap-drop ALL --security-opt no-new-privileges:true \
  "$IMAGE" >/dev/null

echo "[smoke] waiting for readiness"
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then break; fi
  sleep 1
done

echo "[smoke] /healthz"
curl -fsS "http://127.0.0.1:${PORT}/healthz" | grep -q '"status":"ok"'

echo "[smoke] /v1/models (auth required)"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/v1/models")
[ "$code" = "401" ] || { echo "expected 401, got $code"; exit 1; }
curl -fsS -H "Authorization: Bearer ${KEY}" "http://127.0.0.1:${PORT}/v1/models" | grep -q 'echo-fake'

echo "[smoke] /v1/chat/completions"
curl -fsS -H "Authorization: Bearer ${KEY}" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"hi"}]}' \
  "http://127.0.0.1:${PORT}/v1/chat/completions" | grep -q '"object":"chat.completion"'

echo "[smoke] container logs tail:"
docker logs "$NAME" 2>&1 | tail -3

echo "[smoke] PASS"
