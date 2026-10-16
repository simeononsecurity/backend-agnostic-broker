# Model Cache

## Confirmed broker behavior

- `BROKER_CACHE_DIR` (default `/var/cache/broker`) and `BROKER_TMP_DIR`
  (default `/tmp/broker`) are the broker's writable directories. In Docker they
  are created and owned by the non-root `broker` user and mounted as `tmpfs`
  (Compose) so the container root fs can stay read-only.
- Model **weights** live outside these dirs: broker-managed runtimes read from
  `model_path` under `BROKER_ALLOWED_MODEL_ROOTS` (default `/models`), typically
  mounted read-only (`models:/models:ro` in Compose).
- **Retention / unload.** A loaded model stays resident until it is idle beyond
  its timeout, then the idle sweep unloads it (broker-managed: process stop;
  Ollama: native `keep_alive: 0`). Re-selecting the model reloads it.

## Interaction with idle unload and keep_alive

| Backend | Model residency | Unload trigger |
|---------|-----------------|----------------|
| vLLM / llama.cpp / Strata | Broker child process holds the model | Broker idle sweep stops the process after `idle_timeout_seconds` |
| Ollama | Ollama daemon holds the model | Broker idle sweep calls native unload; Ollama also unloads after `keep_alive` |

## Operational notes

- Mount model roots read-only and keep them on fast storage; first-request load
  time dominates latency for broker-managed backends.
- Size `tmpfs` appropriately if a runtime writes scratch data under the cache/tmp
  dirs.
- Backend runtimes may maintain their own caches (e.g. Hugging Face cache for
  vLLM, Ollama's model store) outside the broker; configure those on the runtime
  side.
