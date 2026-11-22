# 0003. Human approval is a token bound to the diff, not a convention

- Status: accepted
- Date: 2026-11-22

## Context

The design said "irreversible actions require explicit human confirmation". A convention is not a
control: with tens of agents, a human clicking through a queue approves whatever arrived, and an
approval that does not name the exact change authorises nothing in particular. Approval fatigue
pushes the pass rate towards 100%, and nobody can prove afterwards what was approved.

## Decision

An approval is an HMAC-SHA256 token over a payload containing the approver, an expiry, a single-use
nonce, a subject digest (`sha256(paths + diff)`), a falsifiable prediction and a rollback plan.
Verification is mechanical and ordered: format, signature, expiry, kind, subject, nonce. A T2 change
without a valid token fails the gate with exit code 4; a token whose diff changed no longer matches.

## Rejected options

| Option | Why not |
| --- | --- |
| "Approve" label on the PR | Bound to nothing; survives code changes; unverifiable later |
| Trust the agent to stop at irreversible steps | Exactly the behaviour that fails under pressure |
| Public-key signatures from day one | Right destination, but Ed25519 needs a dependency today (ADR 0002); the token format is versioned (`oap1.`) so the swap is contained |
| Blanket approval per task | Re-authorises future diffs; the digest would be meaningless |

## Consequences

- Approval is auditable: the token names the change, the approver, and the promise.
- Humans must actually look: pasting a token for unseen code is detectable (digest mismatch).
- HMAC is symmetric, so the signer and the verifier share a key. The key never ships inside an
  agent container; that separation is the current stand-in for asymmetric signatures.

## Falsification

Reopen if approvals are routinely re-issued for the same task (fatigue signal), or if the shared key
ends up inside an agent runtime - at that point the control is gone and Ed25519 is overdue.