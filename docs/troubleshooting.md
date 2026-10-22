# Troubleshooting

Errors are structured (`broker/errors.py`) and carry a `remediation` hint and a
`request_id`. Common cases:

| Symptom | Error type / status | Cause | Fix |
|---------|---------------------|-------|-----|
| Startup aborts: "auth is enabled but no API keys" | `config_error` (500) | `BROKER_AUTH_ENABLED=true` with empty `BROKER_API_KEYS` | Set `BROKER_API_KEYS=["..."]`, or `BROKER_AUTH_ENABLED=false` for local dev only. |
| Startup aborts: "registry not found" | `config_error` (500) | `BROKER_REGISTRY_PATH` missing | Point it at a real registry file (copy `registry.example.yaml`). |
| `401` on every call | `authentication_error` | Missing/invalid key | Send `Authorization: Bearer <key>` or `X-API-Key`. `/healthz`, `/readyz`, `/metrics` need no auth. |
| `404 unknown model alias` | `model_not_found` | Alias not in registry (no silent fallback) | Use an alias from `GET /v1/models`. |
| `400 ... flag-like tokens` / `unsafe characters` | `security_error` | Request `model` contains `--flag`/shell metacharacters | Remove them; model selectors are plain aliases. |
| Startup fails: "executable ... not in the allow-list" | `security_error`/`config_error` | Runtime binary outside `BROKER_ALLOWED_EXEC_DIRS` | Add its dir to `BROKER_ALLOWED_EXEC_DIRS` or move the binary. |
| Startup fails: "model path ... escapes the allowed model roots" | `security_error` | `model_path` outside `BROKER_ALLOWED_MODEL_ROOTS` | Place the model under an allowed root or extend the list. |
| Startup fails: "model path does not exist" | `invalid_request_error` | Wrong path | Fix `model_path` / mount the model. |
| "upstream URL ... not in the backend allow-list" | `security_error` | Backend URL not derived from a registry `base_url` | Set the correct `base_url`; add trusted bases via `BROKER_EXTRA_ALLOWED_BACKEND_URLS`. |
| "resolves to blocked address" | `security_error` | Upstream host resolves to link-local/metadata | Use a real backend host; keep metadata endpoints unreachable. |
| `503 model_load_error` / "readiness probe timed out" | `model_load_error`/`timeout_error` | Runtime slow or failed to start | Raise `startup_timeout_seconds`/`readiness.timeout_seconds`; check runtime logs (broker includes recent child logs). |
| `502 backend_unavailable` | `backend_unavailable` | Runtime unreachable, returned 5xx, or sent a malformed/non-JSON body | Check the runtime process/endpoint; verify `base_url` and readiness. A runtime **4xx** is passed through as that 4xx with `error.type` `upstream_client_error` (ADR 0017), not 502. |
| `504 timeout_error` | `timeout_error` | Request exceeded `request_timeout_seconds` | Raise the per-model/global timeout or reduce load. |
| `429 rate_limit_error` | `rate_limit_error` | Concurrency/loaded-model cap hit | Raise `BROKER_MAX_LOADED_MODELS`/concurrency, or retry; check for a model stuck busy. |
| `413 request_too_large` | `request_too_large` | Body over `BROKER_MAX_REQUEST_BYTES` | Reduce payload or raise the limit. |
| Ollama: "ollama backend is external; remove startup_command" | `config_error` | `startup_command` set for Ollama | Remove it; Ollama manages its own process (set only `base_url`). |
| `auto`: `400 ... no registered model can satisfy this request` | `invalid_request_error` | No model passes the Stage-1 capability filter (e.g. an image request but no vision model) | Add a capable model, or send a concrete `model` id. |
| `auto` routes via heuristic though you expected the LLM | — (works, logged) | The GGUF classifier is unavailable: `BROKER_ROUTER_MODEL_PATH` missing, `llama_cpp` not importable, or a missing runtime lib | Check `GET /status.router.backend`; use the `-llm` image (bundles the model + `libgomp1`) or set `BROKER_ROUTER_MODEL_PATH`; see logs for the fallback reason. |
| `auto` picks a sub-optimal model | — (valid but imperfect) | The bundled 0.5B classifier is accuracy-limited on small catalogs | Refine per-model `tags`/`priority`/`cost_tier` or add a `router.overrides` rule; use `router_backend=heuristic`, or a larger GGUF via `ROUTER_MODEL_URL`. |
| `-llm` image: `libgomp.so.1: cannot open shared object file` | runtime import error | OpenMP runtime missing (only if you built a custom image) | The provided Dockerfile installs `libgomp1` for the llm variant; ensure it's present. |
| Startup aborts: "BROKER_REDIS_URL is set but the 'redis' package is not installed" | `config_error` (500) | Redis shared state enabled without the optional dep | `pip install 'backend-agnostic-broker[redis]'`, or unset `BROKER_REDIS_URL` to use in-process state. |
| A pool member stops receiving traffic | — (works, logged) | Cooldowns are on (`BROKER_COOLDOWN_FAILURES>0`) and the member hit the failure threshold | Check `GET /healthz/backends` → `cooldown.in_cooldown`/`seconds_remaining`; it half-opens automatically after `BROKER_COOLDOWN_SECONDS`. Fix the backend, or disable with `BROKER_COOLDOWN_FAILURES=0`. |
| Requests slower under load after enabling retries | — (expected) | Backoff sleeps between retry attempts | Lower `BROKER_RETRY_BACKOFF_BASE_MS`/`MAX_MS` or `BROKER_RETRY_MAX_ATTEMPTS`; retrying is bounded by `BROKER_REQUEST_TIMEOUT_SECONDS`. |
| A benched member still gets a little traffic | — (works, by design) | `BROKER_MAX_EJECTION_PERCENT>0` reinstated it because too much of the pool was benched (never-strand) | Expected under a correlated outage; `cooldown.in_cooldown=true` with traffic means the ejection cap kept it eligible. Fix the backend, or raise the cap. |
| A flapping member is benched for longer and longer | — (works, by design) | Exponential ejection growth (`BROKER_COOLDOWN_BACKOFF_MULTIPLIER>1`) | Watch `cooldown.ejections`/`seconds_remaining` in `GET /status`; it decays after `BROKER_COOLDOWN_DECAY_SECONDS` of success. Lower the multiplier or set `BROKER_COOLDOWN_MAX_SECONDS`. |
| A 429/4xx was not retried or didn't bench a member | — (by design) | 4xx client errors are never retried/counted; 429 backpressure is not retried unless opted in | For a 429, set `BROKER_RETRY_ON_BACKPRESSURE=true` (and `BROKER_COOLDOWN_ON_BACKPRESSURE=true`); 4xx is intentionally never retried. See per-error policy in configuration.md. |
| `cache_aware`: low prefix hit rate | — (works, suboptimal) | Prompts shorter than one block, little shared prefix, `block_chars` too large (coarse), or the index TTL/size too small so entries expire before reuse | Check `GET /status.cache_aware.hit_rate`/`index_size`; lower `BROKER_CACHE_AWARE_BLOCK_CHARS` for finer matching, raise `BROKER_CACHE_AWARE_TTL_SECONDS`/`MAX_ENTRIES`, and confirm the workload actually shares prefixes. Short prompts correctly fall back to `least_busy`. |
| `cache_aware`: one member hot-spots (uneven load) | — (works, imbalanced) | `BROKER_CACHE_AWARE_BETA` too low, so prefix affinity dominates the load term | Raise `BROKER_CACHE_AWARE_BETA` (e.g. default `0.5`→`1.0`) to spread harder; compare per-member `inflight` in `GET /status`; optionally enable `BROKER_CACHE_AWARE_USE_METRICS` for a real queue-depth signal. See the `beta` table in routing-and-health.md. A value below `0.5` (e.g. `0.25`/`0`) hot-spots under real load — that is the ADR 0018 finding. |
| I want the lowest tail latency (p95 TTFT) under load | — (strategy choice) | `cache_aware`/`sticky` trade tail latency for cache locality / session pinning | Use `BROKER_ROUTE_STRATEGY=least_busy` — in the simulated queueing bench it has the best p95 TTFT and highest SLO-capacity in every scenario. `cache_aware` is within ~10% of it but exists to save prefill work / lift hit rate, not to win the tail; `sticky` has the worst tail. See the when-to-use guide in routing-and-health.md. |
| Log at startup: "usage log init failed … disabling"; `/usage` stays empty | — (works, usage logging off) | The default usage DB lives under `BROKER_CACHE_DIR` (`/var/cache/broker`, meant for the container), which is not writable on a dev machine | Run locally with `BROKER_CACHE_DIR=./.cache` (the broker creates it) or set `BROKER_USAGE_LOG_PATH` to a writable file. |
| `503 model_load_error`: "port N is already in use before starting runtime" | `model_load_error` | An orphaned runtime (survivor of an unclean broker exit) or an unrelated process holds the runtime's port (ADR 0021) | Inspect `GET /status.reconciliation`; reconcile survivors with `BROKER_RUNTIME_RECONCILE=stop` (reap) or `adopt` (reuse), free the port, or set `BROKER_RUNTIME_PORT_PRECHECK=false` to skip the check. |
| After an unclean restart a model won't load / OOMs / collides | — (recoverable) | A runtime orphaned by the previous (killed/crashed) broker still holds VRAM + its port (ADR 0021) | `GET /status.reconciliation.last.orphaned` lists them; set `BROKER_RUNTIME_RECONCILE=adopt` to reuse or `stop` to reap on startup. See [restart-and-recovery](guides/restart-and-recovery.md). |
| An orphan is listed but not adopted (`adoptable: false`) | — (by design) | The registry argv drifted from the recorded runtime, or its port didn't answer readiness — the broker won't adopt a runtime it can't confirm matches the current registry | Use `BROKER_RUNTIME_RECONCILE=stop` to reap it and let the next request load fresh. |
| Startup fails: "another broker (pid N) already owns the runtime ledger under BROKER_CACHE_DIR …" | `config_error` | Two brokers share one `BROKER_CACHE_DIR` and this one runs `BROKER_RUNTIME_RECONCILE=adopt`/`stop`, which would act on the live sibling's runtimes — so it fails closed (ADR 0021) | Give each broker its own `BROKER_CACHE_DIR` (or host). If you only need observation, `report`/`off` start anyway but run without a ledger (see next row). |
| A second broker starts but `GET /status.reconciliation.disabled_reason` is set / `ledger_owned: false` | — (by design) | Another live broker owns the ledger for this `BROKER_CACHE_DIR`; in `report`/`off` the second instance runs ledger-less for v0.6.0 compatibility (ADR 0021) | Give each broker its own `BROKER_CACHE_DIR` to restore per-instance recording/reconciliation. |
| `503 device_busy` on load | `device_busy` | A device the model needs is held incompatibly and can't be freed: an `gpu.exclusive` model against any other holder, or any model against an `exclusive`/**external** holder that isn't an idle broker model (ADR 0026, leases on) | Wait for the holder to release (honor `Retry-After`), pin the model to another device, drop `gpu.exclusive`, or free the co-tenant. Inspect `GET /status.leases`. The broker never evicts/signals an external holder. |
| After a forced stop a co-tenant stays blocked on a device | — (recoverable) | A lease whose owning broker was killed still marks the device busy (ADR 0026, leases on) | Run `python -m broker cleanup` (stop mode reaps a live orphan runtime identity-gated then releases; `--report` only reports). It also runs at startup + in the sweeper. Wire it as systemd `ExecStopPost=`. |
| Lease stuck in `stuck` state | — (self-clearing) | The model was unloaded but its runtime process group hadn't fully released the device; the lease is kept `stuck` until the survivors exit | The sweeper clears it automatically once the memory frees. Inspect `GET /status.leases`; check for a surviving process group holding the device. |

## Diagnostics

- `GET /status` shows each model's state, pid, inflight, idle seconds, and
  `last_error`, plus `state_backend` (`in_process`/`redis`), `retry_max_attempts`,
  `cooldowns_enabled`, a `reliability` block (ejection cap, cooldown growth,
  half-open, Retry-After, Redis sticky bound — ADR 0017), and per-model `cooldown`
  (incl. `ejections`) when cooldowns are on. The
  `router` block shows the active backend (`llm`/`heuristic`), cache size, and
  recent `auto` decisions (`chosen`, `via`, `latency_ms`).
- `GET /healthz/backends` shows per-member liveness and `cooldown`
  (`failures`, `in_cooldown`, `seconds_remaining`, `ejections`).
- `GET /metrics` shows request/latency/in-flight/loaded gauges.
- `GET /status` → `resources` (ADR 0020/0022, when `BROKER_GPU_TELEMETRY` is on):
  measured per-device memory (`source: measured`), per-PID holders, declared
  admission budgets + recent decisions, and any `release_stuck`/`survived_kill`
  holders. Ollama memory appears under `resources.reported` (`source: reported`).
- Logs: set `BROKER_LOG_LEVEL=DEBUG` for verbose output.
- Reproduce the full local check suite via `docs/EVIDENCE.md`.

## GPU memory issues (ADR 0020 / 0022)

| Symptom | Cause / fix |
|---------|-------------|
| `503 release_stuck` on a load | The previous runtime's device memory hasn't come back yet (lazy reclaim, a zombie CUDA/ROCm context, a lingering child worker, or a container PID namespace hiding the holder). It **auto-clears** on the next load attempt or the periodic tick once the runtime's group PIDs are gone and free memory recovers — no restart needed. To force it, free the holder in `resources.release_stuck` (PID/device/MiB); or raise `BROKER_RELEASE_TIMEOUT_SECONDS`. The broker never resets a GPU or kills a foreign process. |
| `429 capacity_unavailable` | A sized model (`memory_mib`) does not fit. `reason=exceeds_device_capacity` means it is larger than the device can ever hold (nothing is evicted — pin to a bigger device or lower `memory_mib`); `reason=*_insufficient` means the device is full and the holder can't be evicted (raise the budget/headroom, pin elsewhere, or wait for it to go idle). The `reason`/`holder` fields name why. |
| `resources.sample.status: unavailable` | The driver tool isn't at an absolute/allow-listed path, timed out, or returned nothing. Run `python -m broker.resources_probe <provider>` and read the `reason`. |
| A model shows `survived_kill: true` | `stop()` could not confirm the process exited after SIGKILL. Check for a wedged runtime holding the device. |

## Required-GPU admission (ADR 0025)

| Symptom | Cause / fix |
|---------|-------------|
| `503 gpu_unavailable` on a load | A `gpu.required` model could not be placed on a GPU, and **nothing was spawned or evicted** (no CPU fallback). `code=empty_inventory`: telemetry is off and no inventory is declared — set `BROKER_GPU_DEVICES=0,1` (or `BROKER_CUDA_VISIBLE_DEVICES`), or turn on `BROKER_GPU_TELEMETRY`. `code=unknown_device_id`: a `gpu.device_ids` entry is not in the inventory — fix the ordinal or the inventory. In a pool the request fails over to a qualifying member first. Check `/status` → the model's `gpu` block for the inventory source. |
| `503 gpu_placement_failed` on a load | A `gpu.required` model loaded but did not actually hold a GPU, so it was **unloaded** (never served on CPU). Managed runtime (evidence `measured`): its process group held no memory on a required device within `BROKER_GPU_VERIFY_SECONDS` — check the runtime really used the GPU (driver, device env, `--n-gpu-layers`/`num_gpu`), or raise the timeout. Ollama (evidence `reported`): `/api/ps` reported `size_vram == 0` — the daemon loaded it on CPU (check `OLLAMA_*` GPU env on the daemon). |
| Startup `config_error`: "sets gpu.required … with no runtime the broker can verify" | `gpu.required: true` on an **external** OpenAI-compatible endpoint (no `startup_command`, non-Ollama) the broker cannot verify. Add `gpu.verify: declared` to assert placement out-of-band (shows as `declared` in `/status`), give the entry a broker-managed `startup_command`, or drop `gpu.required`. |

## Request-queue issues (ADR 0023)

| Symptom | Cause / fix |
|---------|-------------|
| `429 queue_full` | The admission queue hit `BROKER_QUEUE_MAX_DEPTH` (or `_PER_MODEL`). Too much sustained overload — add capacity, raise the depth, or let clients back off per `Retry-After`. The `trigger` names the overload. |
| `503 queue_timeout` | A request waited past `BROKER_QUEUE_MAX_WAIT_SECONDS`. The named `trigger` stayed active the whole wait — add capacity or raise the wait/threshold. Check `/status` → `queue.timeouts_by_reason`. |
| Requests never queue though the box looks busy | Trigger off or threshold not met. Concurrency + capacity are on by default; device-memory needs `BROKER_GPU_TELEMETRY` on; host/backend are opt-in with a threshold (`BROKER_QUEUE_HOST_LOAD_PER_CPU_MAX`, `BROKER_QUEUE_BACKEND_WAITING_MAX`). Confirm `BROKER_QUEUE_ENABLED=true` and see `/status` → `queue.waiting_triggers`. |
| A quota 429 isn't being queued | By design — per-key rpm/tpm/budget 429s (`rate_limit_exceeded`/`insufficient_quota`) are a caller limit, never queued. Only server overload (`queue_full`/`queue_timeout`) is. |
| `capacity_unavailable` with `reason=exceeds_device_capacity` never waits | By design — it can never fit even with everything evicted, so it fails fast instead of queueing. |
| Streaming request has no `x-broker-queue-wait-ms` | SSE headers flush before the pre-first-byte wait; a streaming request reports its wait in `/status` and traces, not a header. Keep client/proxy keep-alive above `BROKER_QUEUE_MAX_WAIT_SECONDS`. |
| One API key starves others | Shouldn't happen — fairness is FIFO-per-key + round-robin across keys. If an important key should go first, set `limits.priority` in the keys file (aging prevents starvation of the rest). |
