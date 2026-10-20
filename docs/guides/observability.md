# How-to: observability (tracing, usage log, /status, metrics)

See what the broker is doing through four complementary surfaces: **OpenTelemetry
tracing**, a **usage log**, the live **`/status`** snapshot, and Prometheus
**`/metrics`**.

## When to use which

| Surface | Answers | Default |
|---|---|---|
| `/status` | "what is the broker doing right now?" (models, routing, cooldowns, caches) | on (auth) |
| `/metrics` | "give me time-series for dashboards/alerts" (Prometheus) | on |
| Usage log | "who called what, when, how many tokens/cost?" (per key/tenant) | on |
| OTel tracing | "trace a single request across its spans" | off (opt-in) |

## OpenTelemetry tracing

Each request emits a `gen_ai` span with model, operation, and token counts —
**metadata only**. Enable it and point the standard OTLP exporter at your collector:

```bash
pip install -e '.[otel]'                 # the otel extra (included in the image)
BROKER_TRACING_ENABLED=true
BROKER_TRACING_SERVICE_NAME=backend-agnostic-broker
# WHERE spans go is the standard OTel contract, not a BROKER_* var:
OTEL_EXPORTER_OTLP_ENDPOINT=http://<COLLECTOR_HOST>:4318
OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
```

Prompt/response **content is never captured** unless you explicitly opt in with
`BROKER_TRACING_CAPTURE_CONTENT=true` (and `BROKER_TRACING_CONTENT_MAX_CHARS`).

## Usage log

```bash
BROKER_USAGE_LOG_ENABLED=true
BROKER_USAGE_LOG_PATH=/var/lib/broker/usage.db   # omit for in-memory; ":memory:" for tests
```

Records key/tenant, placement, namespace, resolved model, endpoint, and token
counts per request; `/usage` exposes a scoped view.

## Metrics

`/metrics` serves Prometheus text (`BROKER_METRICS_ENABLED=true`, default on). Scrape
it for request counts, loaded-model gauges, latencies, and cache hit rates.

## Verify

```bash
curl -s -H "Authorization: Bearer YOUR_API_KEY" http://127.0.0.1:8080/status | python3 -m json.tool
curl -s http://127.0.0.1:8080/metrics | head
curl -s -H "Authorization: Bearer YOUR_API_KEY" http://127.0.0.1:8080/usage | python3 -m json.tool
# with tracing on, send a chat then find service "backend-agnostic-broker" in your tracing UI
```

Expected: `/status` shows `tracing_enabled: true`; `/metrics` returns Prometheus
text; one span per request appears in your collector/Jaeger.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Tracing enabled but errors on boot | Install the `otel` extra; a missing SDK raises a `ConfigError` by design. |
| No spans in the backend | Check `OTEL_EXPORTER_OTLP_ENDPOINT`/protocol; the broker configures nothing else. |
| `/metrics` is 404 | `BROKER_METRICS_ENABLED=false` — re-enable it. |
| Content appears in spans unexpectedly | Only if `BROKER_TRACING_CAPTURE_CONTENT=true`; unset it. |

## Example

[examples/otel](../../examples/otel/) — broker → OpenTelemetry Collector → Jaeger.

## Reference & decisions

- Reference: [configuration](../configuration.md) · [operations runbook](../operations-runbook.md) ·
  [security model](../security-model.md)
- ADRs: [0007 usage accounting store](../adr/0007-usage-accounting-store.md) ·
  [0012 caching, runtime control & observability](../adr/0012-caching-runtime-control-observability.md)
