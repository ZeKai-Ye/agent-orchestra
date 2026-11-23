"""Mechanical gates plus the diff scope guard.

Order matters: cheap and certain first. Every step records ``ok`` and a reason,
so a rejection is always a machine-readable fact rather than a review opinion.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .ledger import TaskLedger, StateError
from .paths import is_within, normalize
from .policy import ApprovalError, classify, verify
from .policy import ApprovalError, classify, read_key, verify
IGNORED_PREFIXES = ("ledger/", ".orchestra/", "")


class GateError(RuntimeError):
    """A gate could not be evaluated (bad repo, missing ref, ...)."""


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout


def git(repo: Path, *args: str) -> str:
    code, out = _git(repo, *args)
    if code != 0:
        raise GateError(f"git {' '.join(args)} failed in {repo}")
    return out


def resolve_base(repo: Path, base: str | None = None) -> str:
    if base:
        return base
    for candidate in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", candidate)[0] == 0:
            return candidate
    return "HEAD"


def _ignored(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in IGNORED_PREFIXES if prefix)


def changed_files(repo: Path, base: str) -> list[tuple[str, str]]:
    """Tracked changes against ``base`` plus untracked files (both count as blast radius)."""

    out = git(repo, "diff", "--name-status", "--find-renames", base)
    changes: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1]
        path = normalize(path)
        if path and not _ignored(path):
            changes.append((status, path))
    for line in git(repo, "ls-files", "--others", "--exclude-standard").splitlines():
        path = normalize(line)
        if path and not _ignored(path):
            changes.append(("A", path))
    return sorted(set(changes), key=lambda item: item[1])


def diff_text(repo: Path, base: str) -> str:
    """Diff body behind risk signals and approval digests.

    ``ledger/`` and ``.orchestra/`` are excluded: they are coordination bookkeeping,
    not the change under review, and including them would flip an approval digest
    every time a task file is touched.
    """

    return git(repo, "diff", base, "--", ".", ":(exclude)ledger/**", ":(exclude).orchestra/**")


def head_message(repo: Path) -> str:
    code, out = _git(repo, "log", "-1", "--format=%B")
    return out if code == 0 else ""


def _count_lines(repo: Path, rel_paths) -> tuple[int, list[str]]:
    total = 0
    offenders: list[str] = []
    for rel in rel_paths:
        root = repo / rel
        if root.is_file():
            files = [root]
        elif root.is_dir():
            files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
        else:
            offenders.append(f"{rel}: missing")
            continue
        for path in files:
            try:
                total += sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
            except OSError:
                offenders.append(f"{path}: unreadable")
    return total, offenders


def run_gate(
    *,
    repo: Path,
    task_id: str,
    stage: str,
    ledger: TaskLedger,
    store,
    clock,
    config,
    base: str | None = None,
    token: str | None = None,
    out_dir: Path | None = None,
    worktree: Path | None = None,
) -> dict:
    repo = Path(repo)
    # git and the checked-out tree can live in a per-task worktree, while the ledger
    # and the coordination state stay in the main repository.
    work = Path(worktree) if worktree else repo
    steps: list[dict] = []
    requires_approval = False
    def step(name: str, ok: bool, detail: str = "", **extra) -> None:
        entry = {"name": name, "ok": bool(ok), "detail": detail}
        entry.update(extra)
        steps.append(entry)

    try:
        task = ledger.load(task_id)
    except Exception as exc:  # noqa: BLE001 - report any ledger failure as the first step
        step("ledger", False, str(exc))
        result = _finish(repo, task_id, stage, steps, {}, False, out_dir, store, clock)
        return result

    problems = ledger.validate(task)
    step("ledger", not problems, "; ".join(problems) or f"{task['role']} task, revision {task['revision']}")

    allowed = {"impl": ("in_progress",), "merge": ("review", "approved")}.get(stage)
    if allowed is None:
        raise GateError(f"unknown stage: {stage}")
    step(
        "state",
        task["state"] in allowed,
        f"state={task['state']} stage={stage} (expected {', '.join(allowed)})",
    )

    signed, meta = ledger.intent_signed(task.get("intent"))
    step("intent", signed, f"intent={task.get('intent')} signed={signed}")

    used, cap = task["attempt"]["used"], task["attempt"]["max"]
    step("attempt-budget", used < cap, f"attempts {used}/{cap} (exhausted -> escalate, do not retry)")

    base_ref = resolve_base(work, base)
    changes = changed_files(work, base_ref) if _git(work, "rev-parse", "--verify", "--quiet", base_ref)[0] == 0 else []
    body = diff_text(work, base_ref) if changes else ""
    step("diff", bool(changes), f"{len(changes)} changed path(s) against {base_ref}")

    outside = [path for _status, path in changes if not any(is_within(path, scope) for scope in task["paths"])]
    step(
        "scope",
        not outside,
        "all changes inside the lease" if not outside else f"outside lease: {', '.join(outside[:8])}",
        outside=outside[:32],
    )

    risk = classify(changes, body, config.risk_rules(), protected=config.protected_patterns())
    ledger.update(task_id, lambda current: current.__setitem__("risk", risk.as_dict()))
    approval = None
    if risk.cls == "T2":
        if not token:
            requires_approval = True
            step("risk", False, f"{risk.cls}: human approval token required for this change", risk=risk.as_dict())
        else:
            try:
                approval = verify(
                    read_key(repo),
                    token,
                    clock=clock,
                    store=store,
                    subject=risk.digest,
                    kinds=("merge", "irreversible"),
                )
                step("risk", True, f"{risk.cls} approved by {approval['by']} ({approval['approval_id']})", risk=risk.as_dict())
            except (ApprovalError, Exception) as exc:  # noqa: BLE001
                requires_approval = True
                step("risk", False, f"{risk.cls}: approval rejected: {exc}", risk=risk.as_dict())
    else:
        step("risk", True, f"{risk.cls} ({', '.join(risk.signals) or 'no signals'})", risk=risk.as_dict())

    for command in config.gate_commands(stage):
        proc = subprocess.run(
            command,
            cwd=str(work),
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
        )
        ok = proc.returncode == 0
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
        step(f"check: {command}", ok, " | ".join(tail) if tail else f"exit {proc.returncode}")

    for module, spec in config.debt_modules().items():
        if not isinstance(spec, dict):
            continue
        ceiling = int(spec.get("max_lines", 0) or 0)
        if ceiling <= 0:
            continue
        paths = [str(p) for p in spec.get("paths", []) or []]
        total, offenders = _count_lines(work, paths)
        step(
            f"debt: {module}",
            total <= ceiling and not offenders,
            f"{total} lines <= {ceiling}" if total <= ceiling else f"{total} lines > budget {ceiling}",
        )

    if stage == "merge":
        message = head_message(work)
        has_trailer = f"Task-Id: {task_id}" in message
        step("provenance", has_trailer, "commit trailer Task-Id present" if has_trailer else "missing Task-Id trailer")

    return _finish(repo, task_id, stage, steps, risk.as_dict(), requires_approval, out_dir, store, clock)


def _finish(repo, task_id, stage, steps, risk, requires_approval, out_dir, store, clock) -> dict:
    ok = all(item["ok"] for item in steps)
    result = {
        "task": task_id,
        "stage": stage,
        "ok": ok,
        "requires_approval": requires_approval,
        "risk": risk,
        "steps": steps,
        "at": clock.iso(),
    }
    target_dir = Path(out_dir) if out_dir else repo / ".orchestra" / "gate-results"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / f"{task_id}-{stage}.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if store is not None:
        store.event(
            "gate.passed" if ok else "gate.failed",
            task_id=task_id,
            actor="gate",
            stage=stage,
            failed=[item["name"] for item in steps if not item["ok"]],
            risk=risk.get("class") if risk else None,
        )
    return result
