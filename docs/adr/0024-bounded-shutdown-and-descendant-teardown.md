# ADR 0024: Bound shutdown with a deadline and tear down every runtime descendant

Status: Accepted
Date: 2026-10-21
Supersedes: —
Related: ADR 0020 (measured release), ADR 0021 (runtime identity + restart reconciliation),
ADR 0026 (device leases + stale-lease cleanup), ADR 0027 (lifecycle failure drills)

## Context

A production canary of v0.6.0 loaded a model on demand, served a request, and then did not
exit when its service manager stopped it. The broker stayed in its stop path until the
service manager killed the whole process tree. The forced kill skipped the broker's own
cleanup, so state that described the GPU as in use stayed behind until an external
reconciliation cleared it.

A local reproduction (a stand-in runtime that the broker spawns, timed from `SIGTERM` to
broker exit) showed the same results on v0.6.0 and on the v0.7.0 release candidate:

| Scenario | Broker exit after `SIGTERM` |
|---|---|
| Loaded model, runtime exits on `SIGTERM` | 0.2 s |
| Runtime ignores `SIGTERM` | 30.2 s (full `BROKER_SHUTDOWN_TIMEOUT_SECONDS` grace, then `SIGKILL`) |
| `SIGTERM` while a model is loading | 97 s: shutdown waits for the load to finish |
| A runtime child that called `setsid` | broker exits, **the child survives** and keeps its device memory |
| A stream open during `SIGTERM` | 0.2 s |

The causes in the code:

- `ModelManager.shutdown()` has no overall deadline.
- `ManagedModel.stop()` first takes `load_lock`, which an in-progress load holds for up to
  its startup timeout (often minutes for large models).
- The process-group kill (`killpg`) cannot reach descendants that started their own session
  or process group.
- The adopted-runtime path calls the synchronous `terminate_process_group()` from async code,
  so its grace period blocks the event loop.
- Nothing ties the broker's grace periods to the service manager's stop timeout (systemd
  defaults to 90 s).

## Decision

We will make shutdown deadline-driven and make teardown cover the runtime's whole descendant
tree.

1. **One shutdown budget.** A new setting, `BROKER_SHUTDOWN_BUDGET_SECONDS` (default 45,
   validated finite and > 0), bounds the whole stop: uvicorn's graceful phase, cancelling
   loads, terminating runtimes, and releasing the ledger, leases and owner lock. Runtimes
   are stopped concurrently; each runtime's `SIGTERM` grace is the smaller of
   `BROKER_SHUTDOWN_TIMEOUT_SECONDS` and the budget still remaining, and a single shared
   hard deadline (budget expiry) bounds the whole set. When the budget runs out, the
   broker sends `SIGKILL` to every remaining runtime and exits. The docs state the rule:
   service-manager stop timeout ≥ budget + 15 s. The example unit uses `TimeoutStopSec`
   and `KillMode=mixed`.
2. **Shutdown preempts loads on the first signal.** The first stop signal
   (`SIGTERM`/`SIGINT`) immediately cancels every in-progress load and stops
   admitting new ones, *concurrently with* uvicorn's graceful HTTP phase — not
   after it. A client request waiting on a cold load fails fast with a typed
   **503 `shutting_down`** (carrying a short `Retry-After`) instead of hanging
   until the budget expires. The spawning runtime is then torn down like any
   other and the model goes to `FAILED`/`UNLOADED`. `ManagedModel.stop()` never
   waits on `load_lock` without a bound.
3. **Bounded graceful HTTP phase.** uvicorn's `timeout_graceful_shutdown` is a
   small share of the budget — `min(10 s, budget / 3)` — so an open request or
   stream cannot hold up shutdown; the rest of the budget is left for runtime
   `SIGTERM` grace. The whole stop (graceful phase + runtime teardown) is drawn
   from one deadline stamped at the first signal, so the total from first signal
   to exit stays within the budget.
4. **Descendant-aware termination and reaping.** The kill set is the runtime's process group
   **plus every recorded descendant**, each identity-gated by PID + start time (ADR 0021):
   - On Linux, the broker makes itself a child subreaper (`prctl(PR_SET_CHILD_SUBREAPER)`), so
     descendants that leave the group are re-parented to it and can be found and reaped.
   - Elsewhere, a periodic parent-PID tree snapshot (argv-only `ps`) records descendants while
     the runtime is alive.
   - A process the broker cannot prove it started is never signalled.
   - After the tree is down, the re-parented descendants that became zombie children of the
     broker are reaped (`waitpid(pid, WNOHANG)`) — both inline in `terminate_tree` after an
     eviction/unload/shutdown and periodically in the idle sweeper — so a long-running broker
     that churns such runtimes does not accumulate zombies. Only the specific recorded/kill-set
     PIDs whose current parent is the broker are waited on; `waitpid(-1)` is never used (it
     would steal exit statuses from asyncio's child watcher for the runtimes the broker spawned).
5. **No blocking calls on the event loop.** Adopted-runtime termination becomes async.
6. **Escalation.** A second `SIGTERM`/`SIGINT` during shutdown triggers immediate `SIGKILL`
   of all runtime groups and descendants, then exit.
7. **Honest result.** Every stop produces a teardown result (signalled, exited, survivors,
   duration). This result is logged, counted in a metric and passed to ADR 0026, so a leftover
   runtime is never reported as released.

All of this is on by default: it is a bug fix to an existing invariant ("stopping the broker
stops what it started"), not a new feature.

## Alternatives considered

- **Leave teardown to the service manager (`KillMode=control-group`).** Rejected as the only
  mechanism. It is systemd-specific (Docker, launchd and bare runs get nothing), and a forced
  kill skips the broker's ledger, lease and lock cleanup, which is the stale-state problem
  itself. The docs keep it as recommended defense in depth.
- **Wait for loads to finish (status quo).** Rejected: a large model's startup timeout is
  longer than any sane stop timeout.
- **Kill only the process group (status quo).** Rejected: GPU runtimes and their wrappers
  start workers in new sessions, and the reproduction shows those survive.
- **cgroup v2 per runtime.** Rejected as a requirement: it needs delegation or root and is
  Linux-only. The subreaper plus identity-gated tree gives the same coverage for a non-root
  broker. It could be added later as an optional provider through a new ADR.

## Consequences

- **Positive:**
  - Stopping the broker completes within a known time on every platform.
  - Leftover processes are found and killed, or reported.
  - Cleanup code always runs before the service manager would have to force-kill.
- **Negative:**
  - A load in progress at shutdown is lost and repeats on the next start; the waiting client
    receives a 503 `shutting_down` (with `Retry-After`) rather than a completed response.
  - The subreaper changes how orphaned descendants are re-parented on Linux: the broker must
    reap them, which `terminate_tree` (inline) and the idle sweeper (periodic) now do.
  - Very slow-to-exit runtimes get `SIGKILL` sooner when the budget is short.
- **Neutral:** two settings (`BROKER_SHUTDOWN_BUDGET_SECONDS`, plus the existing
  `BROKER_SHUTDOWN_TIMEOUT_SECONDS`, which is now capped by the budget) and new teardown
  metrics and log fields.

## Confirmation

Implemented in `broker/teardown.py` (identity-gated `terminate_tree`, descendant
enumeration, Linux child-subreaper, and `reap_reparented`/`reap_broker_zombies` which
`waitpid(WNOHANG)` only the broker's re-parented zombie descendants, never `waitpid(-1)`),
`broker/proc.py` (`ProcessSupervisor` records descendants, passes its own asyncio child
PID so it is not reaped twice, and returns a `TeardownResult`), `broker/manager.py`
(budgeted `ModelManager.shutdown()` drawing one deadline from the first signal,
`begin_preemption()`/`_do_begin_preemption()` first-signal load cancellation, the
per-model `shutting_down` flag that turns a preempted/queued load into a typed 503
`ShuttingDownError`, force-escalation, and the idle-sweeper periodic reap), the adapter
`stop()` contract (`broker/adapters/`), and `broker/__main__.py` (subreaper install,
first-signal `begin_preemption`, uvicorn `timeout_graceful_shutdown = min(10 s, budget/3)`,
second-signal escalation).

Real-process drills (`tests/test_shutdown_teardown.py`, with the stand-in runtime
`tests/fixtures/drill_runtime.py`) spawn a real `python -m broker` and assert:

- `test_sigterm_with_loaded_runtime_exits_fast` — loaded runtime → exit < 2 s, 0 left.
- `test_runtime_ignoring_sigterm_is_killed_within_budget` — ignores `SIGTERM`, small
  budget → SIGKILL within budget, 0 left.
- `test_sigterm_during_slow_load_cancels_and_exits_within_budget` — `SIGTERM` during a
  slow load → load preempted at the first signal, exit **< 5 s**, 0 left.
- `test_sigterm_while_client_waits_on_cold_load_returns_503_fast` — a client blocked on a
  cold load with a 45 s budget → the client gets a fast **503**, the broker exits **< 5 s**
  (not a hang until the budget), 0 left.
- `test_escaped_setsid_descendant_is_torn_down` — escaped `setsid` child holding the
  inherited stdout pipe → killed (0 left; on macOS killed because recorded while its
  parent lived, else reported as a survivor).
- `test_open_stream_at_sigterm_exits_within_budget` — open stream → exit within budget.
- `test_second_sigterm_forces_immediate_kill` — second `SIGTERM` → fast forced exit.
- `test_evictions_reap_reparented_descendants_no_zombies` (Linux container gate) —
  alternating evictions (max 1 loaded model) with an escaped grandchild per runtime →
  every request served, **0 zombie children of the broker** afterwards, evicted
  grandchildren dead.

Unit/async tests (`tests/test_teardown.py`): identity gate, descendant enumeration,
real process-tree `terminate_tree`, the best-effort subreaper, an adopted-runtime stop
keeping the event loop responsive, and the reaping primitives — `reap_reparented`
reaps exactly a zombie child of this process (skips a skipped PID and a foreign PID),
and `reap_broker_zombies` sweeps all re-parented zombie children while honoring `skip`.
`tests/test_proc_release.py` covers the `TeardownResult` supervisor contract.

The Linux-specific descendant/reaping paths (`/proc`, subreaper, `waitpid`) also run in
the project's Python 3.11 container gate. ADR 0027 drills D1–D6 build on these fixtures.
