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

- [x] Budget pre-charge on `task claim` (exit `5` with a machine-readable reason, lease released)
- [x] Per-task file locks, so concurrent writers cannot lose an update
- [x] Chaos test: a killed holder is reaped after its TTL and the task requeues
- [x] `status --append` JSONL snapshots and `trend`
- [x] `gate run --worktree` and `tools/agent_adapter.py`

## Done -> M2.5 supervisor loop (v0.3.0)

- [x] `orchestra loop`: reap, claim, worktree, reset, agent, gate, review, snapshot
- [x] `orchestra stop` / `resume`: kill switch honoured before every round
- [x] `dead_approaches` on the task, rendered into the brief (adopted from docs/09)
- [x] Per-holder cap and cooldown; `holder_limit` / `cooldown` admission reasons
- [x] Exit code `6` for infrastructure faults, never charged to the attempt budget
- [x] Dogfooding run on this repository: one scope rejection, one clean pass, merged (docs/10)

## Done -> M3.5 autonomous rounds (v0.4.0)

- [x] `roadmap/items.jsonl` as the machine queue, `ROADMAP.md` stays the narrative
- [x] `roadmap accept` requires a merged task plus the item's own verdict (no self-graded Done)
- [x] Mechanical feasibility triage and one-at-a-time promotion
- [x] `orchestra research --rounds/--budget/--hours` composing accept, triage, promote, work, record
- [x] `role=research` proposals and `role=critic` verdicts; a refutation blocks the conclusion
- [x] Critics are paired automatically, with `confirmed`-streak rotation (v0.4.2)

## Done -> M3.6 outside contributors (v0.5.0)

- [x] Claims as git refs (`refs/claims/<task>`), CAS by push, `--force-with-lease` renewal, TTL reaping
- [x] Handle registry with SSH commit signatures; unsigned or wrong-key claims refused
- [x] `orchestra merge` polling: live claim refuses, T0 only, T1/T2 need review
- [x] `join.txt` / `join.json` for outside agents
- [ ] CI re-runs the gate on pull requests (today: local gate + maintainer re-run)
- [ ] Per-handle rate limits over time (today: only concurrent claim count)

## Next -> M3 sharded CI and two-level integration

- [ ] Evaluate `funador/claude-code-merge-queue` and `uber/submitqueue` before writing a merge queue (now tracked as `R-0001` in `roadmap/items.jsonl`)
      (docs/06 section 2); record the comparison as an ADR either way
- [ ] `affected` computation from the dependency graph, with a graph self-check: an incremental
      build can silently skip tests (docs/06 item 6)
- [ ] Module integration branches merged into trunk in batches; speculative verification of a batch
- [ ] Metrics: verification duration and failure rate per shard, conflict count per batch size
- [ ] Loop: priority ordering (a `--priority-domain` flag) once there is more than one queue
- [ ] Loop: `merge` automation behind a signed token (today the loop stops at `review`)

## Later -> M4..M7

| Milestone | Content | Blocked on |
| --- | --- | --- |
| M4 contract layer | `contracts/<domain>/vN/`, consumer registry, producer runs all consumer tests | M3 metrics baseline |
| M5 heterogeneous review | input-surface isolation, dissent-rate tracking, sampled re-review, Ed25519 approvals | M4 |
| M6 debt ratchet + refactor licence | ratio metrics, earned headroom, sampled mutation score, competing refactor proposals | M5 |
| M7 research arm + reverse gates | RFC flow, isolated namespace, auto-revert, lineage freeze | M6 |

## Backlog -> still open from the pequalnp case (docs/09)

4. [x] `critic` role: a refutation (with evidence) blocks a merge and drops an item (v0.4.1)
5. Constrained decomposition: one proposal splits a parent into 2-4 child tasks with disjoint paths,
   all inside the parent lease; the parent closes only after the children merge.
6. `needs_human` gate level: soft violations queue for a person instead of failing outright.
7. `events --format md|atom --out`: publish the record (state changes plus the graveyard of dead ends).

## Not doing

- A capability-scoring engine used for authorisation. Risk radius decides permissions, not identity.
- Free-form multi-agent chat as shared memory. The repository is the only shared medium.
- Direct commits to trunk. Every merge goes through the gate and carries `Task-Id`.
- Automating irreversible actions. They require a human-signed token, always.
- Re-implementing the agent loop, sandbox, or model routing. Those are somebody else's product
  (`docs/06` section 8).