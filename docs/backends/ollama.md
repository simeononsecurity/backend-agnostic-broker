# Backend: Ollama

## Broker-confirmed behavior (authoritative — from `broker/adapters/ollama.py`)

- **Native lazy loading.** Ollama runs as its own daemon (`ollama serve`) and
  loads a model on first request, unloading it after `keep_alive`. The broker
  does **not** spawn Ollama; `OllamaAdapter.start()` is a no-op (returns `None`).
- **Config validation.** `validate_config` requires `base_url` and **rejects**
  any `startup_command` (Ollama manages its own process). The `base_url` is
  SSRF-validated.
- **Readiness / health.** Polls `GET {base_url}/api/tags`; ready when `200`.
  This is also the non-loading liveness probe used by `GET /healthz/backends`
  and the background health probe (it lists models without loading any).
- **Residency.** `GET {base_url}/api/ps` (models currently resident) is exposed,
  deduplicated across daemons, via the broker's `GET /api/ps` — a non-loading
  view of real VRAM/RAM usage, distinct from the broker's lifecycle state.
- **Chat / streaming.** Forwarded to `{base_url}/v1/chat/completions`
  (OpenAI-compatible), streaming and non-streaming, via the shared HTTP adapter.
  The upstream `model` is pinned to `runtime_model_id`.
- **Unload.** Best-effort native unload: `POST {base_url}/api/generate` with
  `{"model": runtime_model_id, "keep_alive": 0}`. `stop` unloads and closes the
  client but never kills the external daemon.
- **Idle.** The broker layers its own idle tracking on top of Ollama's
  `keep_alive` for determinism (idle sweep calls `unload`).
- **Status.** `collect_status` adds `external_daemon: true` and the configured
  `keep_alive`.

## Registry example

```yaml
- alias: llama3-ollama
  backend: ollama
  runtime_model_id: llama3.1:8b
  base_url: ${OLLAMA_BASE_URL:-http://127.0.0.1:11434}
  keep_alive: "5m"
  readiness: { kind: http, path: /api/tags, expect_status: 200 }
  supports_streaming: true
  supports_tools: true
```

### Remote daemons (including over tailscale) without committing the host

`base_url` supports `${VAR}` / `${VAR:-default}` expansion (see
`docs/configuration.md`), so point the broker at a remote Ollama daemon — e.g.
one reachable over tailscale — by setting `OLLAMA_BASE_URL` in a **gitignored
`.env`** and referencing `base_url: ${OLLAMA_BASE_URL}` in `registry.yaml`. The
host IP never enters version control. The broker runs locally and makes only
**outbound** API calls to that daemon; it never deploys onto or modifies the
remote host. A live run against a remote daemon (models list, non-streaming and
streaming completions, lazy-load, idle-unload) is recorded in
`docs/EVIDENCE.md`.

## Runtime reference (vendor docs — verify before production)

These are standard Ollama behaviors; confirm against your installed version.

- Required: a running Ollama daemon (`ollama serve`), default port `11434`.
- Model listing: `GET /api/tags`. OpenAI-compatible chat: `POST
  /v1/chat/completions`. Native generate/unload: `POST /api/generate` with
  `keep_alive` (duration string like `5m`, or `0` to unload now).
- GPU selection and daemon lifetime are controlled by `OLLAMA_*` environment
  variables **on the daemon** (e.g. `CUDA_VISIBLE_DEVICES`,
  `OLLAMA_KEEP_ALIVE`, `OLLAMA_MAX_LOADED_MODELS`), documented rather than
  managed by the broker.

## Known limitations

- The broker does not manage the Ollama daemon's own GPU placement or process
  lifetime — configure those on the daemon.
- `keep_alive: 0` unload is best-effort; if the daemon is unreachable the model
  still unloads after its own `keep_alive`.

## Evidence sources

- `broker/adapters/ollama.py`, `registry.example.yaml`.
- Official Ollama API documentation: https://github.com/ollama/ollama/blob/main/docs/api.md
