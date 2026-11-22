# Contributing

The project is a coordination control plane. The bar for a change is not "it works on my machine",
it is "a machine can tell whether it works".

## Before you start

```powershell
powershell -File .\scripts\verify.ps1     # 37 tests + a real gate demo; must be green
```

Read `docs/07-implementation-log.md` section 3 first. It lists seven approaches that were tried and
reverted; re-proposing one of them requires new evidence, not new enthusiasm.

## Rules that are enforced, not suggested

1. **No third-party dependencies in `src/orchestra/`** (ADR 0002). Standard library only.
2. **One invariant, one test.** Test names state the invariant (`test_same_path_is_exclusive`).
3. **Time is injected** (`FakeClock` / `ORCHESTRA_NOW`). No `sleep` in tests.
4. **Git is real in tests**: temporary repositories, never a mocked `git`.
5. **Concurrency needs a real multi-process test.** Threads prove nothing about SQLite.
6. **Behaviour change requires a `docs/07` update in the same commit.**
7. **Frozen decisions change only through a new ADR** in `adr/`, with a falsification condition.

## Commit messages

Conventional Commits, plus a provenance trailer when the change came from an orchestrated task:

```text
fix: reject out-of-lease writes during the impl gate

The scope guard compared against task paths but ignored untracked files.

Task-Id: T-0007
Agent-Id: claude-code/lineage-a
```

The trailer is not decoration: `gate run --stage merge` fails without `Task-Id`, so a commit that
skips it cannot reach trunk through the normal path.

## Pull requests

A PR is acceptable when it contains:

- the failing test (or the new test) **before** the fix in the same diff, when fixing a bug;
- a one-line statement of the invariant it protects;
- `docs/07` updated (what was implemented / tried / abandoned);
- if it touches leases, gates, tokens or schema: a link to the relevant ADR, new or existing.

A PR that only changes prose is fine; a PR that only changes code is not.

## Local conventions

- `python -m orchestra.cli` works without installing anything (set `PYTHONPATH=src`).
- Windows PowerShell 5.1+ is the primary shell; keep scripts portable where it is free.
- Do not commit `.orchestra/`, `*.db`, or the approval key. `.gitignore` already covers them.