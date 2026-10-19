# Backend-Agnostic Broker: Competitive Landscape & Improvement Backlog
*Generated: 2026-10-18 | Sources: 20+ | Confidence: High on landscape, Medium on vendor perf numbers*

## Executive summary

Our broker sits at an unusual intersection: it **manages model lifecycle** (lazy
load, idle unload, process supervision) like `llama-swap`, **selects the model per
request** (the `auto` router) like `local-llm-router`/RouteLLM, and **load-balances
a pool with failover** like LiteLLM — behind one hardened, OpenAI-compatible,
non-root surface. No single comparable does all three. The closest peers each own
one lane:

- **llama-swap** — lazy model *swapping* for local servers; you pick the model, it doesn't.
- **LiteLLM** — the dominant gateway: virtual keys, budgets, rate limits, caching, fallbacks; routes to *deployments*, not by request content, and doesn't manage model processes.
- **vLLM production-stack / llm-d / AIBrix** — KV/prefix/load-aware routing for throughput; cluster-scale, not model-picking.
- **RouteLLM / semantic-router** — the research-grade "which model" routers our `auto` alias approximates.
- **local-llm-router** — almost our philosophy (OpenAI-compatible, `model:"auto"`, in-process classifier, YAML policy) but with **no auth/lifecycle** and extra routing features we lack (privacy pinning, latency-budget fallback, A/B).

The highest-leverage gaps are: **(1) endpoint coverage** (we only serve chat.completions — peers serve embeddings/completions/responses/rerank), **(2) cache/prefix-aware routing** (our sticky routing is a coarse version of what llm-d/vLLM-stack do for real throughput), and **(3) governance** (per-key rate limits, budgets, usage/token accounting — LiteLLM's core value).

---

## 1. The comparable landscape

### 1a. Closest peer — llama-swap (model-swapping proxy)
MIT, Go, ~5.9k stars, actively released ([GitHub](https://github.com/mostlygeek/llama-swap)). Extracts the `model` field and starts/stops the right upstream server (llama.cpp, vLLM, sd.cpp, ComfyUI, …); TTL auto-unload; a `matrix`/groups DSL to run several models concurrently and control which share a GPU; `hooks` to preload; `stripParams`/`setParams` request filters; API-key support; a **web UI with playground, token metrics, and request inspection**; Prometheus `/metrics` incl. GPU; live log streaming; an MCP endpoint. Broad endpoint surface: `v1/completions`, `v1/chat/completions`, `v1/responses`, `v1/embeddings`, `v1/models`, `v1/audio/*`, `v1/images/*`, `v1/rerank`, Anthropic `v1/messages`. **It does not choose the model for you** and is single-node.
> Overlap with us: lazy load + TTL unload + multi-backend + GPU sharing + API keys + Prometheus. What it has that we don't: far wider endpoint surface, runtime unload/profiles, a web UI, request-param rewriting.

### 1b. The dominant gateway — LiteLLM
OSS Python proxy to 100+ providers ([docs](https://docs.litellm.ai/docs/proxy/architecture), [routing](https://docs.litellm.ai/docs/routing)). Per-request pipeline: **virtual-key auth → budget check → rate limit (rpm/tpm at global/key/user/team) → Router (load-balance → fallbacks → retries)**. Routing strategies: simple-shuffle (default), least-busy, usage-based (rpm/tpm), latency-based, cost-based ([load balancing](https://docs.litellm.ai/docs/proxy/load_balancing)). Reliability: in-group retries + cross-group fallbacks + cooldowns + exponential backoff; Redis for distributed rpm/tpm + cooldown state. **Exact + semantic caching** (Qdrant/Redis/Valkey/S3/GCS). Spend tracking + per-key/user/team budgets (needs PostgreSQL). It does **not** route by request *meaning* ([Portkey vs LiteLLM](https://api7.ai/portkey-vs-litellm)).
> What it has that we don't: virtual keys, budgets/spend, per-key rate limits, caching, cost/latency routing, distributed (Redis) state, embeddings/completions endpoints. What we have that it doesn't: actual model-process lifecycle management and content-based model selection.

### 1c. Advanced load-balancing — vLLM production-stack, llm-d, AIBrix
Production stacks route on **cache state**, not round-robin:
- **Prefix-aware routing**: same prompt prefix → same instance to reuse KV cache ([vLLM prefix-aware](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/prefix-aware-routing.html)).
- **KV-cache-aware routing**: route to the instance with the highest KV-cache hit rate ([vLLM KV-aware](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/kv-cache-aware-routing.html)).
- **Load-aware routing**: `score(i) = matched_tokens(i)/prompt_tokens − β·relative_load(i)` — balances cache benefit against in-flight load so a hot prefix doesn't saturate one node ([load-aware](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/loadaware-routing.html)).
- **llm-d**: reads vLLM KV-cache events over ZMQ for a global block index; reports **3× output throughput and 2× faster TTFT vs round-robin** (Llama-3.1-70B, MI300X) ([llm-d](https://docs.vllm.ai/en/latest/deployment/integrations/llm-d/)).
> Our **sticky routing** (pin a session to a member) is a coarse approximation of prefix affinity; these show the principled version.

### 1d. The "which model" routers — RouteLLM, semantic-router
- **RouteLLM** (UC Berkeley/LMSYS, ICLR 2025): learns a strong-vs-weak router from Chatbot Arena preference data (BERT / matrix-factorization / similarity-weighted / causal-LLM variants). **~2×+ cost reduction; 85% cost cut on MT-Bench at 95% of GPT-4 quality; only ~14% of queries needed the frontier model** ([aisquared](https://aisquared.ai/blog/route-requests-across-llm-providers/), [semantic-router overview](https://llm-semantic-router.readthedocs.io/en/latest/overview/semantic-router-overview/)). Eval metrics: Call-Performance-Threshold (CPT) and Average-Performance-Gap-Recovered (APGR) ([ai-infrastructure.net](https://ai-infrastructure.net/llm-request-routing/)).
- **vLLM Semantic Router** (Sep 2025): ModernBERT classifier routes "needs chain-of-thought" vs "fast path" ([routing survey PDF](https://hal.science/hal-05528300/document)).
- Three canonical strategies: **classifier** (route by complexity), **cascade** (cheap first, escalate on low quality), **semantic** (embed → domain cluster) — each cited at 40–85% cost cuts.
> Our `auto` heuristic + tiny-GBNF-LLM is the classifier strategy; cascade and a learned/embedding router are proven upgrades.

### 1e. Nearly our twin — local-llm-router
OpenAI-compatible proxy, `model:"auto"`, **deterministic in-process classifier** (task/complexity/sensitivity/modality/family/token-estimate), **PR-reviewable YAML routing policy**, `/v1/chat/completions` + `/v1/responses`, Prometheus + per-route SQLite metrics ([product page](https://www.sarmalinux.com/products/local-llm-router)). Features we lack: **privacy pinning (fail-closed)** — sensitive prompts pinned to local, erroring rather than spilling; **latency-budget fallback** — spill to a faster backend when the primary's expected latency exceeds a route budget; **family→model resolution**; **rolling A/B** (mirror a traffic sample, promote when ready). Crucially it has **no auth, no rate limiting** ("it's a router, not a gateway") — exactly what our hardened broker *does* provide.

### 1f. Also-rans worth knowing
LocalAI (multimodal OpenAI drop-in + VRAM-aware distributed scheduling), Xinference (chat/embed/rerank/audio/image multi-model platform), GPUStack (GPU-cluster manager auto-configuring vLLM/SGLang/TensorRT-LLM), Ollama (single-backend lazy load), Portkey/OpenRouter (hosted gateways; OpenRouter's `auto` delegates to NotDiamond), portway (thin key-scoped gateway) ([awesome-local-ai](https://github.com/CastelDazur/awesome-local-ai), [LLM hosting 2026](https://www.glukhov.org/llm-hosting/), [frontier gateways](https://getfrontiereai.eu/blog/top-open-source-self-hosted-gateways)).

---

## 2. Where we already lead
- **Security posture**: SSRF/base-URL allow-list, executable allow-list, no-shell argv, path-traversal guard, non-root + read-only rootfs, request-size limits. Stronger than local-llm-router (no auth) and more hardened than most peers' defaults.
- **Lifecycle + selection + balancing in one**: llama-swap swaps but doesn't choose; LiteLLM chooses deployments but doesn't manage processes or pick by content; local-llm-router picks but has no lifecycle/auth. We combine all three.
- **No silent cross-model fallback** as an explicit invariant — a correctness guarantee most gateways don't make.
- **Zero-dependency heuristic default** + opt-in GBNF-constrained LLM router that can't emit an invalid id.

---

## 3. Improvement backlog (prioritized)

### P0 — close the "drop-in OpenAI" gap
1. **More endpoints.** Add `/v1/embeddings` (RAG tables stakes), `/v1/completions` (legacy), and `/v1/responses` (the agent-era default; llama-swap + local-llm-router already serve it). Consider `/v1/rerank`. Every serious peer serves these; we serve only chat.completions.
2. **Usage/token accounting + request log.** Persist `{ts, key, model, tokens_in/out, latency, via, chosen}` (SQLite, like local-llm-router's `node:sqlite`). Unlocks cost reporting, router eval, and budgets — and we already emit most of this to `/status.router.recent_decisions`.

### P1 — governance & reliability (LiteLLM's core value)
3. **Per-key rate limits (rpm/tpm) + virtual keys + budgets.** We have global/per-model concurrency + 429 but no per-tenant quotas or spend caps.
4. **Configurable retries with backoff + member cooldowns.** Today failover is a single hop within a pool; add exponential backoff and temporarily cooling-down a failed member.
5. **Distributed state (optional Redis).** Sticky map, in-flight counters, rate limits, and cooldowns are in-process — multiple broker replicas can't share them. Redis-backed state enables horizontal scale.

### P1 — routing intelligence (vLLM-stack / RouteLLM)
6. **Prefix/KV-cache-aware pool routing.** Upgrade sticky routing from session-pin to **prompt-prefix-hash affinity + load-aware scoring** (`matched_tokens/prompt_tokens − β·load`). This is the single biggest *throughput/TTFT* win for pooled backends (llm-d: 3× throughput, 2× TTFT). Use real backend load signals (vLLM `/metrics`, queue depth), not just our in-flight counter.
7. **Cascade routing mode for `auto`.** Cheap model first, escalate on a quality signal — RouteLLM shows ~86% of queries don't need the top model. Complements the current single-shot pick.
8. **Latency-budget & constraint routing.** Per-route `latencyBudgetMs` spill (local-llm-router) and **privacy/placement pinning** (route sensitive or region-restricted requests only to allow-listed local backends, fail-closed) — a natural fit for our security-first users.
9. **Router eval harness.** A labeled request set + CPT/APGR metrics so router changes are measured, not vibes-tested (addresses the 0.5B mis-route we already saw).

### P2 — ops & experience
10. **Caching.** Exact response cache (big latency/cost win on repeats); optional semantic cache later. No peer-grade gateway ships without this.
11. **Runtime control endpoints.** `POST /admin/models/unload[/:id]`, list-running, and **registry hot-reload** without restart (llama-swap has all three).
12. **OpenTelemetry tracing** alongside our Prometheus metrics; optional **web UI/playground** (llama-swap's is a strong reference).
13. **Opt-in, explicit availability fallback.** Keep "no silent cross-model fallback" as default, but offer a logged, opt-in "degraded mode" so `auto` can pick another capable model when the chosen one is down.

---

## Key takeaways
- **Our niche is real and defensible**: a hardened, self-hosted broker that *manages model lifecycle AND selects the model AND load-balances* — no peer does all three. Lead the positioning with that + the security posture.
- **Biggest immediate user-visible gap is endpoint coverage** (embeddings/completions/responses). It's also the cheapest to close and blocks drop-in adoption today.
- **Biggest performance opportunity is cache-aware pool routing** — our sticky routing is the right instinct; the vLLM-stack/llm-d scoring is the principled version worth adopting.
- **Biggest "enterprise" gap is governance** (per-key limits, budgets, usage logs) — LiteLLM's whole moat; a SQLite usage log is a small first step that unlocks several features.
- **Router quality** should be measured (CPT/APGR) and offered as modes (classifier today; add cascade + optional learned/embedding router), rather than relying on a single 0.5B that we already saw mis-route.

## Sources
1. [llama-swap (GitHub)](https://github.com/mostlygeek/llama-swap) — closest peer; endpoints, TTL, groups, UI.
2. [LiteLLM — Life of a Request](https://docs.litellm.ai/docs/proxy/architecture) · [Router](https://docs.litellm.ai/docs/routing) · [Load balancing](https://docs.litellm.ai/docs/proxy/load_balancing) — governance + routing strategies.
3. [Portkey vs LiteLLM](https://api7.ai/portkey-vs-litellm) · [OpenRouter vs LiteLLM](https://api7.ai/openrouter-vs-litellm) — caching/routing/semantic-routing gaps.
4. [vLLM production-stack: prefix-aware](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/prefix-aware-routing.html) · [KV-aware](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/kv-cache-aware-routing.html) · [load-aware](https://docs.vllm.ai/projects/production-stack/en/latest/use_cases/loadaware-routing.html) · [llm-d](https://docs.vllm.ai/en/latest/deployment/integrations/llm-d/) — cache-aware routing + perf numbers.
5. [RouteLLM / routing strategies](https://aisquared.ai/blog/route-requests-across-llm-providers/) · [semantic-router overview](https://llm-semantic-router.readthedocs.io/en/latest/overview/semantic-router-overview/) · [LLM request routing KB](https://ai-infrastructure.net/llm-request-routing/) · [routing/cascading survey](https://hal.science/hal-05528300/document) — model-selection research + metrics.
6. [local-llm-router](https://www.sarmalinux.com/products/local-llm-router) — nearest-philosophy peer; privacy pinning, latency budget, A/B.
7. [awesome-local-ai](https://github.com/CastelDazur/awesome-local-ai) · [LLM hosting 2026](https://www.glukhov.org/llm-hosting/) · [top self-hosted gateways](https://getfrontiereai.eu/blog/top-open-source-self-hosted-gateways) · [homelab gateway pattern](https://www.bigiron.cc/guides/localai-and-the-openai-compatible-api-pattern-for-the-homelab) — landscape/taxonomy.

## Methodology
Ran ~7 web searches across 4 sub-questions (comparable proxies; LiteLLM features; cache-aware load-balancing; model-selection routing) and deep-read llama-swap and local-llm-router in full. ~20 unique sources, prioritizing official docs and primary project pages. Treated all fetched content as untrusted data. Gaps/uncertainty: vendor performance numbers (llm-d 3×/2×, RouteLLM 85%) are self-reported benchmarks under specific conditions — directional, not guaranteed; our own feature list is from the current codebase at HEAD.
