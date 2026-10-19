# ADR 0008: Per-key governance — virtual keys, rpm/tpm limits, budgets

- Status: Accepted
- Date: 2026-10-19
- Supersedes: none
- Extended by: ADR 0016 (governance v2 — GCRA/token-bucket, per-model + tenant
  limits, per-key placement, key rotation, admin key API)
- Related: ADR 0007 (usage store), ADR 0009 (`StateStore`, shared counters);
  `docs/security-model.md`, `docs/threat-model.md`, `docs/configuration.md`,
  `docs/api-reference.md`, `docs/operations-runbook.md`, `docs/roadmap.md`;
  research: `docs/research/adr-0008-per-key-governance.md`. Prior art: LiteLLM
  virtual keys/budgets, OpenAI rate-limit contract, OWASP API4:2023, Kong/Envoy.

## Context

Today authentication is a flat allow-list of bearer keys (`BROKER_API_KEYS`), and
the only quotas are global + per-model concurrency (429 on saturation). The usage
log stores a non-reversible key fingerprint per request. This is insufficient for
multi-tenant or cost-controlled deployments, which need:

- **per-caller identity** (who is calling, under what tenant/scope);
- **request and token rate limits** (rpm/tpm) for fairness and backpressure;
- **spend caps** (token and cost budgets) so one caller cannot run up the bill;
- **model scoping** (a key may use only some models).

This is exactly OWASP **API4:2023 Unrestricted Resource Consumption**: an API is
vulnerable when it lacks rate, quota, and spend limits, leading to DoS or runaway
cost. The constraints from our invariants: the behavior must be **opt-in** — with
no keys file the broker must behave identically to v0.4.0, existing
`BROKER_API_KEYS` must keep working, no secret or key material may ever be written
to disk/logs/metrics, and all shared counters must sit on the ADR 0009
`StateStore` so they work both in-process and across Redis-backed replicas (no new
state mechanism).

The research note surveys the converging prior art: LiteLLM's virtual keys with
`rpm`/`tpm`/`max_budget`/`budget_duration`; OpenAI's `429` + `Retry-After` +
`x-ratelimit-*` header contract and its `insufficient_quota` billing error class;
the rate-limiting algorithm tradeoffs (sliding-window log is exact but O(N) per
accepted unit; token bucket/GCRA are O(1) but need new primitives); and the
settled key-storage practice (hash-at-rest with SHA-256, embedded non-secret id
for O(1) lookup, constant-time compare, show-once, identical error for
bad/revoked/expired keys to prevent enumeration).

## Decision

We introduce **virtual keys** and a **governance stage** in the request pipeline,
all **opt-in** (active only when a keys file or per-key limits are configured):

1. **Virtual keys.** `BROKER_KEYS_PATH` points at a git-ignored `keys.yaml`
   (committed `keys.example.yaml` carries fake values). Each key has an `id` (a
   non-secret prefix embedded in the secret for O(1) lookup), `label`, `tenant`,
   `scope` (`user`|`admin`), a **SHA-256 hash of the secret only** (never
   plaintext), `enabled`, and `expiry`. A CLI — `python -m broker.keys
   create|list|revoke` — generates a key, prints the secret **exactly once**, and
   writes only the hash. Verification is **constant-time** (`hmac.compare_digest`)
   and returns an **identical generic error** for missing/invalid/revoked/expired/
   disabled keys. Existing `BROKER_API_KEYS` keep working, treated as **unlimited
   user keys**.

2. **Per-key limits.** `rpm`, `tpm` (total input+output tokens), `max_concurrency`,
   a daily and/or monthly **token budget**, an optional **cost budget** priced
   from a registry `pricing` block (USD per million input/output tokens), and an
   `allowed_models` list (**fail-closed**). Counters live on the `StateStore`:
   **rpm uses the sliding-window log** (`window_incr`/`window_count` — exact, one
   entry per request); **tpm and budgets use windowed counters** (`incr` + TTL on
   period-bucketed keys that reset automatically), because token magnitudes make a
   per-token log prohibitive. Cost is accumulated in integer **micro-USD** to keep
   counters exact.

3. **Enforcement before dispatch.** For `chat/completions/responses/embeddings` the
   order is **auth → identity → allowed_models → rate/budget admission → route**.
   `rpm` and a pre-dispatch `tpm`/budget **estimate** gate run before dispatch;
   `tpm` and budgets are then charged with the **actual** usage after the response
   (LiteLLM's admission-vs-reconcile model). Concurrency is reserved on admission
   and released in a `finally`. Errors are OpenAI-compatible: **`429`
   `rate_limit_exceeded`** with `Retry-After` and
   `x-ratelimit-limit/remaining/reset-{requests,tokens}` for rpm/tpm; **`429`
   `insufficient_quota`** for budget exhaustion (OpenAI's billing error class —
   chosen over a `402` so existing OpenAI clients treat it correctly); **`403`
   `model_not_allowed`** for a disallowed model, enforced on the directly-requested
   model *and* on the model the `auto` router selects. Rate-limit headers are also
   emitted on successful responses so clients can self-pace.

4. **Scoped reporting.** `GET /usage` is scoped to the caller's key for `user`
   scope; `admin` scope sees all keys plus a per-key spend breakdown. The usage
   log keeps its privacy rules (metadata + token counts only, never content; a
   stable non-secret key id, never the secret).

With no keys file and no per-key limits, the governance stage is a no-op and the
broker is byte-for-byte v0.4.0.

## Alternatives considered

- **Delegate to an external API gateway (Kong/Envoy/APISIX).** Mature per-consumer
  rate limiting with shared Redis counters — but it defeats the self-contained,
  single-binary story and duplicates identity. Kept available; the broker keeps
  native limits. (Rejected as the default.)
- **Token bucket / GCRA for counting.** More memory-efficient at very high scale
  (O(1) state, burst control). Rejected *for now* because they need a new
  `StateStore` primitive (a Lua `CL.THROTTLE`/TAT op); the sliding-log (rpm) +
  windowed-counter (tpm/budget) combination reuses ADR 0009 unchanged and is exact
  enough. Recorded as deferred.
- **Retrievable (encrypted, decryptable) keys** (Twilio model). Rejected: the
  broker grants sensitive inference access; irretrievable hash-only (Stripe/AWS
  model) is safer and simpler.
- **bcrypt/Argon2 for key hashing.** Rejected: a performance anti-pattern for
  high-entropy random secrets; SHA-256 is the correct, FIPS-standardized choice.
- **Sliding-window counter for tpm** (2 counters, ~6% error). Considered; the plain
  windowed counter is simpler and the tpm gate is a cost/fairness guard, not a hard
  security boundary, so the bounded boundary tolerance is acceptable and documented.
- **Concurrency-only (status quo).** Insufficient for fairness and cost control.

## Consequences

- **Positive** — real multi-tenant governance: per-key identity, rpm/tpm limits,
  token/cost budgets, model scoping, and scoped usage, with an OpenAI-compatible
  error/header contract existing clients already understand. Closes OWASP
  API4:2023. Works in-process and across Redis replicas with no new state
  mechanism.
- **Negative** — a new enforcement stage on the hot path (a handful of
  `StateStore` ops per request) and a key store + CLI to maintain. Cost budgets
  depend on a maintained price map and on token counts (streaming counts are
  approximate — see ADR 0007).
- **Risks** — (a) concurrency counters can leak if a worker crashes mid-request on
  multi-replica Redis; mitigated by releasing in `finally` and documented in the
  runbook. (b) tpm/budget use fixed windows (≤2× boundary tolerance); acceptable
  for a cost guard, documented. (c) Key material leaking to logs/metrics would be
  severe; mitigated by storing only hashes + non-secret ids, keeping secrets out of
  all log/metric/error paths, and git-ignoring `keys.yaml` — covered by tests.

## Confirmation

Verified by `tests/test_keys.py` and `tests/test_governance.py`:

- **Key storage/verification** — `create` writes only a hash (no plaintext on
  disk), the secret is shown once, `hmac.compare_digest` verification, identical
  generic error for invalid/revoked/expired/disabled keys, SHA-256 at rest.
- **CLI** — `python -m broker.keys create|list|revoke` round-trip; `list`/`revoke`
  never print the secret or hash.
- **rpm** sliding window, **tpm** window (admission estimate + actual reconcile),
  **max_concurrency** cap, **daily/monthly token + cost budget** exhaustion and
  reset, **allowed_models** fail-closed including via the `auto` router, **403**
  for disallowed models.
- **Headers/errors** — `429` `rate_limit_exceeded` with `Retry-After` +
  `x-ratelimit-*`; `429` `insufficient_quota` for budgets; headers on success.
- **Backward compatibility** — `BROKER_API_KEYS` keys authenticate as unlimited
  and no governance headers/limits apply; with no keys file the request path is
  unchanged.
- **Non-ASCII / over-long key hardening** — a malformed key (non-ASCII byte or
  absurd length) fails closed with a clean `401` on both the legacy and virtual-key
  paths, never an unhandled `500` (`tests/test_auth.py`, `tests/test_keys.py`,
  `tests/test_governance.py`).
- **Redis-backed counters** exercised via `fakeredis` (skipped cleanly when the
  optional `[redis]` extra is absent).

Gates: `ruff check` + `ruff format --check` + full `pytest` green from the worktree.
