# Backend: Strata

> **Honesty note.** The broker does **not** assume Strata's exact server
> command, version, readiness path, unload mechanism, GPU placement,
> tensor/layer split, MTP, or idle behavior. These are deployment-specific and
> must be confirmed against your Strata version. Items not confirmed here are
> marked **UNVERIFIED** rather than invented, per the project's rule never to
> document unverified flags as facts.

## Broker-confirmed behavior (authoritative — from `broker/adapters/strata.py` + `httpbase.py`)

- **Broker-managed lifecycle.** Strata is treated as a broker-managed,
  OpenAI-compatible HTTP runtime. Because its native idle/lazy behavior is not
  independently confirmed here, the broker provides broker-managed lazy loading:
  start on first request, stop after `idle_timeout_seconds`.
- **No-shell start.** argv **list** from the registry `startup_command` template
  (`security.build_argv`), after `validate_executable` and `validate_model_path`.
  GPU devices via `CUDA_VISIBLE_DEVICES` from `gpu.device_ids`.
- **Readiness / health.** Configurable `ReadinessProbe` (HTTP or log marker);
  defaults to `GET /health` expect `200`.
- **Chat / streaming.** Default `chat_path` is `/v1/chat/completions`
  (OpenAI-compatible); `model` pinned to `runtime_model_id`.
- **Unload / idle.** `unload` stops the process; idle sweep enforces the timeout.

## Registry example (placeholder — fill from your confirmed Strata version)

```yaml
- alias: strata-model
  backend: strata
  runtime_model_id: strata-default
  model_path: /models/strata-default
  base_url: http://127.0.0.1:8090
  startup_command:
    - /opt/strata/bin/strata-server   # UNVERIFIED: use your real binary
    - --model-path                    # UNVERIFIED: use your real flags
    - "{model_path}"
    - --port
    - "8090"
  readiness: { kind: http, path: /health, expect_status: 200, timeout_seconds: 300 }
  gpu: { required: true, device_ids: [0] }
  mtp: { enabled: false }
```

## Runtime reference (UNVERIFIED — confirm with your Strata docs/version)

Record the following from your confirmed Strata release before production, and
replace the placeholders above:

- Exact server command and required version.
- Model loading and first-request startup behavior.
- Readiness detection (HTTP path/status or log marker).
- Unload and idle-shutdown behavior (if Strata has native idle shutdown, use a
  long `idle_timeout_seconds` and document it; otherwise rely on broker-managed
  stop).
- GPU placement, tensor/layer split controls, MTP configuration, expert caching,
  and failure recovery.

If a control is not expressible as a registry `startup_command` placeholder, do
not fabricate it — extend the adapter deliberately and add a test.

## Known limitations

- Runtime specifics are UNVERIFIED here; the broker only guarantees the
  lifecycle, security, and proxying behavior described above.

## Evidence sources

- `broker/adapters/strata.py`, `broker/adapters/httpbase.py`, `registry.example.yaml`.
- Strata vendor documentation for your installed version (fill in).
