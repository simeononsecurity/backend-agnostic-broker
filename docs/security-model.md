# Security Model

The broker centralizes every trust boundary in `broker/security.py` and
enforces them before any side effect. All functions raise `SecurityError` or
`ValidationError` on failure so callers never proceed with an unsafe value.

## Trust boundaries

### 1. Executable allow-listing — `validate_executable`

A runtime binary is accepted only if it is: non-empty, **absolute**, resolves to
an existing **regular file**, is **executable** (`os.X_OK`), and sits inside one
of `BROKER_ALLOWED_EXEC_DIRS` (or equals an allow-listed file). Requests can
never choose an arbitrary executable — the executable comes from configuration,
not from the request.

### 2. Model-path validation — `validate_model_path`

A model path is resolved to its real path (following symlinks) and must be
located **under** one of `BROKER_ALLOWED_MODEL_ROOTS`, and must exist. This
defeats `..` path traversal and symlink escapes. If no roots are configured, the
call fails closed.

### 3. Request-flag rejection — `reject_request_flags`

Any request-derived value that could reach a child process (e.g. the `model`
selector) is scanned and rejected if it contains CLI-flag-looking tokens
(`-x`/`--flag`) or shell metacharacters/NUL (`\x00 \n \r ; & | ` $ > <`). Lists
are checked element-wise. The API layer calls this on `model` before acquiring.

### 4. No-shell argv construction — `build_argv`

Child processes are **always** launched from an argv **list**, never a shell
string. `build_argv` substitutes only whole-token `{name}` placeholders from a
whitelisted substitution map; unknown placeholders raise, and substituted values
are re-checked for unsafe characters. The result is never joined into a shell
string, and `ProcessSupervisor` (`broker/proc.py`) executes the list directly.

### 5. SSRF guard — `validate_upstream_url`

Every upstream URL must: use `http`/`https`; **start with one of the configured,
trusted backend base URLs** (registry `base_url`s plus
`BROKER_EXTRA_ALLOWED_BACKEND_URLS`); and resolve (via `getaddrinfo`) to
addresses that are not link-local, multicast, reserved, unspecified, or the
cloud-metadata address `169.254.169.254`. Loopback is allowed by default (local
runtimes bind `127.0.0.1`) and can be disabled with
`BROKER_ALLOW_LOOPBACK_BACKENDS=false`.

## Authentication

`broker/auth.py`: when `BROKER_AUTH_ENABLED=true`, protected routes require a key
via `Authorization: Bearer <key>` or `X-API-Key: <key>`, compared in constant
time (`hmac.compare_digest`) against `BROKER_API_KEYS`. `/healthz`, `/readyz`,
and `/metrics` are unauthenticated. Keys come from config/secrets and are never
logged or committed.

## Virtual keys & per-key governance (ADR 0008)

`broker/keys.py` + `broker/governance.py` add opt-in per-key governance:

- **Hash-at-rest.** Virtual keys are stored as a **SHA-256 hash** of the secret
  (never the secret). The secret is shown exactly once by `python -m broker.keys
  create`; `keys.yaml` is git-ignored (only `keys.example.yaml`, with fake values,
  is tracked). SHA-256 is the correct primitive for high-entropy secrets (bcrypt/
  Argon2 are for passwords).
- **Constant-time, no enumeration.** A non-secret id is embedded in the key for
  O(1) lookup; the full secret is verified with `hmac.compare_digest` against the
  stored hash (with a dummy compare when the id is unknown, to avoid a timing
  oracle). Unknown, revoked, expired, and wrong-secret keys all fail with the
  **same** generic `401`.
- **Resource limits (OWASP API4:2023).** Before dispatch, the governor enforces
  per-key `rpm`/`tpm`, `max_concurrency`, daily/monthly token and cost budgets,
  and a fail-closed `allowed_models` list (applied to the `auto`-selected model
  too). Counters live on the `StateStore` (in-process or Redis).
- **No key material in logs/metrics/errors.** Only the non-secret key id reaches
  the usage log; errors carry limits/remediation, never secrets. Prometheus labels
  never include a key.
- **Backward compatible.** `BROKER_API_KEYS` keep working as unlimited user keys;
  with no keys file and no limits, behavior is identical to v0.4.0.

## No silent fallback

An unknown model alias raises `ModelNotFoundError` (404). The broker never
substitutes a different model.

## Placement / data-residency pinning (fail-closed)

Per-model `placement` labels in the registry (e.g. `local`, `trusted`,
`region:eu`) express where a model may process data. A request requirement comes
from the `x-broker-placement` header **unioned with** the `BROKER_DEFAULT_PLACEMENT`
baseline, and is enforced **fail-closed** (ADR 0011):

- Only models/pool members carrying **every** required label are eligible — for
  the `auto` router (candidate pre-filter), for an explicitly named model, and for
  pool members (non-compliant members are dropped).
- If nothing qualifies, the broker returns a structured `403 placement_denied` and
  **never** spills to a non-compliant model (matching the no-silent-fallback
  posture above).
- The requirement is set **server-side** — the header is intended to be stamped by
  a trusted caller/gateway, and it can only *narrow* eligibility, never widen it
  below the configured baseline. Labels are validated (`[A-Za-z0-9][A-Za-z0-9._:-]*`);
  a malformed label is rejected (`400`) before any routing.

This makes residency/sovereignty an auditable hard wall rather than a best-effort
preference.

## Container hardening

The image (`Dockerfile`) and Compose files apply defense in depth:

- Multi-stage build; minimal `python:3.11-slim` runtime.
- Runs as a non-root user (uid/gid `10001`); `pip`/`setuptools`/`wheel` removed
  from the runtime layer to shrink the attack surface.
- Compose runs the container `--read-only` with explicit `tmpfs` for
  `/tmp/broker` and `/var/cache/broker`, `--cap-drop ALL`, and
  `--security-opt no-new-privileges:true`.
- `HEALTHCHECK` calls the broker's own `/healthz` (no shell tricks).
- GPU passthrough is opt-in only via `docker-compose.gpu.yml`.

## Secrets handling

No secrets are committed. `.env` is git-ignored; `.env.example` holds
placeholders only. Provide `BROKER_API_KEYS` via Docker secrets or env injection.
CI runs a secret scan (gitleaks) on every PR.

## Container-scan policy

CI scans the image with Trivy for `CRITICAL,HIGH` and fails only on **fixable**
findings (`ignore-unfixed: true`). Unfixed base-OS CVEs are tracked and cleared
by rebuilding against updated base images. See `docs/EVIDENCE.md` gate 19.
