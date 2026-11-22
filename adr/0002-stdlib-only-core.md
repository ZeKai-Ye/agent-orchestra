# 0002. The core is standard-library only

- Status: accepted
- Date: 2026-11-22

## Context

Agents run in heterogeneous containers. Every third-party dependency is a step that can fail before
the coordination layer does anything useful, and a supply-chain surface that agents themselves are
allowed to touch. The workload is I/O and SQL, not numerics; the standard library covers it
(`sqlite3`, `argparse`, `json`, `tomllib`, `hmac`, `unittest`).

## Decision

`src/orchestra/` imports only the Python standard library. Tests use `unittest`. Configuration is
TOML parsed by `tomllib`; machine-written state is JSON (guaranteed round-trip, diffable).

## Rejected options

| Option | Why not |
| --- | --- |
| PyYAML for task books | No round-trip guarantee for machine-written state; extra dependency |
| pytest | Nicer output, but `unittest` is enough for 37 tests and needs no install |
| Pydantic for models | Schema is small; hand-written `validate()` keeps errors actionable |
| Click / Typer for the CLI | `argparse` handles subcommands; one less import at agent startup |

## Consequences

- `python -m orchestra.cli` works on a bare interpreter; no install step in an agent image.
- Hand-rolled validation and CLI plumbing; ergonomics are worse than the ecosystem equivalents.
- If a need appears that the standard library cannot meet (Ed25519, tree-sitter, OTel), it goes
  behind an optional extra, never into the core import graph.

## Falsification

Reopen if the standard library forces more than ~200 lines of plumbing for something a well-known
library does correctly, or if a platform drops `tomllib` from the runtime we target.