# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

Nothing yet.

## [0.5.1] - 2026-11-24

### Added

- `suspended` task state: when a lease TTL expires the task is reserved for its holder for
  `[scheduler] resume_grace_minutes` (default 120) instead of jumping back into the pool.
- `task resume --task T --holder H`: the same holder resumes inside the grace, keeping its attempt
  counter (a disconnect is not a failed attempt).
- `task status --task T --holder H`: a reconnecting agent learns the verdict directly -
  `yours` / `suspended-for-you` / `reassignable` / `suspended-for-other` / `held-by:<other>` /
  `terminal:<state>`.
- Reassignment: once the grace expires, `lease reap` returns the task to `proposed` with a history
  note; a task whose attempt budget is already exhausted is `abandoned` as unrecoverable.
- `task claim` refuses a suspended task with exit 5 and `reason=suspended` (holder + until in the JSON).
- docs/13-disconnect.md, join.txt reconnect section; 108 tests (was 101).

### Changed

- `test_killed_holder_is_reaped_after_ttl` now asserts the two-step recovery (suspended, then
  reassignable after the grace) instead of an immediate requeue.
## [0.5.0] - 2026-11-24

### Added

- Git-native claims: `orchestra claim|beat|release|reap` over `refs/claims/<task>`, with
  `--force-with-lease` for renewal/release and TTL-based reaping.
- Handle registry (`handles/<handle>.pub`) and SSH commit signing; an unsigned claim is refused and a
  signature from the wrong key is rejected (`git verify-commit` + allowed_signers).
- `orchestra merge --task T`: refuses live claims, re-runs the merge gate in a detached worktree and
  fast-forwards only `T0` work; `T1/T2` return exit 4.
- `join.txt` / `join.json`: the paste-once contract for outside agents (still data, not commands).
- ADR 0010 and docs/12; 101 tests (was 96), including a real two-clone claim race.

### Fixed

- claim commits were built with CRLF in tree entries (Windows text-mode stdin), which made
  `claims/<task>.json` unreadable; tree building now uses `mktree -z` with binary input.
## [0.4.2] - 2026-11-24

### Added

- Automatic critic pairing: every imported `S/M/L` item and every task that reaches `review` with a
  roadmap item (or `role=research`) gets one adversarial task.
- Reviewer rotation: `[critic] pool` / `max_confirm_streak`; a holder with two consecutive
  `confirmed` verdicts is skipped, a `refuted` resets its streak. Saturated pools still pair and flag
  `rotated=true`.
- `orchestra critic pair|status`; streaks live in the `critic_stats` table and update on
  `review --task`.
- `review --task` now closes the critic task (release lease, `review -> approved -> merged`), so two
  critics on the same `reviews` scope no longer collide.
- Gate ignores engine-owned roadmap state (`roadmap/items.jsonl`, `roadmap/STATE.md`) when computing
  the change under review; proposals stay in scope.
- 96 tests (was 89), including rotation, saturation and auto-pairing in a research round.
## [0.4.1] - 2026-11-24

### Added

- `role=research` with a machine-read deliverable: `roadmap/proposals/<task-id>.md`, one
  `title/accept/scope/size` block per proposal; the impl gate fails without a valid file.
- `roadmap import --task T [--from worktree]` turns proposals into `later` items and reports rejected
  blocks; the round driver imports automatically after a research round passes.
- `role=critic` with a `reviews/<task-id>.md` verdict (`refuted|confirmed`, `target`, `reason`,
  `evidence`); a refutation without evidence fails the gate.
- `orchestra review --task T` applies a verdict: a refuted task gets `review_verdict=refuted` and can
  never merge; a refuted roadmap item becomes `dropped`.
- `task new --target T-0000|R-0000`; `run_agent`-driven worktrees can carry proposals/verdicts before
  the change is merged (`--from`).
- ADR 0009 (research/critic separation), docs/11 section 9 with the two-round run.
- 89 tests (was 80), including refutation-blocks-merge and empty-verdict cases.
## [0.4.0] - 2026-11-24

### Added

- `orchestra roadmap`: `list|check|add|triage|accept|promote` over `roadmap/items.jsonl`, with an
  id scheme (`R-0000`), a status machine (`later/next/done/dropped`) and shape validation.
- `roadmap accept` is the only path to `done`: it needs a merged task that names the item **and** the
  item's own `accept` command to exit 0 (no self-graded Done).
- Feasibility triage (`accept-runnable`, `scope-exists`, `scope-free`, weighted score) and promotion
  limited to one `next` item at a time.
- `orchestra research --rounds N --budget B --hours H`: accept the previous round, triage Later,
  promote one, run exactly one loop round, record `roadmap/STATE.md`. Stops with `needs_human` rather
  than guessing.
- `task new --roadmap R-0000` so a task names the item it serves; tasks with an invalid id are refused.
- docs/11 (round protocol), ADR 0008 (machine-owned queue), a seeded queue in this repository.
- 80 tests (was 65), including 15 roadmap/round tests.
## [0.3.0] - 2026-11-24

### Added

- `orchestra loop`: supervisor over `reap -> claim -> worktree -> reset -> agent -> gate -> review`,
  with `--once`, `--max-rounds`, `--idle-sleep`, `--holder`, `--timeout` and per-round metric snapshots.
- `orchestra stop` / `resume`: a kill-switch file checked before every round.
- `dead_approaches` on tasks (`task dead`, `task return` with a reason enum) rendered into the brief,
  so a retry can see what already died.
- Per-holder admission: `per_holder_limit` and `cooldown_minutes`; a task returned as
  `out_of_time`/`other` hides from that holder for the cooldown. `holder_limit` and `cooldown` are
  new admission reasons alongside `wip_limit` and `budget_exceeded`.
- Exit code `6` for infrastructure faults: not a verdict, not charged to the attempt budget.
- `reset_worktree`: every round starts from the committed state (git reset --hard + clean -fd).
- `tools/agent_adapter.py` gained `gate run --worktree` support indirectly: the loop diffs the
  per-task worktree while the ledger stays in the main repository.
- `examples/loop_demo_agent.py`: deterministic stand-in used by the dogfooding run (docs/10).
- ADR 0006 (supervisor loop) and ADR 0007 (fault exit code); docs/10 with the experiment record.
- 65 tests (was 51), including a two-round loop test and the killed-holder chaos test.

### Fixed

- A failed round left its worktree dirty, so the next attempt failed `scope` forever (found by
  dogfooding; T-0001 was escalated by this bug and is recorded as abandoned).
- `loop` ran the worktree copy of the agent script, which is the committed version and not the
  working copy; documented as "use an absolute path" in docs/08.
- `docs/09-case-pequalnp.md`: case study of pequalnp.com, a live fleet of agents attacking P vs NP.
  Documents the desk/lease/heartbeat protocol, the single-judge design, refutation parity,
  constrained decomposition, the three verdicts (accepted / rejected / fault), and the public event
  record - plus what we adopt, what we refuse to copy, and what cannot be observed from outside.
- ROADMAP backlog distilled from that case (dead approaches, per-holder caps and cooldown,
  infrastructure-fault exit code, `refute` role, constrained decomposition, `needs_human`,
  publishable event record).

## [0.2.0] - 2026-11-22

M2 of the roadmap: admission control, resilience under a killed agent, evidence over time, and a
reference adapter. No task-schema change, so v0.1.0 ledgers keep working.

### Added

- Budget envelopes with all-or-nothing pre-charge on claim: `intent:<id>` and `lineage:<name>`
  scopes, `budget set|list`, `--cost` override. A refusal exits `5`, prints a machine-readable
  reason, and releases the lease it had already taken.
- Exit code `5` (`admission refused`) separates "wait and retry" from "this change is wrong".
- Per-task file locks (`fslock`): six concurrent `task attempt` calls now persist all six updates;
  id allocation takes a ledger-wide lock.
- `status --append` writes a JSONL snapshot; `trend` reports first/last deltas per metric.
- `gate run --worktree`: diff a per-task worktree while the ledger, state and config stay in the
  main repository.
- `tools/agent_adapter.py`: reference adapter that reads the brief, scrubs credentials from the
  agent environment, runs the agent, and returns the gate verdict.
- Chaos coverage: an agent is killed mid-task, the lease is reaped after its TTL, the task returns
  to `proposed`, and another agent claims it.
- ADR 0004 (OS locks for task files) and ADR 0005 (exit code 5 for admission refusal).
- `docs/06-references.md`: fourteen arXiv entries and eight GitHub projects, each mapped to the
  design decision it supports or contradicts.
- `docs/07-implementation-log.md`: implemented / tried / abandoned, with reproduction commands.
- `docs/08-development-guide.md`, `docs/README.md`, `CONTRIBUTING.md`, `ROADMAP.md`, `adr/`.

### Changed

- `task claim` charges budget by default (`[budget] default_claim_cost`); a repo without envelopes
  behaves exactly as before, only spending is recorded.
- Gate risk classification now persists through `ledger.update`, so it cannot overwrite a
  concurrent transition.
- Documentation map in `README.md`; MIT license noted.

### Fixed

- `budget list` crashed on a scope without a ceiling (format string applied to `"-"`).
- `status` read `--append` from a variable that does not exist in that scope.

## [0.1.0] - 2026-11-22

First working skeleton: the coordination core, not a demo.

### Added

- Task ledger: one JSON file per task, atomic writes, sorted keys, revision counter.
- State machine with a whitelisted edge set, governance-only `quarantined`, and a merge gate that
  refuses to merge without a signed intent.
- Path leases in SQLite (WAL, `BEGIN IMMEDIATE`): read/write/module/repo modes, prefix-overlap
  conflicts, ordered acquisition, TTL, heartbeat, reaping, audit snapshot.
- Structured dedupe fingerprint `(path set x failure signature)`, advisory only.
- Risk classification T0/T1/T2 from the diff: deleted tests, skip markers, new dependencies,
  migrations, CI edits, irreversible commands.
- Human approval tokens: HMAC-SHA256, bound to the diff digest, single-use nonce, expiry, and a
  mandatory falsifiable prediction + rollback plan.
- Mechanical gates: ledger, state, intent, attempt budget, diff, scope guard, risk, configured
  checks, debt ceiling, provenance trailer. Same code path locally and in CI.
- `status` snapshot (WIP, leases, gate outcomes, budget, recent events).
- Zero-dependency core; 37 unit tests including an 8-process lease race; `scripts/verify.ps1`
  runs the suite and drives a real gate through a throwaway repository.
- `docs/01`-`05`: feasibility review, architecture, data model and protocol, gates/risk/security,
  metrics and roadmap.