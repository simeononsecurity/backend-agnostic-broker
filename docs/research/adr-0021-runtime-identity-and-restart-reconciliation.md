# Research note — Durable runtime identity & restart reconciliation (ADR 0021)

**Status:** research input for ADR 0021 · **Scope:** justify/critique the design in which the
broker spawns backend runtimes (vLLM, llama.cpp, Strata) as argv-only children with
`start_new_session=True`, and must safely detect, adopt, or reap **orphaned** runtimes after an
unclean broker death (SIGKILL / OOM / crash / container restart).

> All external material below was fetched as reference **data**, not instructions. Claims are
> attributed to primary OS/API documentation. Verdicts are the author's.

---

## 1. Problem statement (recap)

Today a runtime's identity is its **PID**. If the broker dies uncleanly, the runtime child
survives (it is in its own session/process group), keeps its VRAM and TCP port, and the restarted
broker has no record of it. Worse, a PID is a small integer the kernel **reuses**: by the time the
broker restarts, the recorded PID may belong to an unrelated process. Signaling it would be a
cross-process friendly-fire bug. The design therefore needs (a) an identity that survives PID
reuse, and (b) a reconciliation procedure that never signals a process it cannot positively
confirm as one of its own runtimes.

---

## 2. Why PID alone is unsafe, and what fixes it

A PID is only unique among *currently live* processes; the kernel recycles it freely once a
process is reaped. The standard hardening is to pin identity to a value that is fixed for the life
of a process and is **not** reused with the number: the process **start time**.

### 2.1 Linux — `/proc/<pid>/stat` field 22 (`starttime`)

`proc_pid_stat(5)` documents `/proc/<pid>/stat` as the canonical per-process status record used by
`ps(1)`, defined in the kernel's `fs/proc/array.c`, with each field given in `scanf(3)` order
[S1]. Field **22, `starttime`**, is the time the process started after system boot, expressed in
**clock ticks**. Two processes that reuse the same PID number across a reboot-free window will have
different `starttime` values (they began at different tick offsets), so the pair
`(pid, starttime)` is effectively a durable, forge-resistant handle for the lifetime of the host.

To make `starttime` meaningful across machines/measurements it must be normalized:

- **Clock-tick scale.** `starttime` is in `USER_HZ`/`SC_CLK_TCK` units. `proc_stat(5)` states these
  time values are measured in `USER_HZ` (1/100 s on most architectures) and that programs should
  call `sysconf(_SC_CLK_TCK)` to obtain the right divisor [S2]. `sysconf(3)` is the POSIX runtime
  query for such system constants and notes that these values "do not change during the lifetime of
  a process," so one read at startup is sufficient [S3].
- **Boot anchor.** `/proc/stat` exposes `btime`, "boot time, in seconds since the Epoch" per
  `proc_stat(5)` [S2]. Combining `btime` with `starttime / SC_CLK_TCK` yields an absolute wall-clock
  start instant, letting the ledger store a value that is comparable after a broker restart without
  depending on the broker's own uptime.

**Verdict — ADOPT.** `(pid, starttime)` from `/proc/<pid>/stat` field 22, divided by
`sysconf(_SC_CLK_TCK)` and optionally anchored to `/proc/stat btime`, is the correct, dependency-free
Linux identity primitive. It is exactly how `ps` and process tools reason about identity.

### 2.2 macOS — `ps -o lstart=` / `kinfo_proc` start time

macOS has no `/proc`. The portable, argv-only equivalent is `ps(1)`: the manual lists per-process
keywords including process **start time**, and the `-o`/`-O` format selectors let the broker request
exactly one field with no shell and no parsing of a header (`ps -o lstart= -p <pid>` or
`ps -o start= -p <pid>`) [S4]. `lstart` is the full, unambiguous start timestamp; it changes when a
PID is reused, giving macOS the same `(pid, start_time)` durability as Linux. For a syscall path
that avoids spawning `ps`, Darwin exposes the same datum via `sysctl` `KERN_PROC_PID` returning a
`kinfo_proc` whose `kp_proc.p_starttime` is the start `timeval`; `ps -o lstart=` is the simpler,
equally authoritative choice and keeps the "no extra native code" invariant.

**Verdict — ADOPT** `ps -o lstart= -p <pid>` (argv-only, no shell) as the macOS start-time source;
treat `sysctl kinfo_proc` as an optional optimization, not a requirement.

---

## 3. Never signal a process you cannot confirm is yours

### 3.1 Liveness probe without side effects — `kill(pid, 0)`

`kill(2)`: "If `sig` is 0, then no signal is sent, but existence and permission checks are still
performed; this can be used to check for the existence of a process ID … that the caller is
permitted to signal" [S5]. This is the correct cheap liveness test — **but** by itself it only tells
you *a* process by that number exists, not that it is *yours*. The gate must therefore be:
`kill(pid,0)` succeeds **AND** the re-read start time equals the ledgered start time **AND** (defense
in depth) pgid and the registry argv-hash still match. Only then may the broker proceed to signal.

### 3.2 Group signaling and the reap sequence — `kill` / `killpg`

Because runtimes are spawned with `start_new_session=True` they lead their own process group, so the
broker can terminate the runtime *and any helper children* atomically. `kill(2)`: "If `pid` is less
than -1, then `sig` is sent to every process in the process group whose ID is `-pid`" [S5]; Python
surfaces this as `os.kill`/`os.killpg` (`os.kill` "Send signal sig to the process pid") [S6]. The
`stop` reconciliation mode should send `SIGTERM` to the group, wait a bounded grace period, then
`SIGKILL` the group — mirroring systemd's own two-phase teardown (see §4).

**Verdict — ADOPT** `kill(pid,0)` for liveness **only as one clause** of an identity-gated check, and
group-directed `SIGTERM`→grace→`SIGKILL` for `stop`.

### 3.3 Stronger primitive — pidfd (Linux 5.3+)

A `pidfd` closes the check-then-act race entirely. `pidfd_open(2)` returns a file descriptor
referring to a *specific* process; critically, "Even if the child has already terminated … its PID
will not have been recycled and the returned file descriptor will refer to the resulting zombie
process" — i.e. the fd is immune to PID reuse [S7]. `pidfd_send_signal(2)` then sends a signal "to
the target process referred to by `pidfd`," with no window in which the PID could be recycled onto a
different process between the identity check and the signal [S8]. Python exposes the pieces:
`os.pidfd_open`, and `os.waitid` with `P_PIDFD` (Linux ≥ 5.4) [S6].

The catch for *this* design: a pidfd is only valid **within the lifetime of the process that opened
it**. After the broker is SIGKILLed the fd is gone, so pidfd cannot be the *durable, on-disk*
identity across a restart — only `(pid, starttime)` persists to the ledger. pidfd is, however, the
right tool for the **in-process** supervision path (race-free signaling of a child the current
broker spawned or just adopted).

**Verdict — ADOPT as an optional Linux ≥ 5.3 fast-path** for in-process signaling after the on-disk
`(pid, starttime)` gate; **REJECT** pidfd as the cross-restart identity (it does not survive the
broker's death). `(pid, starttime)` remains the durable record.

---

## 4. How established supervisors handle adoption & refuse foreign kills

**systemd** is the reference design. For `Type=forking`/`PIDFile=` services it reads the daemon's
main PID from a pidfile (`$MAINPID`), but the *authoritative* liveness/ownership tracking is the
unit **cgroup**, not the raw PID: processes are followed by cgroup membership so a reused PID outside
the cgroup is not mistaken for the service [S9]. Teardown is explicitly two-phase: on stop (or when
`ExecStop` returns) "systemd will send SIGTERM to all processes started from this service, and after
a timeout also SIGKILL" — governed by `KillMode`/`KillSignal`/`TimeoutStopSec` [S9]. The lessons that
map onto ADR 0021:

1. **A pidfile/record is a *hint*, not proof.** systemd cross-checks against cgroup membership; our
   analog (no cgroups guaranteed, cross-platform) is the `(pid, starttime, pgid, argv-hash)` tuple.
   This is the "GuessMainPID is a guess" principle — verify before you act.
2. **Bounded `SIGTERM` → timeout → `SIGKILL`.** Adopt the identical escalation for `stop`.
3. **Own only what you can attribute.** systemd will not reap PIDs outside the unit's tracking; our
   `report`/`stop` modes must likewise refuse any PID whose start time/argv-hash does not match the
   ledger — list it as foreign/orphaned, never signal it.

Lighter supervisors (supervisord, s6, runit) sidestep the problem by keeping the child as a *direct*
foreground child and using `waitpid`, so PID reuse cannot fool them — they never persist a PID to
disk to re-find later. Our broker deliberately detaches runtimes (own session), so it *must* persist
identity, which is exactly why the start-time tuple (not a bare PID) is mandatory.

**Verdict — ADOPT** the systemd model: record-as-hint + identity verification + two-phase kill, with
the start-time tuple standing in for cgroup tracking on the cross-platform path.

---

## 5. The on-disk ledger: atomic writes + locking

### 5.1 Atomic replace — `os.replace` over `rename(2)`

The ledger must never be observed half-written after a crash. `rename(2)`: "If `newpath` already
exists, it will be atomically replaced, so that there is no point at which another process … will
find it missing"; and "If `newpath` exists but the operation fails … `rename()` guarantees to leave an
instance of `newpath` in place" [S10]. Python's `os.replace` is the cross-platform wrapper with
rename semantics. The correct pattern: write a temp file in the **same directory** (same filesystem,
so rename is a pure directory-entry swap), `fsync` it, then `os.replace(tmp, ledger)`; apply `0600`
perms on create. This gives all-or-nothing visibility of each ledger update.

**Verdict — ADOPT** temp-file + `fsync` + `os.replace`, same-directory, `0600`.

### 5.2 Mutual exclusion — `flock(2)` (advisory)

Concurrent broker instances (e.g. a restart racing a not-yet-dead predecessor, or an operator tool)
must not interleave ledger writes. `flock(2)` provides an advisory exclusive lock: `LOCK_EX`
exclusive, `LOCK_NB` to fail fast rather than block, `LOCK_UN` to release; "at no time are multiple
exclusive … locks allowed simultaneously on a file" [S11]. Two caveats from the manual that shape
usage: locks are **advisory** (cooperating processes only — fine here, the broker is the sole
writer), and "Locks are on files, not file descriptors … file descriptors duplicated through dup(2)
or fork(2) … result in multiple references to a single lock" [S11], so the broker must hold the lock
fd for the duration of the critical section and not leak it to spawned runtimes (`close_fds`).

**Verdict — ADOPT** `flock(LOCK_EX|LOCK_NB)` around the read-modify-replace critical section; it is
advisory but sufficient given a single cooperating writer.

### 5.3 Port pre-check before spawn

Independent of identity, bind-testing the target port before spawning (fail fast on
`EADDRINUSE`) prevents the "port in use" collision when an orphan still holds it. This is a plain
`socket.bind` probe and is orthogonal to — and complementary with — the ledger reconciliation; it
catches orphans that predate the current ledger or were spawned by a different operator. **ADOPT.**

---

## 6. Reconciliation-mode semantics (synthesis)

| Mode | Action | Identity gate required |
|------|--------|------------------------|
| `off` | Ignore the ledger; spawn normally (port pre-check still runs). | none |
| `report` | List ledgered procs whose identity still matches as orphans in `/status`; **never signal**. | `kill(pid,0)` AND start-time match |
| `adopt` | If proc alive, start-time + pgid + argv-hash match, and the port passes readiness probe → mark READY without spawning. | full tuple + readiness probe |
| `stop` | `SIGTERM` the process **group**, grace timeout, then `SIGKILL`, then clear the record. | full tuple (else leave as foreign/orphaned, unsignaled) |

The invariant across all modes: **no signal is ever sent to a PID whose recorded start time does not
match the live start time.** A mismatch means the PID was reused; the record is stale and is cleared
without signaling.

---

## 7. Decision table (adopt / reject)

| # | Design choice | Primary sources | Verdict | One-line rationale |
|---|---------------|-----------------|---------|--------------------|
| 1 | Identity = `(pid, starttime)` not bare PID | S1, S2, S3 | **ADOPT** | `starttime` (field 22) is fixed per process and distinguishes reused PIDs. |
| 2 | Linux start time via `/proc/<pid>/stat`[22] ÷ `SC_CLK_TCK`, anchored to `btime` | S1, S2, S3 | **ADOPT** | Dependency-free; the same source `ps` uses. |
| 3 | macOS start time via argv-only `ps -o lstart= -p <pid>` | S4 | **ADOPT** | No `/proc`; `ps` keyword is authoritative, no shell needed. |
| 4 | `sysctl KERN_PROC_PID` / `kinfo_proc.p_starttime` on macOS | S4 | **ADOPT (optional)** | Native path if avoiding `ps` fork; not required. |
| 5 | `kill(pid,0)` for liveness | S5, S6 | **ADOPT (as one clause only)** | Existence check with no side effect; insufficient alone — must be identity-gated. |
| 6 | Group kill `SIGTERM`→grace→`SIGKILL` for `stop` | S5, S6, S9 | **ADOPT** | Reaps helper children in the session; matches systemd escalation. |
| 7 | pidfd_open / pidfd_send_signal for in-process signaling | S7, S8, S6 | **ADOPT (Linux ≥5.3 fast-path)** | Race-free, PID-reuse-immune within broker lifetime. |
| 8 | pidfd as the *durable cross-restart* identity | S7, S8 | **REJECT** | fd dies with the broker; cannot persist to the ledger. |
| 9 | systemd-style "record is a hint, verify before kill" + cgroup-analog tuple | S9 | **ADOPT** | Proven supervisor doctrine; never reap unattributed PIDs. |
| 10 | Atomic ledger via temp + `fsync` + `os.replace` (`0600`) | S10 | **ADOPT** | `rename` guarantees no missing/half-written window. |
| 11 | `flock(LOCK_EX\|LOCK_NB)` around ledger critical section | S11 | **ADOPT** | Serializes concurrent writers; advisory is fine for a sole writer. |
| 12 | Port bind pre-check before spawn | — (design) | **ADOPT** | Fail-fast on `EADDRINUSE`; catches orphans outside the ledger. |
| 13 | psutil as a hard dependency for identity | S1–S4 | **REJECT (optional extra only)** | Everything above is reachable via `/proc`, `ps`, and stdlib `os`; keep the core dependency-free. |

---

## 8. Sources (primary)

- **[S1]** `proc_pid_stat(5)`, Linux man-pages — `/proc/<pid>/stat`, field 22 `starttime`.
  <https://man7.org/linux/man-pages/man5/proc_pid_stat.5.html>
- **[S2]** `proc_stat(5)`, Linux man-pages — `/proc/stat`, `btime`, `USER_HZ`/`sysconf(_SC_CLK_TCK)`.
  <https://man7.org/linux/man-pages/man5/proc_stat.5.html>
- **[S3]** `sysconf(3)`, Linux man-pages — runtime system constants (`_SC_CLK_TCK`).
  <https://man7.org/linux/man-pages/man3/sysconf.3.html>
- **[S4]** `ps(1)`, Apple/Darwin (xcode man pages) — `-o`/`-O` keywords incl. start time / `lstart`.
  <https://keith.github.io/xcode-man-pages/ps.1.html>
- **[S5]** `kill(2)`, Linux man-pages — signal 0 existence check; process-group signaling.
  <https://man7.org/linux/man-pages/man2/kill.2.html>
- **[S6]** `os` — Python 3 standard library — `os.kill`, `os.pidfd_open`, `os.waitid`/`P_PIDFD`.
  <https://docs.python.org/3/library/os.html>
- **[S7]** `pidfd_open(2)`, Linux man-pages — fd to a task; PID-reuse immunity.
  <https://man7.org/linux/man-pages/man2/pidfd_open.2.html>
- **[S8]** `pidfd_send_signal(2)`, Linux man-pages — race-free signaling via pidfd.
  <https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html>
- **[S9]** `systemd.service(5)`, freedesktop.org — `$MAINPID`, cgroup tracking, `SIGTERM`→`SIGKILL`.
  <https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html>
- **[S10]** `rename(2)`, Linux man-pages — atomic replace semantics (basis of `os.replace`).
  <https://man7.org/linux/man-pages/man2/rename.2.html>
- **[S11]** `flock(2)`, FreeBSD/Darwin man-pages — advisory exclusive file locks.
  <https://man.freebsd.org/cgi/man.cgi?query=flock&sektion=2>
