# Roadmap

The order is deliberate: coordination first, throughput second, governance only once there is data
to govern. Anything not on this page is not planned; see "Not doing" before proposing it.

Legend: `done` shipped and tested, `next` agreed and unblocked, `later` agreed but blocked on data.

## Now -> M1 coordination kernel (done)

- [x] Task ledger, one file per task, atomic writes, state machine with whitelisted edges
- [x] Signed-intent requirement for `merged`
- [x] Path leases in SQLite: modes, overlap detection, ordered acquisition, TTL, reaping
- [x] Risk classification T0/T1/T2 from the diff
- [x] Approval tokens bound to the diff digest, single-use, expiring
- [x] Mechanical gates + diff scope guard, one implementation for local and CI
- [x] 37 tests, including a true 8-process lease race

## Next -> M2 admission, resilience, evidence

- [ ] Budget pre-charge wired into `task claim` (the ledger and the refusal path already exist)
- [ ] Chaos test: SIGKILL a lease holder mid-task, assert `lease reap` restores `proposed`
- [ ] `status --json` appended as a JSONL time series so trends are possible
- [ ] Per-task file locking (two writers to one task file currently lose an update)
- [ ] Adapter reference implementation for one CLI agent (subprocess, brief-driven)

Acceptance: a killed agent never blocks a path for longer than its TTL; every claim either fits the
budget envelope or is refused with a machine-readable reason.

## Later -> M3..M7

| Milestone | Content | Blocked on |
| --- | --- | --- |
| M3 sharded CI + two-level integration | `affected` computation, module integration branches, batched merge queue, speculative verification, **dependency-graph self-check** (see `docs/06` item 6) | evaluate an existing merge queue first (`docs/06` section 2) |
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