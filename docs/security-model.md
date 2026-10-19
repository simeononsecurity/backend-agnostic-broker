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

## No silent fallback

An unknown model alias raises `ModelNotFoundError` (404). The broker never
substitutes a different model.

## Admin control plane (ADR 0012)

The runtime-control surface (`GET /admin/models`, model unload/load, registry
reload) is **off by default** (`BROKER_ADMIN_ENABLED=false`) and, when enabled, is
gated by a **dedicated** key list (`BROKER_ADMIN_API_KEYS`) checked by
`require_admin`/`check_admin` in `broker/auth.py` — deliberately separate from the
data-plane `BROKER_API_KEYS`, so a leaked user key cannot unload models or reload
the registry. Admin keys are compared in constant time and never logged (actions
are audited by key *fingerprint* only). This single gate is the swap point for
ADR 0008's virtual-key admin scope. The admin surface cannot introduce a new
backend, executable, or path: registry reload re-runs the same executable/
model-path allow-list validation as startup, and an invalid registry makes no
change.

## Response-cache isolation (ADR 0012)

The exact response cache (off by default) keys every entry on the caller's
API-key/tenant scope as the **first** hashed component, so a cached answer can
never be served across a key or tenant boundary. Only successful, non-streaming,
deterministic-by-default responses are stored; errors and partial streams are
never cached. The cache stores response bodies in the StateStore (in-process or
Redis) — treat that store with the same confidentiality as the responses
themselves, and scope `BROKER_REDIS_PREFIX` per deployment when sharing one Redis.

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
