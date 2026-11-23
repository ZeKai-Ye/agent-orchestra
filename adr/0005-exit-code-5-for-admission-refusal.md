# 0005. Admission refusal is its own exit code

- Status: accepted
- Date: 2026-11-22

## Context

Until M2, "the WIP limit is full" and "this change is wrong" both exited `1`. An agent cannot
tell the two apart: one means *wait and retry later*, the other means *change your approach*.
Retrying a wrong change burns budget; escalating a full queue wastes a coordinator's attention.
Budget envelopes make this worse: they are a normal, expected refusal that will happen every day.

## Decision

Admission refusals (WIP limit, budget envelope) exit `5`, print `admission refused: <reason>`
plus a JSON detail on stderr (`{"reason": "budget_exceeded", "scope": ..., "spent": ..., "ceiling": ...}`
or `{"reason": "wip_limit", "wip": ..., "limit": ...}`), and leave no side effects: the lease
taken before the budget check is released, and the task stays `proposed`.

## Rejected options

| Option | Why not |
| --- | --- |
| Reuse `1` with a distinguishable message | Agents branch on exit codes, not on English |
| One code per reason (5 = WIP, 6 = budget) | The set grows with every policy; the reason belongs in the payload |
| Refuse inside the gate instead of at claim | Wasting an agent run to discover there was no budget |
| No exit code, JSON only on stdout | stdout is already the machine-readable payload; a silent success code would hide the refusal |

## Consequences

- Exit codes are now: `0` pass, `1` refused/failed, `2` usage, `3` lease conflict, `4` needs a
  human token, `5` admission refused. Controllers can implement backoff (5), conflict retry (3)
  and escalation (4) separately.
- A refused claim leaves the ledger untouched, so the queue length is not inflated by attempts.
- Budget refusal happens *after* the lease is taken and releases it, which is one extra
  transaction; correctness beats the microseconds.

## Falsification

Reopen if controllers cannot distinguish retryable from terminal failures with these five
codes, or if a refusal ever leaves a lease or a task in a non-`proposed` state (test covers both).