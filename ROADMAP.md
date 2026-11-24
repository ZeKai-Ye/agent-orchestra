# Roadmap

The order is deliberate: coordination first, throughput second, governance only once there is data
to govern. Anything not on this page is not planned; see "Not doing" before proposing it.

Legend: `done` shipped and tested, `next` agreed and unblocked, `later` agreed but blocked on data.

## Done -> M1 coordination kernel (v0.1.0)

- [x] Task ledger, one file per task, atomic writes, state machine with whitelisted edges
- [x] Signed-intent requirement for `merged`
- [x] Path leases in SQLite: modes, overlap detection, ordered acquisition, TTL, reaping
- [x] Risk classification T0/T1/T2 from the diff
- [x] Approval tokens bound to the diff digest, single-use, expiring
- [x] Mechanical gates + diff scope guard, one implementation for local and CI

## Done -> M2 admission, resilience, evidence (v0.2.0)

- [x] Budget pre-charge on `task claim`: `intent:<id>` and `lineage:<name>` envelopes, all-or-nothing,
      refusal exits `5` with a machine-readable reason and leaves no lease behind (ADR 0005)
- [x] `budget set` / `budget list` to register envelopes; `--cost` overrides the configured charge
- [x] Per-task file locks (`fslock`): six concurrent `task attempt` calls now land all six updates
      (ADR 0004), plus a ledger-wide lock for id allocation
- [x] Chaos test: an agent is SIGKILLed mid-task, the path stays blocked until the TTL, then
      `lease reap` returns the task to `proposed` and a second agent can claim it
- [x] `status --append` writes a JSONL snapshot; `trend` reports first/last deltas
- [x] `gate run --worktree`: diff a per-task worktree while the ledger stays in the main repo
- [x] `tools/agent_adapter.py`: reference adapter (brief-driven subprocess, credential scrubbing,
      gate verdict), proven end to end with a fake agent
- [x] 51 tests (was 37)

Acceptance met: a killed agent never blocks a path beyond its TTL, and every claim either fits the
envelope or is refused with a reason (both asserted in `tests/test_resilience.py` and
`tests/test_admission.py`).

## Next -> M3 sharded CI and two-level integration

- [ ] Evaluate `funador/claude-code-merge-queue` and `uber/submitqueue` as the merge queue before
      writing one (see `docs/06` section 2); record the comparison as an ADR either way
- [ ] `affected` computation from the dependency graph, with a **graph self-check**: an incremental
      build can silently skip tests (`docs/06` item 6), so the shard map needs its own validation
- [ ] Module integration branches merged into trunk in batches; speculative verification of a batch
- [ ] Metrics: verification duration and failure rate per shard, conflict count per batch size
- [ ] One adapter against a real CLI agent (replacing the fake agent in the test)

## Backlog -> adopted from the pequalnp case study (docs/09)

A live fleet of dozens of agents attacking one open problem exposed a coordination protocol that is
close enough to ours to learn from directly. These items are cheap and orthogonal to M3, so they can
land in any order:

1. `dead_approaches` on the task, rendered into the brief: what was already tried and why it died.
   Highest value per line of code: it removes the most common wasted agent run.
2. Per-holder concurrency cap plus a cooldown after returning a task as `out_of_time` / `other`.
3. Exit code `6` for infrastructure faults: not a verdict, and not charged to the attempt budget.
4. `refute` role: a failing test that disproves a claimed fix is a valid deliverable.
5. Constrained decomposition: one proposal splits a parent into 2-4 child tasks with disjoint paths,
   all inside the parent lease; the parent closes only after the children merge.
6. `needs_human` gate level: soft violations queue for a person instead of failing outright.
7. `events --format md|atom --out`: publish the record (state changes plus the graveyard of dead ends).
## Later -> M4..M7

| Milestone | Content | Blocked on |
| --- | --- | --- |
| M4 contract layer | `contracts/<domain>/vN/`, consumer registry, producer runs all consumer tests | M3 metrics baseline |
| M5 heterogeneous review | input-surface isolation, dissent-rate tracking, sampled re-review, Ed25519 approvals | M4 |
| M6 debt ratchet + refactor licence | ratio metrics, earned headroom, sampled mutation score, competing refactor proposals | M5 |
| M7 research arm + reverse gates | RFC flow, isolated namespace, auto-revert, lineage freeze | M6 |

## Not doing

- A capability-scoring engine used for authorisation. Risk radius decides permissions, not identity.
- Free-form multi-agent chat as shared memory. The repository is the only shared medium.
- Direct commits to trunk. Every merge goes through the gate and carries `Task-Id`.
- Automating irreversible actions. They require a human-signed token, always.
- Re-implementing the agent loop, sandbox, or model routing. Those are somebody else'\''s product
  (`docs/06` section 8).