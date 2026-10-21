# Request queueing under overload (ADR 0023)

Opt-in. When the system or GPUs are overloaded, the broker **queues** requests —
bounded, timed, and fair — instead of failing them or blocking a semaphore
forever.

## Run it

```bash
export $(grep -v '^#' examples/request-queueing/.env.example | xargs)
python -m broker            # or: uvicorn broker.app:create_app --factory
```

Send more concurrent requests than `BROKER_PER_MODEL_MAX_CONCURRENCY` and watch
them queue instead of fail:

```bash
for i in $(seq 1 20); do
  curl -s -D - -o /dev/null http://localhost:8080/v1/chat/completions \
    -H 'content-type: application/json' \
    -d '{"model":"chat","messages":[{"role":"user","content":"hi"}]}' &
done; wait
```

Queued responses carry `x-broker-queue-wait-ms` (and `x-broker-queue-trigger`).
Inspect the live queue at `GET /status` under the `queue` block (depth per model
and key, oldest wait, active triggers, rejections/timeouts by reason). New
Prometheus series `broker_queue_*` are on `/metrics`.

## What you'll see

- Within concurrency: served immediately, no `x-broker-queue-wait-ms`.
- Over concurrency: queued, then served in **FIFO** order within an API key and
  **round-robin** across keys (one key's flood can't starve another).
- Queue full (`BROKER_QUEUE_MAX_DEPTH[_PER_MODEL]`): **429** `queue_full` +
  `Retry-After`.
- Waited too long (`BROKER_QUEUE_MAX_WAIT_SECONDS`): **503** `queue_timeout` +
  `Retry-After` naming the trigger.

Per-key `priority` (keys file, ADR 0016) serves important keys first, with aging
so low-priority keys still progress. Per-key rpm/tpm/budget **quota** 429s are a
different concern and are **never** queued.

See the [guide](../../docs/guides/request-queueing.md) and
[ADR 0023](../../docs/adr/0023-request-queueing.md). A labeled simulated
benchmark is `scripts/bench_queue.py`.

This example is exercised by `tests/test_queue.py::test_example_request_queueing`.
