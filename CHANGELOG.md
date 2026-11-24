# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added

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