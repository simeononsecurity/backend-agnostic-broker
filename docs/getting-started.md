# Getting Started

A hands-on walkthrough: install the broker, run it with the built-in **fake**
backend (no GPU or external runtime needed), make real requests, then point it
at a **remote Ollama** backend over your network. Every command below is
copy-pasteable.

> **Redaction note:** real host IPs and hostnames in this guide are shown as
> placeholders like `<OLLAMA_HOST>`, `<TAILNET_HOST_OR_IP>`, and `YOUR_API_KEY`.
> Never commit real endpoints or keys — they belong in a gitignored `.env`
> (see [Step 5](#step-5--connect-a-real-backend-ollama-over-the-network)).

---

## Prerequisites

- **Python 3.11+** (`python3 --version`)
- **git**, `make`, and `curl`
- Optional: **Docker** (for the container quickstart)
- Optional: a real backend to route to (e.g. an **Ollama** daemon on your LAN or
  tailnet) — only needed for [Step 5](#step-5--connect-a-real-backend-ollama-over-the-network)

---

## Step 1 — Install

```bash
git clone <YOUR_REPO_URL> backend-agnostic-broker
cd backend-agnostic-broker

make venv && make install     # creates .venv/ and installs runtime + dev deps
# equivalent to:
#   python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

Verify the install:

```bash
.venv/bin/python -c "import broker; print('broker', broker.__version__)"
# -> broker 0.2.0
```

---

## Step 2 — Create local config

Two files drive the broker. Both are **gitignored** so your real values never
get committed; starter templates are checked in with `.example` suffixes.

```bash
cp .env.example .env                      # server + secrets + endpoints
cp registry.example.yaml registry.yaml    # the models you can call
```

Open `.env` and set at least an API key (or disable auth for quick local dev):

```ini
# .env
BROKER_AUTH_ENABLED=true
BROKER_API_KEYS=["YOUR_API_KEY"]          # pick any string for local use
BROKER_PORT=8080
BROKER_REGISTRY_PATH=registry.yaml
```

> To skip auth entirely while experimenting, set `BROKER_AUTH_ENABLED=false` and
> drop the `Authorization` header from the examples below.

The registry (`registry.yaml`) already contains an `echo-fake` model from the
template — that's all you need for the next step.

---

## Step 3 — Run with the fake backend (no GPU, no external runtime)

The **fake** backend is a real in-process adapter that echoes your input. It
exercises the full request path — auth, lifecycle, streaming, `/status` — without
any model download or GPU.

```bash
# Enable only the fake backend for this run:
BROKER_ENABLED_BACKENDS='["fake"]' make run
# (make run just calls: .venv/bin/python -m broker)
```

You should see a startup log like:

```
broker 0.2.0 started; 1 model registered (none loaded)
Uvicorn running on http://0.0.0.0:8080
```

Leave it running and open a second terminal for Step 4.

---

## Step 4 — Make your first requests

Set up two shell variables so the examples stay clean:

```bash
BASE=http://127.0.0.1:8080
KEY=YOUR_API_KEY          # must match a value in BROKER_API_KEYS
```

### Health & readiness (no auth required)

```bash
curl -s $BASE/healthz   # -> {"status":"ok"}
curl -s $BASE/readyz    # -> {"status":"ready","models_registered":1}
```

### List models

```bash
# With auth -> 200 and a model list:
curl -s -H "Authorization: Bearer $KEY" $BASE/v1/models | python3 -m json.tool

# Without auth -> 401 (proves auth is enforced):
curl -s -o /dev/null -w "%{http_code}\n" $BASE/v1/models    # -> 401
```

Expected (trimmed):

```json
{
  "object": "list",
  "data": [
    { "id": "echo-fake", "object": "model", "backend": "fake",
      "capabilities": { "streaming": true, "tools": false } }
  ]
}
```

### Chat — non-streaming

```bash
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{
    "model": "echo-fake",
    "messages": [{"role":"user","content":"Hello, broker!"}]
  }' \
  $BASE/v1/chat/completions | python3 -m json.tool
```

Expected (trimmed):

```json
{
  "object": "chat.completion",
  "model": "echo-fake",
  "choices": [{ "index": 0, "message": {"role":"assistant","content":"Hello, broker!"},
                "finish_reason": "stop" }],
  "usage": { "prompt_tokens": 3, "completion_tokens": 3, "total_tokens": 6 }
}
```

### Chat — streaming (SSE)

```bash
curl -N -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{
    "model": "echo-fake",
    "messages": [{"role":"user","content":"stream this"}],
    "stream": true
  }' \
  $BASE/v1/chat/completions
```

You'll see a sequence of `data: {...}` `chat.completion.chunk` frames, each with an
incremental `delta.content`, a final frame with `"finish_reason":"stop"`, and
exactly one terminating `data: [DONE]`.

### Watch the lifecycle

```bash
curl -s -H "Authorization: Bearer $KEY" $BASE/status | python3 -m json.tool
```

`/status` shows each model's lifecycle state (`unloaded → loading → ready → …`),
when it was loaded, and GPU allocation. The fake backend loads on first request
and unloads after its idle timeout.

### Call it from the OpenAI Python SDK

The broker is OpenAI-compatible, so existing clients work unchanged — just point
`base_url` at the broker:

```python
# pip install openai
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="YOUR_API_KEY")

resp = client.chat.completions.create(
    model="echo-fake",
    messages=[{"role": "user", "content": "Hello from the SDK"}],
)
print(resp.choices[0].message.content)

# Streaming:
for chunk in client.chat.completions.create(
    model="echo-fake",
    messages=[{"role": "user", "content": "stream from the SDK"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

---

## Step 5 — Connect a real backend (Ollama) over the network

Now route to a real **Ollama** daemon. The daemon can be local or on another host
(LAN, VPN, or tailnet). **The host address stays out of git** — you set it in
`.env` as `OLLAMA_BASE_URL`, and `registry.yaml` references it as
`${OLLAMA_BASE_URL}`.

### 5a. Point the broker at your Ollama host

```ini
# .env  — use YOUR host; shown redacted here.
# Local daemon:
OLLAMA_BASE_URL=http://127.0.0.1:11434
# Remote daemon (LAN / tailnet) — replace the placeholder with your host or IP:
# OLLAMA_BASE_URL=http://<OLLAMA_HOST>:11434
# OLLAMA_BASE_URL=http://<TAILNET_HOST_OR_IP>:11434
```

The template `registry.yaml` already has an Ollama entry that consumes it:

```yaml
# registry.yaml (from registry.example.yaml) — no host IP is committed
- alias: llama3-ollama
  backend: ollama
  runtime_model_id: llama3.1:8b          # must exist on the daemon
  base_url: ${OLLAMA_BASE_URL:-http://127.0.0.1:11434}
  readiness: { kind: http, path: /api/tags, expect_status: 200 }
  idle_timeout_seconds: 300
  keep_alive: "5m"
  supports_streaming: true
  supports_tools: true
```

Make sure the model exists on the daemon (run this where Ollama lives, or against
the remote: `OLLAMA_HOST=<OLLAMA_HOST>:11434 ollama pull llama3.1:8b`).

### 5b. Sanity-check connectivity (optional)

```bash
# Replace <OLLAMA_HOST> with your real host/IP. Expect a JSON model list.
curl -s http://<OLLAMA_HOST>:11434/api/tags | python3 -m json.tool
```

### 5c. Restart the broker with the Ollama backend enabled

```bash
BROKER_ENABLED_BACKENDS='["fake","ollama"]' make run
```

### 5d. Use the Ollama-backed model

```bash
BASE=http://127.0.0.1:8080
KEY=YOUR_API_KEY

# It now appears in the model list:
curl -s -H "Authorization: Bearer $KEY" $BASE/v1/models | python3 -m json.tool

# Real completion routed to your Ollama host:
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"llama3-ollama",
       "messages":[{"role":"user","content":"Say hello in one short sentence."}]}' \
  $BASE/v1/chat/completions | python3 -m json.tool
```

### 5e. See lazy-load and idle-unload in action

```bash
# BEFORE any request: llama3-ollama is "unloaded".
curl -s -H "Authorization: Bearer $KEY" $BASE/status | python3 -m json.tool

# Send one chat request (5d), then check again: it's now "ready" with loaded_at set,
# while echo-fake stays "unloaded" — only the selected model loads (no fallback).
curl -s -H "Authorization: Bearer $KEY" $BASE/status | python3 -m json.tool

# Wait past idle_timeout_seconds (300s above, or lower it in registry.yaml for a
# quick demo). The idle sweeper unloads it; /status shows "unloaded" again, and
# the next request transparently reloads it.
```

---

## Step 6 — Run in Docker (optional)

```bash
make docker-build          # build the hardened image
make docker-smoke          # build + container smoke test (healthz, auth, chat)

# Or the full stack via Compose (reads .env):
docker compose up -d --build
docker compose logs -f broker
```

For GPU passthrough and the remote-backend-from-container networking notes, see
[docs/docker.md](docker.md) and [docs/docker-compose.md](docker-compose.md).

---

## Troubleshooting quick hits

| Symptom | Likely cause / fix |
|---|---|
| `401` on `/v1/*` | Missing/!wrong key. Header must be `Authorization: Bearer <key>` (or `X-API-Key`) and match `BROKER_API_KEYS`. |
| `model not found` | The `model` in the request must match a registry `alias`, and that backend must be in `BROKER_ENABLED_BACKENDS`. No silent fallback is by design. |
| Ollama model errors | `runtime_model_id` must be pulled on the daemon; confirm with `curl http://<OLLAMA_HOST>:11434/api/tags`. |
| Can't reach remote Ollama | Usually networking, not SSRF: the broker auto-allows each registry entry's own `base_url`, so `${OLLAMA_BASE_URL}` is trusted by default. Check the URL/port and that the daemon binds a reachable interface (`OLLAMA_HOST=0.0.0.0:11434`). `BROKER_EXTRA_ALLOWED_BACKEND_URLS` is only for upstreams *not* declared in the registry; `BROKER_ALLOW_LOOPBACK_BACKENDS` governs loopback targets — see [configuration.md](configuration.md). |
| Startup fails fast with a config error | That's intentional — fix the reported env/registry field. See [configuration.md](configuration.md). |

More: [docs/troubleshooting.md](troubleshooting.md) ·
[docs/configuration.md](configuration.md) ·
[docs/model-registry.md](model-registry.md) ·
[docs/api-reference.md](api-reference.md)
