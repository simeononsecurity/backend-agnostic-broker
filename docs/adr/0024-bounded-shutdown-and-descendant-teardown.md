# ADR 0024: Bound shutdown with a deadline and tear down every runtime descendant

Status: Proposed
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

1. **One shutdown budget.** A new setting, `BROKER_SHUTDOWN_BUDGET_SECONDS` (default 45),
   bounds the whole stop: uvicorn's graceful phase, cancelling loads, terminating runtimes, and
   releasing the ledger, leases and owner lock. Each runtime's `SIGTERM` grace is the smaller
   of `BROKER_SHUTDOWN_TIMEOUT_SECONDS` and its share of the remaining budget. When the budget
   runs out, the broker sends `SIGKILL` to every remaining runtime and exits. The docs state
   the rule: service-manager stop timeout ≥ budget + 15 s. The example unit uses
   `TimeoutStopSec` and `KillMode=mixed`.
2. **Shutdown preempts loads.** Stopping cancels an in-progress load instead of waiting for
   it: the spawning runtime is killed, and the model goes to `UNLOADED`.
   `ManagedModel.stop()` no longer waits on `load_lock` without a bound.
3. **Bounded graceful HTTP phase.** uvicorn runs with `timeout_graceful_shutdown` taken from
   the budget, so an open request or stream cannot hold up shutdown.
4. **Descendant-aware termination.** The kill set is the runtime's process group **plus every
   recorded descendant**, each identity-gated by PID + start time (ADR 0021):
   - On Linux, the broker makes itself a child subreaper (`prctl(PR_SET_CHILD_SUBREAPER)`), so
     descendants that leave the group are re-parented to it and can be found and reaped.
   - Elsewhere, a periodic parent-PID tree snapshot (argv-only `ps`) records descendants while
     the runtime is alive.
   - A process the broker cannot prove it started is never signalled.
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
  - A load in progress at shutdown is lost and repeats on the next start.
  - The subreaper changes how orphaned descendants are re-parented on Linux: the broker must
    reap them, which the supervisor loop now does.
  - Very slow-to-exit runtimes get `SIGKILL` sooner when the budget is short.
- **Neutral:** two settings (`BROKER_SHUTDOWN_BUDGET_SECONDS`, plus the existing
  `BROKER_SHUTDOWN_TIMEOUT_SECONDS`, which is now capped by the budget) and new teardown
  metrics and log fields.

## Confirmation

ADR 0027 drills D1–D6 pass on Linux in CI and on macOS locally, repeated three times. They
cover: exit within the budget, zero leftover runtime or descendant processes, `SIGTERM` during
a load, a runtime that ignores `SIGTERM`, an escaped descendant, an open stream, and the
second-signal escalation.
