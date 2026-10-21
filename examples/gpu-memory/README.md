# Memory-aware model swapping (ADR 0020 + 0022)

A GPU-free, runnable demonstration of **resource telemetry**, **measured release**,
and **memory-aware admission**. It uses the in-process `fake` backend so it runs on
any machine (no GPU, no downloads), while the same settings apply verbatim to a
real `vllm` / `llamacpp` deployment.

What it shows:

- `BROKER_GPU_TELEMETRY=host` measures real system memory (swap to `nvidia` / `amd`
  on a GPU box) — see it under `/status` → `resources`.
- Two **sized** models (`memory_mib`) that cannot both fit the declared
  `BROKER_GPU_MEMORY_BUDGET_MIB`, so loading the second evicts the first with a
  measured-release wait.
- A `429 capacity_unavailable` when a sized model cannot fit and the holder is busy.

## Files

- `registry.yaml` — two fake models, each `memory_mib: 600`.
- `.env.example` — telemetry on (`host`), a `1000` MiB declared budget (so two
  600 MiB models can't coexist), and a release timeout.

## Try it

```bash
# Probe the telemetry provider directly (exit 0 = ok, 2 = unavailable):
python -m broker.resources_probe host

# Run the broker with this example's config:
export $(grep -v '^#' examples/gpu-memory/.env.example | xargs)
export BROKER_REGISTRY_PATH=examples/gpu-memory/registry.yaml
uvicorn broker.app:create_app --factory --port 8080
```

Then:

```bash
curl -s localhost:8080/status | jq '.resources'            # measured devices + admission
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"small-a","messages":[{"role":"user","content":"hi"}]}'
curl -s localhost:8080/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"small-b","messages":[{"role":"user","content":"hi"}]}'
curl -s localhost:8080/status | jq '.resources.admission.recent'  # fits / evicted / blocked
```

On a real GPU, set `BROKER_GPU_TELEMETRY=nvidia` (or `amd`), drop the declared
budget (measured free memory is used instead), and give each model its true
`memory_mib`. See [docs/guides/gpu-memory.md](../../docs/guides/gpu-memory.md).
