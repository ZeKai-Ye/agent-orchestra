"""Task ledger: one JSON file per task plus the state machine that guards it.

Tasks live in ``ledger/tasks/<id>.json`` inside the managed repo (a single table
would conflict on every write). Files are written atomically and serialised with
sorted keys so diffs stay reviewable. Intents live in ``ledger/intents/<id>.md``
with a tiny front-matter block; a merge is impossible without a signed intent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from .clock import Clock
from .fslock import advisory_lock
from .paths import normalize_many

SCHEMA_VERSION = 1
ROLES = ("patch", "refactor", "test", "verify", "research", "coordinate", "critic")
RISK_CLASSES = ("T0", "T1", "T2")
DEAD_REASONS = (
    "gate_failed",
    "scope_violation",
    "agent_failed",
    "no_progress",
    "missing_lemma",
    "out_of_time",
    "blocked_externally",
    "submitted",
    "reduced",
    "other",
)
STATES = (
    "proposed",
    "claimed",
    "in_progress",
    "blocked",
    "review",
    "approved",
    "merged",
    "abandoned",
    "escalated",
    "quarantined",
)
TERMINAL = frozenset({"merged", "abandoned"})

TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"claimed", "abandoned", "quarantined"}),
    "claimed": frozenset({"in_progress", "proposed", "abandoned", "escalated"}),
    "in_progress": frozenset({"review", "blocked", "proposed", "abandoned", "escalated"}),
    "blocked": frozenset({"in_progress", "abandoned", "escalated"}),
    "review": frozenset({"approved", "in_progress", "abandoned", "escalated"}),
    "approved": frozenset({"merged", "abandoned"}),
    "escalated": frozenset({"proposed", "abandoned"}),
    "merged": frozenset(),
    "abandoned": frozenset(),
    "quarantined": frozenset({"proposed", "abandoned"}),
}

ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*$")


class LedgerError(RuntimeError):
    """Invalid ledger content or an operation the ledger refuses."""


class StateError(LedgerError):
    """Illegal state transition."""


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def compute_fingerprint(paths: Iterable[str], failure_signature: str = "") -> str:
    """Structured dedupe key: (path set x failure signature), not free text."""

    body = {"paths": normalize_many(paths), "failure": (failure_signature or "").strip()}
    return "sha256:" + hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


class TaskLedger:
    def __init__(self, repo: Path, clock: Clock) -> None:
        self.repo = Path(repo)
        self.clock = clock
        self.tasks_dir = self.repo / "ledger" / "tasks"
        self.intents_dir = self.repo / "ledger" / "intents"
        self.lock_dir = self.repo / ".orchestra" / "locks"

    # -- locking -----------------------------------------------------------
    def _task_lock(self, task_id: str):
        """One lock per task file: the unit of read-modify-write serialisation."""

        return advisory_lock(self.lock_dir / f"task-{task_id}.lock")

    def _ledger_lock(self):
        """Repo-wide lock used only for id allocation (see ADR 0004)."""

        return advisory_lock(self.lock_dir / "ledger.lock")

    # -- layout ------------------------------------------------------------
    def ensure_layout(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.intents_dir.mkdir(parents=True, exist_ok=True)

    def update(self, task_id: str, mutator) -> dict:
        """Locked read-modify-write. Everything mutating a task goes through here."""

        with self._task_lock(task_id):
            task = self.load(task_id)
            mutator(task)
            return self.save(task)

    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    # -- read/write --------------------------------------------------------
    def load(self, task_id: str) -> dict:
        path = self.task_path(task_id)
        if not path.exists():
            raise LedgerError(f"unknown task: {task_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise LedgerError(f"task file is not valid JSON: {path.name}: {exc}") from exc

    def save(self, task: dict) -> dict:
        self.ensure_layout()
        task = dict(task)
        task["revision"] = int(task.get("revision", 0)) + 1
        path = self.task_path(task["id"])
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(task, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return task

    def create(self, **kwargs) -> dict:
        """Allocate an id and write the task inside a ledger-wide lock."""

        with self._ledger_lock():
            return self._create(**kwargs)

    def _create(
        self,
        *,
        title: str,
        role: str,
        domain: str,
        lineage: str,
        paths: Iterable[str],
        intent: str | None = None,
        owner: str = "",
        max_attempts: int = 3,
        deps: Iterable[str] = (),
        failure_signature: str = "",
        task_id: str | None = None,
        roadmap: str | None = None,
        target: str | None = None,
    ) -> dict:
        if role not in ROLES:
            raise LedgerError(f"unknown role: {role} (expected one of {', '.join(ROLES)})")
        if not title.strip():
            raise LedgerError("title is required")
        self.ensure_layout()
        tid = task_id or self._next_id()
        if not ID_PATTERN.match(tid):
            raise LedgerError(f"invalid task id: {tid}")
        if self.task_path(tid).exists():
            raise LedgerError(f"task already exists: {tid}")
        now = self.clock.iso()
        task = {
            "schema_version": SCHEMA_VERSION,
            "id": tid,
            "title": title.strip(),
            "role": role,
            "domain": domain or "core",
            "lineage": lineage,
            "owner": owner,
            "intent": intent or None,
            "roadmap": roadmap or None,
            "target": target or None,
            "state": "proposed",
            "paths": normalize_many(paths),
            "deps": sorted(set(deps)),
            "fingerprint": compute_fingerprint(paths, failure_signature),
            "failure_signature": failure_signature.strip(),
            "attempt": {"used": 0, "max": int(max_attempts)},
            "risk": {"class": "T0", "signals": [], "reasons": []},
            "lease": None,
            "dead": [],
            "revision": 0,
            "created_at": now,
            "updated_at": now,
            "history": [{"at": now, "from": None, "to": "proposed", "actor": owner or "cli", "note": "created"}],
        }
        problems = self.validate(task)
        if problems:
            raise LedgerError("invalid task: " + "; ".join(problems))
        return self.save(task)

    def list(self, state: str | None = None) -> list[dict]:
        if not self.tasks_dir.exists():
            return []
        tasks = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.tasks_dir.glob("*.json"))]
        if state:
            tasks = [t for t in tasks if t.get("state") == state]
        return tasks

    def _next_id(self) -> str:
        highest = 0
        for path in self.tasks_dir.glob("*.json"):
            match = re.match(r"^T-(\d+)$", path.stem)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"T-{highest + 1:04d}"

    # -- state machine -----------------------------------------------------
    def transition(
        self,
        task_id: str,
        to: str,
        *,
        actor: str,
        note: str = "",
        extra: dict | None = None,
    ) -> dict:
        """Public entry point: serialise the read-modify-write on the task file."""

        with self._task_lock(task_id):
            return self._transition(task_id, to, actor=actor, note=note, extra=extra)

    def _transition(
        self,
        task_id: str,
        to: str,
        *,
        actor: str,
        note: str = "",
        extra: dict | None = None,
    ) -> dict:
        task = self.load(task_id)
        previous = task.get("state")
        if to not in STATES:
            raise StateError(f"unknown state: {to}")
        if to not in TRANSITIONS.get(previous, frozenset()):
            allowed = ", ".join(sorted(TRANSITIONS.get(previous, frozenset()))) or "nothing"
            raise StateError(f"illegal transition {previous} -> {to} (allowed: {allowed})")
        if to == "quarantined" and not actor.startswith("governance"):
            raise StateError("quarantined is a governance-only state (use --actor governance:<who>)")
        if to == "merged" and task.get("review_verdict") == "refuted":
            raise StateError(
                f"cannot merge {task_id}: refuted by {task.get('review_by')} - answer the refutation first"
            )
        if to == "merged":
            signed, _meta = self.intent_signed(task.get("intent"))
            if not signed:
                raise StateError(f"cannot merge {task_id}: intent {task.get('intent')!r} is missing or unsigned")
        if previous == "review" and to == "in_progress":
            task["attempt"]["used"] = int(task["attempt"]["used"]) + 1
            if task["attempt"]["used"] > int(task["attempt"]["max"]):
                raise StateError(
                    f"attempt budget exhausted for {task_id} ({task['attempt']['used']}/{task['attempt']['max']}); escalate"
                )
        task["state"] = to
        task["updated_at"] = self.clock.iso()
        task.setdefault("history", []).append(
            {"at": task["updated_at"], "from": previous, "to": to, "actor": actor, "note": note}
        )
        if extra:
            task.update(extra)
        if to in ("merged", "abandoned", "escalated", "proposed"):
            task["lease"] = None
        return self.save(task)

    def bump_attempt(self, task_id: str, actor: str, note: str = "") -> dict:
        with self._task_lock(task_id):
            return self._bump_attempt(task_id, actor, note)

    def _bump_attempt(self, task_id: str, actor: str, note: str = "") -> dict:
        task = self.load(task_id)
        task["attempt"]["used"] = int(task["attempt"]["used"]) + 1
        task["updated_at"] = self.clock.iso()
        task.setdefault("history", []).append(
            {
                "at": task["updated_at"],
                "from": task["state"],
                "to": task["state"],
                "actor": actor,
                "note": note or "attempt recorded",
            }
        )
        return self.save(task)

    def set_lease(self, task_id: str, lease: dict | None) -> dict:
        with self._task_lock(task_id):
            return self._set_lease(task_id, lease)

    def _set_lease(self, task_id: str, lease: dict | None) -> dict:
        task = self.load(task_id)
        task["lease"] = lease
        task["updated_at"] = self.clock.iso()
        return self.save(task)

    # -- dead approaches ---------------------------------------------------
    def add_dead(self, task_id: str, reason: str, note: str = "", actor: str = "cli") -> dict:
        """Record an approach that died, so the next claimant does not retry it."""

        if reason not in DEAD_REASONS:
            raise LedgerError(f"unknown dead reason: {reason} (expected one of {', '.join(DEAD_REASONS)})")

        def mutate(task: dict) -> None:
            entries = task.setdefault("dead", [])
            entries.append(
                {"at": self.clock.iso(), "reason": reason, "note": note.strip()[:500], "actor": actor}
            )
            task["dead"] = entries[-20:]

        return self.update(task_id, mutate)

    def set_verdict(self, task_id: str, verdict: str, *, by: str, reason: str = "") -> dict:
        """Record an adversarial verdict; a refuted task can never merge."""

        if verdict not in ("refuted", "confirmed"):
            raise LedgerError(f"invalid verdict: {verdict}")

        def mutate(task: dict) -> None:
            task["review_verdict"] = verdict
            task["review_by"] = by
            task.setdefault("history", []).append(
                {"at": self.clock.iso(), "from": task["state"], "to": task["state"],
                 "actor": by, "note": f"verdict {verdict}: {reason}".strip()}
            )

        return self.update(task_id, mutate)

    # -- validation --------------------------------------------------------

    def validate(self, task: dict) -> list[str]:
        problems: list[str] = []
        required = (
            "schema_version",
            "id",
            "title",
            "role",
            "domain",
            "lineage",
            "state",
            "paths",
            "attempt",
            "created_at",
            "updated_at",
            "history",
        )
        for key in required:
            if key not in task:
                problems.append(f"missing field: {key}")
        if problems:
            return problems
        if task["schema_version"] != SCHEMA_VERSION:
            problems.append(f"unsupported schema_version: {task['schema_version']}")
        if not ID_PATTERN.match(str(task["id"])):
            problems.append(f"invalid id: {task['id']}")
        if task["role"] not in ROLES:
            problems.append(f"invalid role: {task['role']}")
        if task["state"] not in STATES:
            problems.append(f"invalid state: {task['state']}")
        if not isinstance(task["paths"], list) or not task["paths"]:
            problems.append("paths must be a non-empty list")
        attempt = task.get("attempt") or {}
        if not isinstance(attempt.get("used"), int) or not isinstance(attempt.get("max"), int):
            problems.append("attempt must hold integer used/max")
        elif attempt["max"] < 1 or attempt["used"] < 0:
            problems.append("attempt budget out of range")
        risk = task.get("risk") or {}
        if risk.get("class") not in RISK_CLASSES:
            problems.append(f"invalid risk class: {risk.get('class')}")
        if task["state"] == "merged" and not task.get("intent"):
            problems.append("merged task must reference an intent")
        target = task.get("target")
        if target is not None and not re.match(r"^[TR]-\d{4}$", str(target)):
            problems.append(f"invalid target: {target!r} (expected T-0000 or R-0000)")
        roadmap_id = task.get("roadmap")
        if roadmap_id is not None and not re.match(r"^R-\d{4}$", str(roadmap_id)):
            problems.append(f"invalid roadmap id: {roadmap_id!r}")
        dead = task.get("dead")
        if dead is not None:
            if not isinstance(dead, list):
                problems.append("dead must be a list")
            else:
                for entry in dead:
                    if not isinstance(entry, dict) or entry.get("reason") not in DEAD_REASONS:
                        problems.append(f"invalid dead entry: {entry!r}")
                        break
        return problems

    # -- intents -----------------------------------------------------------
    def read_intent(self, intent_id: str | None) -> dict | None:
        if not intent_id:
            return None
        path = self.intents_dir / f"{intent_id}.md"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        meta: dict[str, str] = {}
        body = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) == 3:
                for line in parts[1].splitlines():
                    if ":" in line:
                        key, value = line.split(":", 1)
                        meta[key.strip()] = value.strip()
                body = parts[2]
        meta["body"] = body.strip()
        return meta

    def intent_signed(self, intent_id: str | None) -> tuple[bool, dict]:
        meta = self.read_intent(intent_id)
        if meta is None:
            return False, {}
        return str(meta.get("signed", "")).lower() in ("true", "yes", "1"), meta

    # -- dedupe (advisory only) -------------------------------------------
    def find_by_fingerprint(self, fingerprint: str, *, exclude: str | None = None) -> list[dict]:
        hits = []
        for task in self.list():
            if task["id"] == exclude:
                continue
            if task.get("fingerprint") == fingerprint and task.get("state") not in TERMINAL | {"escalated"}:
                hits.append(task)
        return hits
