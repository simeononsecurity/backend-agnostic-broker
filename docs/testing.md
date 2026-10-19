# Testing Guide

Run the suite:

```bash
make test                 # pytest -q  (368 collected: 342 passed, 26 skipped)
make cov                  # with coverage
python -m pytest -q tests/test_security.py   # one file
```

Tests use FastAPI's `TestClient` against the real app with the **fake backend**,
so they exercise the real request path, lifecycle, locking, and security
boundaries without any external runtime. The suite is CPU-only; optional GPU
tests (if added) should be marked and excluded from CI.

In the default CPU venv, 362 pass and 28 skip — the skips are the Redis-backed
`StateStore` paths (counters, prefix index, response cache, prefix-index sorted-set
LRU) and the OpenTelemetry span path, which need the optional `redis`/`otel` extras.
In a venv with `pip install -e '.[dev,redis,otel]'` those run too: **388 passed, 2
skipped** (the remaining 2 are import-guard paths that only fire when a package is
*absent*).

## Coverage map (390 collected)

| File | Tests | Covers |
|------|-------|--------|
| `tests/test_config.py` | 21 | Typed config + fail-fast `validate_runtime` (auth without keys, auth satisfied by a keys file, bad port, missing registry, bad rate-limit window, policy/log-format/route-strategy validation, retry/cooldown/cache-aware bounds, admin credentials required vs satisfied by a keys file, legacy defaults). |
| `tests/test_registry.py` | 12 | Registry parsing/validation, unknown-field rejection, duplicate-alias detection, backend filtering, pool + router config, `pricing` block (ADR 0008) parse + negative rejection. |
| `tests/test_lifecycle.py` | 5 | State machine legal/illegal transitions, idle timing, snapshots. |
| `tests/test_adapters.py` | 5 | Adapter contract conformance + fake backend behavior. |
| `tests/test_api.py` | 10 | `/v1/models`, `/v1/chat/completions`, `/healthz`, `/readyz`, `/status`, `/metrics`, structured errors, request IDs, size limit. |
| `tests/test_routing.py` | 8 | Load-balancing pools: single public model, concurrent spread, failover, streaming failover, round-robin, pool/alias collision. |
| `tests/test_sticky.py` | 9 | Sticky session routing: pin by header/`user`/IP, distinct-session spread, re-pin on failure, LRU bound, `/status`. |
| `tests/test_retry.py` | 8 | Retries + full-jitter backoff (ADR 0009): backoff bounds, default single-pass-no-sleep, retries stay within the pool (never cross-model), attempt cap, transient recovery, no retry mid-stream. |
| `tests/test_cooldown.py` | 5 | Member cooldowns (ADR 0009): bench after N failures + selection skip, half-open trial after expiry, success resets, `/status` + `/healthz/backends` visibility, default-off. |
| `tests/test_cache_routing.py` | 31 | Cache-aware routing (ADR 0010/0018): prompt normalization + chained block hashing, longest-prefix match, LRU/TTL index eviction, hit-rate stats, vLLM `/metrics` parse; manager routing (warm-member reuse, no-match→least_busy, load flips choice, beta=0 affinity, 3-member longest-prefix); invariants (unhealthy/cooldown excluded, failover, single-member, sticky map untouched, default builds no router, embeddings); StateStore index in-process + Redis (fakeredis); **sorted-set LRU size bound (ADR 0018) in-process + Redis + a cross-replica shared-bound test**; bench smoke run. |
| `tests/test_bench_routing.py` | 19 | Queueing-model routing bench (ADR 0018): pins the re-validated config default β (`0.5`) beats `sticky` on hit rate *and* p95 TTFT (incl. queueing) for `cache_pressure`/`cross_session`, holds the 1.25× low-pressure balance bound while β=0.25 now breaks it; model correctness (percentile ordering, bounded utilization, throughput ≤ offered, uniform⇒symmetric queues, zero-cache⇒cache_aware≈least_busy, β monotonicity); β selection-rule unit tests + a real-matrix integration test; `--smoke` CLI. |
| `tests/test_state.py` | 31 | `StateStore` contract parametrized over in-process + Redis (fakeredis, skips if absent): sticky/LRU, counters + sliding window, cooldown transitions, half-open claim, bytes/JSON KV, factory fail-fast. |
| `tests/test_health_probe.py` | 8 | Non-loading health: `/healthz/backends`, periodic probe marks dead backends failed (never loads), `/api/ps` aggregation. |
| `tests/test_router.py` | 29 | `auto` router: flag extraction, Stage-1 capability filter, overrides, heuristic task routing, fallback, cache, GBNF/prompt builders, manager + API end-to-end. |
| `tests/test_router_cascade.py` | 13 | Cascade mode (ADR 0011): escalation on length/empty/refusal, no-escalation pass-through, both-hops recorded, streaming falls back to single, `should_escalate` signals. |
| `tests/test_placement.py` | 10 | Placement pinning (ADR 0011), fail-closed: auto-filter, explicit + pool + embeddings `403 placement_denied`, pool member filtering, header parse/validation, config default. |
| `tests/test_router_latency.py` | 7 | Latency-budget routing (ADR 0011): faster-capable pick, no-op when fast enough, capability safety, config default, header parse/validation. |
| `tests/test_router_eval.py` | 11 | Eval harness: RouteLLM metric primitives (PGR/frontier/APGR/CPT/oracle) vs hand-computed values incl. no-clamping + `invalid`-on-degenerate; quality ceiling is a true upper bound; router swept curve distinct from and bounded by the oracle; direct metrics in range; markdown renders; dataset labels valid. |
| `tests/test_auth.py` | 9 | Bearer + `X-API-Key`, missing/invalid key, unauthenticated probes, non-ASCII + over-long keys → 401 not 500. |
| `tests/test_streaming.py` | 1 | SSE streaming (`chat.completion.chunk` + `[DONE]`). |
| `tests/test_openai_endpoints.py` | 18 | `/v1/completions`, `/v1/responses`, `/v1/embeddings` shims/adapters: translation, streaming, pool routing, no silent fallback. |
| `tests/test_usage.py` | 4 | Usage log (SQLite) + `GET /usage`: per-request metadata, key fingerprint, self-disable on unwritable path. |
| `tests/test_keys.py` | 16 | Virtual keys (ADR 0008): secret format + SHA-256 hashing, constant-time resolve, wrong/unknown/disabled/expired → generic reject, non-ASCII + over-long keys → None (no 500), legacy keys unlimited, CLI create/list/revoke (secret shown once, no plaintext on disk). |
| `tests/test_governance.py` | 27 | Per-key governance (ADR 0008): rpm sliding window, tpm estimate + reconcile, concurrency cap + release, token & cost budget exhaustion + period bucketing, unlimited no-op; HTTP 429 (`rate_limit_exceeded`/`insufficient_quota`) + `Retry-After` + `x-ratelimit-*`, 403 `model_not_allowed`, non-ASCII/over-long keys → 401, scoped `/usage`, legacy backward-compat. Unit tests parametrized over in-process + Redis (fakeredis). |
| `tests/test_lazy_load.py` | 4 | No model loads at startup; a request loads only its selected model; no fallback. |
| `tests/test_unload.py` | 4 | Idle unload sweep; unload while idle; not while serving. |
| `tests/test_security.py` | 17 | SSRF guard, path traversal, executable allow-list, flag/shell-injection rejection, no-shell argv. |
| `tests/test_docs_examples.py` | 4 | Documentation/API examples run against the real API. |
| `tests/test_examples_cache_aware.py` | 3 | `examples/cache-aware-routing/` is real (ADR 0018): the registry parses into the 3-member `chat` pool, `.env.example` encodes `cache_aware`/`β=0.5` matching the config default, and those settings drive cache-aware selection end-to-end on the fake backend (repeat prefix → warm member; `/status.cache_aware` stats populated). |
| `tests/test_cache.py` | 18 | Response cache (ADR 0012): key stability + scope/model/endpoint isolation, sampling/tools/format in key, deterministic-only default, hit/miss/bypass headers, per-key isolation, errors-not-cached, embeddings cached, disabled pass-through, StateStore-backed roundtrip + TTL + LRU eviction + max-bytes. |
| `tests/test_admin.py` | 19 | Admin runtime control (ADR 0012): disabled blocks, admin-key required, model list, load/unload, refuse/drain/timeout on in-flight, draining member skipped, registry reload add/remove, unchanged-instance kept, invalid-registry rollback; **admin scope reconciliation** — non-ASCII/over-long admin key → 401 (not 500) on every header, and an ADR 0008 admin-scope virtual key authorizes `/admin` while a user-scope key is denied. |
| `tests/test_tracing.py` | 4 | Tracing (ADR 0012): no-op when disabled, app serves with tracing off, `ConfigError` when enabled without otel (skipped if installed), `gen_ai.*` spans asserted via in-memory exporter with **no content captured** (skipped without the SDK). |

## Writing new tests

- Add adapter-contract tests when adding a backend (`tests/test_adapters.py`).
- Keep tests CPU-only and deterministic; use the fake backend or a local HTTP
  stub rather than a real runtime.
- Security regressions must have a test in `tests/test_security.py`.
