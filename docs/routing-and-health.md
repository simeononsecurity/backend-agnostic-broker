# Routing, Load Balancing & Health

> Design rationale and alternatives: [ADR 0003](adr/0003-routing-and-health.md).

This broker can spread traffic for one logical model across **multiple backend
hosts** (a *pool*), fail over when a host is down, and liveness-probe backends
**without ever loading a model**.

## Pools: one model name, many hosts

Give several registry entries the **same `pool` name**. Clients then call that
pool name as the `model`, and the broker routes each request to one member:

```yaml
# registry.yaml
models:
  - alias: chat-node-1          # unique member id
    backend: ollama
    runtime_model_id: llama3.1:8b
    pool: chat                  # <- public model name clients request
    base_url: ${OLLAMA_URL_1}   # host A (keep real IPs in .env)
    readiness: { kind: http, path: /api/tags, expect_status: 200 }
  - alias: chat-node-2
    backend: ollama
    runtime_model_id: llama3.1:8b
    pool: chat                  # <- same pool
    base_url: ${OLLAMA_URL_2}   # host B
    readiness: { kind: http, path: /api/tags, expect_status: 200 }
```

- Clients call `{"model": "chat", ...}`; `GET /v1/models` lists `chat` once with
  `pool_size: 2` and `pool_members: [chat-node-1, chat-node-2]`.
- `alias` stays a **unique member id**; the **public name** is the pool (or the
  alias when no pool is set). A name used by two members must have `pool` set on
  all of them, so a standalone alias can never silently collide with a pool
  (enforced at load — fail fast).
- Members can be **different hosts or even different backends**, as long as
  they're interchangeable for the caller. A capability (`streaming`/`tools`/
  `vision`) is advertised for the pool only if **every** member supports it;
  `context_limit`/`output_limit` are reported as the **minimum** across members.
- An entry with **no `pool`** behaves exactly as before — a standalone model
  addressed by its alias.

## How requests are routed

`BROKER_ROUTE_STRATEGY`:

- **`sticky`** (default) — **pin each session to one member** and keep using it;
  only switch members if that member is unhealthy/unavailable. This keeps a
  conversation on the same backend (warm cache / KV locality) instead of
  re-balancing every request. A session is identified, in order:
  1. the **`X-Session-Id`** request header, if present;
  2. else the OpenAI **`user`** field in the request body;
  3. else the **client IP**.

  A *new* session is assigned the least-busy member (load + round-robin), so
  distinct sessions still spread across the pool — but each individual session
  then sticks 1:1. Pairings are remembered in an LRU map bounded by
  `BROKER_ROUTE_STICKY_MAX_ENTRIES` (default 10000). Visible in `GET /status` as
  `route_strategy` and `sticky_sessions`.
- **`least_busy`** — route *every* request to the member with the fewest
  in-flight requests (tie-break prefers an already-loaded member, then
  round-robin). The in-flight slot is reserved at selection time, so N concurrent
  callers spread across hosts.
- **`round_robin`** — rotate members every request regardless of load.
- **`cache_aware`** — prefix-affinity + load-aware scoring (ADR 0010); route to
  the member that already holds the request's prompt prefix (warm KV cache),
  backing off to a less-loaded member once the warm one saturates. Opt-in; see
  [Cache-aware routing](#cache-aware-routing-adr-0010) below.

Each member keeps its **own** independent lifecycle, load lock, per-model
concurrency, and idle timer. `BROKER_MAX_LOADED_MODELS` and
`BROKER_PER_MODEL_MAX_CONCURRENCY` still apply per member.

> **Sending a session id.** Any client can set `X-Session-Id: <conversation-id>`
> to control stickiness explicitly. OpenAI SDKs that send `user` get stickiness
> per user for free. With neither, callers are stuck per source IP.

### Failover

If a selected member's backend is unavailable (`backend_unavailable`,
`model_load_error`, or `timeout_error`), the request is **transparently retried
on another member** of the pool; the failed member is marked unhealthy and
excluded until it recovers. Under `sticky`, the session is **re-pinned** to the
healthy member it failed over to, so it does not keep retrying the dead one.
Non-streaming requests fail over on any attempt; streaming requests fail over
**before the first byte** is sent (once bytes are flowing a mid-stream failure
propagates, since the response has already begun). If every member is down you
get a single `backend_unavailable` pointing you at `GET /healthz/backends`.

> There is **no silent fallback to a different model** — failover only ever
> retries *equivalent members of the same pool*.

### Retries & backoff (ADR 0009)

Failover is "try each member once". On top of it, optional **retries with
full-jitter backoff** let a request keep trying within its pool:

- `BROKER_RETRY_MAX_ATTEMPTS` caps the total serve attempts. `0` (default) means
  one attempt per pool member — the single failover pass above, unchanged. A
  positive value caps attempts and, once distinct members are exhausted, re-tries
  members (still within the same pool).
- Between attempts the manager waits `delay = uniform(0, min(MAX_MS, BASE_MS *
  2**attempt))` — capped exponential backoff with **full jitter** (the AWS
  pattern, which minimizes retry storms). `BROKER_RETRY_BACKOFF_BASE_MS=0`
  (default) means no sleep, so the legacy path is byte-for-byte unchanged.
- Retrying respects `BROKER_REQUEST_TIMEOUT_SECONDS` as an overall deadline.
- Streaming retries **before the first byte only** (same rule as failover).

Retries **never cross to a different model** — same invariant as failover.

### Member cooldowns / circuit breaker (ADR 0009)

A per-member circuit breaker keeps a flapping backend out of rotation:

- After `BROKER_COOLDOWN_FAILURES` consecutive failures a member is **benched**
  for `BROKER_COOLDOWN_SECONDS`; selection skips benched members.
- Once the window elapses the member is eligible again as a **half-open trial**:
  a success resets its failure count (breaker closes), another failure re-benches
  it (breaker re-opens).
- When enabled, cooldown is the authoritative benching signal (it supersedes the
  transient `healthy` flag so an expired member can be trialed). If *every* member
  is benched, the pool falls through to the healthy tier so it can still self-heal.
- `BROKER_COOLDOWN_FAILURES=0` (default) disables cooldowns entirely.

Cooldown state is visible per member in `GET /status` and `GET /healthz/backends`
(`cooldown: {failures, in_cooldown, until, seconds_remaining}`).

### Shared state across replicas (ADR 0009)

By default routing state (sticky map, cooldowns, counters) is **in-process**, so a
single broker is fully self-contained. To run several broker replicas behind a
load balancer with one consistent view, set `BROKER_REDIS_URL` and the state moves
to Redis (sticky pins, cooldowns, and counters are then shared). `redis` is an
optional extra (`pip install 'backend-agnostic-broker[redis]'`); absent the URL
nothing changes and no new dependency is required. See
[architecture.md](architecture.md) for the `StateStore` API and
[operations-runbook.md](operations-runbook.md) for the multi-replica runbook.

## Cache-aware routing (ADR 0010)

> Design rationale, prior art, and alternatives:
> [ADR 0010](adr/0010-cache-aware-pool-routing.md) and the cited research note
> [docs/research/adr-0010-cache-aware-pool-routing.md](research/adr-0010-cache-aware-pool-routing.md).

`sticky` pins a *session* to a member — a coarse proxy for KV-cache locality.
`cache_aware` (`BROKER_ROUTE_STRATEGY=cache_aware`) routes on the actual prompt
**prefix** instead, so requests that share a system prompt, few-shot preamble, RAG
context, or conversation history land on the member that already has those KV
blocks cached — reusing cache and skipping prefill — while a load term stops a
popular prefix from hot-spotting one member. It is **opt-in**; the default stays
`sticky` and behaves exactly as before.

**How a member is chosen.** After the usual health/cooldown/capability filtering,
the broker:

1. **Normalizes** the prompt (chat `messages` as `role:content` in order;
   completion `prompt`; responses/embeddings `input`) into one canonical string.
2. **Blocks + chain-hashes** it: fixed-size character blocks
   (`BROKER_CACHE_AWARE_BLOCK_CHARS`, default 128 ≈ 32 tokens), each hashed as
   `h_i = sha256(h_{i-1} + block_i)`. Chaining means one block hash identifies the
   whole prefix up to it, so the longest match is just the leading consecutive
   blocks a member holds. Only full blocks are hashed.
3. **Looks up** each candidate in the prefix→member index (kept on the ADR 0009
   `StateStore`, so it is shared via Redis when `BROKER_REDIS_URL` is set) to get a
   `matched_fraction` per member.
4. **Scores and picks the argmax**:

   ```
   score(m) = matched_fraction(m) - beta * relative_load(m)
   relative_load(m) = (load(m) - mean_load) / max(1, mean_load)
   ```

   `matched_fraction` is in [0,1] (so one `beta` works at any prompt length);
   `relative_load` is normalized to the pool mean (so `beta` is
   deployment-independent). Ties break deterministically: highest score, then
   least load, then round-robin. **No member holds any prefix → fall back to
   `least_busy`.** After a successful serve the chosen member's blocks are recorded
   so the next matching request finds it warm.

**Tuning `beta`** (`BROKER_CACHE_AWARE_BETA`, default `0.5`) — the single knob, the
exchange rate between cache benefit and load:

- `0.0` → **pure prefix affinity**: always the warm member; best hit rate, but under
  real load it **hot-spots** a popular prefix onto one member, whose queue then blows
  up the tail latency (see the queueing matrix below).
- `0.5` (default) → prefix affinity still dominates, but a member running above the
  fleet-average load is nudged aside enough to keep queues balanced. Chosen from the
  **queueing** benchmark matrix below: the smallest `beta` that keeps a low-pressure
  pool balanced (≤1.25×) *and* is no worse than `sticky` on p95 TTFT in any scenario.
- `1.0` → a member at **2× the fleet-average load** is docked one full cache hit's
  worth of preference. This over-spreads: `cache_aware` gives up cache reuse (hit rate
  under cache pressure collapses toward `least_busy`) for only marginal extra tail
  relief.
- higher → spreads harder, trading away cache reuse for even load (→ `least_busy`).

> **Why the default is `0.5`, not `0.25` (ADR 0018) or `1.0`.** ADR 0010 shipped
> `0.25`, chosen from a *balance-only* heuristic because its benchmark modeled **no
> queueing** — nothing priced load imbalance as latency. ADR 0018 added a queueing
> model to the bench; re-measured against it, `beta=0.25` **breaks its own 1.25×
> balance bound** (low-pressure spread 1.35×) and leaves a real tail on the table
> (long_lived p95 190 ms vs 27 ms at `beta=0.5`). `beta=0.5` is the smallest `beta`
> that is balanced *and* no worse than `sticky` on p95 anywhere — and it matches
> llm-d/KServe, which weight the prefix scorer ≈2× the load scorer (effective
> `beta ≈ 0.5`). The vLLM production-stack ships `beta=1.0`, but it routes on a
> *precise*, token-exact index; this broker's *approximate* character-block index is
> a weaker match signal, so `1.0` over-spreads here.

**Load signal.** `load` defaults to in-flight request count. Set
`BROKER_CACHE_AWARE_USE_METRICS=true` to let the **non-loading** health probe
scrape an exposed vLLM `/metrics` (`vllm:num_requests_waiting`) for a real
queue-depth signal, falling back to in-flight counts when unavailable. The probe
**never loads a model** to read metrics.

**Index bounds.** The prefix index is LRU-bounded (`BROKER_CACHE_AWARE_MAX_ENTRIES`,
default 10000) with a TTL approximating KV-cache residency
(`BROKER_CACHE_AWARE_TTL_SECONDS`, default 1800). The LRU is maintained on the
StateStore's sorted set, so under Redis the **size bound is shared across replicas**
(ADR 0018) — not just per replica — with the TTL as a second, time-based bound.

**Interplay with sticky.** `cache_aware` *replaces* sticky selection — the sticky
session map is not used while it is active. Prefix affinity provides the locality
that session pinning approximated (a conversation's growing prefix keeps hitting
its warm member) **and** captures cross-session shared prefixes that sticky
cannot. Failover, cooldowns, capability filtering, and the no-cross-model rule are
unchanged.

**Observability.** `GET /status` reports `route_strategy` and a `cache_aware`
block: `{enabled, index_size, lookups, hits, hit_rate, block_chars, beta}`.

### Benchmark

`scripts/bench_routing.py` replays named, seeded workloads against a **discrete-event
simulation of a pool of continuous-batching servers** (ADR 0018), driving the **real**
routing logic. Unlike the ADR 0010 harness — whose TTFT was a pure routing-savings
formula with *no queueing* — this version prices load imbalance as real latency, so it
can report **TTFT p50/p95/p99 including queueing, throughput, and per-member
utilization**. It runs from anywhere — no `PYTHONPATH` needed:

```
python scripts/bench_routing.py --matrix          # scenario × β matrix (headline)
python scripts/bench_routing.py --select-beta      # apply the ADR 0018 β selection rule
python scripts/bench_routing.py --scenario cache_pressure   # one scenario, all strategies
python scripts/bench_routing.py --load-signal chars # compare load signals (ADR 0018 item 3)
python scripts/bench_routing.py --capacity-sweep  # sticky vs cache_aware across cache sizes
python scripts/bench_routing.py --smoke           # tiny fast run (CI)
```

**Service model** (per the LLM-serving simulator literature — inference-fleet-sim,
BestServe, Carbon-SLO; see
[the research note](research/adr-0018-cache-aware-v2.md)): each member has `nmax`
continuous-batching slots; one engine step advances every batched sequence by one
iteration costing `t_iter(n) = W + H·n` (so a busier member runs every sequence
slower). A request needs `⌈uncached_prefix_tokens / chunk⌉` prefill iterations then
`output_tokens` decode iterations, and **TTFT = queue wait + prefill time** — a prefix
cache hit shrinks the uncached prefill and frees the slot sooner. Arrivals are a seeded
Poisson process. Four scenarios (4 members, 600 requests, `block_chars=128`, fixed
seeds) span the regimes that matter:

- **long_lived** — few long-lived sessions, 3 shared prompts, ample cache (low
  pressure; the original "representative" run).
- **cache_pressure** — small per-member cache + more distinct prefixes, so naive
  spreading thrashes the KV cache.
- **cross_session** — many short sessions sharing a few system prompts (reuse is
  *across* sessions — what sticky-by-session cannot capture).
- **skewed_bursty** — Zipf-skewed session popularity under a high arrival rate.

**Scenario × β matrix** (`sticky` baseline vs `cache_aware` at each β; hit rate / TTFT
**p95 ms (incl. queueing)** / load spread min–max). The **default `β=0.5`** column is
marked:

| scenario | sticky | β=0.0 | β=0.25 | **β=0.5** | β=1.0 |
|---|---|---|---|---|---|
| long_lived (low pressure) | 91.2% / 888 / 143-165 | 93.4% / 3657 / 65-217 | 92.5% / 190 / 123-166 | **90.9% / 27 / 142-158** | 89.2% / 25 / 147-153 |
| cache_pressure | 32.6% / 1006 / 144-164 | 53.1% / 599 / 141-159 | 50.7% / 475 / 142-154 | **46.2% / 342 / 146-155** | 36.8% / 206 / 147-154 |
| cross_session | 76.3% / 433 / 143-160 | 92.3% / 397 / 141-174 | 91.9% / 368 / 135-175 | **90.7% / 155 / 142-158** | 84.2% / 28 / 148-155 |
| skewed_bursty | 43.8% / 8305 / 128-192 | 36.6% / 1776 / 133-167 | 34.0% / 1326 / 138-159 | **35.5% / 1172 / 142-159** | 30.8% / 1099 / 145-154 |

Reproduce with `python scripts/bench_routing.py --matrix` (figures are a *simulation*,
deterministic for the fixed seeds; absolute ms depend on the documented service-model
constants — the *rankings* and the β decision are the point).

**How the default `β=0.5` was chosen (ADR 0018 selection rule).** *Among the swept β,
keep those that (a) hold the low-pressure `long_lived` load spread ≤ 1.25× and (b) are
no worse than `sticky` on p95 TTFT by >10% in any scenario; pick the smallest eligible
β (most cache reuse).* Run `python scripts/bench_routing.py --select-beta`:

| β | long_lived spread | balance ok | worst p95 vs sticky | eligible |
|---|---|---|---|---|
| 0.0 | 3.34× | no | +312% | no |
| 0.1 | 3.23× | no | +168% | no |
| 0.25 | 1.35× | no | +0% | no |
| **0.5** | **1.11×** | **yes** | **+0%** | **YES (smallest)** |
| 1.0 | 1.04× | yes | +0% | yes |
| 2.0 | 1.05× | yes | +0% | yes |

Once queueing is modeled, **the old `β=0.25` hot-spots** (long_lived spread 1.35×,
breaking the balance bound it held under the no-queueing harness) and leaves a real
tail (p95 190 ms vs 27 ms at `β=0.5`). `β=0.5` is the smallest β that is balanced and
no worse than `sticky` on p95 anywhere; `β≥1.0` over-spreads and gives up cache reuse
(cache_pressure hit 46% → 37%). ADR 0010's "TTFT is an upper bound (no queueing)"
caveat is **resolved** for these claims — the queueing model now supports them.

**When to use `cache_aware` vs `sticky`.**

- **Use `cache_aware`** (at the default `β=0.5`) when your pool serves **shared
  prefixes**: shared system prompts / few-shot preambles / RAG contexts across many
  (even short) sessions, or long multi-turn chats. In `cache_pressure` and
  `cross_session` it lifts hit rate (33%→46%, 76%→91%) *and* cuts p95 TTFT (1006→342,
  433→155 ms). Even in `long_lived` (a hit-rate near-tie) it slashes p95 (888→27 ms)
  by keeping the pool balanced.
- **Under bursty, skewed load (`skewed_bursty`)**, `cache_aware` β=0.5 now also beats
  `sticky` on p95 (8.3 s → 1.2 s) and throughput (+18%) by spreading the hot-session
  queue — though `sticky` keeps a higher raw hit rate (44% vs 36%). This reverses the
  ADR 0010 guidance (which, under the no-queueing model, saw `skewed_bursty` as a
  sticky win on hit rate alone).
- **Keep `sticky` as the default strategy** for conservatism (unchanged behavior) and
  for workloads where per-session pinning is contractually required; `cache_aware`
  stays opt-in.

`β` moves `cache_aware` along the cache-vs-balance curve: lower β → higher hit rate but
looser balance (β=0 hot-spots a member to 2–4× the fleet average and blows up the
tail); higher β → tighter balance approaching `least_busy`. Re-tune per workload with
`--matrix` / `--select-beta` / `--capacity-sweep` if your prompt-length, reuse, and
load distribution differ.

## Health & liveness (never loads a model)

Three surfaces, all non-loading:

### `GET /healthz/backends`
Live, on-demand liveness of every backend member:

```json
{
  "status": "ok",                        // or "degraded" if any member is unhealthy
  "backends": [
    {"alias": "chat-node-1", "model": "chat", "backend": "ollama",
     "state": "ready", "status": "ok", "detail": "tags 200", "inflight": 1,
     "cooldown": {"failures": 0, "in_cooldown": false, "until": null, "seconds_remaining": 0.0}},
    {"alias": "qwen-llamacpp", "model": "qwen-llamacpp", "backend": "llamacpp",
     "state": "unloaded", "status": "not_loaded", "detail": "model not loaded"}
  ]
}
```

- **Ollama** (external daemon) is probed via `GET /api/tags` — reports daemon
  liveness even when no model is resident.
- **Broker-managed** runtimes (vLLM/llama.cpp/Strata) are probed only while their
  process is **already running**; when not loaded they report `not_loaded`
  (**not** a failure, and never a spawn).

### `GET /status`
Per-member lifecycle + a cached `healthy` flag and `last_health` from the
background probe, plus the `pools` map. Cheap (no network per call).

### `GET /api/ps`
Passthrough of Ollama's `/api/ps` across all configured Ollama daemons
(deduplicated by `base_url`) — shows what is **actually resident in VRAM/RAM**,
distinct from the broker's lifecycle state. Non-loading.

```json
{"daemons": [{"base_url": "http://hostA:11434", "models": [{"name": "llama3.1:8b", ...}]}]}
```

### Background probe

When `BROKER_HEALTH_PROBE_ENABLED=true` (default), a loop every
`BROKER_HEALTH_PROBE_INTERVAL_SECONDS` (default 30) liveness-probes active models
with the non-loading calls above. A **`ready` member whose backend has gone
away is marked `failed`**, so the next request reloads it (managed) or reroutes
to a healthy pool member — instead of the caller hitting a dead endpoint
mid-flight. Probes use `BROKER_HEALTH_PROBE_TIMEOUT_SECONDS` (default 5s) and
never touch a member that is mid-request.

> **Why there's no "warm keep-alive that avoids loading":** for a *model*,
> keeping it alive *is* keeping it resident — the concept only exists once
> loaded. Ollama's `keep_alive` governs how long a loaded model stays resident
> (the broker uses `keep_alive: 0` to unload on idle). The probes above are
> *daemon/process* liveness, which is what you can check without a load.

## Settings reference

| Env var | Default | Meaning |
|---|---|---|
| `BROKER_ROUTE_STRATEGY` | `sticky` | `sticky`, `least_busy`, `round_robin`, or `cache_aware` |
| `BROKER_ROUTE_STICKY_MAX_ENTRIES` | `10000` | max remembered session→member pairings (LRU, in-process) |
| `BROKER_CACHE_AWARE_BETA` | `0.5` | cache-aware: load penalty weight (`0` = pure affinity; tuned from the queueing bench matrix, ADR 0018) |
| `BROKER_CACHE_AWARE_BLOCK_CHARS` | `128` | cache-aware: characters per prefix block (~4 chars/token) |
| `BROKER_CACHE_AWARE_MAX_ENTRIES` | `10000` | cache-aware: prefix-index LRU bound (`0` = unbounded); shared across replicas under Redis (ADR 0018) |
| `BROKER_CACHE_AWARE_TTL_SECONDS` | `1800` | cache-aware: prefix-index entry TTL (`0` = no expiry) |
| `BROKER_CACHE_AWARE_USE_METRICS` | `false` | cache-aware: scrape vLLM `/metrics` queue depth via the non-loading probe |
| `BROKER_RETRY_MAX_ATTEMPTS` | `0` | serve attempts per request (`0` = one failover pass) |
| `BROKER_RETRY_BACKOFF_BASE_MS` | `0` | full-jitter backoff base (`0` = no sleep) |
| `BROKER_RETRY_BACKOFF_MAX_MS` | `2000` | backoff cap |
| `BROKER_COOLDOWN_FAILURES` | `0` | consecutive failures before benching a member (`0` = off) |
| `BROKER_COOLDOWN_SECONDS` | `30` | bench window before a half-open trial |
| `BROKER_REDIS_URL` | _(unset)_ | share routing state across replicas (optional `[redis]` extra) |
| `BROKER_REDIS_PREFIX` | `broker` | Redis key namespace |
| `BROKER_ROUTE_STICKY_TTL_SECONDS` | `0` | TTL for Redis sticky keys (`0` = none) |
| `BROKER_HEALTH_PROBE_ENABLED` | `true` | enable the background liveness probe |
| `BROKER_HEALTH_PROBE_INTERVAL_SECONDS` | `30` | seconds between probe sweeps |
| `BROKER_HEALTH_PROBE_TIMEOUT_SECONDS` | `5` | per-probe timeout |

Registry: add `pool: <name>` to each member entry. See
[model-registry.md](model-registry.md) and [registry.example.yaml](../registry.example.yaml).
