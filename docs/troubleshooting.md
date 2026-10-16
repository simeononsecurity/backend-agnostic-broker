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
| `502 backend_unavailable` | `backend_unavailable` | Runtime returned 4xx/5xx or is unreachable | Check the runtime process/endpoint; verify `base_url` and readiness path. |
| `504 timeout_error` | `timeout_error` | Request exceeded `request_timeout_seconds` | Raise the per-model/global timeout or reduce load. |
| `429 rate_limit_error` | `rate_limit_error` | Concurrency/loaded-model cap hit | Raise `BROKER_MAX_LOADED_MODELS`/concurrency, or retry; check for a model stuck busy. |
| `413 request_too_large` | `request_too_large` | Body over `BROKER_MAX_REQUEST_BYTES` | Reduce payload or raise the limit. |
| Ollama: "ollama backend is external; remove startup_command" | `config_error` | `startup_command` set for Ollama | Remove it; Ollama manages its own process (set only `base_url`). |

## Diagnostics

- `GET /status` shows each model's state, pid, inflight, idle seconds, and
  `last_error`.
- `GET /metrics` shows request/latency/in-flight/loaded gauges.
- Logs: set `BROKER_LOG_LEVEL=DEBUG` for verbose output.
- Reproduce the full local check suite via `docs/EVIDENCE.md`.
