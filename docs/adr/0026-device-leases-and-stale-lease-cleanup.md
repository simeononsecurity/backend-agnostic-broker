# ADR 0026: Host-local device leases with exclusive admission and stale-lease cleanup

Status: Accepted
Date: 2026-10-21
Supersedes: —
Related: ADR 0020 (measured release), ADR 0021 (runtime identity + reconciliation),
ADR 0022 (memory-aware admission), ADR 0024 (bounded teardown), ADR 0025 (required GPU)

## Context

GPUs on an inference host are often shared with co-tenant workloads: other services that use
a device whenever no model holds it. Those co-tenants need a reliable "the broker is using
device N" signal, and the broker needs to know when a device is taken.

In the production canary, a forced stop of the broker left the "device in use" state stale,
so the co-tenant stayed paused until a separate reconciliation noticed. The broker has two
pieces of related state, and neither does the job:

- The ADR 0021 runtime ledger records *processes*, not *devices*. Nothing outside the broker
  reads it, and nothing clears it after a forced kill until the next broker start.
- Admission (ADR 0022) budgets memory but has no notion of *exclusive* use. Two models, or a
  model and an external holder, can share a device that one of them needs alone.

The product direction is to adopt lease ideas natively, not to integrate an external lease
manager. Any cleanup must stay identity-gated: never signal a process the broker did not
start.

## Decision

We will add opt-in, host-local **device leases** (`BROKER_DEVICE_LEASES=off|on`, default
`off`).

1. **Lease records.** There is one lease file per device (`device-<key>.lease.json`) under
   `BROKER_DEVICE_LEASE_DIR` (default `<cache_dir>/leases`). Writes are atomic and
   flock-protected, and files are world-readable but writable only by the owner (`0644`).
   Each file carries a schema `version`, the `device`, the lease `mode`
   (`exclusive` | `shared`) and a list of `holders`. A `shared` lease may have several
   holders (co-residency); an `exclusive` lease has exactly one. Each holder records:
   - holder (model alias or an external name) and `state`
     (`held` | `releasing` | `stuck` | `orphaned` | `external`);
   - holder identity: broker PID + start time, and the runtime's PID/pgid/start time;
   - `acquired_at` and `heartbeat_at`.

   The documented flat top-level fields (`holder`, `broker_pid`, `runtime_pid`, …) mirror
   the first holder for the common single-holder case. The format is a stable, versioned
   interface: co-tenants and operators can read it, and external holders may write
   `external` leases with the same protocol (see `docs/device-leases.md`).
2. **Acquire before load, release after measured release.**
   - The broker acquires leases for the model's devices (`gpu.device_ids`, or the devices
     admission placed it on) inside the atomic admission step (ADR 0022) and before spawning.
   - A lease is released only when teardown (ADR 0024) and measured release (ADR 0020)
     confirm the runtime is gone. Otherwise it moves to `stuck`, with the survivors recorded,
     and clears itself once they exit.
3. **Exclusive admission.** A model with `gpu.exclusive: true` (new registry field) takes an
   `exclusive` lease; other models take `shared` leases. When a device is held incompatibly:
   - if the holder is an idle broker model, the existing eviction policy may evict it;
   - otherwise, including when the holder is external, the load fails fast with `device_busy`
     (503 + `Retry-After`).

   The broker never evicts or signals an external holder.
4. **Stale-lease cleanup.** A lease is *stale* when its broker identity is dead (PID + start
   time mismatch) and its runtime identity is gone. Stale leases are removed:
   - at broker startup, before serving;
   - by the periodic sweeper;
   - by a new one-shot command, `python -m broker cleanup`, which is bounded, runs no server
     and is meant for systemd `ExecStopPost=`. It runs even after a forced kill.

   A lease whose broker is dead but whose runtime is alive is an orphan. It follows the ADR
   0021 reconcile mode: `report` keeps it and marks it `orphaned`, `stop` kills
   (identity-gated) then releases, and `adopt` re-owns it.
5. **Optional hooks.** `BROKER_LEASE_ACQUIRE_COMMAND` and `BROKER_LEASE_RELEASE_COMMAND` are
   argv templates with `{device}`, `{mode}` and `{holder}` placeholders. The executable must
   be in an allow-listed directory; no shell is used, and calls are bounded by
   `BROKER_LEASE_HOOK_TIMEOUT_SECONDS`. They run on acquire and release, including the
   release of a stale lease by `cleanup`. Operators can pause and resume a co-tenant with them
   without the broker depending on any external tool. A failed acquire hook fails the load
   (`device_busy`); a failed release hook is logged and retried by the sweeper.
6. **Visibility.** `/status` gains a `leases` block. Metrics count leases by state, stale
   cleanups and hook failures.

## Alternatives considered

- **Extend the ADR 0021 ledger instead.** Rejected: it is process-keyed and private, while
  co-tenants need a per-device view with a stable format. The lease layer reuses the ledger's
  identity and owner-lock code.
- **In-memory leases.** Rejected: they vanish on the forced kill that causes stale state.
- **An external lease manager / daemon.** Rejected by product direction (learn from it, do
  not integrate it), and it adds a second process that must itself survive crashes.
- **Redis-backed leases.** Rejected: devices are host-local, and a network store adds a
  dependency and a failure mode for a single-host fact.
- **Cleanup only at next start.** Rejected: the next start may be hours away. An
  `ExecStopPost` one-shot plus the sweeper releases the device within seconds of the stop.

## Consequences

- **Positive:**
  - Co-tenants get a reliable, readable signal of device use.
  - A forced stop no longer leaves a device marked busy, because cleanup runs from the
    service manager's post-stop step.
  - Exclusive models cannot be co-scheduled by accident.
- **Negative:**
  - It is a new on-disk interface that we must version and keep compatible.
  - Hooks run operator code; this is mitigated by the allow-list, argv-only calls and
    timeouts.
  - Exclusive leases can reduce packing density.
- **Neutral:**
  - Default `off`: with no settings, behaviour equals the previous release.
  - New settings: `BROKER_DEVICE_LEASES`, `BROKER_DEVICE_LEASE_DIR`, the two hook commands
    and the hook timeout.
  - New registry field `gpu.exclusive`, new error `device_busy` and the `cleanup` CLI
    subcommand.

## Confirmation

Implemented in `broker/leases.py` (lease store, pure classification/compatibility,
hooks, and the shared cleanup) wired into `broker/manager.py` (acquire inside the
atomic admission step before spawn; release on the single "released" decision;
startup + sweeper stale cleanup; `/status.leases`; metrics) and the
`python -m broker cleanup` CLI in `broker/__main__.py`. Covered by:

- `tests/test_device_leases.py` — a load writes a `held` lease with the runtime
  identity and fires the acquire hook once; an unload removes it and fires the
  release hook once; an exclusive model evicts an idle shared holder and is
  refused (`device_busy` 503 + `Retry-After`) on a busy holder and on an
  **external** holder whose real process is asserted still alive (never
  signalled); a stale lease is reaped; leases off create no directory; plus the
  pure compatibility/classification matrix and the shipped example hook script.
- `tests/test_device_leases_realproc.py` — real `python -m broker cleanup` against
  a lease whose broker is dead and whose runtime is a real alive process: `stop`
  mode terminates it identity-gated, removes the lease and fires the release hook
  within 10 s; `--report` keeps it `orphaned` and signals nothing; a decoy whose
  recorded start time does not match is never killed (identity gate).

ADR 0027's lifecycle drills (D9–D11) add the forced-kill / exclusive-refusal
drills as a release gate. The v0.6.0 and v0.7.0 suites pass unchanged with leases
off, and no lease directory is created.
