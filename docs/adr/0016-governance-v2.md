# ADR 0016: Governance v2 — GCRA/token-bucket, per-model + tenant limits, per-key placement, rotation

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none
- Extends: ADR 0008 (per-key governance)
- Related: ADR 0009 (`StateStore`, shared counters), ADR 0011 (placement),
  ADR 0012 (admin control plane); `docs/security-model.md`,
  `docs/threat-model.md`, `docs/configuration.md`, `docs/api-reference.md`,
  `docs/operations-runbook.md`, `docs/roadmap.md`;
  research: `docs/research/adr-0016-governance-v2.md`. Prior art: ITU-T GCRA,
  redis-cell `CL.THROTTLE`, Cloudflare/Kong sliding-window counter, LiteLLM
  virtual keys/teams/budgets/rotation, OWASP API4:2023, OpenAI rate-limit headers.

## Context

ADR 0008 shipped per-key governance (virtual keys, rpm/tpm, budgets, model
scoping) but **explicitly deferred** a cluster of items as "future work": a
token-bucket/GCRA rate-limiting algorithm, a sliding-window counter for tpm,
per-model rpm/tpm limits, hierarchical tenant budgets, per-key placement policy
(itself deferred from ADR 0011), retrievable keys, and an admin key-management
API. Leaving them as open deferrals violates the repo's "no pending leftovers"
rule, so this ADR resolves **every one** — each is implemented or explicitly
rejected with a rationale.

The governing threat is still OWASP **API4:2023 Unrestricted Resource
Consumption**: rate, token/compute quota, and third-party/compute **spend** must
be bounded per-API-key (not per-IP), must hold for authenticated sessions, and —
for an LLM broker — must bound cost, not just request frequency. The invariants
from ADR 0008 hold: opt-in (defaults reproduce v0.5.0 byte-for-byte), no key
material to disk/logs/metrics, all shared counters on the ADR 0009 `StateStore`
so they work in-process and across Redis replicas, and constant-time **byte**
key comparison.

## Decision

1. **GCRA / token-bucket rate limiting (selectable).** A new atomic
   `StateStore.gcra(key, emission_interval, burst, cost)` primitive implements
   the continuous-state leaky bucket (ITU-T GCRA): a single theoretical-arrival-
   time scalar, O(1) state, with explicit **per-key burst** control. It is atomic
   on both backends — a single event loop in-process, a single **Lua** round-trip
   on Redis (the redis-cell `CL.THROTTLE` pattern) — closing the GET-then-SET race
   a hand-rolled distributed token bucket would have. `BROKER_RATE_LIMIT_ALGO`
   selects `sliding_log` (exact, **default**) or `gcra`; per-key burst is
   `limits.rpm_burst` (default = `rpm`). GCRA's remaining/retry-after map onto the
   existing `x-ratelimit-*` / `Retry-After` contract.

2. **Sliding-window counter for tpm (selectable).** `BROKER_RATE_LIMIT_TPM_ALGO`
   selects `fixed` (windowed counter, **default**, = v0.5.0) or `sliding` — the
   Cloudflare/Kong weighted two-bucket estimate `current + previous·(1−progress)`,
   which blocks the fixed-window boundary burst at two integers of state
   (Cloudflare-measured <0.003% error). The full sliding **log** stays rejected
   for tpm (token magnitudes make one entry per token prohibitive — ADR 0008).

3. **Per-model rpm/tpm per key.** `limits.per_model: {model: {rpm, tpm[, rpm_burst]}}`
   is enforced at **each resolved hop** — the `auto` router's pick and *every*
   cascade hop — via a model guard threaded into the manager alongside the
   existing `allowed_models` check, so a per-model cap cannot be bypassed through
   `auto` or an escalation. Per-model tpm is reconciled against the served model.

4. **Hierarchical tenant budgets.** A `tenants:` block in the keys file defines
   per-tenant daily/monthly **token** and **cost** budgets, enforced in addition
   to per-key budgets (LiteLLM's org>team>user>key: a request must pass every
   budget that applies) and reconciled across all keys of the tenant. A key with
   no per-key limits but a budgeted tenant is still governed.

5. **Per-key placement policy.** `limits.placement: [labels]` is **unioned** into
   the request's placement requirement before routing — it can only tighten, never
   loosen, the requirement — and enforced fail-closed by the existing placement
   machinery (403 `placement_denied`). No new enforcement path; placement needs no
   counters, so it does not toggle the metered fast-path.

6. **Retrievable keys — REJECTED; rotation — ADOPTED.** Storing a decryptable
   secret would trade the core property (a leaked `keys.yaml` yields only SHA-256
   hashes of high-entropy secrets) for convenience the show-once model already
   covers. Instead, **rotation**: a `rotate` CLI verb and admin API mint a
   successor inheriting the predecessor's label/tenant/scope/limits, print the new
   secret **once**, and set the old key's expiry to
   `min(existing expiry, today + grace_days)` (`grace_days` an integer `0..365`,
   default 7; `0` = immediate revoke) for zero-downtime cutover. Rotation only
   ever **shortens** a key's life (never extends it) and is **refused on an
   inactive key** (`409 key_not_active` for a revoked/expired key) so a dead key is
   never resurrected; the CLI enforces the identical rules.

7. **Admin key-management API.** `GET /admin/keys` (list; never secrets/hashes),
   `POST /admin/keys` (create; secret shown **once**),
   `POST /admin/keys/{id}/rotate` (secret shown once),
   `DELETE /admin/keys/{id}` (revoke → `enabled:false`). Admin-scope only (reuses
   `require_admin`), fail-closed (400 `keys_file_required` with no
   `BROKER_KEYS_PATH`). All create/rotate inputs are validated before any write
   (`grace_days`, ISO non-past `expiry`, charset/length-capped `tenant`/`label`) →
   `400` rather than a stored dead key or a `500`; an oversized body → `413`. Every
   `keys.yaml` mutation is **atomic + locked**: an exclusive `flock` on a
   `keys.yaml.lock` sidecar wraps the read-modify-write and the file is swapped via
   temp-file + `fsync` + `os.replace`, created `0600` (a stricter existing mode is
   preserved). A reload over a corrupt file keeps the last known-good key set and
   logs an error (fail closed for new keys; auth never dropped wholesale). The
   create/rotate response is the **only** place a secret appears in HTTP —
   intentional, admin-only, never logged.

**Fail-closed on governance store error.** A `StateStore` exception during
admission propagates (request denied) rather than being swallowed: for a resource-
consumption guard, denying availability is safer than silently removing the cap.
Documented in the threat model and runbook.

With no keys file, default `sliding_log`/`fixed`, no per-model/tenant limits, and
no per-key placement, the governance stage is inert and the broker is v0.5.0.

## Alternatives considered

- **GCRA as the new default (replace sliding-log).** Rejected: the sliding-log is
  exact and already shipped; GCRA is offered opt-in for burst control at scale.
- **GCRA via `WATCH`/`MULTI` optimistic retry instead of Lua.** Rejected: a Lua
  script is a single atomic round-trip with no retry loop — the redis-cell pattern.
- **GCRA applied to tpm too.** Viable (cost = tokens) but the brief scopes tpm to
  the sliding-window **counter** (item 2); GCRA stays the rpm algorithm. Noted.
- **Retrievable (encrypted) keys.** Rejected — see Decision 6; hash-only is the
  security property, rotation covers the real operational need.
- **Per-model limits enforced only on the requested alias.** Rejected: that leaks
  through `auto` and cascade escalation; enforcement must be on the resolved hop.
- **Tenant rpm/tpm (not just budgets).** Out of scope here; tenants carry
  aggregate token/cost budgets only, per the card. rpm/tpm stay per-key.
- **A separate `/admin/keys` credential.** Rejected: reuse the ADR 0012 admin gate
  (admin-scope virtual key or `BROKER_ADMIN_API_KEYS`) — one control-plane identity.

## Consequences

- **Positive** — every ADR 0008 deferral is resolved: burst-aware O(1) rate
  limiting, a boundary-safe tpm counter, model- and tenant-level governance,
  placement pinning per key, and a full key lifecycle (create/rotate/revoke) via
  CLI and admin API — all opt-in, OpenAI-contract-compatible, in-process and Redis.
- **Negative** — more governance surface on the hot path (a per-model guard per
  hop; tenant counters), a Lua script to maintain, and a mutable keys file behind
  the admin API (file writes serialized by the single process; multi-replica
  deployments should treat one node as the key-management writer or use a shared
  mount — documented in the runbook).
- **Risks** — (a) GCRA/sliding-counter are approximations vs the exact log; both
  are opt-in and documented, and the exact log stays the default. (b) The admin
  create/rotate response carries a secret; mitigated by show-once, admin-scope,
  never-logged, and tests asserting no secret in list/log paths. (c) Fail-closed
  on store error can convert a Redis outage into denied requests; this is the
  deliberate security choice, documented, and only reached when governance is on.

## Confirmation

Verified by `tests/test_governance_v2.py` (+ `tests/test_state.py`,
`tests/test_keys.py`), across BOTH state backends (in-process always; Redis via
`fakeredis`):

- **GCRA** — burst then reject, refill after wait, retry-after/remaining, cost>1,
  and Governor rpm under `algo=gcra` (burst=rpm and custom `rpm_burst`), with
  Redis atomicity exercised through the Lua script under `fakeredis`.
- **Sliding tpm** — estimate gate + reconcile, and the two-bucket weighting
  (full weight at window start, 0.5 at the half-way point).
- **Per-model** — rpm and tpm caps (sliding-log and GCRA), a no-entry model
  unaffected, and HTTP enforcement incl. the `auto`-resolved model.
- **Tenant budgets** — a token budget shared across two keys, a cost budget, and
  per-key-vs-tenant precedence (per-key trips first).
- **Per-key placement** — deny (403 `placement_denied`), allow, and union with
  the `x-broker-placement` header (can only tighten).
- **Rotation** — grace overlap (both keys valid), immediate revoke (grace 0),
  successor inherits limits/tenant, hash-only on disk.
- **Admin key API** — list (no secrets/hashes), create (secret once + usable +
  hash-only on disk), admin-only (401 for a user key), rotate, revoke (then 401),
  unknown-id 404.
- **Admin key API hardening (ADR 0016 follow-up)** — `grace_days` that is a
  string/list/float-inf/out-of-range → `400 invalid_grace_days` (never 500);
  rotation clamps the old expiry to `min(existing, today+grace)` (a
  key-expiring-tomorrow stays tomorrow under grace 30); rotating a revoked or
  expired key → `409 key_not_active` with **no** successor minted; `create` with a
  non-date/past `expiry`, dict `tenant`, or non-string/over-long `label` → `400`;
  an over-`BROKER_MAX_REQUEST_BYTES` body → `413`. `keys.yaml` is written `0600`
  (stricter existing mode preserved); 12 concurrent creators all land with the file
  always parseable (flock + atomic replace); a corrupted `keys.yaml` on reload keeps
  the previous keys resolvable. The CLI `rotate`/`create` paths enforce the same
  validation and the `409` (non-zero exit).
- **Hardening** — non-ASCII / over-long keys still fail closed with a clean 401;
  invalid `BROKER_RATE_LIMIT_ALGO` / `..._TPM_ALGO` fail fast.
- **Backward compatibility** — defaults (`sliding_log`, `fixed`, no per-model/
  tenant/placement) reproduce v0.5.0; the v0.5.0 suite passes unchanged.

Gates: `ruff check` + `ruff format --check` + full `pytest` (shared venv AND
private `[dev,redis,otel]` venv) + Python 3.11 container gate incl. `mypy` +
v0.5.0 backward-compat suite + IP/secret scan, all green from the worktree.
