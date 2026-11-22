# 0001. Write leases live in SQLite, not in a git-tracked lock file

- Status: accepted
- Date: 2026-11-22

## Context

The reviewed design put path leases in a repository lock file so that they are visible and versioned.
With tens of concurrent agents that fails three ways at once: one file is a single-writer hot spot;
concurrent writers produce merge conflicts in the thing that is supposed to prevent conflicts; and a
file has no atomic compare-and-swap. Any of the three is enough to lose mutual exclusion, and the
failure is silent - two agents believe they hold the same path.

## Decision

Leases live in `SQLite` (`<repo>/.orchestra/state.db`) with WAL, acquired inside a single
`BEGIN IMMEDIATE` transaction that re-checks for overlaps before inserting. Auditability is restored
by exporting active leases to `ledger/leases.snapshot.jsonl` on demand (`lease snapshot`).

## Rejected options

| Option | Why not |
| --- | --- |
| Lock file in git | Single-writer hot spot, merge conflicts, no CAS |
| Lock files in the working tree, gitignored | Still no CAS across processes; crash leaves stale locks |
| External Redis / etcd | Another service to run and secure, for a single-host workload |
| `flock` on a file | Advisory, per-host, and no path-scoped semantics |

## Consequences

- Mutual exclusion is real: `lease acquire` from 8 processes yields exactly one winner.
- The lock is invisible to `git diff`, so reviews no longer see who holds what by default; the
  snapshot command and the `events` table are the replacement.
- Single-host assumption is now explicit. Multi-host means Postgres, and the seam is
  `leases.LeaseManager` - not scattered call sites.

## Falsification

Reopen if: leases must be shared across machines without a database; or if the exported snapshot
proves useless in practice (nobody reads it while debugging a conflict).