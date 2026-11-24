# 0010. Claims are git refs, verified by SSH signatures

- Status: accepted
- Date: 2026-11-24

## Context

Outside researchers should be able to hand `join.txt` to their own agent and start working, with no
server to run. Our leases lived in a local SQLite file, which cannot coordinate across machines. The
options were a hosted desk API (pequalnp's design) or something the repository itself can arbitrate.

## Decision

A claim is an atomically created ref: `git push <claim-commit>:refs/claims/<task>`. Git refuses to
overwrite an existing ref without force, which is a compare-and-swap for free. Renewal and release use
`--force-with-lease=<ref>:<old-sha>`, so only the holder can extend or drop it. Expiry is a timestamp
inside the claim commit; a scheduled `orchestra reap` deletes expired refs.

The claim commit holds `claims/<task>.json` and must be SSH-signed with a key registered at
`handles/<handle>.pub`; `git verify-commit` with a generated allowed_signers file decides who signed
it. Revocation is deleting that file. SQLite stays a local cache; git is the source of truth.

`orchestra merge` is the maintainer's poller: it refuses a task whose claim is still live, re-runs the
merge gate in a detached worktree, and only fast-forwards `T0` work.

## Rejected options

| Option | Why not |
| --- | --- |
| Hosted desk API now | Requires ops, auth and abuse handling before the protocol is even tested |
| Unsigned claims | Anyone could squat tasks under someone else's handle |
| HMAC tokens with the key in the repo | The verifier would hold the same secret as the forger |
| Leases in `ledger/*.json` (one file per task) | Claim files conflict exactly when two agents race |
| Auto-merge inside a GitHub Action | The maintainer chose a local poller; Actions write access is a bigger blast radius |

## Consequences

- Zero infrastructure: two clones and one remote are enough; the claim history is in git.
- Latency is git-push latency; stale claims persist until `reap` runs (schedule it).
- Signature UX depends on OpenSSH (present on Windows and Linux); an unsigned claim is refused unless
  `--allow-unsigned` is passed for local experiments.
- Migration to a hosted desk later only replaces the `coord_git` module behind the same commands.

## Falsification

Reopen if: two agents ever hold the same claim; a claim can be renewed by a non-holder; or `reap`
deletes a live claim (clock/ttl bug).