# Testing Guide

Run the suite:

```bash
make test                 # pytest -q  (231 tests)
make cov                  # with coverage
python -m pytest -q tests/test_security.py   # one file
```

Tests use FastAPI's `TestClient` against the real app with the **fake backend**,
so they exercise the real request path, lifecycle, locking, and security
boundaries without any external runtime. The suite is CPU-only; optional GPU
tests (if added) should be marked and excluded from CI.

## Coverage map (231 tests)

| File | Tests | Covers |
|------|-------|--------|
| `tests/test_config.py` | 5 | Typed config + fail-fast `validate_runtime` (auth without keys, bad port, missing registry, policy/log-format validation). |
| `tests/test_registry.py` | 10 | Registry parsing/validation, unknown-field rejection, duplicate-alias detection, backend filtering, pool + router config. |
| `tests/test_lifecycle.py` | 5 | State machine legal/illegal transitions, idle timing, snapshots. |
| `tests/test_adapters.py` | 5 | Adapter contract conformance + fake backend behavior. |
| `tests/test_api.py` | 10 | `/v1/models`, `/v1/chat/completions`, `/healthz`, `/readyz`, `/status`, `/metrics`, structured errors, request IDs, size limit. |
| `tests/test_routing.py` | 8 | Load-balancing pools: single public model, concurrent spread, failover, streaming failover, round-robin, pool/alias collision. |
| `tests/test_sticky.py` | 9 | Sticky session routing: pin by header/`user`/IP, distinct-session spread, re-pin on failure, LRU bound, `/status`. |
| `tests/test_retry.py` | 8 | Retries + full-jitter backoff (ADR 0009): backoff bounds, default single-pass-no-sleep, retries stay within the pool (never cross-model), attempt cap, transient recovery, no retry mid-stream. |
| `tests/test_cooldown.py` | 5 | Member cooldowns (ADR 0009): bench after N failures + selection skip, half-open trial after expiry, success resets, `/status` + `/healthz/backends` visibility, default-off. |
| `tests/test_state.py` | 31 | `StateStore` contract parametrized over in-process + Redis (fakeredis, skips if absent): sticky/LRU, counters + sliding window, cooldown transitions, half-open claim, bytes/JSON KV, factory fail-fast. |
| `tests/test_health_probe.py` | 8 | Non-loading health: `/healthz/backends`, periodic probe marks dead backends failed (never loads), `/api/ps` aggregation. |
| `tests/test_router.py` | 29 | `auto` router: flag extraction, Stage-1 capability filter, overrides, heuristic task routing, fallback, cache, GBNF/prompt builders, manager + API end-to-end. |
| `tests/test_auth.py` | 6 | Bearer + `X-API-Key`, missing/invalid key, unauthenticated probes. |
| `tests/test_streaming.py` | 1 | SSE streaming (`chat.completion.chunk` + `[DONE]`). |
| `tests/test_openai_endpoints.py` | 18 | `/v1/completions`, `/v1/responses`, `/v1/embeddings` shims/adapters: translation, streaming, pool routing, no silent fallback. |
| `tests/test_usage.py` | 4 | Usage log (SQLite) + `GET /usage`: per-request metadata, key fingerprint, self-disable on unwritable path. |
| `tests/test_lazy_load.py` | 4 | No model loads at startup; a request loads only its selected model; no fallback. |
| `tests/test_unload.py` | 4 | Idle unload sweep; unload while idle; not while serving. |
| `tests/test_security.py` | 17 | SSRF guard, path traversal, executable allow-list, flag/shell-injection rejection, no-shell argv. |
| `tests/test_docs_examples.py` | 4 | Documentation/API examples run against the real API. |
| `tests/test_cache.py` | 18 | Response cache (ADR 0012): key stability + scope/model/endpoint isolation, sampling/tools/format in key, deterministic-only default, hit/miss/bypass headers, per-key isolation, errors-not-cached, embeddings cached, disabled pass-through, StateStore-backed roundtrip + TTL + LRU eviction + max-bytes. |
| `tests/test_admin.py` | 13 | Admin runtime control (ADR 0012): disabled blocks, admin-key required, model list, load/unload, refuse/drain/timeout on in-flight, draining member skipped, registry reload add/remove, unchanged-instance kept, invalid-registry rollback. |
| `tests/test_tracing.py` | 4 | Tracing (ADR 0012): no-op when disabled, app serves with tracing off, `ConfigError` when enabled without otel (skipped if installed), `gen_ai.*` spans asserted via in-memory exporter with **no content captured** (skipped without the SDK). |

## Writing new tests

- Add adapter-contract tests when adding a backend (`tests/test_adapters.py`).
- Keep tests CPU-only and deterministic; use the fake backend or a local HTTP
  stub rather than a real runtime.
- Security regressions must have a test in `tests/test_security.py`.
