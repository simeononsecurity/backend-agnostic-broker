# Research note — ADR 0016: governance v2

*Scope:* resolve the deferred items from ADR 0008 (per-key governance): a
token-bucket/GCRA rate-limiting algorithm, a sliding-window counter for tpm,
per-model rpm/tpm limits, hierarchical tenant budgets, per-key placement policy,
retrievable vs. rotatable keys, and an admin key-management API.

*Threat frame:* all of this is OWASP **API4:2023 Unrestricted Resource
Consumption** — an API is vulnerable when request-rate, token/compute quota, and
third-party/compute **spend** limits are missing or set inappropriately, keyed on
something an attacker cannot trivially rotate (per-API-key, not per-IP), and must
still hold for an authenticated session [1][2]. Secondary frame: key material is a
credential; its storage/lifecycle is CWE-522/CWE-208 territory (constant-time
compare, no plaintext at rest).

> **Source trust:** every quoted page below was retrieved as *untrusted web data*.
> None contained agent-directed text / injection attempts; all are cited as claims
> to corroborate, not instructions. Vendor blogs (Dragonfly, LiteLLM, assorted
> engineering posts) are treated as secondary and cross-checked against the ITU-T
> GCRA standard and the OWASP/OpenAI primary docs where it matters.

## 1. Rate-limiting algorithm: token bucket / GCRA

The **Generic Cell Rate Algorithm** is the continuous-state leaky bucket (exactly
dual to a token bucket) from ITU-T Q.2961.1 / I.371. It keeps a *single* scalar —
the **Theoretical Arrival Time (TAT)** — and two parameters: increment `I` (= the
emission interval `T = period / rate`) and limit `L` (= burst tolerance `τ`). A
request at time `ta` is *conforming* iff `ta > TAT − L`; a conforming request sets
`TAT = max(ta, TAT) + I`; a non-conforming request leaves `TAT` unchanged [3][4].
Because the whole decision is one compare-and-update of one number, it is O(1) in
state and can be made **atomic in a single Redis round-trip** (a Lua script or the
`redis-cell` `CL.THROTTLE` command), which sidesteps the classic GET-then-SET race
that makes a hand-rolled distributed token bucket unsafe [5][6][7]. `CL.THROTTLE`
returns a five-value array (limited, limit, remaining, retry-after, reset) that
maps directly onto the `Retry-After` / `x-ratelimit-*` headers [6][8].

Mapping to our config: for a limit of `rate` per `window` with burst capacity
`burst`, emission interval `T = window / rate` and burst offset `= T * burst`; a
request is limited when `now < (max(TAT, now) + T·cost) − T·burst`. With
`burst == rate` this allows one window's worth back-to-back (token bucket of
capacity = rate) and then refills at `rate/window`.

**Decision — ADOPT, selectable, default off.** Add an atomic `gcra()` StateStore
primitive (in-process single-event-loop atomic; Redis via a loaded Lua script,
one round-trip) and a `BROKER_RATE_LIMIT_ALGO = sliding_log | gcra` switch
(default `sliding_log`, so v0.5.0 behavior is byte-for-byte preserved). GCRA adds
real burst control and O(1) state; the sliding-log remains the exact-count
default. Per-key burst is `limits.rpm_burst` (default = `rpm`).

## 2. tpm counter: fixed window vs. sliding-window counter

ADR 0008 counts tpm with a **fixed** windowed counter (`int(now/window)` bucket),
which tolerates up to ~2× over the boundary. The **sliding-window counter**
(Cloudflare/Kong's production default) keeps two fixed buckets and estimates the
rolling rate as `estimate = current + previous × (1 − progress)` where
`progress = (now − window_start)/window`. It is ~20 bytes of state (two integers),
blocks the boundary-burst attack, and Cloudflare measured **<0.003 %** error vs a
true sliding log for normal traffic [9][10][11]. The full sliding-window *log* is
rejected for tpm because token magnitudes make one log entry per token
prohibitive — the same reasoning ADR 0008 already recorded.

**Decision — ADOPT, selectable, default off.** Add `BROKER_RATE_LIMIT_TPM_ALGO =
fixed | sliding` (default `fixed`). The sliding option weights the previous
bucket; both the pre-dispatch estimate gate and the post-response reconcile charge
into the current bucket. Default `fixed` preserves v0.5.0.

## 3. Per-model rpm/tpm limits per key

Azure APIM's OWASP-API4 guidance distinguishes short-window **rate-limit-by-key**
from longer-window **quota-by-key** and specifically calls out **token limiting**
to control LLM consumption/cost per caller [12]. LiteLLM applies model-scoped
limits that must be enforced on the *resolved* model, not just the requested
alias [13]. The OWASP REST cheat sheet stresses limits must hold per-key even for
an authenticated session and cannot be bypassed [2].

**Decision — ADOPT.** `limits.per_model: {<model>: {rpm, tpm}}`, enforced at
**each resolved hop** — the `auto` router's pick and *every* cascade hop — via a
model-guard threaded into the manager alongside the existing `allowed_models`
check, so a per-model cap cannot be evaded through `auto` or an escalation.
Per-model tpm is reconciled against the actually-served model.

## 4. Hierarchical tenant budgets

LiteLLM's hierarchy is *organization > team > user > key*, and "a request must
pass all [budgets] that apply"; team budgets aggregate spend across every key in
the team and are enforced in addition to per-key budgets [13][14]. This is the
standard multi-tenant control and maps cleanly onto our existing per-key budget
counters.

**Decision — ADOPT.** A `tenants:` block in the keys file defines per-tenant
daily/monthly **token** and **cost** budgets. The governor checks the tenant
aggregate (keyed by tenant id) in addition to the per-key budget, and reconciles
actual usage into both. A key with no per-key limits but a budgeted tenant is
still governed (the `unlimited` fast-path must account for tenant budgets).

## 5. Per-API-key placement policy

ADR 0011 deferred per-key placement. Placement is already enforced fail-closed in
the manager (`PlacementDeniedError`, 403). The principle (OWASP REST [2]) is that
a key's restriction must *intersect with*, never be loosened by, the request: the
effective requirement is the **union** of the key's required labels and the
request's `x-broker-placement` labels (a member must satisfy both), so a key can
be pinned to e.g. `local`/`region:eu` members regardless of headers.

**Decision — ADOPT.** `limits.placement: [labels]` is unioned into the request's
placement requirement before routing; unsatisfiable → existing 403
`placement_denied`. No new enforcement path; placement needs no governance
counters, so it does not toggle the metered `unlimited` fast-path.

## 6. Retrievable (encrypted) keys vs. rotation

LiteLLM stores virtual keys **hashed** and never needs the plaintext back; master
rotation keeps hashed keys working, and per-key "rotation" is `regenerate` (new
secret, same record/alias) with an optional **grace period** during which both old
and new keys work ("omitted/empty = immediate revoke") [13][15]. No mainstream
broker exposes decryptable retrieval of inference keys — the irretrievable
hash-at-rest model (Stripe/AWS/GitHub) is the settled practice.

**Decision — REJECT retrievable keys.** Storing a decryptable secret trades the
core security property (a leaked `keys.yaml` yields only SHA-256 hashes of
high-entropy secrets, not usable credentials) for convenience the show-once model
already covers. **ADOPT rotation instead:** a `rotate` CLI verb and admin API that
mints a successor key inheriting the predecessor's label/tenant/scope/limits,
prints the new secret once, and sets the old key's expiry to `today + grace_days`
(default 7; `0` = immediate revoke) so callers can cut over without downtime.

## 7. Admin key-management API

LiteLLM exposes `/key/generate`, `/key/delete|block`, `/key/{key}/regenerate`
behind the master (admin) credential, with the secret returned once at generation
and only the hash persisted [13][14][15]. This is the expected operational surface
and reuses our existing admin gate (`require_admin`: admin-scope virtual key or
`BROKER_ADMIN_API_KEYS`).

**Decision — ADOPT.** `GET /admin/keys` (list; never secrets/hashes),
`POST /admin/keys` (create; secret shown **once** in the response),
`POST /admin/keys/{id}/rotate` (secret shown once), `DELETE /admin/keys/{id}`
(revoke → `enabled:false`). Admin-scope only, mutates `keys.yaml` + the in-memory
store atomically, fail-closed (400 when no `BROKER_KEYS_PATH`). The create/rotate
response is the *only* place a secret appears in HTTP — intentional, admin-only,
never logged.

## Security bar (applies to all items)

- Constant-time **byte** comparison everywhere (`hmac.compare_digest` on
  UTF-8/surrogateescape bytes; non-ASCII/over-long presented keys fail closed to a
  generic 401, never a 500) — unchanged from ADR 0008, re-tested here.
- No key material in logs/metrics/errors/usage. The admin create/rotate response
  is the sole secret egress and is never logged.
- **Fail-closed on any governance store error:** a `StateStore` exception during
  admission propagates (request denied) rather than being swallowed — availability
  is sacrificed for the consumption guard (documented in threat-model/runbook).
- Defaults reproduce v0.5.0: `sliding_log` rpm, `fixed` tpm, no per-model/tenant
  limits, no placement restriction, governance inert with no keys file.

## Adopt / reject summary

| # | Item | Decision | Confidence |
|---|------|----------|------------|
| 1 | GCRA / token-bucket rate limiting (atomic, in-proc + Redis) | **Adopt**, selectable, default `sliding_log` | High |
| 2 | Sliding-window counter for tpm | **Adopt**, selectable, default `fixed` | High |
| 3 | Per-model rpm/tpm per key (auto + cascade hops) | **Adopt** | High |
| 4 | Hierarchical tenant token/cost budgets | **Adopt** | High |
| 5 | Per-key placement policy (union, fail-closed 403) | **Adopt** | High |
| 6 | Retrievable (encrypted) keys | **Reject** — hash-only | High |
| 6b | Key rotation with grace/overlap | **Adopt** (CLI + admin API) | High |
| 7 | Admin key-management API | **Adopt** (admin-scope, show-once) | Medium-High |

Confidence is High where a primary standard (ITU-T GCRA) or an official security
doc (OWASP, OpenAI, Azure) backs the choice; Medium-High on the admin API shape,
which follows LiteLLM prior art (secondary) but is a small, well-contained surface.

## Sources

1. OWASP API Security Top 10 — **API4:2023 Unrestricted Resource Consumption** (official). https://api-security.owasp.org/editions/2023/en/0xa4-unrestricted-resource-consumption/
2. OWASP — REST Assessment Cheat Sheet (rate limiting keyed per-API-key must hold for authenticated sessions). https://cheatsheetseries.owasp.org/cheatsheets/REST_Assessment_Cheat_Sheet.html
3. ITU-T Recommendation Q.2961.1 — Generic Cell Rate Algorithm GCRA(I, L), TAT (primary standard). https://www.itu.int/rec/T-REC-Q.2961.1-199510-I
4. Generic cell rate algorithm — Wikipedia (virtual scheduling description, `ta > TAT − τ`). https://en.wikipedia.org/wiki/Generic_cell_rate_algorithm
5. Losant/redis-gcra — GCRA rate limiting on Redis (Lua, per-request cost). https://github.com/Losant/redis-gcra
6. Rate-limiting algorithms for defense — GCRA / Brandur Weinstein's `redis-cell` `CL.THROTTLE` as one atomic command, five-int return mapping to headers. https://blog.crawlex.net/blog/rate-limiting-algorithms-defense/
7. Dragonfly — Rate Limiting API / GCRA & `CL.THROTTLE` (emission interval T, cost, TAT, burst τ+T). https://www.dragonflydb.io/blog/introducing-a-rate-limiting-api-by-dragonfly
8. Rate limits — OpenAI API (official: `Retry-After`, `x-ratelimit-remaining-requests`, 429 handling). https://developers.openai.com/api/docs/guides/rate-limits
9. Sliding Window Counter — Subroute (weighted `previous×(1−progress)+current`; Cloudflare <0.003% error). https://subroute.dev/topics/rate-limiting/sliding-window
10. Rate Limiting Patterns That Scale — Fordel Studios (sliding-window counter; Cloudflare at scale). https://fordelstudios.com/research/rate-limiting-patterns-that-scale
11. awesome-system-design — rate limiter (sliding window counter formula; used by Cloudflare, Kong). https://github.com/SiluPanda/awesome-system-design/blob/main/Classic-Foundational/rate-limiter.md
12. Microsoft Learn — Mitigate OWASP API threats in Azure API Management (rate-limit-by-key, quota-by-key, token limiting). https://learn.microsoft.com/en-us/azure/api-management/mitigate-owasp-api-threats
13. LiteLLM — Virtual Keys / teams / budgets / `regenerate` + `grace_period` (official docs). https://docs.litellm.ai/docs/proxy/virtual_keys
14. LiteLLM keys/teams/budgets reference (hierarchy org>team>user>key; a request must pass all budgets). https://github.com/ScaleDockHQ/llm-skills/blob/main/skills/litellm/references/keys-budgets.md
15. mittwald Developer Portal — End-to-end key management with LiteLLM (per-key rpm/tpm/budget; rotate = new key + delete old). https://developer.mittwald.de/docs/v2/platform/aihosting/dedicated/litellm/
