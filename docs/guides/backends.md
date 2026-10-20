# How-to: configure backends

Point the broker at one or more inference backends: **Ollama**, **vLLM**,
**llama.cpp**, **Strata**, or any **OpenAI-compatible external URL**. Each model in
the registry names a `backend` and the details that backend needs.

## When to use which backend

| Backend | Use when | Loading model |
|---|---|---|
| `ollama` | You want the simplest local setup; Ollama manages model loading | Native lazy load via `keep_alive` |
| `vllm` | You need high-throughput GPU serving, one model per process | Broker-supervised process |
| `llamacpp` | You run GGUF models (incl. rerankers) on CPU or GPU | Broker-supervised process |
| `strata` | You run a Strata server (flags are deployment-specific) | Broker-supervised process |
| external URL | You front an existing OpenAI-compatible server | No process management |
| `fake` | Local trials, CI, demos — no GPU or network | In-process |

The broker **never loads a model at startup** and **never falls back to a different
model**: a request loads only the model it selected.

## Minimal config (Ollama)

```yaml
# registry.yaml
models:
  - alias: llama3
    backend: ollama
    runtime_model_id: llama3.1:8b          # must be pulled on the daemon
    base_url: ${OLLAMA_BASE_URL:-http://127.0.0.1:11434}
    readiness: { kind: http, path: /api/tags, expect_status: 200 }
    supports_streaming: true
```

```bash
BROKER_ENABLED_BACKENDS='["ollama"]' python -m broker
```

A backend only activates when it appears in `BROKER_ENABLED_BACKENDS`.

## Full config by backend

Each process-managed backend (`vllm`, `llamacpp`, `strata`) needs a `model_path`
under an allow-listed root (`BROKER_ALLOWED_MODEL_ROOTS`, default `/models`), an
argv **`startup_command`** template, a `readiness` probe, and a `gpu` block. The
broker substitutes `{model_path}`, `{context_limit}`, `{tensor_split}`,
`{layer_split}` into the template — **no request can inject a flag**. See the
annotated [`registry.example.yaml`](../../registry.example.yaml) for a complete,
working entry per backend, and the per-backend pages:

- [Ollama](../backends/ollama.md) · [vLLM](../backends/vllm.md) ·
  [llama.cpp](../backends/llamacpp.md) · [Strata](../backends/strata.md)

### External OpenAI-compatible URL

Front an existing server by giving its `base_url` and adding the host to the SSRF
allow-list. The broker auto-allows each registry entry's own `base_url`; use
`BROKER_EXTRA_ALLOWED_BACKEND_URLS` only for upstreams not declared in a registry
entry, and `BROKER_ALLOW_LOOPBACK_BACKENDS` to permit loopback targets.

## Verify

```bash
curl -s -H "Authorization: Bearer YOUR_API_KEY" http://127.0.0.1:8080/v1/models
# -> the model appears with its backend and capabilities
curl -s -H "Authorization: Bearer YOUR_API_KEY" -H 'content-type: application/json' \
  -d '{"model":"llama3","messages":[{"role":"user","content":"Say hi."}]}' \
  http://127.0.0.1:8080/v1/chat/completions
# -> a chat.completion; /status now shows llama3 "ready"
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `model not found` | The `model` must match a registry `alias`, and its backend must be in `BROKER_ENABLED_BACKENDS`. No silent fallback is by design. |
| Can't reach a remote backend | Usually networking, not SSRF — the entry's own `base_url` is trusted. Check URL/port and that the daemon binds a reachable interface. |
| `startup_command` errors | Verify the binary path and that `model_path` is under `BROKER_ALLOWED_MODEL_ROOTS`. |
| Process backend never becomes ready | Raise the `readiness.timeout_seconds`; check the runtime's own logs. |

## Example

[examples/ollama-quickstart](../../examples/ollama-quickstart/) — runs offline,
then shows exactly what to change for a real Ollama host.

## Reference & decisions

- Reference: [model registry](../model-registry.md) · [configuration](../configuration.md) ·
  [backend adapter contract](../backend-adapter.md)
- ADRs: [0002 architecture](../adr/0002-architecture-review.md) ·
  [0006 OpenAI endpoint surface](../adr/0006-openai-endpoint-surface.md)
