# Device leases (ADR 0026)

Host-local **device leases** give co-tenant workloads a reliable, readable
"device N is in use by the broker" signal, let the broker admit **exclusive**
models safely, and let a forced stop be cleaned up from the service manager's
post-stop step so a device is freed within seconds instead of at the next start.

Leases are **opt-in**: with `BROKER_DEVICE_LEASES=off` (the default) nothing is
read or written and no lease directory is created — behaviour is byte-for-byte
the previous release.

> Design rationale: [ADR 0026](adr/0026-device-leases-and-stale-lease-cleanup.md).

## Enabling

```bash
BROKER_DEVICE_LEASES=on
# optional — defaults to <cache_dir>/leases
BROKER_DEVICE_LEASE_DIR=/var/cache/broker/leases
```

See [configuration.md](configuration.md#host-local-device-leases-adr-0026) for
every setting.

## File format (stable, versioned interface)

One file per device, named `device-<key>.lease.json` (the key is the device
ordinal such as `0`, or `default` for an unpinned model). Files are **atomic**
(temp-file + `fsync` + `os.replace`), serialized with an advisory `flock` on a
sidecar `.lock`, and written **world-readable, owner-writable (`0644`)** so a
co-tenant can read them. The schema is versioned (`version: 1`):

```json
{
  "version": 1,
  "device": "0",
  "mode": "exclusive",
  "holders": [
    {
      "holder": "llama3-8b",
      "state": "held",
      "broker_pid": 4321,
      "broker_start_time": "173...",
      "runtime_pid": 4777,
      "runtime_pgid": 4777,
      "runtime_start_time": "173...",
      "acquired_at": 1792700000.0,
      "heartbeat_at": 1792700000.0
    }
  ],
  "holder": "llama3-8b",
  "state": "held",
  "broker_pid": 4321,
  "runtime_pid": 4777
}
```

* `mode` is `exclusive` or `shared`. A `shared` lease may have several holders
  (co-residency); an `exclusive` lease has exactly one.
* The top-level flat fields (`holder`, `broker_pid`, `runtime_pid`, …) mirror the
  **first** holder for the common single-holder case; `holders` is the full set.
* Holder identity is always `(pid, start_time)`, never a bare PID — a reused PID
  with a different start time is treated as gone (see
  [runtime identity, ADR 0021](adr/0021-runtime-identity-and-restart-reconciliation.md)).
* `state` is one of `held` | `releasing` | `stuck` | `orphaned` | `external`.

### Writing an `external` lease (co-tenants)

A co-tenant that uses a device when the broker is idle can publish its own lease
so the broker refuses to co-schedule an exclusive model onto it. Write a file
`device-<key>.lease.json` in `BROKER_DEVICE_LEASE_DIR` with `version: 1`, your
`mode`, and a single holder whose `state` is `"external"`:

```json
{
  "version": 1,
  "device": "0",
  "mode": "exclusive",
  "holders": [
    {"holder": "my-training-job", "state": "external", "runtime_pid": 9900}
  ]
}
```

The broker **reads and honours** an `external` lease but **never signals,
evicts, or removes it** — only the external owner manages its own lease. Omit
`broker_pid` (or set it to a PID you do not own); the broker never treats an
external holder as its own runtime.

## Exclusive admission

Add `gpu.exclusive: true` to a model's registry `gpu` block to take an
`exclusive` lease when it loads; every other model takes a `shared` lease. When a
model needs a device that is held incompatibly (`exclusive` conflicts with any
other holder; `shared` conflicts only with an `exclusive` holder):

* if the holder is an **idle broker model**, it is evicted under the normal
  eviction policy, and the exclusive model takes the device;
* otherwise — a **busy** broker model, or **any external holder** — the load
  fails fast with **`device_busy`** (HTTP 503 + `Retry-After`). Nothing is
  spawned and no external holder is ever signalled.

## Release and stuck leases

A lease is acquired inside the atomic admission step, **before** the runtime is
spawned, and released only when teardown + measured release confirm the runtime
is gone (the single "released" decision; see
[architecture.md](architecture.md)). If the runtime survives, the lease is marked
`stuck` and the sweeper clears it once the survivors exit.

## Cleanup after a stop — `python -m broker cleanup`

A forced stop (SIGKILL / OOM / `systemctl stop` past the timeout) can leave a
lease whose owning broker is dead. The one-shot command reaps it:

```bash
python -m broker cleanup           # bounded (<=20s), no server, JSON on stdout
python -m broker cleanup --report  # never kill an orphan runtime; only report
```

* A **stale** lease (broker dead **and** runtime gone) is removed and the release
  hook fires.
* A **live orphan** (broker dead, runtime alive) follows
  `BROKER_RUNTIME_RECONCILE`: `stop` terminates the runtime **identity-gated**
  then releases; `report`/`off`/`adopt` keep it and mark it `orphaned`.
  `--report` forces keep-and-mark regardless of the mode.
* A lease owned by a **live** broker is left untouched.

It exits `0` on success (`1` on a per-lease error) and prints a JSON summary. It
is designed for a systemd `ExecStopPost=` — see
[operations-runbook.md](operations-runbook.md). The same stale-cleanup also runs
at broker startup (before serving) and in the periodic sweeper.

## Hooks

`BROKER_LEASE_ACQUIRE_COMMAND` and `BROKER_LEASE_RELEASE_COMMAND` are argv
templates with `{device}` `{mode}` `{holder}` placeholders, run (no shell) when a
lease is acquired/released — e.g. to pause and resume a co-tenant without the
broker depending on any external tool. The executable must resolve inside
`BROKER_ALLOWED_EXEC_DIRS`; calls are bounded by
`BROKER_LEASE_HOOK_TIMEOUT_SECONDS`. A failed **acquire** hook fails the load
with `device_busy`; a failed **release** hook is logged and retried by the
sweeper. A worked example lives in
[`examples/device-leases/`](../examples/device-leases/).

## Visibility

`/status` gains a `leases` block (`enabled`, `dir`, and the current per-device
holders with their state). Prometheus gains `broker_device_leases{state=...}`,
`broker_device_lease_stale_cleanups_total`, and
`broker_device_lease_hook_failures_total{phase=...}`.
