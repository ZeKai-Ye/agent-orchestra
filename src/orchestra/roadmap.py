"""Machine-readable roadmap queue: items, feasibility triage, acceptance, promotion.

ROADMAP.md stays the human narrative; ``roadmap/items.jsonl`` is the queue the round
driver works on. One invariant keeps them honest: an item reaches ``done`` only through
``accept_item``, which requires a merged task that names the item *and* the item's own
acceptance command to exit 0. Humans set the envelope; they do not hand-flip checkboxes.

See docs/11-rounds.md and ADR 0008.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .ledger import LedgerError

STATUSES = ("later", "next", "done", "dropped")
SIZES = ("S", "M", "L")
ID_PATTERN = re.compile(r"^R-\d{4}$")
QUEUE_RELATIVE = Path("roadmap") / "items.jsonl"
ACCEPT_TIMEOUT = 300


class RoadmapError(RuntimeError):
    """Invalid queue content, or an item that cannot legally move."""


def queue_path(repo: Path) -> Path:
    return Path(repo) / QUEUE_RELATIVE


def load(repo: Path) -> list[dict]:
    path = queue_path(repo)
    if not path.exists():
        return []
    items: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise RoadmapError(f"{path}:{number} is not valid JSON: {exc}") from exc
    return items


def save(repo: Path, items: list[dict]) -> None:
    path = queue_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in sorted(items, key=lambda i: i["id"])]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def next_id(items: list[dict]) -> str:
    highest = 0
    for item in items:
        match = re.match(r"^R-(\d+)$", item.get("id", ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"R-{highest + 1:04d}"


def validate(item: dict) -> list[str]:
    problems: list[str] = []
    if not ID_PATTERN.match(str(item.get("id", ""))):
        problems.append(f"invalid id: {item.get('id')!r} (expected R-0000)")
    if item.get("status") not in STATUSES:
        problems.append(f"invalid status: {item.get('status')!r}")
    if not str(item.get("title", "")).strip():
        problems.append("title is required")
    if not str(item.get("accept", "")).strip():
        problems.append("accept command is required: an item without a verdict is a wish")
    scope = item.get("scope")
    if not isinstance(scope, list) or not scope:
        problems.append("scope must be a non-empty list of paths")
    if item.get("size") not in SIZES:
        problems.append(f"invalid size: {item.get('size')!r} (expected S, M or L)")
    return problems


def create(repo: Path, *, title: str, accept: str, scope: list[str], size: str = "S",
           status: str = "later", origin: str = "human") -> dict:
    items = load(repo)
    item = {
        "id": next_id(items),
        "status": status,
        "title": title.strip(),
        "accept": accept.strip(),
        "scope": [str(path).replace("\\", "/").strip("/") for path in scope],
        "size": size,
        "origin": origin,
    }
    problems = validate(item)
    if problems:
        raise RoadmapError("invalid item: " + "; ".join(problems))
    items.append(item)
    save(repo, items)
    return item


def get(items: list[dict], item_id: str) -> dict:
    for item in items:
        if item["id"] == item_id:
            return item
    raise RoadmapError(f"unknown roadmap item: {item_id}")


def triage(repo: Path, item: dict, items: list[dict] | None = None) -> dict:
    """Cheap feasibility: can this item be run at all, without running it yet?"""

    repo = Path(repo)
    checks: dict[str, bool] = {}
    notes: list[str] = []

    command = str(item.get("accept", "")).split()
    executable = command[0] if command else ""
    if executable in ("python", "python3", "py", "pwsh", "powershell", "git"):
        checks["accept-runnable"] = True
    else:
        checks["accept-runnable"] = bool(shutil.which(executable))
    if not checks["accept-runnable"]:
        notes.append(f"accept command not found: {executable!r}")

    missing = [path for path in item.get("scope", []) if not (repo / path).exists() and not (repo / path).parent.exists()]
    checks["scope-exists"] = not missing
    if missing:
        notes.append(f"scope missing: {', '.join(missing[:4])}")

    others = [other for other in (items or load(repo)) if other["id"] != item["id"] and other.get("status") == "next"]
    clash = []
    for other in others:
        for left in item.get("scope", []):
            for right in other.get("scope", []):
                left, right = left.rstrip("/"), right.rstrip("/")
                if left == right or left.startswith(right + "/") or right.startswith(left + "/"):
                    clash.append(other["id"])
    checks["scope-free"] = not clash
    if clash:
        notes.append(f"scope overlaps with next item(s): {', '.join(sorted(set(clash)))}")

    score = 0.4 * checks["accept-runnable"] + 0.4 * checks["scope-exists"] + 0.2 * checks["scope-free"]
    if item.get("size") == "L":
        score -= 0.1
    feasible = checks["accept-runnable"] and checks["scope-exists"] and score >= 0.6
    return {
        "score": round(score, 2),
        "checks": checks,
        "feasible": feasible,
        "notes": notes,
    }


def set_status(repo: Path, item_id: str, status: str, **fields) -> dict:
    if status not in STATUSES:
        raise RoadmapError(f"invalid status: {status}")
    items = load(repo)
    item = get(items, item_id)
    item["status"] = status
    item.update(fields)
    save(repo, items)
    return item


def run_accept(ctx, item: dict) -> dict:
    """Run the item's own verdict. This is the only way an item becomes done."""

    command = str(item.get("accept", "")).strip()
    if not command:
        raise RoadmapError(f"{item['id']} has no acceptance command")
    proc = subprocess.run(
        command,
        cwd=str(ctx.repo),
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=ACCEPT_TIMEOUT,
    )
    tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
    return {"ok": proc.returncode == 0, "exit": proc.returncode, "detail": " | ".join(tail), "command": command}


def accept_item(ctx, item_id: str) -> dict:
    items = load(ctx.repo)
    item = get(items, item_id)
    if item.get("status") != "next":
        raise RoadmapError(f"{item_id} is {item.get('status')}, only a next item can be accepted as done")
    merged = [task for task in ctx.ledger.list("merged") if task.get("roadmap") == item_id]
    if not merged:
        raise RoadmapError(f"{item_id} has no merged task: nothing to accept (no self-graded Done)")
    verdict = run_accept(ctx, item)
    if not verdict["ok"]:
        raise RoadmapError(
            f"{item_id} acceptance failed (exit {verdict['exit']}): {verdict['detail']} -> needs a human"
        )
    updated = set_status(
        ctx.repo,
        item_id,
        "done",
        done_at=ctx.clock.iso(),
        evidence={"tasks": [task["id"] for task in merged], "accept": verdict["detail"]},
    )
    ctx.store.event("roadmap.accepted", task_id=merged[-1]["id"], actor="research", item=item_id)
    return {"item": updated, "verdict": verdict, "tasks": [task["id"] for task in merged]}


def promote_item(ctx, item_id: str, *, intent: str | None = None, role: str = "patch") -> dict:
    """Move one feasible Later item to Next and create the task that will do it."""

    items = load(ctx.repo)
    item = get(items, item_id)
    if item.get("status") != "later":
        raise RoadmapError(f"{item_id} is {item.get('status')}, only a later item can be promoted")
    pending = [other["id"] for other in items if other.get("status") == "next"]
    if pending:
        raise RoadmapError(f"{item_id} cannot start while {', '.join(pending)} is still next (one at a time)")
    feasibility = triage(ctx.repo, item, items)
    if not feasibility["feasible"]:
        raise RoadmapError(
            f"{item_id} is not feasible yet ({feasibility['score']}): " + "; ".join(feasibility["notes"])
        )
    task = ctx.ledger.create(
        title=f"{item_id}: {item['title']}",
        role=role,
        domain="core",
        lineage="roadmap",
        paths=item["scope"],
        intent=intent,
        max_attempts=3,
        roadmap=item_id,
    )
    updated = set_status(ctx.repo, item_id, "next", promoted_at=ctx.clock.iso(), task=task["id"],
                         feasibility=feasibility)
    ctx.store.event("roadmap.promoted", task_id=task["id"], actor="research", item=item_id)
    return {"item": updated, "task": task, "feasibility": feasibility}


def triage_all(repo: Path) -> list[dict]:
    items = load(repo)
    results = []
    for item in items:
        if item.get("status") != "later":
            continue
        verdict = triage(repo, item, items)
        results.append({"id": item["id"], "title": item["title"], "size": item.get("size"), **verdict})
    return results


def best_candidate(repo: Path) -> dict | None:
    candidates = [entry for entry in triage_all(repo) if entry["feasible"]]
    if not candidates:
        return None
    candidates.sort(key=lambda entry: (-entry["score"], entry["id"]))
    return candidates[0]