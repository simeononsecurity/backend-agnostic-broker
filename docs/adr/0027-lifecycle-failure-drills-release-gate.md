# ADR 0027: Gate releases on repeatable lifecycle failure drills

Status: Accepted
Date: 2026-10-21
Supersedes: —
Related: ADR 0020, ADR 0021, ADR 0024, ADR 0025, ADR 0026, `docs/testing.md`

## Context

v0.6.0 passed 1,101 unit and integration tests and a real-backend test, then failed its first
production canary on *teardown*. The test suite exercised starting and serving models
thoroughly. It barely touched stopping under stress, process trees that escape their group,
forced kills, or device admission against real processes.

Most lifecycle tests use the in-process fake adapter, which has no processes, signals or
descendants. The defects lived exactly in the paths that a fake cannot exercise. A broker
proves little by starting a model once; production readiness depends on clean teardown,
bounded recovery, exclusive device admission and repeatable failure handling.

## Decision

We will maintain a **lifecycle failure-drill suite** that runs real broker processes against
real stand-in runtime processes, and we will make it a release gate.

1. **Harness:**
   - `tests/drills/` (pytest marker `drill`, excluded from the default fast run) and a runner
     `scripts/failure_drills.py`.
   - A stand-in runtime executable (`tests/fixtures/drill_runtime.py`) whose behaviour is set
     per drill: ignore `SIGTERM`, slow start, fork an escaped `setsid` descendant, stream
     slowly, crash, hold a port.
   - Required-GPU drills use a recorded-fixture telemetry provider.
   - No GPU and no network are needed.
2. **Drills:** each asserts wall-clock bounds, zero leftover processes (checked by PID + start
   time), an empty ledger and lease directory, and the typed error.

   | Drill | Scenario | Expected |
   |---|---|---|
   | D1 | `SIGTERM` with a loaded runtime | exit within the budget |
   | D2 | runtime ignores `SIGTERM` | `SIGKILL` escalation within the budget |
   | D3 | escaped `setsid` descendant | killed (Linux) or reported as a survivor |
   | D4 | `SIGTERM` during a load | load cancelled; exit within the budget |
   | D5 | open stream at `SIGTERM` | stream ends; exit within the budget |
   | D6 | second-signal escalation | immediate `SIGKILL` of runtimes, then exit |
   | D7 | `gpu.required` with no device | fails before spawn |
   | D8 | Ollama-style CPU placement | unloaded with `gpu_placement_failed` |
   | D9 | lease lifecycle around load and unload | lease acquired, then released |
   | D10 | broker `SIGKILL`, then `python -m broker cleanup` | runtime and lease reaped within 10 s; release hook fired |
   | D11 | exclusive model vs. an external lease | `device_busy`, nothing signalled |
   | D12 | restart after `SIGKILL` | `report` / `adopt` / `stop` behave per ADR 0021 |
3. **Repeatability.** The runner executes the suite N times (default 3) and fails on any
   flake. A drill that is flaky is a defect, not a retry.
4. **Gate:**
   - A CI job on `ubuntu-latest` (actions pinned to SHAs) runs the drills ×3 on every PR that
     touches `broker/`.
   - The release checklist requires the drills to be green on Linux CI and on a local macOS
     run, with the run log attached to the release evidence.
   - A production canary must include a stop-under-load and a forced-kill drill before the
     broker re-enters a production path.

## Alternatives considered

- **More fake-adapter unit tests.** Rejected as sufficient: fakes have no processes,
  signals or descendants, which is where these defects were.
- **Rely on manual canaries.** Rejected: they find defects late, in production, and are not
  repeatable.
- **Chaos testing in production.** Rejected: unsafe on a shared host, and it does not run per
  PR.
- **Real GPU runtimes in CI.** Rejected: there is no GPU in CI. The stand-in runtime
  reproduces the process and signal behaviour that matters. Real-backend runs stay part of the
  release test.

## Consequences

- **Positive:**
  - Teardown, recovery and admission regressions are caught per PR.
  - Fixes ship with proof.
  - Canary results become reproducible locally.
- **Negative:**
  - CI time grows by a few minutes.
  - The drills spawn real processes and need careful cleanup themselves, so the harness kills
    its own stand-ins in a `finally` step, gated by identity.
- **Neutral:** a new CI job, a pytest marker, a runner script and a section in
  `docs/testing.md` and the release checklist.

## Confirmation

Implemented on `integration/p5`. The drill suite lives in `tests/drills/`
(`test_lifecycle_drills.py`, D1–D12) behind the `drill` marker, with the stand-in
runtime `tests/fixtures/drill_runtime.py` (now also `crash` / `hold_port`), the
recorded-telemetry `tests/fixtures/fake_nvidia_smi.sh`, and the shared harness
`tests/drills/harness.py`. The runner `scripts/failure_drills.py --repeat 3` fails
on any flake; the CI job `.github/workflows/drills.yml` (`ubuntu-latest`, actions
pinned to SHAs, path-gated on `broker/**` / the drills / the runner) runs it ×3.
`docs/testing.md` lists each drill and its assertion; the production-deployment
guide records the release gate (Linux CI + local macOS, log attached to release
evidence; canary includes a stop-under-load and a forced-kill drill). The drill
log for the next version is attached to the release evidence.
