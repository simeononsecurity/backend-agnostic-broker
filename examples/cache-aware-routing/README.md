# Cache-aware pool routing — runnable example

A single logical model **`chat`** backed by a **3-member vLLM pool**, routed with
`BROKER_ROUTE_STRATEGY=cache_aware` so each request lands on the member that already
holds its prompt **prefix** (warm KV cache), while a load term keeps a popular prefix
from hot-spotting one member. Design: [ADR 0010](../../docs/adr/0010-cache-aware-pool-routing.md),
extended by [ADR 0018](../../docs/adr/0018-cache-aware-v2.md) (queueing-model bench +
re-validated default `β=0.5`). Full guide: [routing-and-health.md](../../docs/routing-and-health.md#cache-aware-routing-adr-0010).

## Files

- `registry.yaml` — the 3-member `chat` pool (host URLs from `${VLLM_URL_1..3}`).
- `.env.example` — copy to `.env`; sets `cache_aware`, `β=0.5`, and the member URLs.

## Run

```bash
cp examples/cache-aware-routing/.env.example .env     # then edit the VLLM_URL_* hosts
uvicorn broker.app:app --host 127.0.0.1 --port 8080   # loads the registry above
```

Send a few requests that **share a system prompt** (the shared prefix is what
cache-aware routing exploits):

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"chat","messages":[
        {"role":"system","content":"You are a meticulous assistant. Cite sources."},
        {"role":"user","content":"Summarize the CAP theorem."}]}'
```

Repeat with the **same system prompt** but different user turns: they chain-hash to the
same leading blocks, so cache-aware routing sends them to the member that already
served that prefix.

## Reading the `cache_aware` stats in `GET /status`

```bash
curl -s http://127.0.0.1:8080/status | python -m json.tool
```

Look at the `cache_aware` block:

```json
"cache_aware": {
  "enabled": true,
  "index_size": 1234,   // prefix-block index entries (LRU + TTL bounded;
                        //   shared across replicas under Redis — ADR 0018)
  "lookups": 500,       // routing decisions that consulted the index
  "hits": 410,          // lookups where some member already held a prefix
  "hit_rate": 0.82,     // hits / lookups — the knob to watch when tuning
  "block_chars": 128,
  "beta": 0.5
}
```

Also compare per-member `inflight` in the `models` list: a healthy `cache_aware`
deployment keeps hit rate high **and** in-flight load reasonably even. If one member
hot-spots, raise `BROKER_CACHE_AWARE_BETA` (e.g. `0.5`→`1.0`); if hit rate is low,
lower `BROKER_CACHE_AWARE_BLOCK_CHARS` or raise the TTL / `MAX_ENTRIES`, and confirm
your workload actually shares prefixes.

## Tune β for your workload

The default `β=0.5` was chosen by the queueing benchmark's selection rule. Re-run it
against your own scenario mix:

```bash
python scripts/bench_routing.py --matrix        # scenario × β table (hit rate / p95 / spread)
python scripts/bench_routing.py --select-beta    # apply the ADR 0018 selection rule
python scripts/bench_routing.py --capacity-sweep # sticky vs cache_aware across cache sizes
```
