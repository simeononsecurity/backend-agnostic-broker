# Device-lease hooks example (ADR 0026)

This example shows how to pause and resume a co-tenant workload when the broker
acquires and releases a device lease, without the broker depending on any
external lease manager.

## Files

* `pause-cotenant.sh` — the lease hook. It receives `{device} {mode} {holder}`
  and records the event (and would, in production, signal the co-tenant — e.g.
  `systemctl kill --signal=STOP cotenant@<device>` on acquire, `CONT` on
  release). It is intentionally side-effect-free here so it is safe to run in CI.

## Enabling

The hook executable must live inside an allow-listed directory and be referenced
as an argv template:

```bash
BROKER_DEVICE_LEASES=on
BROKER_ALLOWED_EXEC_DIRS='["/opt/leasehooks"]'
BROKER_LEASE_ACQUIRE_COMMAND="/opt/leasehooks/pause-cotenant.sh {device} {mode} {holder}"
BROKER_LEASE_RELEASE_COMMAND="/opt/leasehooks/pause-cotenant.sh {device} {mode} {holder}"
BROKER_LEASE_HOOK_TIMEOUT_SECONDS=10
```

No shell is used: the template is split into argv and each `{...}` placeholder is
substituted whole-token. A failed **acquire** hook fails the load with
`device_busy`; a failed **release** hook is logged and retried by the sweeper.

## Test it

```bash
./pause-cotenant.sh 0 exclusive llama3-8b
cat "${LEASE_HOOK_LOG:-/tmp/lease-hook.log}"
# -> acquire-or-release device=0 mode=exclusive holder=llama3-8b
```
