# Documentation map

Read in this order. Each file answers one question, and no file tries to answer two.

| File | Answers | Status |
| --- | --- | --- |
| [01-feasibility-review.md](01-feasibility-review.md) | Is the proposed architecture workable? What must change? | design, stable |
| [02-architecture.md](02-architecture.md) | What are the components and their contracts? | design, stable |
| [03-data-model-and-protocol.md](03-data-model-and-protocol.md) | What exactly is stored, and how do leases/state machines behave? | design, matches code |
| [04-gates-risk-security.md](04-gates-risk-security.md) | What must a change pass, and who may authorise what? | design, matches code |
| [05-metrics-and-roadmap.md](05-metrics-and-roadmap.md) | How do we know it works, and in what order do we build it? | design + M0-M7 |
| [06-references.md](06-references.md) | What does the outside world already know? | evidence, dated 2026-11-22 |
| [07-implementation-log.md](07-implementation-log.md) | What is actually implemented, tried, and abandoned? | **factual log, update on every merge** |
| [08-development-guide.md](08-development-guide.md) | How do I add a gate / lease mode / adapter without breaking the design? | process |

## Rules

- `01`-`05` describe **intended** behaviour. `07` describes **actual** behaviour. When they disagree, `07` is right and a bug exists.
- Any change in behaviour must update `07`. A change to a frozen decision needs a new file in `../adr/`.
- References in `06` must say what they support or contradict. "Interesting paper" without a claim is noise.