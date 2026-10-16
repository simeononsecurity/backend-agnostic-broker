# Backend Adapter Guide

Every backend implements the same contract so the manager treats them
uniformly. An adapter instance is bound to exactly one `ModelEntry`. The
contract is defined in `broker/adapters/base.py` (`BackendAdapter`), and most
HTTP runtimes inherit the heavy lifting from `broker/adapters/httpbase.py`
(`HTTPBackendAdapter`).

## The contract

| Method | Kind | Responsibility |
|--------|------|----------------|
| `validate_config()` | sync | Validate this entry for this backend at startup; raise `ConfigError`. **Must not** start a process or load a model. |
| `describe_model()` | sync | Return an OpenAI `/v1/models`-style object plus broker metadata (backend, runtime id, context/output limits, capabilities, gpu, mtp). Provided by the base class. |
| `start()` | async | Start the backend for this model; return a child PID if broker-owned, else `None`. Must not block until ready. |
| `wait_until_ready(timeout)` | async | Block until the model can serve or raise `ModelLoadError`/`TimeoutError_`. |
| `health()` | async | Return a `HealthResult` (healthy, detail, optional gpu). |
| `chat(payload, request_id)` | async | Non-streaming completion → OpenAI-shaped response. |
| `stream_chat(payload, request_id)` | async iter | Yield raw SSE lines (`data: {...}\n\n`). |
| `cancel(request_id)` | async | Best-effort cancellation of an in-flight request. |
| `unload()` | async | Release the loaded model (native unload or stop the child process). |
| `stop()` | async | Forcefully stop and clean up any broker-owned process/resources. |
| `collect_status()` | async | Return a status dict (pid, GPU allocation, endpoint, …) for `/status`. |

## Broker-managed vs native lifetime

- `HTTPBackendAdapter` supports runtimes that load a model at **process start**
  (vLLM, llama.cpp, Strata): `start()` launches a supervised child process via
  `broker/proc.py::ProcessSupervisor` using an argv **list** built by
  `security.build_argv`, and `wait_until_ready` polls the configured
  `ReadinessProbe`. `unload`/`stop` terminate the process.
- For runtimes that manage their own model lifetime (Ollama), `start()` is a
  readiness no-op and `unload()` uses the native mechanism (`keep_alive`).

The shared HTTP adapter implements `chat`, `stream_chat`, and `cancel` against
the runtime's OpenAI-compatible (or native) HTTP endpoint, with every URL passed
through `security.validate_upstream_url` (SSRF guard).

## Security invariants every adapter must preserve

- Never build a shell command string. Always produce an argv list via
  `build_argv`.
- Validate executables with `validate_executable` against
  `BROKER_ALLOWED_EXEC_DIRS`.
- Validate `model_path` with `validate_model_path` against
  `BROKER_ALLOWED_MODEL_ROOTS`.
- Never let request-supplied data become a CLI flag; the API layer already calls
  `reject_request_flags` on the `model` selector.
- Route all HTTP egress through `validate_upstream_url`.

## Adding a new backend

1. Add a value to `BackendType` in `broker/registry.py`.
2. Create `broker/adapters/<name>.py`. Subclass `HTTPBackendAdapter` for an
   HTTP runtime (override `validate_config`, the start command / readiness, and
   any native unload), or subclass `BackendAdapter` directly for a special case
   (see `fake.py`).
3. Register it in `broker/adapters/__init__.py::make_adapter`.
4. Add a registry example and a backend guide under `docs/backends/`.
5. Add adapter-contract tests (see `tests/test_adapters.py`).

The `FakeAdapter` (`broker/adapters/fake.py`) is a complete, dependency-free
reference implementation used throughout the test suite.
