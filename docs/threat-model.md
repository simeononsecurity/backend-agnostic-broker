# Threat Model

Repository-grounded threat model for the broker. Mitigations reference actual
code in `broker/`.

## Assets

- Host integrity (the broker launches local runtime processes).
- Model files and the local filesystem.
- Internal/adjacent network services (the broker makes outbound HTTP calls).
- API keys and operator configuration.
- Availability of the broker and its backends.

## Trust boundaries

1. **Untrusted API client → broker** (the HTTP request body, headers, model
   selector).
2. **Broker → runtime child process** (argv, model path, executable).
3. **Broker → upstream runtime HTTP endpoint** (outbound URL).
4. **Operator/config → broker** (trusted, but validated fail-fast).

## Actors

- Untrusted/authenticated API client (may send hostile payloads).
- Untrusted upstream content (treated as data).
- Host operator (trusted; supplies config/registry).

## Threats and mitigations (STRIDE-oriented)

| Threat | Vector | Mitigation (code) |
|--------|--------|-------------------|
| **Command/shell injection** | Request data reaching a child process | No shell anywhere; argv lists via `security.build_argv`; `ProcessSupervisor` runs the list directly. |
| **Arbitrary executable selection** | Config/request pointing at an attacker binary | `validate_executable`: absolute, existing, executable, allow-listed dir only. |
| **Path traversal / symlink escape** | `model_path` with `..`/symlinks | `validate_model_path`: real-path resolve + confinement under `BROKER_ALLOWED_MODEL_ROOTS`. |
| **CLI-flag injection** | `model`/fields smuggling `--flag` tokens | `reject_request_flags` on request-derived values; `build_argv` whitelists placeholders. |
| **SSRF** | Tricking the broker into calling internal/metadata endpoints | `validate_upstream_url`: scheme + base-URL allow-list + DNS resolution check blocking link-local/multicast/reserved/metadata; loopback toggle. |
| **Silent model substitution** | Unknown alias served by a different model | `ModelNotFoundError` (404); no fallback path exists. |
| **Denial of service (size)** | Oversized request bodies | `BROKER_MAX_REQUEST_BYTES` → 413 (content-length fast path + post-read check). |
| **Denial of service (load)** | Flooding concurrent/loaded models | Global + per-model semaphores; `max_loaded_models` with LRU eviction; `reject` policy → 429. |
| **Resource exhaustion (stuck model)** | A runtime that never becomes ready or hangs | Startup + request timeouts; failure path transitions to `failed` and tears down the process; idle sweep unloads idle models. |
| **Secret leakage** | Keys in repo/logs | `.env` git-ignored; constant-time key compare; keys never logged; CI secret scan. |
| **Privilege escalation in container** | Exploit → host | Non-root user, read-only rootfs, dropped capabilities, `no-new-privileges`, minimal image. |
| **Info disclosure via errors** | Leaking internals | Structured errors expose only backend/model/state/remediation, not stack traces or secrets. |

## Residual risks and assumptions

- The operator is trusted to supply a safe registry, allow-lists, and
  executables. Mis-scoped `BROKER_ALLOWED_EXEC_DIRS`/`BROKER_ALLOWED_MODEL_ROOTS`
  weaken boundaries 2.
- Backend runtimes themselves are outside the broker's control; the broker
  confines *how* they are launched and reached, not their internal safety.
- Unfixed base-OS CVEs may exist between base-image updates (see container-scan
  policy). Rebuild regularly.
- Loopback upstreams are permitted by default; disable with
  `BROKER_ALLOW_LOOPBACK_BACKENDS=false` in hardened deployments where runtimes
  are not co-located.
- The broker authenticates but does not itself rate-limit per-key beyond the
  global/per-model concurrency caps; front it with a gateway if per-tenant
  quotas are required.
