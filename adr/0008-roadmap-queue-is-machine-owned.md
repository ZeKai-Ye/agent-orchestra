# 0008. The roadmap queue is machine-owned; Done needs a merged task and a verdict

- Status: accepted
- Date: 2026-11-24

## Context

A roadmap edited by hand is a wish list: checkboxes get flipped when someone feels done, "Next" grows
every time a plan is written, and nothing stops an agent from claiming a Later item that cannot even
run. M3 already showed that an unattended loop needs machine-checkable gates; the roadmap is the same
problem one level up. If the queue is prose, the round driver has nothing to decide on.

## Decision

Two artefacts, one invariant.

- `ROADMAP.md` stays the human narrative (why, in what order, what we refuse to do).
- `roadmap/items.jsonl` is the queue the engine works on: `{id, status, title, accept, scope, size}`.
- `roadmap accept` is the **only** path to `done`, and it requires (a) a `merged` task whose `roadmap`
  field names the item, and (b) the item's own `accept` command exiting 0.
- Promotion is one at a time: exactly one item may be `next`. Feasibility is triaged mechanically
  (`accept-runnable`, `scope-exists`, `scope-free`, weighted score >= 0.6) and an infeasible item is
  never promoted.
- The driver (`orchestra research`) honours `--rounds`, `--budget`, `--hours` and the stop file; when
  it cannot proceed it stops with `needs_human` instead of guessing.

## Rejected options

| Option | Why not |
| --- | --- |
| Parse `ROADMAP.md` for checkboxes | Fragile, and editing prose would change machine state |
| Let the agent mark its own item done | Self-grading; the whole point is an external verdict |
| Accept an item without a merged task | "Done" would mean "an agent said so", not "the repository changed" |
| Promote several Later items at once | Scope collisions and an ambiguous acceptance order |
| Auto-write the ROADMAP.md narrative | The "why" is the part humans must keep |

## Consequences

- A human sets an envelope and reads `roadmap/STATE.md`; nothing else is required for a run.
- The queue can drift from the narrative. `roadmap check` catches structural problems; keeping the two
  in step is a review duty, deliberately not automated.
- Items need a runnable verdict up front, which forces "done" to be defined before work starts.

## Falsification

Reopen if: an item reaches `done` without a merged task or without its verdict; more than one item is
`next` at once; or the queue and the narrative disagree for more than one round.