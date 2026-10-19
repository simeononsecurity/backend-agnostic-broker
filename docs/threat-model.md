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
| **Unrestricted resource/cost consumption (per key)** | One key flooding requests/tokens or running up cost (OWASP API4:2023) | Per-key `rpm`/`tpm`/`max_concurrency` + daily/monthly token & cost budgets enforced pre-dispatch on the `StateStore`; `429 rate_limit_exceeded` / `insufficient_quota` with `Retry-After` (ADR 0008). Governance v2 (ADR 0016) adds selectable atomic **GCRA** rpm + burst and a **sliding-window** tpm counter, **per-model** rpm/tpm (enforced on the resolved model incl. auto + cascade hops), and **tenant** aggregate token/cost budgets across all keys of a tenant. A governance store error **fails closed** (request denied). |
| **Model-scope / placement bypass via `auto`** | Using `auto` to reach a disallowed model or a non-compliant placement | `allowed_models` and per-key `placement` (ADR 0016; unioned fail-closed → `403 placement_denied`) are enforced on the resolved model and every cascade hop, not just the requested alias → `403 model_not_allowed` / `placement_denied`. |
| **Key material theft at rest** | Leaked repo/DB/backup exposing keys | Keys stored as SHA-256 **hash** only (never plaintext); retrievable/encrypted keys **rejected** (ADR 0016); `keys.yaml` git-ignored; secret shown once at creation and once in the admin create/rotate response, never logged; rotation (`broker.keys rotate` / `/admin/keys/{id}/rotate`) with a grace overlap for clean cutover (`broker/keys.py`). |
| **Key enumeration / timing oracle** | Probing which keys are valid | Constant-time `hmac.compare_digest` (+ dummy compare for unknown ids); identical generic `401` for unknown/revoked/expired/wrong-secret keys. |
| **Model-scope bypass via `auto`** | Using the `auto` alias to reach a disallowed model | `allowed_models` is fail-closed and checked on the model the router selects, not just the requested alias → `403 model_not_allowed`. |
| **Resource exhaustion (stuck model)** | A runtime that never becomes ready or hangs | Startup + request timeouts; failure path transitions to `failed` and tears down the process; idle sweep unloads idle models. |
| **Secret leakage** | Keys in repo/logs | `.env` git-ignored; constant-time key compare; keys never logged; CI secret scan. |
| **Privilege escalation in container** | Exploit → host | Non-root user, read-only rootfs, dropped capabilities, `no-new-privileges`, minimal image. |
| **Info disclosure via errors** | Leaking internals | Structured errors expose only backend/model/state/remediation, not stack traces or secrets. |
| **XSS / key exfiltration (web UI)** | Injected script in the `/ui` page stealing the operator's key or calling out | UI is opt-in/default-off; strict CSP `default-src 'self'` with no `unsafe-inline`/`unsafe-eval` and no external origin (`connect-src 'self'` blocks third-party exfil); no inline script/style; key held in tab memory only, never persisted (ADR 0015). |
| **Clickjacking (web UI)** | Framing `/ui` to trick an operator into admin actions | `frame-ancestors 'none'` + `X-Frame-Options: DENY` on every `/ui` response. |
| **Static-asset path traversal (web UI)** | `/ui/../` reading package files | Resolve + confine under the packaged UI dir; extension allow-list; `..`/unknown → 404 (ADR 0015). |
| **UI widening the API surface** | A new unauthenticated endpoint to feed the UI | None added: the UI drives the existing authenticated API; enabling it registers only static `/ui` routes (tested). |

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
