# Restart & recovery (ADR 0021)

How the broker survives an **unclean exit** without orphaning its runtimes or
killing a foreign process. When the broker spawns a runtime (vLLM / llama.cpp /
Strata) it records it in a host-local **runtime ledger** under
`BROKER_CACHE_DIR` (`<cache_dir>/runtime-ledger.json`). If the broker is
`SIGKILL`ed, OOM-killed, crashes, or its container restarts, that runtime keeps
running (own session) — and on restart the broker reconciles the ledger.

Identity is **PID + process start time** (plus process-group id and an argv
hash), never a bare PID, so a recycled PID is never mistaken for one of ours.

## Files

- `registry.example.yaml` — a broker-managed `llamacpp` runtime (has a
  `startup_command`, so it is spawned, ledgered, and reconciled).
- `.env.example` — the two settings this example is about.

## Reconcile modes (`BROKER_RUNTIME_RECONCILE`)

| Mode | On startup, for each recorded runtime that is still alive with a matching identity |
|------|------------------------------------------------------------------------------------|
| `off` | Ignore the ledger. Only the port pre-check guards a new spawn. |
| `report` | **Default.** List it as `orphaned` in `/status`. Never signal, never adopt. |
| `adopt` | If the registry argv still matches and its port answers readiness, mark the model READY **without spawning** — reuse the orphan (no reload, no second copy). |
| `stop` | `SIGTERM` → grace → `SIGKILL` its process group, then clear the record. |

In **every** mode, a record whose live start time no longer matches is dropped as
stale and **never signalled**, and a process the broker did not record is never
touched.

## Try it

```bash
# 1. Start the broker (adopt mode) with this registry.
BROKER_RUNTIME_RECONCILE=adopt \
BROKER_REGISTRY_PATH=examples/restart-recovery/registry.example.yaml \
  python -m broker

# 2. Send one request so the runtime actually loads, then confirm it is ledgered:
cat "$BROKER_CACHE_DIR/runtime-ledger.json"    # one record: pid, start_time, port, ...

# 3. Simulate an unclean death (no graceful shutdown) and restart:
kill -9 <broker_pid>        # the llama-server child keeps running, holding :8081
python -m broker            # same command as step 1

# 4. The restarted broker ADOPTS the still-running runtime instead of spawning a
#    second copy. Inspect the result:
curl -s localhost:8080/status | jq '.reconciliation, .models[].process'
```

With `BROKER_RUNTIME_RECONCILE=report` (the default) step 4 instead lists the
runtime under `reconciliation.last.orphaned`; with `stop` it reaps it.

## Port pre-check

If an orphan still holds the port and you spawn anew (e.g. `off`/`report` mode),
the broker fails fast with a clear error naming the port instead of a confusing
readiness timeout. Disable with `BROKER_RUNTIME_PORT_PRECHECK=false`.

## Graceful shutdown

A normal stop (`SIGTERM` to `python -m broker`, including `docker stop`) runs the
FastAPI lifespan shutdown, which stops every runtime **and clears the ledger** —
so a clean restart has nothing to reconcile.

See [ADR 0021](../../docs/adr/0021-runtime-identity-and-restart-reconciliation.md)
and [the restart-and-recovery guide](../../docs/guides/restart-and-recovery.md).
