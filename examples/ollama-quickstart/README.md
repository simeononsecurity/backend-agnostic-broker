# Ollama quickstart

Connect the broker to an **Ollama** daemon — the fastest path to a real local
model. The example ships an offline `echo-fake` model so it runs (and its test
passes) with no daemon, then shows exactly what to change for a real Ollama host.

The host address stays **out of git**: you set `OLLAMA_BASE_URL` in a gitignored
`.env`, and the registry references it as `${OLLAMA_BASE_URL}`.

## Files

- `registry.example.yaml` — a fake model (active) + a commented real Ollama entry.
- `.env.example` — copy to `.env`; offline by default.
- `curl.sh` / `openai_sdk.py` — the same requests via curl and the OpenAI SDK.
- `docker-compose.yml` — the broker + an Ollama daemon on one Compose network.

## Run it (offline, no daemon)

```bash
set -a && . ./examples/ollama-quickstart/.env.example && set +a
python -m broker
# in another shell:
bash examples/ollama-quickstart/curl.sh
```

Expected: `/healthz` returns `{"status":"ok"}`, `/v1/models` lists `echo-fake`,
and the chat call returns a `chat.completion` whose content is `echo: echo-fake`.

## Switch to a real Ollama model

1. Uncomment the `llama3` entry in `registry.example.yaml` (copy to `registry.yaml`).
2. In `.env`, set `OLLAMA_BASE_URL=http://<OLLAMA_HOST>:11434` and
   `BROKER_ENABLED_BACKENDS=["fake","ollama"]`.
3. Make sure the model is pulled on the daemon: `ollama pull llama3.1:8b`.
4. Call it with `"model":"llama3"`.

## With Docker Compose

```bash
cd examples/ollama-quickstart
docker compose up -d
docker compose exec ollama ollama pull llama3.1:8b
curl -s -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"model":"llama3","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
```

The broker reaches the daemon by its service name (`http://ollama:11434`), so no
host IP is committed.

## See also

- Guide: [backends](../../docs/guides/backends.md)
- Reference: [configuration](../../docs/configuration.md) ·
  [model registry](../../docs/model-registry.md) ·
  [Ollama backend](../../docs/backends/ollama.md)

Tested by `tests/test_examples.py` (config loads; the documented chat runs
against the fake backend).
