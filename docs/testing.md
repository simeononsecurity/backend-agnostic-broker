# Testing Guide

Run the suite:

```bash
make test                 # pytest -q  (180 tests)
make cov                  # with coverage
python -m pytest -q tests/test_security.py   # one file
```

Tests use FastAPI's `TestClient` against the real app with the **fake backend**,
so they exercise the real request path, lifecycle, locking, and security
boundaries without any external runtime. The suite is CPU-only; optional GPU
tests (if added) should be marked and excluded from CI.

## Coverage map (180 tests)

| File | Tests | Covers |
|------|-------|--------|
| `tests/test_config.py` | 5 | Typed config + fail-fast `validate_runtime` (auth without keys, bad port, missing registry, policy/log-format validation). |
| `tests/test_registry.py` | 10 | Registry parsing/validation, unknown-field rejection, duplicate-alias detection, backend filtering, pool + router config. |
| `tests/test_lifecycle.py` | 5 | State machine legal/illegal transitions, idle timing, snapshots. |
| `tests/test_adapters.py` | 5 | Adapter contract conformance + fake backend behavior. |
| `tests/test_api.py` | 10 | `/v1/models`, `/v1/chat/completions`, `/healthz`, `/readyz`, `/status`, `/metrics`, structured errors, request IDs, size limit. |
| `tests/test_routing.py` | 8 | Load-balancing pools: single public model, concurrent spread, failover, streaming failover, round-robin, pool/alias collision. |
| `tests/test_sticky.py` | 9 | Sticky session routing: pin by header/`user`/IP, distinct-session spread, re-pin on failure, LRU bound, `/status`. |
| `tests/test_health_probe.py` | 8 | Non-loading health: `/healthz/backends`, periodic probe marks dead backends failed (never loads), `/api/ps` aggregation. |
| `tests/test_router.py` | 29 | `auto` router: flag extraction, Stage-1 capability filter, overrides, heuristic task routing, fallback, cache, GBNF/prompt builders, manager + API end-to-end. |
| `tests/test_router_cascade.py` | 13 | Cascade mode (ADR 0011): escalation on length/empty/refusal, no-escalation pass-through, both-hops recorded, streaming falls back to single, `should_escalate` signals. |
| `tests/test_placement.py` | 11 | Placement pinning (ADR 0011), fail-closed: auto-filter, explicit + pool + embeddings `403 placement_denied`, pool member filtering, header parse/validation, config default. |
| `tests/test_router_latency.py` | 10 | Latency-budget routing (ADR 0011): faster-capable pick, no-op when fast enough, capability safety, config default, header parse/validation. |
| `tests/test_router_eval.py` | 3 | Eval harness: runs on the heuristic backend, metrics computed and sane (accuracy/APGR/CPT in range, CPT monotonic), markdown renders, dataset labels valid. |
| `tests/test_auth.py` | 6 | Bearer + `X-API-Key`, missing/invalid key, unauthenticated probes. |
| `tests/test_streaming.py` | 1 | SSE streaming (`chat.completion.chunk` + `[DONE]`). |
| `tests/test_lazy_load.py` | 4 | No model loads at startup; a request loads only its selected model; no fallback. |
| `tests/test_unload.py` | 4 | Idle unload sweep; unload while idle; not while serving. |
| `tests/test_security.py` | 17 | SSRF guard, path traversal, executable allow-list, flag/shell-injection rejection, no-shell argv. |
| `tests/test_docs_examples.py` | 4 | Documentation/API examples run against the real API. |

## Writing new tests

- Add adapter-contract tests when adding a backend (`tests/test_adapters.py`).
- Keep tests CPU-only and deterministic; use the fake backend or a local HTTP
  stub rather than a real runtime.
- Security regressions must have a test in `tests/test_security.py`.
