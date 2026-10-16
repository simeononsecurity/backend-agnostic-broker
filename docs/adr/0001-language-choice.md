# ADR 0001 — Implementation language: Python 3.11+

- **Status:** Accepted
- **Date:** 2026-10-15
- **Deciders:** Lead engineer (this project)

## Context

The broker supervises local AI runtime child processes (Strata, vLLM,
llama.cpp), proxies OpenAI-compatible HTTP (including Server-Sent-Event
streaming) to them and to network backends (Ollama), and must offer strong
concurrency control, request cancellation, typed configuration, and a deep
test ecosystem. We compared **Go, Python, Rust, TypeScript**.

The dominant workload is **I/O-bound**: spawning/supervising subprocesses and
proxying HTTP/SSE streams. It is *not* CPU-bound numerical work (the GPU
runtimes do the inference), so a GIL-bound interpreter is not a throughput
bottleneck for the broker tier.

## Options considered

| Criterion | Go | Python (asyncio) | Rust | TypeScript (Node) |
|---|---|---|---|---|
| Process supervision | Excellent (`os/exec`, process groups) | Very good (`asyncio.subprocess`, `os.setsid`/`killpg`) | Excellent but verbose | Good (`child_process`) |
| HTTP/SSE streaming | Good | Excellent (`httpx` + `fastapi` `StreamingResponse`) | Excellent (`axum`/`reqwest`) but heavy | Good |
| Concurrency control | Goroutines + channels | `asyncio` locks/semaphores (sufficient for I/O) | Fearless concurrency (overkill here) | Event loop |
| GPU runtime integration | Shells out | Shells out; native vendor SDKs are Python-first | Shells out | Shells out |
| Docker image size | Tiny static binary | Small with `slim` + multi-stage (~120–180 MB) | Tiny static binary | Medium |
| Startup time | ms | ~200–500 ms | ms | ~150 ms |
| Test ecosystem | Good (`testing`) | **Excellent** (`pytest`, `pytest-asyncio`, rich mocking) | Good (`cargo test`) | Good (`vitest`) |
| Operational complexity | Low | Low–medium | Higher (build toolchain) | Medium (node_modules) |
| Security posture | Memory-safe, small deps | Memory-safe, mature security libs | Memory-safe, strongest | Memory-safe, large dep tree |
| Long-term maintenance | Good | **Excellent** — ubiquitous in ML/infra teams | Steeper hiring/learning curve | Good |

## Decision

**Python 3.11+** with FastAPI + httpx + pydantic(v2).

Rationale by strongest evidence:
1. The broker is I/O-bound (subprocess + HTTP proxy); Python's `asyncio` fully
   covers process supervision, SSE streaming, cancellation, locks, and
   semaphores without the GIL being a throughput limiter for this tier.
2. The AI-runtime ecosystem (vLLM, Ollama, llama.cpp bindings, vendor GPU SDKs)
   is Python-first; documentation, flags, and lifecycle references are easiest
   to verify and keep correct in Python.
3. `pydantic` v2 gives typed, fail-fast config and registry validation for free.
4. `pytest` + `pytest-asyncio` is the strongest ecosystem for the large, mostly
   async test suite this task requires (fake backends, streaming, concurrency,
   security regressions).
5. Multi-stage Docker with `python:3.11-slim` yields a small, non-root,
   read-only-rootfs image — adequate for an I/O broker.

## Consequences

- **Positive:** fast development, deep test tooling, easiest-to-verify runtime
  behavior, low operational complexity, broad maintainer availability.
- **Negative:** larger base image than a Go/Rust static binary, and ~200–500 ms
  interpreter startup. Both are acceptable for a long-lived broker daemon and
  are mitigated by multi-stage builds and keeping the daemon resident.
- **Mitigations:** pin dependencies, run as non-root with a read-only root
  filesystem, and keep the dependency surface minimal (FastAPI, httpx,
  pydantic, uvicorn, pyyaml, prometheus-client).
