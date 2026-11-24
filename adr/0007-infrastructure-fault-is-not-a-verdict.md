# 0007. Infrastructure faults are a distinct exit code

- Status: accepted
- Date: 2026-11-24

## Context

A gate can fail for two very different reasons: the change is wrong, or the machine is broken
(git missing, worktree gone, agent binary not found, disk full). Exiting `1` for both makes a broken
runner look like a bad change: the attempt budget is consumed, the task is requeued, and the loop
retries until the task is escalated for a reason that has nothing to do with the work.

A live agent fleet (docs/09) draws the same line with a third verdict: `FAULT`, explicitly "our
machine failed, it says nothing about your proof".

## Decision

Exit code `6` means **infrastructure fault**: nothing was judged, nothing is charged.

- `GateError` and `OSError` map to `6` (`infrastructure fault: ...` on stderr).
- The loop does not call `return_task` on a fault: the task keeps its state and its attempt counter.
- The agent exit code is a verdict; the *inability to start* the agent is a fault.

Exit codes are now: `0` pass, `1` refused/failed, `2` usage, `3` lease conflict, `4` needs a human
token, `5` admission refused, `6` infrastructure fault.

## Rejected options

| Option | Why not |
| --- | --- |
| Reuse `1` and inspect stderr | Controllers branch on codes, not on English |
| Treat a fault as a rejection and retry | Consumes the attempt budget for a machine problem |
| Retry a fault forever inside the gate | Hides a broken runner instead of surfacing it |
| Charge the fault to the task as a dead approach | Pollutes the "what already failed" record |

## Consequences

- A controller can distinguish "retry now" (`6`), "wait" (`5`), "retry another path" (`3`) and
  "escalate to a human" (`4`).
- Faults are visible in `events`/snapshots but do not move the task, so a broken runner cannot
  escalate the queue.
- The loop leaves the lease in place on a fault; the TTL reaps it. That is deliberate: a fault may
  be transient, and the lease is the evidence that a round started.

## Falsification

Reopen if a fault is ever observed consuming an attempt, or if controllers cannot tell a broken
runner from a bad change using codes `1` and `6`.