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

### Governance v2 (ADR 0016)

ADR 0016 extends the above, preserving every property (hash-only at rest,
constant-time byte compare, no key material in logs/metrics/errors):

- **Selectable algorithms.** rpm can use an atomic **GCRA** token-bucket (one
  event-loop step in-process, one Lua round-trip on Redis — no GET-then-SET race)
  with per-key burst; tpm can use a weighted **sliding-window counter**. Both are
  opt-in; the exact sliding-log / fixed window remain the defaults.
- **Per-model + tenant limits.** `limits.per_model` is enforced on the **resolved**
  model (auto pick + every cascade hop), so a model cap cannot be bypassed via
  `auto` or an escalation; a `tenants:` block caps aggregate token/cost across all
  keys of a tenant.
- **Per-key placement.** `limits.placement` is **unioned** into the request
  requirement (can only tighten, never loosen it), enforced fail-closed by the same
  machinery below (`403 placement_denied`).
- **Fail-closed on store error.** A `StateStore` exception during admission
  propagates (request denied) rather than silently removing the consumption cap —
  availability is sacrificed for the guard.
- **Rotation, not retrieval.** Retrievable/decryptable keys are **rejected** — a
  leaked `keys.yaml` yields only hashes. Rotation (`broker.keys rotate` / admin API)
  mints a successor with a grace overlap; the secret is still shown once. Rotation
  can only ever **shorten** a key's life (`expiry = min(existing, today + grace)`)
  and **refuses to resurrect** a revoked or expired key (`409 key_not_active`), so
  the kill switch stays a kill switch.
- **Admin key API.** `/admin/keys` (list/create/rotate/revoke) is admin-scope only
  and never logs or re-shows a secret; the create/rotate response is the sole
  one-time secret egress. All create/rotate inputs are validated before any write
  (`grace_days` integer `0..365`; ISO non-past `expiry`; charset/length-capped
  `tenant`/`label`), so a malformed admin request is a `400`, never a stored
  dead key or a `500`. Oversized bodies hit the global `413` limit.
- **Key file at rest.** `keys.yaml` (hashes only) is written **atomically** (temp
  file + `fsync` + `os.replace`) under an exclusive `flock` on a `keys.yaml.lock`
  sidecar, and created `0600` with any stricter existing mode preserved — a reader
  never sees a half-written file and the secret hashes aren't world-readable even
  when several admin calls / CLI runs / replicas write the shared file at once. A
  reload that hits a corrupt file keeps the last good key set and logs an error
  (fail closed for new keys; auth is never dropped wholesale at runtime).

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

## Admin control plane (ADR 0012, admin scope per ADR 0008)

The runtime-control surface (`GET /admin/models`, model unload/load, registry
reload) is **off by default** (`BROKER_ADMIN_ENABLED=false`) and, when enabled, is
authorized by `require_admin` in `broker/app.py` by **either** an ADR 0008 virtual
key whose scope is `admin` **or** a dedicated `BROKER_ADMIN_API_KEYS` value
(back-compat) — both deliberately separate from an ordinary data-plane user key, so
a leaked user key cannot unload models or reload the registry. Every credential is
compared **byte-wise** in constant time and never logged (actions are audited by key
*fingerprint* only); a non-ASCII/over-long key fails closed with `401`, and the
admin surface stays locked even when data-plane auth is disabled. The admin surface
cannot introduce a new backend, executable, or path: registry reload re-runs the
same executable/model-path allow-list validation as startup, and an invalid registry
makes no change.

## Response-cache isolation (ADR 0012)

The exact response cache (off by default) keys every entry on the caller's
API-key/tenant scope (and placement requirement) as the **first** hashed component,
so a cached answer can never be served across a key, tenant, or placement boundary.
Only successful, non-streaming, deterministic-by-default responses are stored;
errors and partial streams are never cached. The cache stores response bodies in the
StateStore (in-process or Redis) — treat that store with the same confidentiality as
the responses themselves, and scope `BROKER_REDIS_PREFIX` per deployment when
sharing one Redis.

## Semantic-cache isolation + false-positive risk (ADR 0014)

The semantic cache (off by default) inherits the same scope-first isolation: its
bucket key hashes the caller's key/tenant + placement + namespace scope **and**
the resolved model, endpoint, every non-prompt parameter, **the system prompt, and
the full conversation history**, so only the *last user turn's text* is matched
fuzzily and a semantic hit can **never** cross a key, tenant, placement, model,
parameter, system-prompt, or history boundary. (An earlier revision embedded the
whole message list, so a shared long history could push similarity over the
threshold and leak an answer across differing system prompts/histories — now
fixed: see ADR 0014 Confirmation.) The residual risk is a *false positive* — two
different last-turn prompts within one identical scope+context whose embeddings are
close enough to share an answer. This is mitigated by a high default cosine
threshold (**0.95**, precision-favoring per the research survey) and the
deterministic-only default; raise the threshold toward `0.99` to further reduce
false hits (lower hit rate), or lower it only after measuring your own
false-positive rate.

**Bounded growth / client-controlled TTL.** A per-request `x-broker-cache-ttl` may
only *shorten* the operator TTL and must be finite and `>= 0` (`0` = do not store,
non-finite → `400`), so an API-key holder cannot store entries that never expire or
override the operator's ceiling. Every write carries a finite TTL whenever the
operator TTL is finite. The semantic index is bounded on both axes — entries per
bucket (`MAX_ENTRIES`) **and** the number of buckets (`MAX_BUCKETS`, LRU) — so a
caller varying non-prompt params cannot grow Redis without limit. The cache also
stores only under the **served** model: a cascade escalation to a different model
is not stored (it would otherwise serve a later primary-routed request a foreign
model's answer).

Stored embeddings and response bodies live in the StateStore — treat them as
confidential as the responses. The embedding model is the broker's own registry
alias; with `BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=true` (default) a chat request
never loads it, preserving the lazy-load invariant.

## Span content capture (ADR 0014) — PII risk

Tracing captures **metadata only** by default. `BROKER_TRACING_CAPTURE_CONTENT`
is an explicit opt-in that writes truncated prompt/response content to the
`inference` span (`gen_ai.input.messages` / `gen_ai.output.messages`). **Prompts
and responses routinely contain PII and secrets**, and span data is visible to
anyone with trace-backend access and subject to its retention. Leave it off unless
you have a specific debugging need and have reviewed the trust boundary of your
OTLP collector/backend; prefer per-request `BROKER_TRACING_CONTENT_MAX_CHARS`
truncation and short retention when enabled.

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
