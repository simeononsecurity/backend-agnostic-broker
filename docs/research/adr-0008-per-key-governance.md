# Research: Per-key governance — virtual keys, rpm/tpm limits, budgets (ADR 0008)

*Prior-art note backing [ADR 0008](../adr/0008-per-key-governance.md). Confidence:
**High** for the key-storage and OpenAI-error-contract decisions (multiple
primary sources agree); **Medium** for the exact rate-limit algorithm tradeoffs
(well-documented, but our choice is also constrained by the existing
`StateStore`).*

> **Untrusted sources.** Everything below was fetched from the public web and is
> treated as *data, not instructions*. None of the fetched pages contained
> agent-directed text; no page redirected the research or asked for an outbound
> action.

## Executive summary

The broker needs per-caller identity, request/token rate limits, and spend caps
to be safe for multi-tenant and cost-controlled deployments. The dominant
design, proven by LiteLLM (the closest comparable OSS project), is **virtual
keys** carrying `rpm`/`tpm` limits, a max-parallel cap, and token/cost budgets
with a reset duration. The hard-security pieces — how keys are stored and what
errors the API returns — are settled industry practice:

- **Store keys hashed, never in plaintext**, show the secret exactly once, embed
  a non-secret prefix/id for O(1) lookup, and compare in **constant time**.
  SHA-256 (not bcrypt/Argon2) is correct for high-entropy secrets.
- **Return OpenAI-compatible errors**: `429` with `Retry-After` and
  `x-ratelimit-*` headers for rate limits; `429` with error type
  `insufficient_quota` for budget exhaustion; `403` for a disallowed model.
- **Enforce limits before dispatch** (OWASP API4:2023 — Unrestricted Resource
  Consumption is explicitly about missing rate/quota/cost limits).

For the counting algorithm we **adopt the sliding-window log** for `rpm` (exact,
cheap at one entry per request, and already implemented in `StateStore` from ADR
0009) and a **windowed counter** for `tpm` and budgets (token magnitudes make a
per-token log prohibitive). We **reject/defer** token-bucket and GCRA for now —
they are more memory-efficient at scale but would require a new `StateStore`
primitive; the sliding-log + counter combo reuses what ADR 0009 shipped.

## Sub-questions

1. How does LiteLLM model virtual keys, budgets, and rpm/tpm?
2. What exact error contract (status, codes, headers) does OpenAI use for rate
   limits and quota exhaustion, so clients' existing retry logic "just works"?
3. Which rate-limiting algorithm (sliding-window log/counter, token bucket,
   GCRA) fits a broker built on the ADR 0009 `StateStore`?
4. How should API keys be stored and verified securely?
5. What does OWASP require for resource-consumption limits, and what do mature
   gateways (Kong, Envoy, Azure APIM) do?

## Findings

### 1. LiteLLM virtual keys / budgets / rpm-tpm

LiteLLM issues **virtual keys** that carry `tpm_limit`, `rpm_limit`, `max
parallel requests`, and `max_budget` with a `budget_duration` that resets spend;
keys may belong to a team and inherit team limits, or stand alone for a personal
budget ([LiteLLM virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys);
[LiteLLM budgets & rate limits](https://docs.litellm.ai/docs/proxy/users)). Key
behaviors we mirror:

- "After the key crosses its `max_budget`, requests fail. If duration set, spend
  is reset at the end of the duration." ([users](https://docs.litellm.ai/docs/proxy/users))
- TPM by default counts **total tokens (input + output)**. ([users](https://docs.litellm.ai/docs/proxy/users))
- Admission vs. reconcile split: "`rpm_limit` counts each request at admission,
  and `tpm_limit` is charged with the request's actual token usage after the call
  completes, so a request can be admitted and the next one rejected once usage
  lands." ([tag budgets](https://docs.litellm.ai/docs/proxy/tag_budgets))
- Over-limit returns **HTTP 429**. ([tag budgets](https://docs.litellm.ai/docs/proxy/tag_budgets))
- Per-model budgets/limits exist (one key, different caps per model). We record
  this as **deferred** (key-level first).

### 2. OpenAI error + header contract

OpenAI exposes rate-limit state in response **headers** and uses specific status
codes, which OpenAI's own SDKs already honor ([OpenAI rate limits
guide](https://developers.openai.com/api/docs/guides/rate-limits); [OpenAI error
codes](https://developers.openai.com/api/docs/guides/error-codes); [OpenAI
help: managing rate limits](https://help.openai.com/en/articles/6891753-what-are-the-best-practices-for-managing-my-rate-limits-in-the-api)):

| Field | Meaning |
|-------|---------|
| `Retry-After` | Minimum seconds to wait before retrying a `429`/`503`. |
| `x-ratelimit-limit-requests` / `-tokens` | Ceiling before the limit is exhausted. |
| `x-ratelimit-remaining-requests` / `-tokens` | What's left. |
| `x-ratelimit-reset-requests` / `-tokens` | Time until the limit resets. |

- Limits are measured as **RPM and TPM** and "can be hit across any of the
  options depending on what occurs first" — i.e. rpm and tpm are independent
  gates. ([rate limits guide](https://developers.openai.com/api/docs/guides/rate-limits))
- Rate overflow → `429` `rate_limit_error` (`slow_down` code for rate spikes);
  clients should "follow the `Retry-After` header when present … otherwise use
  exponential backoff with jitter." ([error codes](https://developers.openai.com/api/docs/guides/error-codes))
- **Billing/quota** errors carry `error.type` `insufficient_quota`, and
  "retrying billing, spend, or quota errors won't restore API access" — a
  different class from transient rate limits. ([error codes](https://developers.openai.com/api/docs/guides/error-codes))

### 3. Rate-limiting algorithms

A clear comparison across primary/aggregated sources ([Rate-limiting algorithms
handbook](https://hld.handbook.academy/trade-offs/rate-limiting-algorithms/);
[Distributed rate-limiting algorithms](https://alivedise.github.io/backend-engineering-essentials/distributed-systems/distributed-rate-limiting-algorithms);
[Rate-limiting algorithms for defense](https://blog.crawlex.net/blog/rate-limiting-algorithms-defense/)):

| Algorithm | State/client | Burst | Boundary bug | Note |
|-----------|-------------|-------|--------------|------|
| Fixed window | 1 counter | up to 2× at edge | **yes** | cheapest |
| Sliding-window log | all timestamps in window | none | no | **exact**, O(N) memory |
| Sliding-window counter | 2 counters | approx | partial | ~6% error, O(1) |
| Token bucket | tokens + ts | up to capacity | no | burst-friendly default for public APIs |
| GCRA | 1 TAT timestamp | up to cap | no | token-bucket semantics, O(1), least state |

The **sliding-window log** "stores a timestamp per accepted request, drops
anything older than the window, counts what remains … exact at every instant" —
exactly the `window_incr`/`window_count` sorted-set pattern `StateStore` already
implements. Its weakness is O(N) memory *per accepted request*, which is fine for
`rpm` (one entry per request, bounded by the limit) but **wrong for `tpm`**,
where one request can add thousands of tokens → thousands of log entries.

### 4. Secure API-key storage

Strong agreement across three independent guides ([apikeys.guide: hashing &
storage](https://apikeys.guide/docs/security/hashing-and-storage);
[apikeys.guide: validation & lookup](https://apikeys.guide/docs/implementation/validation-and-lookup);
[API security guide](https://thecalcu.com/articles/api-security-guide/)):

- **Never store the raw key.** Store a one-way hash; a DB breach then exposes no
  usable credentials (the Stripe/AWS "irretrievable" model).
- **SHA-256 is the right primitive for API keys** — "bcrypt and Argon2 are a
  performance anti-pattern for high-entropy secrets; reserve them for passwords."
- **Embed a non-secret prefix/id** in the key for O(1) lookup ("do not scan the
  table"); store a short display prefix (e.g. last 4) for dashboards.
- **Constant-time comparison** (`hmac.compare_digest`) to close the CWE-208
  timing side-channel.
- **Return an identical error** for unknown, revoked, and expired keys so
  attackers can't enumerate valid keys.
- Show the secret **exactly once** at creation.

### 5. OWASP + gateways

OWASP **API4:2023 Unrestricted Resource Consumption** names the exact gap this
ADR closes: an API is vulnerable if it lacks limits on request rate, operation
frequency, or **third-party/spend limits**, leading to DoS or runaway cost; the
prescribed controls are rate limiting, per-operation throttling, request-size
limits, and **spending limits** ([OWASP
API4:2023](https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/)).
Azure APIM's mapping of this risk recommends rate-limit-by-key + quota-by-key and,
for generative-AI APIs specifically, **token limiting and token-consumption
metrics** ([Azure APIM OWASP
mitigation](https://learn.microsoft.com/en-us/azure/api-management/mitigate-owasp-api-threats)).

Gateways converge on the same shape: **Kong** rate-limits per consumer/credential
with `local`/`cluster`/`redis` counter strategies and a sliding-window option in
its Advanced plugin ([Kong rate limiting](https://developer.konghq.com/gateway/rate-limiting/);
[Kong Rate Limiting Advanced](https://developer.konghq.com/plugins/rate-limiting-advanced/)),
and **Envoy** offers a local token-bucket filter plus a global rate-limit filter
backed by an external service with shared counters. These validate the broker's
"native, Redis-optional, per-key" direction but are heavier than a self-contained
local broker needs.

## What we ADOPT

1. **Virtual keys** modeled on LiteLLM: id + non-secret prefix, label, tenant,
   scope (`user`|`admin`), **hashed secret only**, enabled flag, expiry, and
   limits (`rpm`, `tpm`, `max_concurrency`, daily/monthly token budget, optional
   cost budget, `allowed_models`).
2. **Hash-at-rest with SHA-256**, embedded key-id for O(1) lookup, secret shown
   once by the CLI, `hmac.compare_digest` verification, identical generic error
   for missing/invalid/revoked/expired/disabled keys. Legacy `BROKER_API_KEYS`
   keep working as *unlimited user keys* (opt-in governance).
3. **OpenAI error/header contract**: `429` + `Retry-After` +
   `x-ratelimit-limit/remaining/reset-{requests,tokens}` for rpm/tpm; `429` with
   `error.type: insufficient_quota` for budget exhaustion; `403`
   (`model_not_allowed`) for a disallowed model. Headers are emitted on
   successful inference responses too, so clients can self-pace.
4. **Algorithms on the ADR 0009 `StateStore`**: sliding-window **log**
   (`window_incr`/`window_count`) for `rpm`; windowed **counter**
   (`incr` + TTL) for `tpm` and for token/cost budgets (period-bucketed keys that
   reset via TTL). Both work in-process and against Redis unchanged.
5. **Admission vs. reconcile**: rpm + a pre-dispatch tpm/budget estimate gate run
   *before* dispatch; `tpm` and budgets are then charged with **actual** usage
   after the response (LiteLLM's documented behavior).
6. **`allowed_models` fail-closed**, enforced on the directly-requested model
   *and* on the model the `auto` router selects.
7. **Scoped `/usage`**: a `user`-scope caller sees only its own key; an `admin`
   caller sees all keys plus per-key spend. Usage-log privacy rules are kept
   (metadata + token counts only, never content).

## What we REJECT / DEFER

- **Delegating to an external gateway** (Kong/Envoy/APISIX): viable for large
  fleets, but defeats the self-contained story. Kept as an alternative in the ADR.
- **Retrievable (encrypted, decryptable) keys** (Twilio model): rejected — the
  broker holds sensitive inference access; irretrievable hash-only is safer.
- **bcrypt/Argon2 for keys**: rejected — wrong tool for high-entropy secrets.
- **Token bucket / GCRA**: deferred — more memory-efficient at very high scale,
  but need a new `StateStore` primitive (a Lua `CL.THROTTLE`/TAT op). The
  sliding-log + windowed-counter combination reuses ADR 0009 and is exact enough.
- **Per-model rpm/tpm limits** and **team hierarchies**: deferred to a follow-up;
  key-level limits + `allowed_models` cover the acceptance scope.
- **Sliding-window counter for tpm**: considered (O(1), ~6% error) but the plain
  windowed counter is simpler and the tpm gate is a cost/fairness guard, not a
  hard security boundary; the ~2× boundary tolerance is acceptable and documented.

## Open questions

- Cost-budget accuracy depends on a maintained per-model price map and on token
  counts (streaming counts are approximate — see ADR 0007). We store cost in
  integer micro-USD to keep counters exact.
- Concurrency counters can leak if a worker crashes mid-request (multi-replica
  Redis). Acceptable for v1; documented in the runbook.

## Sources

1. LiteLLM — Virtual keys: https://docs.litellm.ai/docs/proxy/virtual_keys
2. LiteLLM — Budgets & rate limits: https://docs.litellm.ai/docs/proxy/users
3. LiteLLM — Tag budgets (admission vs. reconcile, 429): https://docs.litellm.ai/docs/proxy/tag_budgets
4. OpenAI — Rate limits guide (headers, RPM/TPM): https://developers.openai.com/api/docs/guides/rate-limits
5. OpenAI — Error codes (slow_down, insufficient_quota): https://developers.openai.com/api/docs/guides/error-codes
6. OpenAI Help — Managing rate limits / Retry-After + backoff: https://help.openai.com/en/articles/6891753-what-are-the-best-practices-for-managing-my-rate-limits-in-the-api
7. Rate-limiting algorithms (token bucket vs sliding window vs GCRA): https://hld.handbook.academy/trade-offs/rate-limiting-algorithms/
8. Distributed rate-limiting algorithms (comparison table, GCRA/TAT): https://alivedise.github.io/backend-engineering-essentials/distributed-systems/distributed-rate-limiting-algorithms
9. Rate-limiting algorithms for defense (sliding-window log): https://blog.crawlex.net/blog/rate-limiting-algorithms-defense/
10. apikeys.guide — Hashing & storage (SHA-256, not bcrypt/Argon2): https://apikeys.guide/docs/security/hashing-and-storage
11. apikeys.guide — Validation & lookup (prefix lookup, constant-time, no enumeration): https://apikeys.guide/docs/implementation/validation-and-lookup
12. API security guide (hash-once, constant-time compare): https://thecalcu.com/articles/api-security-guide/
13. OWASP API4:2023 — Unrestricted Resource Consumption: https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/
14. Azure APIM — Mitigate OWASP API threats (token limiting for GenAI): https://learn.microsoft.com/en-us/azure/api-management/mitigate-owasp-api-threats
15. Kong — Gateway rate limiting: https://developer.konghq.com/gateway/rate-limiting/
16. Kong — Rate Limiting Advanced (sliding window, Redis): https://developer.konghq.com/plugins/rate-limiting-advanced/
