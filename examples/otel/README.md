# OpenTelemetry tracing

Export the broker's request spans over **OTLP** to an **OpenTelemetry Collector**,
then view them in **Jaeger** (ADR 0012). Every request emits a span with
`gen_ai.*` attributes (model, operation, token counts) — **metadata only**; prompt
and response content are never captured unless you opt in with
`BROKER_TRACING_CAPTURE_CONTENT=true`.

Tracing is **off by default** and requires the `otel` extra
(`pip install -e '.[otel]'`); the lean Docker image includes it.

## Files

- `docker-compose.yml` — broker → otel-collector → Jaeger.
- `otel-collector-config.yaml` — OTLP receiver, batch processor, Jaeger exporter.
- `registry.example.yaml` — one offline fake model.
- `.env.example` — tracing settings, including the standard `OTEL_*` exporter vars.

## Key point: the exporter is configured with `OTEL_*`, not `BROKER_*`

The broker only decides **whether** to trace (`BROKER_TRACING_ENABLED`) and the
**service name** (`BROKER_TRACING_SERVICE_NAME`). **Where** spans go is the
standard OpenTelemetry SDK contract — `OTEL_EXPORTER_OTLP_ENDPOINT`,
`OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_EXPORTER_OTLP_HEADERS`, etc. This means any
OTLP-compatible backend (Jaeger, Tempo, Honeycomb, Datadog) works with no broker
code change.

## Run it

```bash
cd examples/otel
docker compose up -d
curl -s -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{"model":"echo-fake","messages":[{"role":"user","content":"hi"}]}' \
  http://127.0.0.1:8080/v1/chat/completions
# open the Jaeger UI and find service "backend-agnostic-broker":
#   http://127.0.0.1:16686
```

You should see one trace per request with a `gen_ai` span carrying the model,
operation, and token counts — and no message content.

## Run locally without Docker

```bash
pip install -e '.[otel]'
set -a && . ./examples/otel/.env.example && set +a   # point OTLP at your collector
python -m broker
```

## See also

- Guide: [observability](../../docs/guides/observability.md)
- Reference: [configuration](../../docs/configuration.md) ·
  [security model](../../docs/security-model.md)
- ADR: [0012 caching, runtime control & observability](../../docs/adr/0012-caching-runtime-control-observability.md)

Tested by `tests/test_examples.py`: the env maps to real Settings, and with the
`otel` extra installed the broker boots with tracing enabled and serves a traced
request (skipped if the extra is absent).
