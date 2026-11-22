# Changelog

All notable changes to this project. Format: [Keep a Changelog](https://keepachangelog.com/),
versioning: [SemVer](https://semver.org/).

## [Unreleased]

### Added

- `docs/06-references.md`: reference architectures and papers (arXiv API + GitHub Search API,
  retrieved 2026-11-22), each mapped to the design decision it supports or contradicts.
- `docs/07-implementation-log.md`: what is implemented, what was tried and reverted,
  known limitations, and how to reproduce every claim.
- `docs/08-development-guide.md`: how to add a gate step, a lease mode, an adapter; test and
  release rules; the 30-minute onboarding order.
- `ROADMAP.md`, `CONTRIBUTING.md`, `adr/` (template + three accepted decisions).

### Changed

- `README.md` and `docs/README.md` now point at the full document map.

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