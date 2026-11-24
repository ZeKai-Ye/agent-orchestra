"""Git-native coordination: refs/claims/* as the lease table.

No server. A claim is an *atomically created ref*:

  create   git push <commit>:refs/claims/<task>            (fails if the ref exists -> CAS)
  renew    git push --force-with-lease=<ref>:<old> <new>:<ref>
  release  git push --force-with-lease=<ref>:<old> :<ref>
  reap     delete refs whose expires_at has passed (ADR 0010)

The claim commit carries one JSON file, `claims/<task>.json`, and must be signed by a
key registered for the handle in `handles/<handle>.pub` (ssh-keygen -Y verify through
git's allowed_signers). SQLite stays the local cache; this module is the source of truth
for cross-machine coordination.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

DEFAULT_TTL = 1800
CLAIM_REF = "refs/claims/{task}"
HANDLES_DIR = Path("handles")


class CoordError(RuntimeError):
    """A claim could not be taken, renewed or released."""


class ClaimConflict(CoordError):
    """Somebody else holds (or just took) the claim."""


def git(repo: Path, *args: str, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise CoordError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:400]}")
    return proc


def git_bytes(repo: Path, args: list[str], data: bytes) -> str:
    """Run git with binary stdin: text mode would translate LF to CRLF and corrupt trees."""

    proc = subprocess.run(["git", *args], cwd=str(repo), input=data, capture_output=True)
    if proc.returncode != 0:
        raise CoordError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace')[:300]}")
    return proc.stdout.decode().strip()


def claim_commit(repo: Path, record: dict, signing_key: str | None) -> str:
    """Build a commit holding claims/<task>.json, optionally SSH-signed."""

    task = record["task"]
    body = json.dumps(record, sort_keys=True, indent=2) + chr(10)
    blob = git_bytes(repo, ["hash-object", "-w", "--stdin"], body.encode("utf-8"))
    leaf = git_bytes(repo, ["mktree", "-z"], f"100644 blob {blob}{chr(9)}{task}.json".encode("utf-8") + bytes([0]))
    tree = git_bytes(repo, ["mktree", "-z"], f"040000 tree {leaf}{chr(9)}claims".encode("utf-8") + bytes([0]))
    message = f"claim {task} by {record['handle']}" + chr(10)
    if signing_key:
        proc = git(
            repo,
            "-c", "gpg.format=ssh",
            "-c", f"user.signingkey={signing_key}",
            "commit-tree", tree, "-S", "-m", message,
        )
    else:
        proc = git(repo, "commit-tree", tree, "-m", message)
    return proc.stdout.strip()


def claim_ref(task: str) -> str:
    return CLAIM_REF.format(task=task)


def push_claim(repo: Path, task: str, sha: str, *, force_lease: str | None = None) -> None:
    ref = claim_ref(task)
    if force_lease is None:
        proc = git(repo, "push", "--porcelain", "origin", f"{sha}:{ref}", check=False)
        if proc.returncode != 0:
            raise ClaimConflict(f"{task} is already claimed (git refused to create {ref})")
        return
    proc = git(repo, "push", "--porcelain", f"--force-with-lease={ref}:{force_lease}", "origin", f"{sha}:{ref}", check=False)
    if proc.returncode != 0:
        raise ClaimConflict(f"{task}: lease mismatch, the claim moved (expected {force_lease[:12]})")


def delete_claim(repo: Path, task: str, expected: str | None = None) -> None:
    ref = claim_ref(task)
    args = ["push", "--porcelain"]
    if expected:
        args.append(f"--force-with-lease={ref}:{expected}")
    proc = git(repo, *args, "origin", f":{ref}", check=False)
    if proc.returncode != 0:
        raise CoordError(f"could not delete {ref}: {(proc.stderr or proc.stdout).strip()[:300]}")


def fetch_claims(repo: Path) -> dict[str, str]:
    """Fetch every claim ref; returns {task: sha}."""

    git(repo, "fetch", "--quiet", "origin", "+refs/claims/*:refs/claims/*")
    out = git(repo, "for-each-ref", "--format=%(refname:strip=2) %(objectname)", "refs/claims").stdout
    claims: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2:
            claims[parts[0]] = parts[1]
    return claims


def read_claim(repo: Path, task: str, sha: str) -> dict:
    out = git(repo, "show", f"{sha}:claims/{task}.json", check=False)
    if out.returncode != 0:
        raise CoordError(f"{sha[:12]} does not contain claims/{task}.json")
    return json.loads(out.stdout)


def allowed_signers(repo: Path) -> Path:
    """Build a temporary allowed_signers file from handles/<handle>.pub."""

    directory = Path(repo) / HANDLES_DIR
    if not directory.exists():
        raise CoordError(f"no {HANDLES_DIR}/ directory: register a handle first")
    lines = []
    for key in sorted(directory.glob("*.pub")):
        handle = key.stem
        text = key.read_text(encoding="utf-8").strip()
        if text:
            lines.append(f"{handle} {text}")
    if not lines:
        raise CoordError(f"{HANDLES_DIR}/ has no public keys")
    handle_file = tempfile.NamedTemporaryFile("w", suffix=".allowed", delete=False, encoding="utf-8")
    handle_file.write("\n".join(lines) + "\n")
    handle_file.close()
    return Path(handle_file.name)


def verify_claim(repo: Path, sha: str, handle: str) -> None:
    """Refuse an unsigned claim, or one signed by a key that is not this handle's."""

    allowed = allowed_signers(repo)
    try:
        proc = git(repo, "-c", f"gpg.ssh.allowedSignersFile={allowed}", "verify-commit", sha, check=False)
    finally:
        allowed.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise CoordError(f"claim {sha[:12]} is not signed by a registered key: {(proc.stderr or '').strip()[:200]}")
    if f"for {handle} " not in proc.stderr and f"for {handle} " not in proc.stdout:
        raise CoordError(f"claim {sha[:12]} is signed, but not by handle {handle!r}")


def verify_handle(repo: Path, handle: str) -> Path:
    path = Path(repo) / HANDLES_DIR / f"{handle}.pub"
    if not path.exists():
        raise CoordError(f"handle {handle!r} is not registered ({path})")
    return path


def is_expired(record: dict, now: float) -> bool:
    return float(record.get("expires_at", 0)) <= now


def reap(repo: Path, now: float, *, dry_run: bool = False) -> list[dict]:
    """Delete claim refs whose TTL has passed (run from a scheduled job)."""

    reaped = []
    for task, sha in fetch_claims(repo).items():
        try:
            record = read_claim(repo, task, sha)
        except CoordError:
            continue
        if is_expired(record, now):
            reaped.append({"task": task, "sha": sha, "handle": record.get("handle")})
            if not dry_run:
                delete_claim(repo, task, sha)
    return reaped