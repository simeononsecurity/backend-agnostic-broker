# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **Bounded, descendant-aware shutdown (ADR 0024) — production-canary blocker.**
  Stopping the broker is now deadline-driven and tears down each runtime's whole
  descendant tree, fixing three stop-path defects seen on a production canary and
  reproduced locally (same on v0.6.0 and the v0.7.0 RC):
  - `SIGTERM` **during an in-progress load** no longer waits for the load to finish
    (was up to ~97 s): the **first** stop signal **cancels** the load *before*
    uvicorn's graceful phase, kills the spawning runtime, and the model goes to
    `UNLOADED`. `ManagedModel.stop()` no longer waits unbounded on `load_lock`. A
    client request waiting on that cold load fails fast with a typed **503
    `shutting_down`** (with `Retry-After`) instead of hanging until the budget
    expires. uvicorn's graceful HTTP phase is now capped at `min(10 s, budget/3)` and
    the whole stop is drawn from one deadline stamped at the first signal, so the
    total stays within the budget.
  - An **escaped `setsid` runtime descendant** (e.g. a GPU worker in a new session)
    is no longer left holding device memory after the broker exits: descendants are
    recorded while the runtime lives (Linux `/proc` + child-subreaper; a periodic
    argv-only `ps` snapshot elsewhere) and the kill set is the process group **plus
    every recorded descendant**, each identity-gated (PID + start time).
  - **Re-parented descendants no longer accumulate as zombies.** On Linux the broker
    is a child subreaper, so a descendant that outlives its runtime re-parents to the
    broker; after teardown those are now **reaped** (`waitpid(WNOHANG)` on only the
    specific kill-set PIDs whose parent is the broker — never `waitpid(-1)`), both
    inline in `terminate_tree` (after an eviction/unload/shutdown) and periodically in
    the idle sweeper. Previously repeated evictions left zombie children behind.
  - A runtime that ignores `SIGTERM` is now bounded by a total budget instead of
    only a per-runtime grace: adopted-runtime termination moved off the event loop
    (no longer blocks the loop during the grace), and uvicorn runs with
    `timeout_graceful_shutdown` bounded by the budget.

### Added

- **`BROKER_SHUTDOWN_BUDGET_SECONDS`** (default `45`, must be > 0): the total
  wall-clock budget for the whole stop path. Each runtime's SIGTERM grace is
  `min(BROKER_SHUTDOWN_TIMEOUT_SECONDS, remaining budget)`; at expiry everything
  left is SIGKILLed and the broker exits. A second `SIGTERM`/`SIGINT` during
  shutdown escalates immediately to SIGKILL of every runtime tree.
- Per-stop teardown telemetry: `broker_runtime_teardown_total{outcome}` counter and
  `broker_runtime_teardown_duration_seconds` histogram; the teardown result
  (signalled / exited / survivors / reaped / duration) is logged and surfaced through
  `survived_kill` / `release_stuck` in `/status` while survivors exist.
- New typed error `shutting_down` (HTTP **503**, with `Retry-After`): returned to a
  client whose request was waiting on a model load the broker cancelled while stopping.

### Changed

- **Behaviour change (on by default — a bug fix to the "stopping the broker stops
  what it started" invariant, not an opt-in feature):** a model load interrupted by
  shutdown is abandoned and repeats on the next start; the broker now tears down the
  runtime's whole descendant tree rather than only its process group. Set the
  service manager's stop timeout to **≥ `BROKER_SHUTDOWN_BUDGET_SECONDS` + 15 s**
  (`TimeoutStopSec`, `KillMode=mixed`; see the operations runbook).
- `ProcessSupervisor.stop()` and the adapter `stop()` contract now return a
  `TeardownResult` (was a bool on the supervisor) describing the teardown outcome.

## [0.7.0] - 2026-10-21

### Added

- **Runtime process identity + restart reconciliation (ADR 0021).** The broker now
  records every runtime it spawns (vLLM / llama.cpp / Strata) in a host-local,
  `0600`, atomic + `flock` ledger under `BROKER_CACHE_DIR`
  (`runtime-ledger.json`), keyed by a durable identity of **PID + process start
  time** (`/proc/<pid>/stat` field 22 on Linux; `ps -o lstart=` on macOS) plus the
  process-group id and an argv hash — never a bare PID. On startup it reconciles
  runtimes orphaned by an unclean exit (SIGKILL / OOM / crash / container restart)
  per `BROKER_RUNTIME_RECONCILE`: `off`, `report` (default — list orphans in
  `/status`, never signal/adopt), `adopt` (reuse a still-healthy orphan: mark the
  model READY without spawning), or `stop` (`SIGTERM`→grace→`SIGKILL` the recorded
  process group). In every mode a PID whose recorded start time no longer matches
  is dropped as stale and **never signalled**, and a process the broker did not
  record is never touched; a corrupt ledger is ignored with a warning (fail-safe).
- **Port pre-check before spawn** (`BROKER_RUNTIME_PORT_PRECHECK`, default on): a
  broker-managed runtime whose target port is already bound fails fast with a clear
  error naming the port instead of a confusing readiness timeout.
- `GET /status` now carries a per-model `process` object `{pid, start_time,
  adopted}` and a top-level `reconciliation` summary of the last startup.
- **Multi-replica ledger safety (ADR 0021).** A live broker that shares another
  broker's `BROKER_CACHE_DIR` can never have its runtimes stopped or adopted. Each
  broker takes an exclusive, lifetime `flock` on
  `runtime-ledger.owner.lock`; if a live sibling holds it, `adopt`/`stop` refuse to
  start (clear `ConfigError` naming the cache dir + owner PID — fail closed) and
  `report`/`off` start without a ledger (one warning;
  `reconciliation.disabled_reason` + `ledger_owned` in `/status`), preserving v0.6.0
  behavior. As defense in depth every record now carries its owning broker's
  identity, and reconciliation acts only on records whose owner broker is dead
  (others are listed as `owned_by_live_broker`). A stale lock from a crashed owner
  is taken over automatically.
- New config: `BROKER_RUNTIME_RECONCILE`, `BROKER_RUNTIME_PORT_PRECHECK`
  (`docs/configuration.md`, `.env.example`). New guide
  `docs/guides/restart-and-recovery.md` and tested `examples/restart-recovery/`.
- **Vendor-neutral GPU/host resource telemetry (ADR 0020, opt-in, default OFF).**
  A pluggable `ResourceTelemetry` provider selected by `BROKER_GPU_TELEMETRY`
  (`off` | `nvidia` | `amd` | `host` | `auto`) measures per-device memory and, where
  the driver exposes it, which PIDs hold it. Providers shell out argv-only to an
  absolute/allow-listed binary (`nvidia-smi` query mode, `amd-smi`/`rocm-smi` JSON,
  Linux `/proc/meminfo`, macOS `sysctl`+`vm_stat`) with a timeout and parse
  defensively; a failed probe degrades to `unavailable` (never a crash, never an
  invented number). Samples carry `source: measured`. New `python -m
  broker.resources_probe <provider>` CLI. `/status` and `/healthz/backends` gain a
  `resources` block; Ollama daemon memory is reported separately, labeled `reported`.
- **Measured release on model swap (ADR 0020).** After evicting a broker-spawned
  runtime, the next load on the same device waits (bounded by
  `BROKER_RELEASE_TIMEOUT_SECONDS`) for the runtime's **process group** (parent and
  children) to stop holding memory **and** for the device's free memory to recover
  (≥ `BROKER_GPU_RELEASE_RECOVERY_FRACTION`), so a child worker (vLLM) or a container
  PID namespace that hides the holder can't be mistaken for a release. If the memory
  never comes back the model enters a visible `release_stuck` state and the load
  fails **503** instead of OOMing; `release_stuck` **auto-recovers** to `unloaded`
  (next load or periodic tick) once the memory returns, with no restart.
  `ProcessSupervisor.stop()` now reports (logs + returns) when a process survives
  SIGKILL instead of silently returning. The broker never resets a GPU or kills a
  process it did not start.
- **Vendor-correct device env var (ADR 0020).** `gpu.device_ids` is set into the var
  the active provider expects — `ROCR_VISIBLE_DEVICES` for `amd`, `CUDA_VISIBLE_DEVICES`
  otherwise (the portable default, unchanged) — overridable with
  `BROKER_GPU_DEVICE_ENV_VAR`.
- **Memory-aware admission (ADR 0022, opt-in, default OFF).** An optional per-model
  `memory_mib` admits a load only if it fits the device budget — measured free
  memory (minus `BROKER_GPU_MEMORY_HEADROOM_MIB`) when telemetry is on, else the
  declared `BROKER_GPU_MEMORY_BUDGET_MIB` (integer or `0=…,1=…` map). Admission is
  **atomic** (a global lock serializes admit→evict→reserve, and in-flight loads are
  counted as reservations so concurrent loads can't double-book a device) and
  **evicts no more than necessary**: a model larger than the device can ever hold
  fails fast with `reason=exceeds_device_capacity` and evicts nothing, and eviction
  runs only when it could actually make room. Otherwise it raises a typed
  `CapacityUnavailableError` (429, with `reason` + `holder`) that the ADR 0023 queue
  can wait on. Unsized models keep v0.6.0 behavior.
- New tunables: `BROKER_GPU_RELEASE_RECOVERY_FRACTION` (default `0.9`) and
  `BROKER_GPU_MEMORY_RESERVATION_SECONDS` (default `10.0`).
- New guide `docs/guides/gpu-memory.md` and tested `examples/gpu-memory/`.
- **Bounded, fair request queueing under overload (ADR 0023, opt-in, default OFF).**
  With `BROKER_QUEUE_ENABLED`, a request that cannot be admitted right now —
  because of concurrency saturation, memory-aware capacity (ADR 0022), a model mid
  load/release, or a measured overload signal — waits in a **bounded, timed, fair**
  admission queue instead of blocking a semaphore unboundedly or failing. Bounds
  (`BROKER_QUEUE_MAX_DEPTH[_PER_MODEL]`) → **429** `queue_full` + `Retry-After`;
  wait timeout (`BROKER_QUEUE_MAX_WAIT_SECONDS`) → **503** `queue_timeout` +
  `Retry-After` naming the trigger. Fairness is FIFO within an API key and
  round-robin across keys, with an optional per-key `priority` (keys file) and
  aging so low-priority requests never starve. Overload triggers (concurrency,
  capacity, measured device memory, host load average, backend backpressure) are
  **each individually configurable**; telemetry-based ones are inert when telemetry
  is off, and `exceeds_device_capacity` (never fits) fails fast instead of queueing.
  It composes with the existing semaphores (no double-limiting), retries/failover
  (a queue verdict is terminal), cache hits (bypass the queue), the auto-router (no
  silent cross-model switch), streaming (the wait is before the first byte), and
  Redis multi-replica (each replica queues locally). Client disconnect frees the
  slot immediately. Visibility: `x-broker-queue-wait-ms` (+ trigger) header, a
  `/status` `queue` block, new `broker_queue_*` Prometheus series (`/metrics`
  exposure unchanged), and an OTel span attribute. Per-key quota 429s (rpm/tpm/
  budget) are a different concern and are **never** queued.
- New module `broker/admission.py`, guide `docs/guides/request-queueing.md`, tested
  `examples/request-queueing/`, and a labeled simulated benchmark `scripts/bench_queue.py`.

### Changed

- Graceful shutdown (FastAPI lifespan / `SIGTERM` to `python -m broker`, incl.
  `docker stop`) now also clears the runtime ledger after stopping runtimes. The
  Docker image declares `STOPSIGNAL SIGTERM` explicitly (exec-form entrypoint
  already made python PID 1).

### Fixed

- **Restart reconciliation no longer loses an orphan on an unrelated broker's
  shutdown (ADR 0021).** `ManagedModel.stop()` removed a model's ledger record
  unconditionally, so a graceful shutdown of a broker running in `report`/`off`
  mode (or one whose load failed on the orphan-held port) wiped the ledger record
  of a *different*, crashed broker's still-running runtime — stranding the orphan
  so a later `adopt`/`stop` restart could no longer reconcile it. The ledger
  record is now dropped only when this broker actually owns the runtime (it
  spawned or adopted it, i.e. `process_start_time` is set); an orphan it merely
  observed is left intact. Found by the v0.7.0 real-backend test matrix;
  regression test in `tests/test_runtime_reconcile.py`.
- **Client disconnect now frees a held admission slot end-to-end (ADR 0023).**
  The admission queue was already cancellation-safe, but uvicorn does not cancel a
  buffered (non-streaming) handler when the client goes away, so a disconnected
  request kept holding its concurrency slot until the upstream call finished — a
  queued follow-up could time out behind it. The non-streaming serve path
  (`/v1/chat/completions`, `/v1/completions`, `/v1/responses`, `/v1/embeddings`,
  `/v1/rerank`) now watches the ASGI `receive` channel for `http.disconnect` and
  cancels the serve the moment the client leaves, releasing the slot and the
  inflight upstream request at once (internal `client_disconnected` / 499, never
  delivered). Found by the v0.7.0 real-backend test matrix; regression tests in
  `tests/test_queue.py`.

### Notes

- Default behavior is byte-for-byte v0.6.0: with `BROKER_GPU_TELEMETRY=off` (the
  default), no `memory_mib`/budget, `BROKER_QUEUE_ENABLED` unset, and the runtime
  ledger (ADR 0021) in `report` mode, nothing new runs.
- Ollama long-context truncation (`prompt_tokens` capped near 16k) is a server-side
  setting, not a broker bug: the broker forwards `options.num_ctx` unchanged, but
  Ollama's OpenAI-compatible endpoint ignores it. Raise `OLLAMA_CONTEXT_LENGTH` or the
  model's Modelfile `num_ctx` on the Ollama host. See `docs/backends/ollama.md`
  ("Context window"); pinned by `tests/test_ollama_ctx_passthrough.py`.

## [0.6.0] - 2026-10-19

### Added

- **Getting Started tutorial rewritten into three tracks** (`docs/getting-started.md`):
  no-GPU fake backend, local Python + Ollama, and the Docker lean image — each
  showing `auto`, streaming, embeddings, and the OpenAI Python SDK. Track C's
  commands are marked `<!-- runnable -->` and executed by `tests/test_docs_examples.py`.
- **How-to guides for every feature** (`docs/guides/`): backends, pools & routing,
  reliability, Redis multi-replica, auto-router, virtual keys, response cache, admin
  API, web UI, observability, embeddings & rerank, production deployment, and
  upgrading — each with when-to-use, minimal/full config, a verify step, and
  troubleshooting, cross-linked to its reference page and ADR. Added `docs/README.md`
  and `docs/guides/README.md` indexes; the top-level README points at both.
- **Examples library completed** (`examples/`): new `ollama-quickstart`,
  `docker-compose-redis` (two replicas + Redis + nginx), `otel`
  (Collector → Jaeger), and `rag-pipeline` (embed → retrieve → rerank → chat)
  examples, plus an `examples/README.md` index. Placeholders only.
- **Examples are tested, not decorative**: `tests/test_examples.py` loads every
  example's registry/keys/env through the real loaders, validates every
  `docker-compose*.yml` against an image allow-list, and runs the new examples'
  requests against the fake backend. `tests/test_links.py` checks every relative
  link in `docs/` and `examples/`.

- **Reliability v2** (ADR 0017) — resolves every item ADR 0009 deferred. All knobs
  are opt-in and default to v0.5.0 behavior (the v0.5.0 test suite passes unchanged).
  - **Ejection cap** `BROKER_MAX_EJECTION_PERCENT` (Envoy `max_ejection_percent`):
    caps the fraction of a pool that may be benched at once; when exceeded, the
    soonest-to-recover members are kept eligible so a correlated failure can **never
    strand** the pool. `0` = no cap.
  - **Exponential ejection growth + decay** `BROKER_COOLDOWN_BACKOFF_MULTIPLIER`,
    `BROKER_COOLDOWN_MAX_SECONDS`, `BROKER_COOLDOWN_DECAY_SECONDS`: a member ejected
    N times in a row is benched for `cooldown_seconds * multiplier**(N-1)` (capped),
    decaying after sustained success. `multiplier=1.0` = fixed window. The ejection
    count is surfaced as `cooldown.ejections`.
  - **Single-trial half-open** `BROKER_COOLDOWN_HALF_OPEN_SINGLE_TRIAL`
    (+ `BROKER_COOLDOWN_HALF_OPEN_TTL_SECONDS`): admits exactly one probe to a
    recovering member via the StateStore half-open lock; concurrent requests skip it
    until the trial resolves. Default off.
  - **Per-error retry/cooldown policies** (`broker/reliability.py`): each failure is
    classified (`timeout`/`connection`/`server_error`/`backpressure`/`client_error`)
    and `BROKER_RETRY_ON_*` / `BROKER_COOLDOWN_ON_*` decide retry/cooldown per class.
    Every HTTP adapter now raises a **typed upstream error** (carrying the upstream
    status + `Retry-After`) when the backend responds with `>= 400`, so the policy
    keys on the real status: `408`→timeout, `429`→backpressure, other `4xx`→client_error,
    `5xx`→server_error. **4xx client errors are never retried, failed over, or counted.**
    An upstream `Retry-After` (RFC 9110) triggers immediate failover + a throttle
    bench; sleep only when no other member, clamped to the new
    `BROKER_RETRY_AFTER_MAX_SECONDS` (default 120s) and the request deadline — so even
    `BROKER_REQUEST_TIMEOUT_SECONDS=0` can't stall unbounded.
    `BROKER_RETRY_MAX_ATTEMPTS_BY_CLASS` sets per-class attempt caps. Defaults
    reproduce ADR 0009's failover set.
  - **Redis sticky global LRU bound** `BROKER_REDIS_STICKY_MAX_ENTRIES`: a
    sorted-set index scored by last pin caps total sticky entries per Redis prefix,
    evicting least-recently-pinned on overflow (`ZPOPMIN`). `0` = TTL-only.
  - `GET /status` gains a `reliability` block (incl. `retry_after_max_seconds`); the
    `cooldown` JSON in `/status` + `/healthz/backends` gains `ejections`, `recovering`,
    and (when single-trial half-open is on) `half_open_trial`.
  - `examples/reliability/` — a runnable, GPU-free demo (multi-member fake pool +
    tuned settings + README showing how to observe cooldowns in `/status` and
    `/healthz/backends`).
- **ADR 0018** (`docs/adr/0018-cache-aware-v2.md`) + research note
  (`docs/research/adr-0018-cache-aware-v2.md`): resolves every ADR 0010 deferral —
  queueing-model bench + re-validated β (implemented), cross-replica Redis LRU
  (implemented), and output-length/queue-aware load signal, token-ID hashing, precise
  KV-event index, and Preble autoscaling (all **Rejected** with rationale/data).
- **`examples/cache-aware-routing/`**: a runnable 3-member pool example (registry,
  `.env`, README with bench + `/status` walkthrough), tested against the fake backend.
- **Governance v2** (ADR 0016) — resolves every deferred per-key governance item:
  - **Selectable rate-limit algorithms.** `BROKER_RATE_LIMIT_ALGO` =
    `sliding_log` (exact, default) or `gcra` (atomic GCRA/token-bucket with
    per-key burst via `limits.rpm_burst`), backed by a new atomic
    `StateStore.gcra` primitive (in-process + Redis Lua, single round-trip).
    `BROKER_RATE_LIMIT_TPM_ALGO` = `fixed` (default) or `sliding` (weighted
    two-bucket sliding-window counter).
  - **Per-model limits** — `limits.per_model: {model: {rpm, tpm}}` enforced on
    the resolved model incl. the `auto` pick and every cascade hop.
  - **Hierarchical tenant budgets** — a `tenants:` block in `keys.yaml` caps
    aggregate daily/monthly token + cost across all keys of a tenant.
  - **Per-key placement** — `limits.placement` unioned (fail-closed) into the
    request placement requirement (`403 placement_denied`).
  - **Key rotation** — `python -m broker.keys rotate <id> --grace-days N`
    (integer `0..365`) mints a successor with a grace overlap (`0` = revoke now).
    Rotation sets the old expiry to `min(existing, today+N)` so it can only
    **shorten** a key's life, and is **refused on a revoked/expired key**
    (`409 key_not_active`) so a dead key is never resurrected (CLI parity).
  - **Admin key-management API** — `GET/POST /admin/keys`,
    `POST /admin/keys/{id}/rotate`, `DELETE /admin/keys/{id}` (admin-scope only,
    secret shown once, never logged). Create/rotate inputs are validated
    (`grace_days`, ISO non-past `expiry`, charset/length-capped `tenant`/`label`)
    → `400` rather than a stored dead key or a `500`; oversized bodies → `413`.
    `keys.yaml` writes are atomic (temp file + `fsync` + `os.replace`) under an
    exclusive `flock` on a `keys.yaml.lock` sidecar, created `0600` (stricter
    existing mode preserved); a reload over a corrupt file keeps the last good key
    set and logs an error.
  - Governance now **fails closed** on a `StateStore` error (request denied).
  - **Rejected:** retrievable/encrypted keys — hash-only at rest is the security
    property; rotation covers the operational need.
- CLI `broker.keys create` gains `--rpm-burst`, `--placement`, `--per-model`;
  new `rotate` subcommand.
- **Router v2** (ADR 0019), resolving every deferred item from ADR 0011. All new
  behavior is **opt-in and default-off** — a request/registry with none of the new
  knobs reproduces v0.5.0 exactly.
  - **TTFT estimation.** The usage log records **time-to-first-token** (`ttft_ms`)
    for streaming requests via a backward-compatible additive schema migration (an
    existing `usage.sqlite3` is upgraded in place; old rows keep NULL). The
    latency-budget router uses the **TTFT** p90 for a **streaming** request and the
    **end-to-end** p90 otherwise (`UsageLog.latency_percentiles(metric=...)`).
  - **Calibrated cascade reliability scorer** (`router.cascade.scorer`, opt-in).
    Reduces the cheap hop's answer to one reliability score in `[0,1]` — graded
    length + optional token-logprob features with hard refusal/truncation gates —
    and escalates when `score < threshold`, giving one tunable knob in place of the
    booleans (which remain the default). The trained FrugalGPT-style scorer was
    **rejected** (needs labeled data + a model); see ADR 0019.
  - **Buffered streaming cascade** (`router.cascade.stream_mode: buffer`, opt-in).
    The chat endpoint buffers the cheap hop, scores it, then **replays** it as a
    stream or **streams the stronger hop live** — never mixing two models' output in
    one stream. Per-key scope (ADR 0008) and per-model limits (ADR 0016) are enforced
    on **both** hops. Default `fallback_single` (= v0.5.0); completions/responses keep
    `fallback_single`.
  - **Heuristic code/math/reasoning accuracy via structural features.** Instead of
    growing keyword lists (which over-fit), the scorer reads the **structure** of a
    request: code-like punctuation / file extensions / CLI-tool & language names /
    error shapes; digit-operator/fraction/`prime`/`sum of` math phrasing; and
    `why`/`prove`/`plan`/`trade-off`, syllogisms, and long multi-constraint
    questions. Keyword lists are reverted to v0.5.0 (minus the over-broad `"write a"`
    creative token). Accuracy is reported by **independence**, with the **blind** set
    (a 44-prompt set the implementer never saw, `tests/data/router_eval_blind.jsonl`,
    authored by the orchestrator) as the **headline**: v0.5.0 → now **code 0.08 → 0.75,
    math 0.00 → 0.70, reasoning 0.00 → 0.44**, chat/creative 1.0. The earlier
    "independent" set (`router_eval_independent.jsonl`, code 0.92 / math 0.90 /
    reasoning 1.00) was **seen during the fix**, so it is labeled as such and is not
    the headline. In-sample PGR stays **< 1.0** (not saturated). Keyword heuristics
    generalize only partially — the docs point to the LLM router backend / cascade
    for hard routing; per-feature unit tests, the frozen disjoint sets, and the
    honest per-task floors are enforced in tests.
  - **`router.weights.cost_normalized`** (default **true**; `false` = v0.5.0
    scoring) and a `--preload` option for `scripts/router_eval.py` (warm-state
    evaluation).
  - Example: `examples/auto-router/` (registry with tags/cost tiers + cascade +
    scorer + buffer + latency budget + placement, a README, and a tested pytest).
- **`POST /v1/rerank`** (ADR 0013): a Cohere/Jina-compatible reranking endpoint.
  Request `{model, query, documents (strings or {text} objects), top_n?,
  return_documents?}`; response `{id, object:"rerank", model, results:[{index,
  relevance_score, document?}], usage}` — sorted by score descending, `index` is
  the original ordinal, `document` echoed (as `{text}`) only when
  `return_documents` is true. Routed by model name (no `auto`) through the same
  pool/lazy-load/concurrency/failover machinery as `/v1/embeddings`, with per-key
  scoping, placement, usage logging, and response-cache eligibility
  (always-deterministic). **vLLM** and **llama.cpp** reranker models are served by
  an HTTP passthrough to their `/v1/rerank`; the **fake** backend scores by
  deterministic lexical overlap so CI covers the endpoint. **Ollama and Strata
  rerank return 400** (Ollama has no native rerank endpoint and the broker refuses
  the embeddings-cosine substitute; Strata rerank is unverified).
- **`supports_rerank`** registry capability (default `false`), surfaced in
  `/v1/models` capabilities; requesting rerank on a model without it returns 400.
- **`BROKER_RERANK_MAX_DOCUMENTS`** (default `1000`): caps the documents count on
  `/v1/rerank` (Cohere recommends ≤1,000); the body is still bounded by
  `BROKER_MAX_REQUEST_BYTES`.
- **Semantic (embedding-similarity) response cache** (ADR 0014):
  `BROKER_SEMANTIC_CACHE_*`, default **off**. A second cache layer consulted after
  an exact miss for chat/completions/responses — it embeds the prompt via the
  broker's own `/v1/embeddings` path and serves the closest stored response whose
  cosine similarity clears `BROKER_SEMANTIC_CACHE_THRESHOLD` (default **0.95**,
  precision-favoring). **Strict scoping**: key/tenant + placement + namespace +
  resolved model + endpoint + every non-prompt param + **the system prompt and the
  whole conversation history** must match exactly — only the **last user turn's
  text** is fuzzy — so a semantic hit never crosses a scope, system prompt, or
  history boundary. Pure-Python cosine over a bounded per-bucket index (no numpy),
  with the **number of buckets** globally bounded
  (`BROKER_SEMANTIC_CACHE_MAX_BUCKETS`, default 1024); in-process or Redis via the
  StateStore. A semantic hit returns `x-broker-cache: semantic-hit` +
  `x-broker-cache-similarity`. The layer stores only under the **served** model (a
  cascade escalation to a different model is not stored). **Lazy-load invariant
  preserved**: with `BROKER_SEMANTIC_CACHE_REQUIRE_RESIDENT=true` (default) the
  cache is consulted only when the embedding model is already resident, so a chat
  request never silently loads an extra model (`false` allows on-demand loading).
- **Streaming cache replay** (ADR 0014): `stream: true` requests now consult the
  cache (exact + semantic) and, on a hit, return a well-formed SSE replay
  (`chat.completion.chunk` / `text_completion` / Responses events + `[DONE]`)
  instead of bypassing. Only fully-successful **non-streaming** responses populate
  the cache; streaming reads but never writes.
- **Per-request cache controls** (ADR 0014): `x-broker-cache-ttl: <seconds>`
  clamps this request's stored TTL — finite, `>= 0`, may only **shorten** the
  operator TTL; `0` means do not store; non-finite / overflowing values → `400`.
  `x-broker-cache-namespace: <token>` partitions/invalidates the caller's cache
  scope; `Cache-Control: max-age=0` is a bypass. `s-maxage` and request
  `max-age>0` freshness are rejected (no stored-age /revalidation protocol).
- **Opt-in span content capture** (ADR 0014): `BROKER_TRACING_CAPTURE_CONTENT`
  (default off) + `BROKER_TRACING_CONTENT_MAX_CHARS` (default 2000). When tracing
  is enabled and this is set, the `inference` span carries truncated
  `gen_ai.input.messages` / `gen_ai.output.messages` (OTel GenAI `spanOnly` mode).
  Off by default — prompts/responses may contain PII; disabled is byte-for-byte
  v0.5.0.
- **Optional web UI / operator playground at `/ui`** (ADR 0015):
  `BROKER_UI_ENABLED` (default **off**) now serves a hardened, fully-offline
  single-page app — vanilla HTML/CSS/JS shipped as package data, **no CDN, no
  external URLs, no build toolchain, no new runtime dependency**. Features: model
  list (capabilities/pool/candidates), chat playground with SSE streaming over
  `fetch()` (shows `x-broker-route`/`x-broker-cache`/rate-limit headers),
  completions/responses/embeddings tabs, a rerank tab that hides itself unless
  `/v1/rerank` exists, `/status` and scoped `/usage` views, and an admin panel
  (list/load/unload/reload, with confirmations) shown only when an admin key is
  supplied. Every `/ui` response carries a strict CSP (`default-src 'self'`, no
  `unsafe-inline`/`unsafe-eval`, `frame-ancestors 'none'`), `X-Content-Type-
  Options: nosniff`, and `Referrer-Policy: no-referrer`. The operator's API/admin
  keys are held in browser memory only (never `localStorage`/`sessionStorage`/
  cookies). The UI drives the **existing authenticated API** and adds **no new
  unauthenticated endpoints**. Asset serving never raises on hostile paths
  (embedded NUL, control chars, backslashes, over-long segments/paths): each is
  rejected before any filesystem access and returns a hardened 404 **with the
  full security headers**. Disabled (the default) → `/ui` 404s and nothing
  changes vs v0.5.0. Docs: `docs/web-ui.md`; example: `examples/web-ui/`.

### Changed

- **`auto` scoring: `router.weights.cost_normalized` now defaults to `true`** (ADR 0019).
  With the cheapest model already loaded (the normal live state), v0.5.0's
  un-normalized cost term plus `prefer_loaded` sent code/math/reasoning prompts to
  it — blind-set accuracy 0.36 vs 0.75 cold. Normalized cost keeps 0.75 warm or
  cold with no chat/creative regression. Set it `false` to restore v0.5.0 scoring.
- **Cache-aware p95-SLO capacity wording corrected to be seed-honest**
  (`docs/routing-and-health.md`): `cache_aware` trails `least_busy` by ~2–8%
  depending on the seed and is roughly on par with `round_robin` (they tie in
  `cache_pressure`/`long_lived` under some seeds), well ahead of `sticky`. The
  earlier "~2–5%" / strict-ordering claim did not hold across seeds; figures are
  now labeled as seed-dependent simulations.
- **Upstream HTTP errors are surfaced by status instead of collapsed to `502`**
  (ADR 0017). A backend 4xx (e.g. a context-length `400`) now returns that `4xx`
  with `error.type` `upstream_client_error`, is called exactly once, and never
  benches a pool member — previously it became `502 backend_unavailable` and was
  retried across every member (the behavior in v0.5.0 and earlier). An upstream
  `429`/`503` now fails over to a healthy member immediately and benches the
  throttled one. Transport failures (connect/read timeout, unreachable) remain
  `502`. See `docs/api-reference.md#upstream-backend-errors-adr-0017`.
- **Cache-aware routing: default `BROKER_CACHE_AWARE_BETA` changed `0.25` → `0.5`**
  (ADR 0018). The ADR 0010 benchmark modeled no queueing, so its default was chosen
  from a balance-only heuristic. `scripts/bench_routing.py` now runs a **discrete-event
  queueing simulation** of continuous-batching pool members (per-member `nmax` slots,
  `t_iter(n)=W+H·n`, prefill ∝ uncached prefix tokens, decode ∝ output tokens,
  **TTFT = queue wait + prefill**), reporting TTFT **p50/p95/p99 incl. queueing**,
  **prefill work (uncached tokens computed)**, throughput, and per-member utilization.
  The default is re-derived against the right baseline — **`least_busy`, the
  tail-latency-best strategy — at a moderate (≤85% util) load, not one saturated
  point**: `β=0.5` is the smallest β that holds the 1.25× low-pressure balance bound
  *and* stays within 10% of `least_busy` on p95 in every scenario (matching the
  llm-d/KServe ≈2:1 prefix:load weighting), while `β=0.25` fails both (spread ~1.5×,
  +13% p95 in cross_session). `sticky` remains the default *strategy*; this only affects
  deployments that opted into `cache_aware` without setting β. New bench modes:
  `--sweep` (all 4 strategies at ~50/70/85/95% util + SLO-capacity bisection),
  `--select-beta`, `--load-signal`.
- **Cache-aware routing docs corrected to honest conclusions** (ADR 0018 follow-up):
  `least_busy` has the best p95 TTFT and highest p95-SLO capacity in every simulated
  scenario; `cache_aware`'s benefit is **fewer uncached prefill tokens (GPU work, 20–48%
  less) and higher hit rate**, not tail latency (it stays within ~10% of `least_busy`).
  Added a "when to use which strategy" guide; the earlier "doubles hit rate" wording is
  weakened to "materially higher" (cache_pressure 1.4×, cross_session 1.2×). All figures
  labeled *simulated*.
- **Cache-aware prefix index size bound is now cross-replica under Redis** (ADR 0018):
  a new `StateStore` sorted-set LRU (`zlru_touch`/`zlru_trim`/`zlru_size`) backs the
  `BROKER_CACHE_AWARE_MAX_ENTRIES` bound, so N replicas sharing Redis keep one shared
  bound instead of N× it (the in-process backend is unchanged). The TTL remains a
  second, time-based bound.

### Fixed

- **Usage log on a fresh local checkout:** the usage DB's parent directory is now created on demand, and an unwritable path logs a warning naming `BROKER_CACHE_DIR` / `BROKER_USAGE_LOG_PATH` instead of silently disabling the log. The tutorial sets `BROKER_CACHE_DIR=./.cache`.
- **`/v1/rerank` strict upstream validation** (ADR 0013 follow-up): a malformed
  reply from a vLLM/llama.cpp reranker is now a `502` (`backend_unavailable`)
  instead of being passed through (`200`) or crashing (`500`). The broker
  requires every result to carry a non-bool integer `index` in
  `[0, len(documents))` that is unique across the response, and a finite numeric
  `relevance_score`; out-of-range/negative/duplicate/non-integer/boolean indices,
  non-numeric or `NaN`/`Infinity` scores, non-JSON bodies, and missing/wrong
  `results` arrays all fail closed. A malformed body is never partially emitted,
  silently coerced, or written to the response cache, and it participates in pool
  failover/cooldown. The response `id` is always broker-generated
  (`rerank-<request_id>`); the upstream `id` is no longer echoed. A shared
  non-JSON guard also hardens the chat and (non-stream) embeddings passthroughs
  against the same latent `500`. Reconciled with ADR 0017: upstream HTTP errors on
  `/v1/rerank` now raise the typed `UpstreamHTTPError` (classified by status) like
  chat/embeddings, so a genuine upstream 4xx/429/503 follows the per-error policy.

### Security

- **Cache TTL / growth hardening** (ADR 0014 follow-up): the per-request
  `x-broker-cache-ttl` may now only **shorten** the operator TTL and must be a
  finite number `>= 0`; `0` means "do not store this response" and non-finite /
  overflowing values (`nan`/`inf`/`-inf`/`1e400`) are rejected with `400` — an
  API-key holder can no longer store non-expiring entries or override the
  operator's ceiling, and every Redis write carries a finite TTL whenever the
  operator TTL is finite. The semantic index now bounds the **number of buckets**
  (`BROKER_SEMANTIC_CACHE_MAX_BUCKETS`, LRU), not only entries per bucket.
- **Semantic cache context isolation** (ADR 0014 follow-up): only the **last user
  turn's text** is matched fuzzily; the system prompt, earlier conversation
  history, and any non-text content parts are part of the exact bucket key, so a
  long shared history can no longer push similarity over the threshold and serve an
  answer generated for a different system prompt or context.
- **Served-model cache scoping** (ADR 0014 follow-up): a response is stored only
  when the served model equals the model the request keyed on; a cascade that
  escalated to a different model is not stored under the primary's key.

### Notes

- **Defaults reproduce v0.5.0 byte-for-byte** (`sliding_log` rpm, `fixed` tpm, no
  per-model/tenant limits, no per-key placement; governance inert with no keys
  file). The `[dev]` extra now pulls `fakeredis[lua]` to exercise the Redis Lua
  path in tests.
- All ADR 0014 cache/observability capabilities are **opt-in and default-off**;
  defaults reproduce v0.5.0. No new mandatory dependency (semantic cosine is pure
  Python). ADR 0014 resolves every deferred item from ADR 0012 (now marked
  *Extended by ADR 0014*).
- **Integration (ADRs 0013–0019 reconciled on one branch):** the `auto`-router
  accuracy headline is now the **blind** eval set (`tests/data/router_eval_blind.jsonl`,
  44 prompts the implementer never saw; code 0.75 / math 0.70 / reasoning 0.44,
  chat & creative 1.0) — the earlier "independent" set is relabeled *seen during fix*
  and kept as a lower-trust column (`docs/auto-router.md`). Added a combined
  fake-backend end-to-end test across every feature 0006–0019
  (`tests/test_e2e_all_features.py`) and a config/env/docs completeness test (every
  `BROKER_*` setting appears in `.env.example` and `docs/configuration.md`). No
  version bump and no behavior change — docs/tests only.

## [0.5.0] - 2026-10-19

### Added

- **Retries with full-jitter backoff** (ADR 0009): `BROKER_RETRY_MAX_ATTEMPTS`,
  `BROKER_RETRY_BACKOFF_BASE_MS`, `BROKER_RETRY_BACKOFF_MAX_MS`. Retries re-select
  a member **within the requested model's pool only** (never a different model),
  wait `uniform(0, min(max, base·2^n))` between attempts, respect the request
  timeout as a deadline, and never retry once a stream is emitting. **Defaults
  (`0`/`0`) reproduce v0.4.0 exactly** — one failover pass, no sleep.
- **Member cooldowns / circuit breaker** (ADR 0009): `BROKER_COOLDOWN_FAILURES`,
  `BROKER_COOLDOWN_SECONDS`. After N consecutive failures a pool member is benched
  and skipped; after the window it half-opens for a trial (success resets, failure
  re-benches). Visible per member in `GET /status` and `GET /healthz/backends`.
  Default-off preserves current behavior.
- **`StateStore` abstraction** (`broker/state.py`, ADR 0009): one async API over
  the sticky map, counters (+ sliding-window helper), cooldown records, and a
  generic bytes/JSON KV. `InProcessStateStore` is the default; **optional
  `RedisStateStore`** (`BROKER_REDIS_URL`, `BROKER_REDIS_PREFIX`,
  `BROKER_ROUTE_STICKY_TTL_SECONDS`) shares state across replicas. `redis` is an
  optional extra (`pip install 'backend-agnostic-broker[redis]'`), import-guarded
  → `ConfigError` if enabled-but-missing. One shared `StateStore` instance backs
  the sticky map, the ADR 0008 governance counters, the ADR 0010 prefix index, and
  the ADR 0012 response cache.
- **Cache-aware pool routing** (ADR 0010): opt-in `BROKER_ROUTE_STRATEGY=cache_aware`
  routes each request to the pool member that already holds its prompt **prefix**
  (warm KV cache), scoring `score = matched_fraction - beta·relative_load` and
  falling back to `least_busy` when no member holds any prefix. Prompts are
  normalized and sliced into fixed-size **chained** character blocks
  (`BROKER_CACHE_AWARE_BLOCK_CHARS`); the prefix→member index lives on the
  `StateStore` (shared via Redis when configured), LRU-bounded
  (`BROKER_CACHE_AWARE_MAX_ENTRIES`) with a TTL (`BROKER_CACHE_AWARE_TTL_SECONDS`).
  `BROKER_CACHE_AWARE_BETA` is the single cache-vs-load knob (**default `0.25`**,
  tuned from the benchmark — `1.0` over-penalized affinity);
  `BROKER_CACHE_AWARE_USE_METRICS` lets the **non-loading** health probe scrape
  vLLM `/metrics` (`vllm:num_requests_waiting`) for a real load signal (falls back
  to in-flight counts). Default (`sticky`) behavior is unchanged.
  `scripts/bench_routing.py` ships four seeded scenarios (long-lived, cache
  pressure, cross-session sharing, skewed/bursty) with a `--matrix` scenario×β mode
  and a `--capacity-sweep`, and runs without `PYTHONPATH`; `docs/routing-and-health.md`
  records the matrix, the default-β selection rule, and a "when to use cache_aware
  vs sticky" guide (cache_aware wins under prefix reuse + cache pressure, `sticky`
  wins under bursty skewed load).
- **Auto-router evolution (ADR 0011)** — four opt-in, default-off routing features;
  with none configured, `auto` behaves exactly as v0.4.0:
  - **Cascade mode** (`router.mode: cascade`): serve the cheapest capable model
    first, inspect the actual answer, and escalate **once** to a stronger capable
    model on a data-driven signal (`finish_reason=length`, empty/too-short output,
    or a refusal regex — all in `router.cascade`). Both hops are recorded (usage +
    decision log) and the response header `x-broker-route` shows the path
    (`tiny->coder`). Streaming falls back to the single strong pick (documented).
  - **Latency budget**: `x-broker-latency-budget-ms` header (+ registry
    `router.latency_budget_ms` / `BROKER_ROUTER_LATENCY_BUDGET_MS`) picks a faster
    capable model when the pick's estimated latency (usage-log p90 + cold-start
    penalty) exceeds the budget; never overrides capability/placement.
  - **Placement / data-residency pinning (fail-closed)**: per-model `placement`
    labels + `x-broker-placement` header and `BROKER_DEFAULT_PLACEMENT`; candidates,
    explicit models, and pool members are filtered to those carrying all required
    labels, else `403 placement_denied` — never spills to a non-compliant model.
  - **Eval harness**: `scripts/router_eval.py` + `tests/data/router_eval.jsonl`
    (45 labeled prompts) computes accuracy, cost-weighting, and RouteLLM
    PGR/APGR/CPT — PGR against a true quality ceiling (perfect-oracle accuracy,
    not the most-expensive model), APGR/CPT swept from the router's own
    cost-penalty curve (≥10 points) with the oracle label frontier reported
    separately as an upper bound; PGR is raw (no clamping) and degenerate/out-of-
    range values are flagged `invalid` with a reason. Markdown/JSON report; runs
    offline on the heuristic backend.
  - New settings: `BROKER_ROUTER_LATENCY_BUDGET_MS`, `BROKER_ROUTER_LATENCY_WINDOW_SECONDS`,
    `BROKER_ROUTER_COLD_START_MS`, `BROKER_DEFAULT_PLACEMENT`. New error type
    `placement_denied` (403); usage log gains `latency_percentiles`.
    See [docs/auto-router.md](docs/auto-router.md),
    [ADR 0011](docs/adr/0011-auto-router-evolution.md), and the research note.
- **Per-key governance: virtual keys, rate limits & budgets** (ADR 0008). Opt-in
  via `BROKER_KEYS_PATH` (a git-ignored `keys.yaml` of **hashed** virtual keys;
  manage with `python -m broker.keys create|list|revoke`, secret shown once).
  Each key has an id/label/tenant/scope (`user`|`admin`), optional expiry, and
  limits: `rpm` (sliding window), `tpm` (windowed, pre-dispatch estimate +
  post-response reconcile with real usage), `max_concurrency`, daily/monthly
  **token budgets**, optional **cost budgets** (registry `pricing` block, USD per
  million tokens), and a fail-closed `allowed_models` list (applies to the model
  the `auto` router picks and to each cascade hop). Counters live on the ADR 0009
  `StateStore` (in-process or Redis). Enforced before dispatch on
  chat/completions/responses/embeddings with OpenAI-compatible errors: `429`
  `rate_limit_exceeded` + `Retry-After` + `x-ratelimit-limit/remaining/reset-{requests,tokens}`
  headers (also emitted on success), `429` `insufficient_quota` for budgets, and
  `403` `model_not_allowed`. `GET /usage` is now **scoped** — a `user` key sees
  only its own usage; an `admin` key sees all keys plus a per-key spend breakdown.
  Legacy `BROKER_API_KEYS` keep working as **unlimited user keys**; with no keys
  file and no limits, behavior is identical to v0.4.0. New settings
  `BROKER_KEYS_PATH`, `BROKER_RATE_LIMIT_WINDOW_SECONDS`; new registry `pricing`
  block; `keys.yaml` git-ignored (`keys.example.yaml` tracked).
- **Exact response cache** (ADR 0012, default off): `BROKER_RESPONSE_CACHE_ENABLED`
  with TTL / `MAX_ENTRIES` / `MAX_BYTES` / `DETERMINISTIC_ONLY`. Identical requests
  to the same model are served from a StateStore-backed cache (in-process or
  Redis), skipping inference. Key = hash of (API-key/tenant scope, public model
  **after `auto` resolution**, normalized messages/prompt/input + sampling +
  tools + `response_format`), so a cached answer **never crosses a key/tenant
  boundary**. Deterministic requests only by default (`temperature == 0` or an
  explicit `seed`); embeddings always. Errors/partials never cached; streaming
  bypasses. Bypass via `Cache-Control: no-cache`/`no-store` or
  `x-broker-cache: bypass`; every response carries `x-broker-cache: hit|miss|bypass`.
  Covers `/v1/chat/completions`, `/v1/completions`, `/v1/responses` (non-stream),
  `/v1/embeddings`. Semantic cache deferred.
- **Admin runtime control** (ADR 0012, `BROKER_ADMIN_ENABLED`, default off): behind
  a dedicated `BROKER_ADMIN_API_KEYS` gate (`require_admin`). `GET /admin/models`
  (state, inflight, last-used), `POST /admin/models/{alias}/unload` (refuse on
  in-flight, or `?drain=true` to drain first), `POST /admin/models/{alias}/load`
  (explicit preload — the only non-request load path), `POST /admin/registry/reload`
  (transactional hot-reload: validate-before-apply, drain changed/removed models,
  invalid registry → no change + structured error). Admin actions are audit-logged.
- **OpenTelemetry tracing** (ADR 0012, `BROKER_TRACING_ENABLED`, default off):
  optional `otel` extra (`pip install 'backend-agnostic-broker[otel]'`). Spans
  (`auto_route`→`load_model`→`inference`→`cache_lookup`) carry `gen_ai.*`
  attributes per the GenAI semantic conventions; **no prompt/response content**.
  OTLP exporter configured via standard `OTEL_*` env vars. No-op + zero overhead
  when disabled; `ConfigError` if enabled but the package is missing.
- **Web UI/playground**: `BROKER_UI_ENABLED` reserved; the UI itself is **deferred**
  (ADR 0012, roadmap item 12).

### Changed

- `GET /status` now reports `state_backend`, `retry_max_attempts`,
  `cooldowns_enabled`, a `cache_aware` block (`enabled`, and when active
  `index_size`/`lookups`/`hits`/`hit_rate`/`block_chars`/`beta`), `tracing_enabled`,
  a `response_cache` stats block, and (when cooldowns are on) per-model `cooldown`.
- `GET /healthz/backends` entries now include a per-member `cooldown` object.
- `GET /usage` responses now include a `scope` field and (for `user` scope) the
  caller's `api_key_id`; `admin` scope adds a `by_key` breakdown.

### Security

- **Auth no longer 500s on a non-ASCII API key.** `hmac.compare_digest` raises
  `TypeError` on a non-ASCII `str`, so a request presenting a key with a non-ASCII
  (e.g. latin-1) byte in `Authorization`/`X-API-Key` previously returned **500**
  instead of **401** on the unauthenticated-reachable auth path (pre-existing in
  v0.4.0 `broker/auth.py`, and the same class in the new virtual-key verification).
  Both paths now compare the UTF-8/surrogateescape **byte** encodings (and reject
  absurdly long keys early), so malformed keys fail closed with a clean `401`.
  Regression tests cover non-ASCII and over-long keys on the data-plane auth path.

## [0.4.0] - 2026-10-18

### Added

- **`POST /v1/completions`** (legacy) and **`POST /v1/responses`** (OpenAI
  Responses API), implemented as translation shims over the chat pipeline — they
  inherit routing, pools, lazy-load, failover, and the `auto` router; streaming +
  non-streaming. Unknown models still 404 (no silent fallback). See
  [ADR 0006](docs/adr/0006-openai-endpoint-surface.md).
- **`POST /v1/embeddings`** via a new `embeddings()` adapter method — Ollama native
  `/api/embed`, OpenAI-compatible passthrough for vLLM/llama.cpp, deterministic
  fake for CI. Routes by model name through pool/lazy-load/failover; chat-only
  models return 400. Verified end-to-end against a live Ollama backend
  (nomic-embed-text, 768-dim). See [ADR 0006](docs/adr/0006-openai-endpoint-surface.md).
- **Request usage/decision log** (SQLite) + **`GET /usage`**: per-request token
  counts, latency, requested-vs-served model, endpoint, and a non-reversible API-key
  fingerprint (metadata only, never content). Config: `BROKER_USAGE_LOG_ENABLED`,
  `BROKER_USAGE_LOG_PATH`. See [ADR 0007](docs/adr/0007-usage-accounting-store.md).

### Docs

- Competitive-landscape research ([docs/research/competitive-landscape.md](docs/research/competitive-landscape.md))
  and a [roadmap](docs/roadmap.md) with proposed ADR drafts 0008–0012 (per-key
  governance; routing reliability + optional Redis shared state; cache-aware pool
  routing; auto-router evolution — cascade/latency-budget/privacy-pinning/eval;
  caching + runtime control + OpenTelemetry).

## [0.3.1] - 2026-10-18

### Documentation

- Document the bundled-LLM router across the user-facing docs: README + Getting
  Started now cover the two published image variants (lean vs `-llm`) and GHCR
  pulls; `docs/docker.md` adds pulling, running, and swapping the bundled
  classifier; `docs/auto-router.md` adds running the LLM outside the container and
  verifying `via: llm` vs a heuristic fallback.
- Annotate the auto-router configuration surface in the shipped examples:
  `registry.example.yaml` documents every per-model router field and the full
  `router` block (policy, weights, overrides); `.env.example` describes every
  `BROKER_ROUTER_*` setting.

## [0.3.0] - 2026-10-18

### Added

- **`auto` router alias**: a tiny classifier that, given a request plus the live
  candidate list, routes to the single best model id. Only the `auto` alias is
  affected. Pipeline: derive flags (images/tools/json/ctx) → Stage-1 capability
  filter → data-driven hard overrides → classifier → validate → deterministic
  fallback → LRU decision cache.
  - Two backends: a zero-dependency **heuristic** scorer (default/fallback) and a
    CPU **llama.cpp GGUF** classifier constrained by a GBNF grammar to the exact
    candidate ids (`BROKER_ROUTER_BACKEND=auto|llm|heuristic|off`).
  - Model-agnostic: candidates are injected at inference time from the registry,
    so adding/removing a model changes routing with no retrain.
  - Customization is all data: per-model `tags`/`description`/`priority`/
    `cost_tier`/`router_eligible`, plus a registry `router` block
    (`default`, `policy`, `overrides`, `weights`).
  - `auto` is listed in `GET /v1/models`; `GET /status.router` shows backend,
    cache size, and recent decisions. Dockerfile `--build-arg WITH_ROUTER_LLM=1`
    bundles the CPU wheel + a small Q4 GGUF for a self-contained classifier.
  - Docs: [docs/auto-router.md](docs/auto-router.md); `BROKER_ROUTER_*` settings.

### Changed

- **Dockerfile refactored to 3 stages** (`pydeps` → `router` → `runtime`): the
  compile toolchain and build wheels now live only in build stages and the final
  image receives just the installed packages (relocatable prefix), so there is no
  wheels layer or compiler in the published image. Lean image ≈ 272 MB.
- **Release workflow publishes two image variants** on each tag: lean
  (`:X.Y.Z`, `:latest`) and the bundled-LLM router (`:X.Y.Z-llm`, `:latest-llm`,
  built with `WITH_ROUTER_LLM=1`). The LLM variant installs `libgomp1` for the
  compiled llama.cpp runtime and bundles a small Q4 GGUF.

## [0.2.0] - 2026-10-17

### Added

- **Sticky session routing** (now the **default** `route_strategy`): each session
  is pinned 1:1 to a pool member and reused across requests (warm cache / KV
  locality), switching members only on failure/unavailability (failover re-pins).
  Session key = `X-Session-Id` header, else the OpenAI `user` field, else client
  IP; new sessions are assigned the least-busy member so distinct sessions still
  spread. Bounded LRU map via `BROKER_ROUTE_STICKY_MAX_ENTRIES` (default 10000).
  `GET /status` now reports `route_strategy` and `sticky_sessions`.

### Changed

- `BROKER_ROUTE_STRATEGY` default changed from `least_busy` to `sticky`;
  `least_busy` and `round_robin` remain available. (A request with no derivable
  session key behaves like `least_busy`.)

### Added (load balancing & health — earlier this release)

- **Load-balancing pools**: registry entries sharing a `pool` name form one
  logical model; requests are routed across members per `BROKER_ROUTE_STRATEGY`
  with the in-flight slot reserved at selection so concurrent callers spread
  across hosts instead of piling onto one.
- **Automatic failover**: a request whose chosen pool member is unavailable
  transparently retries another member (before first byte for streams); failed
  members are marked unhealthy and excluded until they recover. No silent
  fallback to a *different* model.
- **Non-loading health surfaces**: `GET /healthz/backends` (live per-member
  liveness — Ollama via `/api/tags`, managed runtimes only while running,
  unloaded = `not_loaded`), a `healthy`/`last_health` field and `pools` map in
  `GET /status`, and a background liveness probe
  (`BROKER_HEALTH_PROBE_ENABLED` / `_INTERVAL_SECONDS` / `_TIMEOUT_SECONDS`) that
  marks a `ready` model `failed` when its backend has gone away so the next
  request reloads/reroutes.
- **`GET /api/ps`**: passthrough of Ollama `/api/ps` across daemons (deduped by
  base URL) showing models actually resident in memory — distinct from the
  broker's lifecycle state. Non-loading.
- Docs: [docs/routing-and-health.md](docs/routing-and-health.md); pool example in
  `registry.example.yaml`; new routing/health env vars in `.env.example`.

## [0.1.0] - 2026-10-17

Initial release.

### Added

- **Broker core**: OpenAI-compatible FastAPI service with lazy per-model loading,
  an explicit lifecycle state machine
  (`unloaded → loading → ready → busy → unloading → failed → stopping`),
  per-model load locking, global + per-model concurrency limits, idle-unload
  sweep, and LRU eviction past `max_loaded_models`.
- **Adapters**: uniform backend contract (`base.py`), shared HTTP adapter
  (`httpbase.py`), and adapters for `fake`, `ollama`, `vllm`, `llamacpp`, and
  `strata`. Ollama uses native `keep_alive`; vLLM/llama.cpp/Strata are
  broker-managed child processes. Streaming proxies emit exactly one terminal
  `data: [DONE]` (upstream duplicates are dropped).
- **API**: `GET /v1/models`, `POST /v1/chat/completions` (streaming +
  non-streaming), `GET /healthz`, `GET /readyz`, `GET /status`, `GET /metrics`,
  and an optional Ollama-style `GET /api/tags`. API-key auth (Bearer /
  `X-API-Key`), request IDs, structured errors, request-size and concurrency
  limits, timeouts, and cancellation.
- **Configuration**: typed, fail-fast `Settings` (env prefix `BROKER_`),
  declarative pydantic model registry (YAML/JSON), `.env.example`,
  `registry.example.yaml`. Registry files expand `${VAR}` / `${VAR:-default}`
  references from the environment at load time (parsed string values only), so
  deployment-specific endpoints (e.g. a remote Ollama daemon's URL) stay out of
  version control; `python -m broker` loads a local `.env` (stdlib-only,
  non-overriding, overridable by real env / Docker secrets).
- **Security**: executable allow-listing, model-path validation
  (traversal/symlink-safe), request-flag rejection, no-shell argv construction,
  and an SSRF guard (each registry `base_url` is auto-trusted); no silent
  fallback.
- **Docker**: hardened multi-stage image (non-root, read-only rootfs, dropped
  capabilities, healthcheck), base + GPU Compose files, Makefile, smoke test.
- **CI**: fork-safe PR workflow (lint/format/type/tests, Compose validation,
  secret scan, image build + smoke, SBOM, Trivy scan) and a release workflow that
  builds, smoke-tests, Trivy-scans, and then publishes the image to GHCR
  (`:<version>` + `:latest`) on semver tag push via the repo-scoped
  `GITHUB_TOKEN`.
- **Docs**: README, Getting Started guide, architecture overview, ADRs, API
  reference + OpenAPI spec, configuration + registry references,
  Docker/Compose/local-dev/testing/CI/secure-PR guides, backend lazy-loading
  guides, GPU/tensor-split/layer-split/MTP/cache guides, operations runbook,
  troubleshooting, security model, threat model, contributing guide, and an
  acceptance-gate evidence ledger (including a verified live remote-Ollama
  integration: models list, non-streaming + streaming completions, lazy-load,
  idle-unload).
- **Tests**: 71 unit/integration/API tests (config, registry incl. env-var
  substitution, lifecycle, adapters, API, auth, streaming, lazy-load, unload,
  security, docs-examples).

[0.1.0]: https://github.com/simeononsecurity/backend-agnostic-broker/releases/tag/v0.1.0
