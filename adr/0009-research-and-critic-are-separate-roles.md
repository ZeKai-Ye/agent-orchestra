# 0009. Research proposes, a critic refutes; a refutation blocks the conclusion

- Status: accepted
- Date: 2026-11-24

## Context

Two failure modes survived the first two milestones:

1. A roadmap item was a sentence. Nothing forced it to carry a machine-checkable acceptance command,
   so "done" was whatever the author felt. Research output had the same shape: an agent could write
   prose forever and no gate would notice.
2. Everything a gate checked was *mechanical*: does the command run, is the scope free. A vacuous
   verdict (`python -c "print(1)"`) scores 1.0 in triage while proving nothing. No amount of
   schema validation catches a semantically empty acceptance criterion.

Mechanical checks cannot refute a claim; another party has to try.

## Decision

Two extra task roles, each with a deliverable the gate can read:

- `role=research` must write `roadmap/proposals/<task-id>.md`, one `title/accept/scope/size` block per
  proposal. The impl gate fails without it; `roadmap import` turns valid blocks into `later` items and
  records malformed ones as rejected.
- `role=critic` must write `reviews/<task-id>.md` with `verdict` (`refuted|confirmed`), `target`
  (`T-0000` or `R-0000`), `reason`, and `evidence` when refuting. The gate fails without a valid block.
- `orchestra review --task <critic>` applies the verdict: a refuted **task** gets
  `review_verdict=refuted` and can no longer merge (`state -> merged` is refused); a refuted **item**
  goes to `dropped`, so triage never promotes it.

## Rejected options

| Option | Why not |
| --- | --- |
| Let the research agent write items directly | No format contract, no way to reject malformed output |
| Treat a critic as another patch task | A critic must not "fix" the thing it judges; separation is the point |
| Block merges only by human reading | The measured failure (a vacuous verdict scoring 1.0) shows reading does not scale |
| Let a refuted task merge with a note | That is exactly "wrong result enters the conclusion" |
| Auto-confirm when no critic ran | Silence is not agreement; the default stays "unreviewed" |

## Consequences

- Research output is machine-consumable, and its quality is bounded by the same triage as human items.
- A refutation is at least as cheap as a fix (same loop, one file), so refuting is not a luxury.
- Every refuted task carries `review_by`/`review_verdict` in the ledger: the conclusion is blocked with
  evidence, not with a comment.
- This does not make the artifact correct. It makes a specific class of wrongness (empty acceptance,
  uncriticised claim) costly to ship.

## Falsification

Reopen if: a refuted task merges anyway; a refuted item is promoted; or critics confirm everything for
two consecutive rounds (that would mean the role has become a rubber stamp, see docs/05 dissent rate).