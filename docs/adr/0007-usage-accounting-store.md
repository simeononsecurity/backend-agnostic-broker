# ADR 0007: Request usage & routing-decision accounting store (SQLite)

- Status: Accepted
- Date: 2026-10-18
- Supersedes: none
- Related: `broker/usage.py`, `broker/app.py`, `docs/configuration.md`

## Context

The broker emitted Prometheus counters and kept the last few router decisions in
`/status.router`, but nothing durable linked a request to the model it used, its
token counts, and its latency. That record is the substrate for cost reporting,
per-key budgets, and router evaluation (CPT/APGR) — all on the roadmap — and
LiteLLM's spend-tracking value rests on exactly this data.

## Decision

We will record one row per completed request in a small, stdlib-only SQLite log
(`broker/usage.py`): timestamp, endpoint, requested vs served model, token counts,
latency, status, stream flag, and a non-reversible API-key fingerprint. It is
exposed via `GET /usage` (aggregates + recent rows).

Principles:
- **Metadata only** — never request/response content; the key is stored as a
  truncated SHA-256 fingerprint, never in the clear.
- **Best-effort** — a write failure is logged and swallowed; the log can never
  break or slow a request (short, lock-serialized writes).
- **Configurable** — `usage_log_enabled` (default on) and `usage_log_path`
  (default `<cache_dir>/usage.sqlite3`; `:memory:` for ephemeral; disable to opt
  out). Degrades gracefully to disabled if the path is unwritable.

## Alternatives considered

- **Prometheus only.** Rejected: counters can't answer "tokens by key last week"
  or feed a router eval; they're aggregates, not records.
- **External datastore (Postgres/Redis) like LiteLLM.** Rejected as the default:
  adds an operational dependency. SQLite is zero-dependency and sufficient for a
  single broker; a pluggable external store can come with the Redis work (ADR 0009).
- **Structured log lines only.** Rejected: not queryable in-process for `/usage`
  or budgets without shipping logs elsewhere.

## Consequences

- Unlocks cost/usage reporting, per-key budgets (ADR 0008), and a router eval
  harness (ADR 0011) without new infra.
- Single-writer SQLite is fine for one broker; multi-replica aggregation needs the
  shared-state work in ADR 0009.
- Default on means a writable `cache_dir` (already true in the container); it
  self-disables where it isn't.

## Confirmation

`tests/test_usage.py` covers record/summary/fingerprint/disabled; the endpoint
tests assert `/usage` aggregates across chat/completions/responses.
