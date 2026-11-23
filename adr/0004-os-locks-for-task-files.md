# 0004. Per-task files are protected by OS advisory locks

- Status: accepted
- Date: 2026-11-22

## Context

Every task is one JSON file, and every transition is a read-modify-write. Two writers
racing on the same task (a heartbeat and a review transition, or two retries) silently lose
one update: the file is replaced by whoever wrote last. `docs/07` listed this as a known
weak point; M2 closes it.

The constraint is that the ledger must stay a plain file tree in git, usable from any
interpreter with no daemon, and multiple agent containers on the same host must coordinate.

## Decision

Critical sections take an OS advisory lock on a sidecar file under `.orchestra/locks/`:
`fcntl.flock` on POSIX, `msvcrt.locking` on Windows (`fslock.advisory_lock`). Everything
that mutates a task goes through `TaskLedger.update(task_id, mutator)`; id allocation takes
a single ledger-wide lock. Locks are held for milliseconds and released even on exceptions.

## Rejected options

| Option | Why not |
| --- | --- |
| No lock, rely on "one holder per task" | The lease prevents two *tasks* on one path, not two processes on one task |
| One global lock for the whole journal | Correct but serialises all 50 agents; the lock would become the throughput ceiling |
| Lock rows inside SQLite | Task bodies would have to move into the database, losing file-level diffs and git history |
| `.lock` files created with `O_EXCL` and deleted | A crashed process leaves a stale lock; TTL on locks is a bug factory |

## Consequences

- Concurrent `task attempt` from 6 processes yields `used == 6` (test), where before it lost updates.
- Locks are host-local, like the SQLite store. Multi-host still means Postgres.
- Lock files accumulate in `.orchestra/locks/`; they are gitignored and harmless (empty files).
- On Windows a lock is per-handle, so a process that re-enters the same critical section
  would time out instead of deadlocking forever; the timeout (15s) turns that into an error.

## Falsification

Reopen if a lost update is ever observed again, if lock waits show up as a measurable share
of claim latency, or if lock files start appearing in `git status` (they must not).