# How-to: restart & recovery (orphaned runtimes)

Survive an **unclean broker exit** without orphaning runtimes, double-loading a
model, or ever killing a process the broker did not start. When the broker spawns
a runtime (vLLM / llama.cpp / Strata) it records it in a host-local **runtime
ledger** under `BROKER_CACHE_DIR`; on restart it reconciles that ledger against
the live process table.

Identity is **PID + process start time** (plus process-group id and an argv
hash), never a bare PID — so a recycled PID is never mistaken for one of ours.
This is on by default in the safe, observe-only `report` mode.

## When to use

Any deployment that runs **broker-managed** runtimes (an entry with a
`startup_command`) and can be killed uncleanly: a crash, OOM-kill, `kill -9`, or a
container/host restart. External daemons (Ollama) are not spawned by the broker,
so they are never ledgered or reconciled. The feature is inert with no managed
runtimes.

## Minimal config

```bash
# Default: after an unclean restart, still-running runtimes show up as `orphaned`
# in /status. Nothing is signalled or adopted.
BROKER_RUNTIME_RECONCILE=report
```

## Modes

```bash
BROKER_RUNTIME_RECONCILE=off      # ignore the ledger; only the port pre-check guards spawns
BROKER_RUNTIME_RECONCILE=report   # (default) list orphans in /status; never signal/adopt
BROKER_RUNTIME_RECONCILE=adopt    # reuse a still-healthy orphan: mark READY without spawning
BROKER_RUNTIME_RECONCILE=stop     # SIGTERM -> grace -> SIGKILL the orphan's group, then clear
BROKER_RUNTIME_PORT_PRECHECK=true # (default) fail fast if the target port is already bound
```

- **`adopt`** is the choice when you want the restarted broker to *reuse* a
  runtime that survived (no reload, no second copy, no VRAM thrash). It only
  adopts when the process is alive with a matching identity, the registry argv
  still hashes to the record (no drift), and its port answers the readiness probe.
- **`stop`** is the choice when you want a clean slate: reap survivors, then let
  the next request load fresh.

The safety invariant holds in **every** mode: a record whose live start time no
longer matches is dropped as **stale** and never signalled, and a process the
broker did not record is never touched. A corrupt/unreadable ledger is ignored
with a warning — the broker never kills anything on the strength of bad data.

## Verify

```bash
# After the runtime loads, one record appears in the ledger:
cat "$BROKER_CACHE_DIR/runtime-ledger.json"

# /status shows per-model process identity and the last reconciliation:
curl -s localhost:8080/status | jq '.reconciliation, .models[].process'
# .models[].process -> {pid, start_time, adopted}
# .reconciliation    -> {runtime_reconcile, last: {adopted, stopped, orphaned, stale}}
```

Simulate recovery: load a model, `kill -9` the broker (its runtime keeps
running), then restart with `BROKER_RUNTIME_RECONCILE=adopt` — `/status` shows the
model READY with `process.adopted = true` and no new process was spawned. See the
tested [`restart-recovery` example](../../examples/restart-recovery/).

## Graceful shutdown

A normal stop — `SIGTERM` to `python -m broker`, which is what `docker stop` sends
to PID 1 (the image uses an exec-form entrypoint + `STOPSIGNAL SIGTERM`) — runs
the FastAPI lifespan shutdown. That stops every runtime **and clears the ledger**,
so a clean restart has nothing to reconcile. Only an *unclean* death leaves
records behind.

## Multi-replica

The ledger is **per host**: local child processes can only be reconciled on the
machine that spawned them, so (unlike the ADR 0009 routing state) it is never
shared via Redis. Each replica writes its own ledger under its own
`BROKER_CACHE_DIR` and reconciles only its own records. Give each replica a
distinct cache dir (or a distinct host) so their ledgers never collide.

## Troubleshooting

- **"port N is already in use before starting runtime"** — an orphan (or an
  unrelated process) holds the runtime's port. Use `BROKER_RUNTIME_RECONCILE=stop`
  (or `adopt`) to reconcile recorded runtimes, free the port, or set
  `BROKER_RUNTIME_PORT_PRECHECK=false` to skip the check.
- **An orphan is listed but not adopted** (`adoptable: false`) — the registry argv
  drifted from the recorded one, or the port did not answer readiness. The broker
  refuses to adopt a runtime it can't confirm matches the current registry; stop
  it (`stop`) and let the next request load fresh.

See [ADR 0021](../adr/0021-runtime-identity-and-restart-reconciliation.md),
[`configuration.md`](../configuration.md#runtime-identity--restart-reconciliation-adr-0021),
and the [operations runbook](../operations-runbook.md).
