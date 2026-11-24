# 0006. A supervisor loop owns the retry policy

- Status: accepted
- Date: 2026-11-24

## Context

An agent that runs once per invocation cannot "keep working" by itself: sessions grow, behaviour
drifts, a crash loses everything, and nothing is auditable afterwards. Something has to decide what
happens next, how many times, and when to stop. If that something is the agent, the retry budget and
the admission rules exist only as prose inside a prompt.

M2 exposed the concretes: leases expire, tasks need a reason when handed back, and a failed attempt
leaves a worktree behind that poisons the next one.

## Decision

A plain process, `orchestra loop`, drives rounds:

```
reap -> pick task -> claim (admission) -> worktree + brief -> reset -> agent -> gate
     -> pass: review | fail: return(reason), exhaust: escalate | snapshot metrics
```

Retry policy lives here, never in the agent. Three supporting rules are part of the decision:

- **Dead approaches are part of the brief.** `task return` writes `dead_approaches` (reason + note);
  the next claimant, same or different, sees them in `brief.json`.
- **Per-holder cap and cooldown.** `per_holder_limit` bounds how many tasks one holder may hold;
  a task handed back as `out_of_time`/`other` is hidden from that holder for `cooldown_minutes`.
- **A round starts clean.** `reset_worktree` (git reset --hard + clean -fd) runs before the agent and
  after any failed round.

The loop stops at `review`. Merging stays human: `merge` gate plus `git merge` (M3 owns the queue).

## Rejected options

| Option | Why not |
| --- | --- |
| Let the agent continue its own session | Context bloat, drift, no audit trail, one crash loses the round |
| `while true` around `task claim` with no policy | Bypasses admission and the attempt budget; burns budget on a stuck task |
| Cooldown stored in the task file | It is per (holder, task) and ephemeral: SQLite is the right place |
| Reset the worktree only at creation | A failed round leaves changes behind; measured: the next round then fails scope forever |
| Auto-merge T0 changes | Ledger would claim `merged` while git has not merged; merge ordering is M3 |

## Consequences

- `loop --once` is cron-friendly; `--max-rounds`/`--idle-sleep` cover long runs; `stop`/`resume` are
  a file, so a stuck controller can be stopped by hand.
- Every failure leaves a reason and an attempt; escalation is mechanical, not a judgement call.
- The measured cost of a round is ~0.6-1.1s when the agent is a stub; the gate dominates real rounds.

## Falsification

Reopen if: discarded worktrees ever leak into a later round again; a holder collects tasks past its
cap; or cooldown ever hides a task from every holder (that would mean the pool starved).