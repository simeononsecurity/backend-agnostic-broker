# ADR 0021: Runtime process identity (PID + start time) and restart reconciliation

- Status: Accepted
- Date: 2026-10-20
- Supersedes: none (extends ADR 0002 architecture, ADR 0003 health/lifecycle)
- Related: `broker/proc.py`, `broker/manager.py`, `broker/adapters/httpbase.py`,
  `broker/runtime_identity.py`, `broker/ledger.py`, `broker/reconcile.py`,
  `docs/guides/restart-and-recovery.md`, `docs/security-model.md`,
  `docs/research/adr-0021-runtime-identity-and-restart-reconciliation.md`;
  prior art: systemd (`$MAINPID` is a hint, cgroup is truth, two-phase kill),
  Linux `proc_pid_stat(5)` field 22, `kill(2)` signal-0 existence check,
  `pidfd_open(2)`, `rename(2)` / `flock(2)`.

## Context

The broker spawns backend runtimes (vLLM, llama.cpp, Strata) as **argv-only**
children with `start_new_session=True` (`broker/proc.py`), so each runtime leads
its own session/process-group. That isolation is deliberate — it lets the broker
`killpg` the whole tree — but it also means a runtime **outlives an unclean broker
death**: on `SIGKILL`, OOM-kill, a crash, or a container/host restart, the child
keeps running, holding its VRAM and its TCP port. The restarted broker has **no
record of it**. The next request for that model then either collides on the port
(startup failure, previously surfaced only as a confusing readiness timeout) or
starts a second copy (OOM).

Worse, the only identity the broker tracked was the **PID**, and the kernel
recycles PID numbers. A record written before the crash can, by the time the
broker restarts, point at an unrelated process — so any "clean up the old runtime"
logic built on a bare PID risks **signalling a foreign process** (friendly fire),
violating the hard invariant *never kill a process the broker did not start*.

We need: (a) a durable identity that survives PID reuse; (b) a persistent record
of spawned runtimes that survives the broker's own death; (c) a startup
reconciliation that can report, adopt, or reap orphans **without ever signalling a
process it cannot positively confirm as its own**; and (d) a fail-fast on a
port already in use. The research note surveys the OS primitives in detail.

## Decision

### 1. Identity = `(pid, process_start_time)` + pgid + argv hash

A new dependency-free module `broker/runtime_identity.py` computes a normalized,
opaque **process start-time token**, compared only for equality on the same host:

- **Linux** — `/proc/<pid>/stat` field 22 (`starttime`, in `USER_HZ` ticks)
  divided by `sysconf(SC_CLK_TCK)` and anchored to `/proc/stat btime`, giving an
  absolute wall-clock start instant. The parser splits on the text *after the last
  `)`* so a `comm` containing spaces/parens cannot corrupt the field offset.
- **macOS/BSD** — `ps -o lstart= -p <pid>` (argv list, **no shell**), the full
  start timestamp. `sysctl KERN_PROC_PID`/`kinfo_proc` is noted as an optional
  native path but **not** required (keeps the core dependency-free; `psutil`
  rejected for the same reason).

Liveness uses `kill(pid, 0)` (existence check, no signal) but **only as one clause**
of an identity gate: `identity_matches(pid, recorded_start_time)` requires the PID
alive **and** the re-read start time equal to the record. A zombie (dead but
unreaped — still answers `kill(0)`) is treated as terminated. pidfd
(`pidfd_open`/`pidfd_send_signal`) is **rejected as the durable identity** (the fd
dies with the broker, so it cannot persist across a restart); `(pid, start_time)`
is the on-disk anchor.

### 2. Host-local runtime ledger (`broker/ledger.py`)

One JSON record per spawned runtime — `alias, pid, pgid, start_time, port,
argv_sha256, broker_instance_id, base_url, spawned_at, broker_pid,
broker_start_time` — in `<BROKER_CACHE_DIR>/runtime-ledger.json`. Writes reuse the
proven `broker/keys.py` pattern: **atomic** (temp file + `fsync` + `os.replace`),
**`0600`**, serialized by an advisory **`flock`** on a sidecar `.lock`. A
torn/unparsable/unknown-version ledger reads as **empty with a warning** — the
broker never signals anything on the strength of bad data (fail-safe). The record
is written on a successful spawn and removed on a clean stop/unload. Only
broker-managed runtimes (those with a `startup_command`) are ledgered — the fake
backend and external daemons (Ollama) are not, so default/test behavior is
unchanged. `broker_pid` / `broker_start_time` record the **owning broker's**
identity (see Multi-replica below).

**Multi-replica:** scope is intentionally per host. Local child processes cannot be
reconciled from another machine, so Redis is **not** involved (unlike the ADR 0009
routing state). Each replica should write a ledger under its **own**
`BROKER_CACHE_DIR` and reconciles only its own records on its own host. Because two
local brokers accidentally sharing one cache dir is a realistic mistake — and a
non-owner must never signal or adopt the owner's live runtimes — two mechanisms
enforce single ownership (defense in depth), rather than relying on the convention
alone:

1. **Lifetime ownership lock.** At startup each broker takes an exclusive,
   non-blocking `flock` on `<BROKER_CACHE_DIR>/runtime-ledger.owner.lock`, held for
   the broker's whole life (the fd stays open until clean shutdown), stamping its
   PID + start time + instance id into the file. If a live sibling already holds
   it:
   - `adopt` / `stop` — **refuse to start** with a `ConfigError` naming the cache
     dir and the owner PID. The operator opted into signalling/adopting runtimes,
     so acting on a sibling's is unsafe: fail closed.
   - `report` (default) / `off` — **start normally but disable this instance's
     ledger and reconciliation**, with one WARNING and a
     `reconciliation.disabled_reason` in `/status`. This keeps v0.6.0 behavior: two
     brokers sharing a dir must not start failing.
   An advisory `flock` is released by the kernel when the owning fd closes —
   including on process death — so a **stale lock** (previous owner crashed) is
   taken over automatically, with no manual liveness check.
2. **Per-record owner check.** Each record carries its owning broker's
   `broker_pid` + `broker_start_time`. Reconcile acts only on a record whose owner
   broker is **dead** (identity mismatch or gone); a record owned by a live broker
   is listed as `owned_by_live_broker` and is never signalled, adopted, or dropped.
   Legacy records with no recorded owner fall through to the normal
   runtime-identity gate (single-broker behavior preserved).

### 3. Startup reconciliation (`BROKER_RUNTIME_RECONCILE`, `broker/reconcile.py`)

Run once from the FastAPI lifespan **before** the sweeper/health loops, never
loading a model:

- `off` — ignore the ledger; the port pre-check is the only remaining guard.
- `report` — **default**; list still-alive, identity-matched runtimes as
  `orphaned` in `/status`; never signal, never adopt.
- `adopt` — if the recorded process is alive with a matching identity, the
  registry argv still hashes to the record (no drift), and its port answers the
  readiness probe, mark the model **READY without spawning** (the adapter takes
  ownership of the live PID/pgid so a later stop can terminate it).
- `stop` — `SIGTERM` the recorded **process group** → grace → `SIGKILL`, then
  clear the record.

Across **every** mode the invariant holds: a record whose live start time does not
match is dropped as **stale** and never signalled; a process the broker never
recorded is never touched.

**Default = `report`, justified.** The hard constraint is "default keeps v0.6.0
behavior plus at most a warning." `report` never signals, never adopts, and never
changes the load path (a request still spawns as before); with the empty ledger of
a fresh v0.6.0→v0.x upgrade it is completely silent. It is strictly more useful
than `off` because an operator immediately *sees* orphans in `/status` after an
unclean restart, which is the whole point of the ledger. `adopt`/`stop` are opt-in
because they take an action (reuse/reap) that an operator should choose.

### 4. Port pre-check before spawn

`broker/adapters/httpbase.py` now probes the target port (parsed from the registry
`base_url`) with a **non-loading** TCP connect before spawning; if something is
listening it raises `ModelLoadError` naming the exact port and pointing at the
reconcile/opt-out knobs, instead of the old confusing readiness timeout. Gated by
`BROKER_RUNTIME_PORT_PRECHECK` (default on). Readiness probes still never load a
model (health invariant preserved).

### 5. Graceful shutdown & signal handling

The FastAPI lifespan already drives `manager.shutdown()`, which stops every runtime
and (now) clears each ledger record; a test confirms the ledger is empty after
shutdown. `python -m broker` runs under uvicorn, whose `SIGTERM` handler drives the
lifespan shutdown — verified by a real-subprocess test that sends `SIGTERM` and
asserts the application-shutdown ran. The Docker image uses an **exec-form**
`ENTRYPOINT ["python","-m","broker"]` (python is PID 1, receives `SIGTERM`
directly) with an explicit `STOPSIGNAL SIGTERM`; no init shim is needed.

### 6. `/status` surface

Each model gains a `process` object `{pid, start_time, adopted}`, and a top-level
`reconciliation` summary reports the configured mode, whether this instance owns the
ledger (`ledger_owned`), a `disabled_reason` (set when a live sibling owns the
ledger), and the last startup's adopted/stopped/orphaned/stale/`owned_by_live_broker`
breakdown.

## Alternatives considered

- **pidfd as the durable handle** — rejected: a pidfd is valid only within the
  lifetime of the process that opened it, so it cannot survive the broker's death.
  It remains a sound *in-process* fast-path (noted for future use); `(pid,
  start_time)` is the cross-restart record.
- **`psutil` for identity** — rejected as a hard dep: `/proc`, `ps`, and stdlib
  `os`/`socket` cover everything, keeping the core dependency-free (vendor-neutral,
  smaller image). Allowed only as an optional extra if ever justified.
- **Redis-shared ledger** — rejected: runtimes are local child processes; a record
  is only actionable on the host that spawned it. Sharing it across replicas would
  invite one replica to reason about another host's PIDs.
- **`adopt` or `stop` as the default** — rejected: both take an action on startup,
  which could surprise an operator or (for `stop`) reap a runtime they wanted to
  keep. `report` is the safe, observable default; the action modes are opt-in.
- **Bind-test the port by binding** — rejected in favor of a connect probe: binding
  can race with the about-to-start runtime and needs `SO_REUSEADDR` reasoning; a
  connect probe cleanly answers "is someone already serving here?".

## Consequences

- An unclean broker restart no longer silently collides with or double-loads
  orphaned runtimes: operators see them (`report`), reuse them (`adopt`), or reap
  them (`stop`), and a port collision fails fast with a clear message.
- The broker will **never** signal a process whose identity it cannot confirm,
  upholding *never kill a process the broker did not start* even across PID reuse.
- A new tiny state file lives under `BROKER_CACHE_DIR`; it holds no secrets (pids,
  ports, hashes only) and is `0600`. A corrupt ledger degrades to "no
  reconciliation" with a warning, never a crash and never a wrong kill.
- Two brokers accidentally sharing one `BROKER_CACHE_DIR` can never cross-reconcile:
  the lifetime ownership lock makes `adopt`/`stop` refuse to start and `report`/`off`
  run ledger-less, and the per-record owner check means even a stray record is left
  alone while its owner lives. The only new startup failure is this deliberate
  `adopt`/`stop` refusal, with a remediation ("give each broker its own
  `BROKER_CACHE_DIR`").
- Default behavior is unchanged for v0.6.0 deployments (empty ledger + `report`);
  the fake backend and Ollama are never ledgered, so the existing suite is
  untouched.

## Confirmation

Verified by the test suite (CPU-only, real processes, no GPU):

- `tests/test_runtime_identity.py` — Linux/macOS start-time parsers against
  fixtures (incl. a `comm` with spaces and `)`), `btime` parse, port helpers;
  `kill(0)` liveness + identity gate and identity-gated process-group termination
  against **real** child processes (wrong start time ⇒ never signalled); the
  ledger round-trip, `0600` mode, corrupt/unknown-version/`None`-path fail-safes.
- `tests/test_runtime_reconcile.py` — a real orphaned `http.server` child recorded
  as a prior broker's runtime, then a fresh manager reconciles it: `adopt` marks
  READY without spawning and a later shutdown terminates it **and** clears the
  ledger; `stop` terminates the group and clears; `report` lists it without
  signalling; **PID reuse** (wrong start time) is dropped as stale and the live
  process is left running; a **corrupt ledger** causes no kill; the **port
  pre-check** fails fast naming the port; argv-drift refuses adoption and lists it
  as a non-adoptable orphan; and `python -m broker` runs a graceful lifespan
  shutdown on `SIGTERM`.
- `tests/test_ledger_ownership.py` — multi-replica safety: the `LedgerOwnerLock`
  is exclusive and reports the current holder; `acquire_ledger_ownership` refuses to
  start (`ConfigError` naming the cache dir + owner PID) under `adopt`/`stop` when a
  live sibling owns the ledger, disables the ledger with a `disabled_reason` under
  `report`/`off`, and takes over a stale (dead-owner) lock; the per-record owner
  check leaves a record owned by a **live** broker untouched (listed as
  `owned_by_live_broker`, real orphan survives) and acts only once that owner dies,
  while a legacy record with no owner reconciles as before; and a committed
  **scenario E** starts two real `python -m broker` processes on one cache dir — the
  stop/adopt sibling refuses to start, the report sibling starts with its ledger
  disabled, and broker A keeps serving throughout.
- `tests/test_config.py` — the new settings are validated and present in both
  `.env.example` and `docs/configuration.md`.
