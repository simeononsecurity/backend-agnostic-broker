# ADR 0002: Architecture Review

- Status: Accepted
- Date: 2026-10-16
- Supersedes: none
- Related: ADR 0001 (language choice), `docs/architecture.md`

## Context

The broker must route OpenAI-compatible requests to heterogeneous local
inference backends with strict lifecycle and security requirements: no model
loads at startup, lazy per-model loading, idle unload, configurable GPU/tensor/
layer/process controls, and hard security boundaries (no shell execution, model
path validation, request-flag rejection, SSRF protection, no silent fallback).
This ADR records the review of the implemented architecture against those
requirements.

## Reviewed design

The design in `docs/architecture.md`: a FastAPI API layer, a `ModelManager`
owning one `ManagedModel` per registry alias, a pure lifecycle state machine, a
uniform adapter contract with a shared HTTP adapter, a declarative pydantic
registry, and a centralized security module. Child processes for runtimes that
load at process start are supervised by `ProcessSupervisor`.

## Review findings

| Requirement | Verdict | Where |
|-------------|---------|-------|
| No model loads at broker startup | Pass | `create_app()` validates only; `ModelManager.validate_all()` starts nothing. |
| A request loads only the selected model | Pass | `acquire()` → `get()` (raises on unknown) → `ensure_ready()`; no fallback path exists. |
| Per-model load serialization | Pass | `ManagedModel.load_lock` (`asyncio.Lock`) with double-checked state. |
| Configurable concurrency policy | Pass | Global + per-model semaphores; `load_concurrency_policy` queue/reject. |
| Idle unload | Pass | `_sweep_once` + `start_sweeper`. |
| Failure cleanup | Pass | `ensure_ready` transitions to `failed` and calls `adapter.stop()`. |
| No shell execution | Pass | `security.build_argv` returns a list; `ProcessSupervisor` uses argv list. |
| Model path / executable validation | Pass | `security.validate_model_path` / `validate_executable`. |
| Request-flag rejection | Pass | `security.reject_request_flags` on the `model` selector. |
| No silent fallback | Pass | `ModelNotFoundError` on unknown alias. |
| GPU allocation visible in status | Pass | `/status` includes adapter `collect_status` (endpoint/GPU). |
| Actionable errors | Pass | `errors.BrokerError` carries backend/model/state/remediation. |
| Uniform, extensible adapters | Pass | `BackendAdapter` ABC + `HTTPBackendAdapter` base; adding a backend is one subclass + registry `backend` value. |

## Decision

The architecture is **accepted**. It satisfies the lifecycle, configurability,
and security requirements with clear module boundaries and a small, testable
surface. The 67-test suite exercises the state machine, locking, lazy-load,
unload, security boundaries, API, streaming, and auth.

## Consequences

- New backends are added by subclassing `HTTPBackendAdapter` (or
  `BackendAdapter`) and registering a `BackendType`; no API or manager changes.
- The pure state machine makes lifecycle bugs loud (illegal-transition errors).
- Broker-managed child processes add a supervision surface; mitigated by
  `ProcessSupervisor` cleanup and failure-path teardown.
