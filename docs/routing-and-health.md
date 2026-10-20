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
(`cooldown: {failures, in_cooldown, until, seconds_remaining, ejections}`).

### Reliability v2: ejection cap, exponential growth, single-trial half-open, per-error policies (ADR 0017)

Four refinements on the circuit breaker above, all opt-in (defaults = v0.5.0):

- **Ejection cap** (`BROKER_MAX_EJECTION_PERCENT`, Envoy `max_ejection_percent`).
  Caps the fraction of a pool that may be benched at once so a correlated failure
  can't eject everyone. When more members are benched than the cap allows, the
  **soonest-to-recover** ones are kept eligible — the pool is **never stranded**.
  `0` = no cap (the existing all-benched fall-through still applies).
- **Exponential ejection growth** (`BROKER_COOLDOWN_BACKOFF_MULTIPLIER`, capped by
  `BROKER_COOLDOWN_MAX_SECONDS`). A member ejected N times in a row is benched for
  `COOLDOWN_SECONDS * multiplier**(N-1)` — longer and longer if it keeps failing.
  The multiplier decays after `BROKER_COOLDOWN_DECAY_SECONDS` of sustained success
  (`0` = reset on any success). `multiplier = 1.0` = fixed window. The current
  ejection count shows as `cooldown.ejections`.
- **Single-trial half-open** (`BROKER_COOLDOWN_HALF_OPEN_SINGLE_TRIAL`). When on, a
  recovering member admits exactly ONE probe (via the StateStore half-open lock);
  concurrent requests skip it until the trial resolves. Default off keeps the ADR
  0009 behavior (any post-expiry request may probe).
- **Per-error policies.** When a backend *responds* with an HTTP error (status
  `>= 400`) the adapter raises a typed error carrying that status + `Retry-After`;
  it is classified by status — `408` → `timeout`, `429` → `backpressure`, other
  `4xx` → `client_error`, `5xx` → `server_error` — while a transport failure stays
  `connection`. Per-class settings decide retry and cooldown-counting. **A 4xx
  client error is never retried, failed over, or benched** — the upstream `4xx` is
  returned to the caller as that status (type `upstream_client_error`), never a
  `502`. An upstream `Retry-After` (RFC 9110) triggers immediate failover to a
  healthy member plus a throttle bench; the broker sleeps only when no other member
  is available, clamped to `BROKER_RETRY_AFTER_MAX_SECONDS` and the request
  deadline (bounded even when `request_timeout_seconds = 0`). See
  [configuration.md](configuration.md#per-error-retry--cooldown-policies-adr-0017).

A runnable demo lives in [`examples/reliability/`](../examples/reliability/).

### Shared state across replicas (ADR 0009)

By default routing state (sticky map, cooldowns, counters) is **in-process**, so a
single broker is fully self-contained. To run several broker replicas behind a
load balancer with one consistent view, set `BROKER_REDIS_URL` and the state moves
to Redis (sticky pins, cooldowns, and counters are then shared). `redis` is an
optional extra (`pip install 'backend-agnostic-broker[redis]'`); absent the URL
nothing changes and no new dependency is required. Redis sticky entries are
TTL-bounded by default; set `BROKER_REDIS_STICKY_MAX_ENTRIES` for a global LRU
bound across replicas (ADR 0017). See
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
  up the tail latency (see the sweep below).
- `0.5` (default) → prefix affinity still dominates, but a member running above the
  fleet-average load is nudged aside enough to keep queues balanced. Chosen from the
  **queueing** benchmark below: the smallest `beta` that keeps a low-pressure pool
  balanced (≤1.25×) *and* stays within 10% of `least_busy` (the tail-latency-best
  strategy) on p95 TTFT in every scenario **at a moderate ≤85% utilization**.
- `1.0` → a member at **2× the fleet-average load** is docked one full cache hit's
  worth of preference. This over-spreads: `cache_aware` gives up cache reuse (hit rate
  under cache pressure collapses toward `least_busy`) for only marginal extra tail
  relief.
- higher → spreads harder, trading away cache reuse for even load (→ `least_busy`).

> **`cache_aware` is not the tail-latency-best strategy — `least_busy` is.** In this
> simulated queueing model `least_busy` has the lowest p95 TTFT and the highest
> SLO-capacity in **every** scenario (see the tables below). `cache_aware`'s payoff is
> a different metric: it computes **far fewer uncached prefill tokens** (GPU work) and
> lands a **higher hit rate** on shared-prefix workloads, while staying within ~10% of
> `least_busy` on p95. Pick the strategy by your goal — see
> [when to use which strategy](#when-to-use-which-strategy).

> **Why the default is `0.5`, not `0.25` (ADR 0018) or `1.0`.** ADR 0010 shipped
> `0.25`, chosen from a *balance-only* heuristic because its benchmark modeled **no
> queueing**. ADR 0018 added a queueing model; its follow-up re-derives the default
> against the right baseline (`least_busy`) at a realistic, **non-saturated** load.
> Re-measured there, `beta=0.25` **breaks its own 1.25× balance bound** (low-pressure
> spread ~1.5×) *and* is >10% worse than `least_busy` on p95 in `cross_session`.
> `beta=0.5` is the smallest `beta` that is balanced *and* within 10% of `least_busy`
> on p95 everywhere — and it matches llm-d/KServe, which weight the prefix scorer ≈2×
> the load scorer (effective `beta ≈ 0.5`). The vLLM production-stack ships `beta=1.0`,
> but it routes on a *precise*, token-exact index; this broker's *approximate*
> character-block index is a weaker match signal, so `1.0` over-spreads here.

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
python scripts/bench_routing.py --sweep           # utilization sweep (50/70/85/95% util) + SLO capacity
python scripts/bench_routing.py --matrix          # scenario × β matrix at a moderate (~85% util) load
python scripts/bench_routing.py --select-beta      # apply the β selection rule (vs least_busy)
python scripts/bench_routing.py --scenario cache_pressure   # one scenario, all strategies (saturated)
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

**All four strategies at the saturated operating point** (each scenario's native
arrival rate, ~99% max utilization; hit rate / TTFT **p95 ms (incl. queueing)** /
prefill **work per req** — uncached prefill tokens computed). Run
`python scripts/bench_routing.py --scenario <name>`:

| strategy | long_lived | cache_pressure | cross_session | skewed_bursty |
|---|---|---|---|---|
| round_robin | 87% / 81 / 37 | 28% / 356 / 178 | 77% / 193 / 53 | 20% / 1206 / 802 |
| **least_busy** | 87% / **25** / 37 | 26% / **181** / 183 | 75% / **26** / 53 | 20% / **1093** / 793 |
| sticky | 91% / 888 / 24 | 33% / 1006 / 171 | 76% / 433 / 54 | 44% / 8305 / 673 |
| cache_aware β=0.5 | 91% / 27 / **29** | 46% / 342 / **139** | 91% / 155 / **27** | 36% / 1172 / **745** |

**`least_busy` has the best (lowest) p95 TTFT in every scenario.** `cache_aware` is a
close second and far ahead of `sticky`, while computing the **fewest uncached prefill
tokens** and the **highest hit rate** in the reuse-heavy scenarios. `sticky` has the
worst tail by far — session pinning imbalances the pool — but keeps the highest raw hit
rate under skew. These are all *simulated* figures (see the caveat below).

**Utilization sweep.** A single saturated point does not generalize, so `--sweep` runs
every strategy across load levels (arrival rate calibrated per scenario via
`least_busy` to ~50/70/85/95% max utilization — the *same* offered load for every
strategy at each level). p95 differences are tiny until the pool approaches saturation;
the enduring, load-independent difference is **prefill work** and **hit rate**. Example
(`cross_session`, p95 ms / work per req):

| load level | round_robin | least_busy | sticky | cache_aware β=0.5 |
|---|---|---|---|---|
| ~50% util | 15 / 53 | 14 / 55 | 16 / 50 | 15 / **29** |
| ~70% util | 16 / 53 | 15 / 54 | 18 / 55 | 16 / **25** |
| ~85% util | 19 / 53 | 16 / 52 | 19 / 54 | 18 / **27** |
| ~95% util | 21 / 53 | 19 / 52 | 21 / 54 | 20 / **26** |

At every load `cache_aware` is within ~10% of `least_busy` on p95 while computing
roughly **half** the prefill tokens. Reproduce with `python scripts/bench_routing.py
--sweep`.

**Capacity under an SLO.** `--sweep` also reports each strategy's **capacity** — the
max sustained arrival rate (req/s) that keeps TTFT **p95 ≤ SLO**, found by a bisection
search. The SLO is **200 ms** (a common interactive first-token budget, ~an order of
magnitude above this model's unloaded batched-prefill latency and above the bursty
service floor, so capacity reflects the onset of *queueing*, not the service floor):

| scenario | round_robin | least_busy | sticky | cache_aware β=0.5 |
|---|---|---|---|---|
| long_lived | 12.5 | **13.1** | 10.3 | 12.8 |
| cache_pressure | 12.5 | **13.0** | 10.3 | 12.7 |
| cross_session | 13.0 | **14.3** | 12.7 | 13.6 |
| skewed_bursty | 11.8 | **12.7** | 7.9 | 12.1 |

On **p95-SLO capacity** `least_busy` leads in every scenario; `cache_aware` trails it
by ~2–8% depending on the seed, lands roughly on par with `round_robin` (they tie in
`cache_pressure` and `long_lived` under some seeds — e.g. `--sweep --seed 7` gives 11.4
vs 11.4), and stays well ahead of `sticky` (by ~15–55%). These are seed-dependent
simulations — the exact ordering of the middle strategies shifts run to run, so treat
the figures as approximate and reproduce with your own seed. `cache_aware`'s distinct
advantage is the **prefill
work it saves** (20–48% fewer uncached tokens in reuse-heavy scenarios): if your
bottleneck is prefill compute (GPU FLOPs/energy) rather than batch slots, that work
saved *is* extra capacity — but this slot-bound simulation does not turn it into higher
p95-SLO capacity. State the goal, then pick (below).

**How the default `β=0.5` was chosen (selection rule).** *Evaluated at a moderate
≤85% utilization (not saturation): among the swept β, keep those that (a) hold the
low-pressure `long_lived` served-count spread ≤ 1.25× and (b) are no worse than
`least_busy` on p95 TTFT by >10% in any scenario; pick the smallest eligible β (most
cache reuse / least prefill work).* Run `python scripts/bench_routing.py --select-beta`:

| β | long_lived spread | balance ok | worst p95 vs least_busy | p95 ok | eligible |
|---|---|---|---|---|---|
| 0.0 | 4.69× | no | +48% | no | no |
| 0.1 | 3.01× | no | +39% | no | no |
| 0.25 | 1.50× | no | +13% | no | no |
| **0.5** | **1.10×** | **yes** | **+8%** | **yes** | **YES (smallest)** |
| 1.0 | 1.07× | yes | +0% | yes | yes |
| 2.0 | 1.07× | yes | +6% | yes | yes |

Re-measured against `least_busy` at a realistic load, **the old `β=0.25` fails on both
counts** — it hot-spots the low-pressure pool (spread 1.50×) *and* is 13% worse than
`least_busy` on p95 in `cross_session`. `β=0.5` is the smallest β that is balanced *and*
within 10% of `least_busy` on p95 everywhere; `β≥1.0` over-spreads and gives up cache
reuse (cache_pressure hit 46% → 32%). ADR 0010's "TTFT is an upper bound (no queueing)"
caveat is resolved, and the ADR 0018 "beats sticky in every scenario" framing is
replaced by this honest, `least_busy`-referenced one.

> **These are simulation results.** Absolute milliseconds depend on the service-model
> constants (`iter_base_ms`, `iter_per_seq_ms`, `member_slots`, arrival rates), which
> are documented and overridable; what the bench establishes are the **rankings**, the
> **work saved**, and the **β decision** — robust across the scenario matrix. Validate
> in production via `/status` (live hit rate + per-member in-flight).

### When to use which strategy

Pick by your primary goal (all `cache_aware` figures at the default `β=0.5`; all
simulated):

- **Lowest tail latency / p95 under load, and simplest** → **`least_busy`**. It has the
  best p95 TTFT and the highest p95-SLO capacity in every scenario here, and needs no
  tuning. This is the right default choice when latency is the objective.
- **GPU work / energy saved, or prefix-compute-bound capacity, or shared-prefix
  workloads** (shared system prompts / few-shot preambles / RAG contexts across many —
  even short — sessions, or long multi-turn chats) → **`cache_aware`**. It computes
  20–48% fewer uncached prefill tokens and lands a much higher hit rate (cache_pressure
  26%→46%, cross_session 75%→91%) while staying within ~10% of `least_busy` on p95. It
  does **not** beat `least_busy` on tail latency.
- **Session affinity** (a conversation must stay on one backend; contractual per-session
  pinning) or **highest raw hit rate under skew** → **`sticky`** (the **default**
  strategy, kept for conservatism/unchanged behavior). Accept its worse tail and lower
  capacity under load as the cost of strict pinning.
- **`round_robin`** → only when you want a deterministic even spread and care about
  neither cache locality nor adapting to load.

`β` moves `cache_aware` along the cache-vs-balance curve: lower β → higher hit rate /
less prefill work but looser balance (β=0 hot-spots a member to 3–5× the fleet average
and blows up the tail at saturation); higher β → tighter balance approaching
`least_busy` (and its work/hit-rate advantage fades). Re-tune per workload with
`--sweep` / `--matrix` / `--select-beta` / `--capacity-sweep` if your prompt-length,
reuse, and load distribution differ.

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
     "cooldown": {"failures": 0, "in_cooldown": false, "until": null, "seconds_remaining": 0.0, "ejections": 0}},
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
| `BROKER_MAX_EJECTION_PERCENT` | `0` | max % of a pool benched at once (`0` = no cap; never strands) |
| `BROKER_COOLDOWN_BACKOFF_MULTIPLIER` | `1.0` | grow the window per consecutive ejection (`1.0` = fixed) |
| `BROKER_COOLDOWN_MAX_SECONDS` | `0` | cap on the grown window (`0` = no cap) |
| `BROKER_COOLDOWN_DECAY_SECONDS` | `0` | sustained-success window before the multiplier resets (`0` = any success) |
| `BROKER_COOLDOWN_HALF_OPEN_SINGLE_TRIAL` | `false` | admit exactly one probe to a recovering member |
| `BROKER_COOLDOWN_HALF_OPEN_TTL_SECONDS` | `0` | trial-lock lifetime (`0` = derive from request timeout) |
| `BROKER_RETRY_ON_{TIMEOUT,CONNECTION,SERVER_ERROR,BACKPRESSURE}` | `true`/`true`/`true`/`false` | per-class retry toggles (4xx never retried) |
| `BROKER_COOLDOWN_ON_{TIMEOUT,CONNECTION,SERVER_ERROR,BACKPRESSURE}` | `true`/`true`/`true`/`false` | per-class cooldown-counting toggles (4xx never counted) |
| `BROKER_RETRY_RESPECT_RETRY_AFTER` | `true` | honor an upstream `Retry-After`: immediate failover + bench the throttled member; sleep only when no other member (clamped) |
| `BROKER_RETRY_AFTER_MAX_SECONDS` | `120.0` | finite ceiling on any `Retry-After` wait / throttle bench (bounds even `request_timeout=0`) |
| `BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS` | `{}` | optional JSON per-class attempt cap |
| `BROKER_REDIS_URL` | _(unset)_ | share routing state across replicas (optional `[redis]` extra) |
| `BROKER_REDIS_PREFIX` | `broker` | Redis key namespace |
| `BROKER_ROUTE_STICKY_TTL_SECONDS` | `0` | TTL for Redis sticky keys (`0` = none) |
| `BROKER_REDIS_STICKY_MAX_ENTRIES` | `0` | global LRU bound on Redis sticky entries (`0` = TTL-only) |
| `BROKER_HEALTH_PROBE_ENABLED` | `true` | enable the background liveness probe |
| `BROKER_HEALTH_PROBE_INTERVAL_SECONDS` | `30` | seconds between probe sweeps |
| `BROKER_HEALTH_PROBE_TIMEOUT_SECONDS` | `5` | per-probe timeout |

Registry: add `pool: <name>` to each member entry. See
[model-registry.md](model-registry.md) and [registry.example.yaml](../registry.example.yaml).
