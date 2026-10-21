# Getting Started

A hands-on, ~10-minute walkthrough from zero to your first chat. Pick the track
that matches what you have:

- **[Track C — no GPU, no backend](#track-c--no-gpu-no-backend-fake)** — runs
  anywhere using the built-in **fake** backend. Start here if you just want to see
  the broker work; every command is runnable as written.
- **[Track A — local Python + Ollama](#track-a--local-python--ollama)** — your
  first **real** model, run from source.
- **[Track B — Docker (lean image) + Ollama](#track-b--docker-lean-image--ollama)**
  — the same, containerized.

All three show the OpenAI-compatible surface: `auto` routing, streaming,
embeddings, and the OpenAI Python SDK.

> **Redaction note:** real hosts and keys are shown as placeholders
> (`<OLLAMA_HOST>`, `${OLLAMA_BASE_URL}`, `YOUR_API_KEY`). Never commit real
> endpoints or keys — they belong in a gitignored `.env`.

## Prerequisites (all tracks)

- **Python 3.11+** (`python3 --version`) and `git`, `curl` — for Tracks A and C.
- **Docker** — for Track B.
- A real backend (e.g. an **Ollama** daemon) — for Tracks A and B only.

---

## Track C — no GPU, no backend (fake)

The **fake** backend is a real in-process adapter that echoes your input. It
exercises the full request path — auth, lifecycle, streaming, embeddings, `auto`,
`/status` — with no model download, GPU, or network. Perfect for a first look and
for CI.

### 1. Install

```bash
git clone <YOUR_REPO_URL> backend-agnostic-broker
cd backend-agnostic-broker
make venv && make install        # creates .venv/ and installs runtime + dev deps
.venv/bin/python -c "import broker; print('broker', broker.__version__)"
# -> broker 0.7.0
```

### 2. Write a tiny fake-only registry

<!-- runnable -->
```bash
cat > registry.yaml <<'YAML'
router:
  default: echo-fake            # used if the classifier returns an invalid choice
models:
  - alias: echo-fake
    backend: fake
    runtime_model_id: fake-echo-v1
    supports_streaming: true
    tags: [general, cheap, fast]
    cost_tier: 1
  - alias: echo-embed
    backend: fake
    runtime_model_id: fake-embed
    router_eligible: false       # an embedding model is never a router target
    tags: [embedding]
YAML
```

### 3. Run the broker (fake backend only)

```bash
BROKER_AUTH_ENABLED=true BROKER_API_KEYS='["YOUR_API_KEY"]' \
  BROKER_ENABLED_BACKENDS='["fake"]' BROKER_REGISTRY_PATH=registry.yaml \
  BROKER_CACHE_DIR=./.cache \
  .venv/bin/python -m broker
```

You should see a startup log and `Uvicorn running on http://0.0.0.0:8080`. Leave it
running; open a second terminal for the requests below.

```bash
BASE=http://127.0.0.1:8080
KEY=YOUR_API_KEY                 # must match a value in BROKER_API_KEYS
```

### 4. Health and models

<!-- runnable -->
```bash
curl -s $BASE/healthz            # -> {"status":"ok"}
curl -s $BASE/readyz             # -> {"status":"ready", ...}
```

<!-- runnable -->
```bash
curl -s -H "Authorization: Bearer $KEY" $BASE/v1/models | python3 -m json.tool
```

Expected (trimmed): a `list` containing `echo-fake` and `echo-embed`. A request
**without** the key returns `401` — auth is enforced.

### 5. Chat (non-streaming)

<!-- runnable -->
```bash
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"Hello, broker!"}]}' \
  $BASE/v1/chat/completions | python3 -m json.tool
```

Expected: a `chat.completion` whose `choices[0].message.content` is
`echo: echo-fake`, with a `usage` block.

### 6. Let `auto` pick the model

<!-- runnable -->
```bash
curl -s -D - -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"hi"}]}' \
  $BASE/v1/chat/completions
```

Expected: the `x-broker-route` response header names the served model (`echo-fake`
here). You send `"model":"auto"` and the broker chooses from the registry.

### 7. Streaming (SSE)

<!-- runnable -->
```bash
curl -N -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"stream this"}],"stream":true}' \
  $BASE/v1/chat/completions
```

Expected: a sequence of `data: {...}` `chat.completion.chunk` frames with
incremental `delta.content`, a final frame with `"finish_reason":"stop"`, and one
terminating `data: [DONE]`.

### 8. Embeddings

<!-- runnable -->
```bash
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"echo-embed","input":"embed me"}' \
  $BASE/v1/embeddings | python3 -m json.tool
```

Expected: `{"object":"list","data":[{"object":"embedding","index":0,"embedding":[...]}], "usage":{...}}`.

### 9. From the OpenAI Python SDK

The broker is OpenAI-compatible, so existing clients work unchanged — only
`base_url` and `api_key` change:

```python
# pip install openai
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8080/v1", api_key="YOUR_API_KEY")

print(client.chat.completions.create(
    model="echo-fake",
    messages=[{"role": "user", "content": "Hello from the SDK"}],
).choices[0].message.content)        # -> echo: echo-fake

for chunk in client.chat.completions.create(
    model="echo-fake",
    messages=[{"role": "user", "content": "stream from the SDK"}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
```

### What just happened

You ran the full broker — auth, lazy lifecycle, `auto` routing, streaming,
embeddings — with no GPU or external runtime. The fake backend stands in for a real
model; the request path is identical to production.

### Next steps

Swap the fake backend for a real one: continue to **Track A** (Ollama from source)
or **Track B** (Docker). Then explore the [how-to guides](guides/README.md).

---

## Track A — local Python + Ollama

Run your first **real** model from source against an **Ollama** daemon (local, LAN,
or tailnet).

### Prerequisites

- Track C's install done (`.venv` ready).
- An Ollama daemon with a model pulled: `ollama pull llama3.1:8b`.

### 1. Point the broker at Ollama (host stays out of git)

```bash
cp .env.example .env
cp registry.example.yaml registry.yaml
```

In `.env`, set your host and a key:

```ini
BROKER_AUTH_ENABLED=true
BROKER_API_KEYS=["YOUR_API_KEY"]
OLLAMA_BASE_URL=http://<OLLAMA_HOST>:11434     # local: http://127.0.0.1:11434
```

The template `registry.yaml` already has an Ollama entry (`llama3-ollama`) that
reads `${OLLAMA_BASE_URL}`, plus an embedding model (`nomic-embed-text`).

### 2. Run with the Ollama backend enabled

```bash
BROKER_ENABLED_BACKENDS='["fake","ollama"]' make run
```

### 3. Use the real model

```bash
BASE=http://127.0.0.1:8080; KEY=YOUR_API_KEY
curl -s -H "Authorization: Bearer $KEY" $BASE/v1/models | python3 -m json.tool   # llama3-ollama appears
curl -s -H "Authorization: Bearer $KEY" -H 'content-type: application/json' \
  -d '{"model":"llama3-ollama","messages":[{"role":"user","content":"Say hi in one sentence."}]}' \
  $BASE/v1/chat/completions | python3 -m json.tool
```

Streaming, embeddings (`nomic-embed-text`), `auto`, and the OpenAI SDK all work the
same as in Track C — just use the real model names.

### 4. See lazy-load and idle-unload

```bash
curl -s -H "Authorization: Bearer $KEY" $BASE/status | python3 -m json.tool
```

Before any request `llama3-ollama` is `unloaded`; after one chat it's `ready` with
`loaded_at` set, while `echo-fake` stays `unloaded` — **only the selected model
loads, never a fallback**. Past `idle_timeout_seconds` the idle sweeper unloads it,
and the next request reloads it transparently.

### What just happened

The broker lazily loaded a real Ollama model on first use, kept the host out of git
via `${OLLAMA_BASE_URL}`, and exposed it through the same OpenAI API.

### Next steps

[backends guide](guides/backends.md) · [pools & routing](guides/pools-and-routing.md) ·
[auto-router](guides/auto-router.md).

---

## Track B — Docker (lean image) + Ollama

The same real setup, containerized with the hardened **lean** image.

### Prerequisites

- Docker installed; an Ollama daemon reachable from the container.

### 1. Pull (or build) the image

```bash
docker pull ghcr.io/simeononsecurity/backend-agnostic-broker:latest
# or build locally: make docker-build
```

### 2. Run it

Mount a registry and pass config as env. The host stays in `OLLAMA_BASE_URL`:

```bash
docker run --rm -p 8080:8080 \
  -e BROKER_AUTH_ENABLED=true \
  -e BROKER_API_KEYS='["YOUR_API_KEY"]' \
  -e BROKER_ENABLED_BACKENDS='["fake","ollama"]' \
  -e OLLAMA_BASE_URL='http://<OLLAMA_HOST>:11434' \
  -e BROKER_REGISTRY_PATH=/config/registry.yaml \
  -v "$PWD/registry.example.yaml:/config/registry.yaml:ro" \
  ghcr.io/simeononsecurity/backend-agnostic-broker:latest
```

For a full stack (plus an Ollama service on the same network, so you use
`http://ollama:11434` instead of a host IP), use
[examples/ollama-quickstart/docker-compose.yml](../examples/ollama-quickstart/docker-compose.yml).

### 3. Use it

Identical requests to Track A — `curl http://127.0.0.1:8080/v1/chat/completions`
with `"model":"llama3-ollama"` (or `"auto"`), streaming, embeddings, and the SDK.

### What just happened

You ran the broker as a non-root, read-only-root-fs container and reached a real
Ollama model over the network, with no code checkout.

### Next steps

[Docker guide](docker.md) · [production deployment](guides/production-deployment.md) ·
[auto-router `-llm` image](guides/auto-router.md).

---

## Troubleshooting quick hits

| Symptom | Likely cause / fix |
|---|---|
| `401` on `/v1/*` | Missing/wrong key. Header must be `Authorization: Bearer <key>` (or `X-API-Key`) and match `BROKER_API_KEYS`. |
| `model not found` | The `model` must match a registry `alias`, and its backend must be in `BROKER_ENABLED_BACKENDS`. No silent fallback is by design. |
| Ollama model errors | `runtime_model_id` must be pulled on the daemon (`ollama pull ...`). |
| Can't reach remote Ollama | Usually networking, not SSRF — the entry's own `base_url` is auto-trusted. Check URL/port and that the daemon binds a reachable interface. |
| Startup fails with a config error | Intentional — fix the reported env/registry field. See [configuration](configuration.md). |

More: [how-to guides](guides/README.md) · [troubleshooting](troubleshooting.md) ·
[configuration](configuration.md) · [API reference](api-reference.md).
