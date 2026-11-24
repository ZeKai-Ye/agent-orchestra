"""Path leases: who may currently write what.

Modes: ``read`` (shared), ``write`` (exclusive on the given paths/prefixes),
``module`` (exclusive, whole module), ``repo`` (exclusive on the repository,
requires a human approval token). Acquisition is a single SQLite transaction
with a fixed lock order (normalised paths sorted), so there is no deadlock path
and no partial acquisition.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from .clock import Clock
from .paths import normalize_many, overlaps
from .policy import ApprovalError, PolicyError, verify as verify_approval
from .store import Store

MODES = ("read", "write", "module", "repo")


class LeaseError(RuntimeError):
    """Lease request refused for a non-conflict reason."""


class LeaseConflict(LeaseError):
    def __init__(self, conflicts: list[dict]) -> None:
        self.conflicts = conflicts
        detail = "; ".join(
            f"{item['path']} held by {item['holder']} ({item['mode']}) until {int(item['expires_at'])}"
            for item in conflicts
        )
        super().__init__(f"paths already leased: {detail}")


class LeaseManager:
    def __init__(self, store: Store, clock: Clock, config=None, key: bytes | None = None) -> None:
        self.store = store
        self.clock = clock
        self.config = config
        self.key = key

    # -- acquire -----------------------------------------------------------
    def acquire(
        self,
        *,
        task_id: str,
        holder: str,
        paths,
        mode: str = "write",
        ttl: int | None = None,
        lineage: str = "",
        token: str | None = None,
    ) -> dict:
        if mode not in MODES:
            raise LeaseError(f"unknown lease mode: {mode}")
        if not holder.strip():
            raise LeaseError("holder is required")
        scopes = normalize_many(paths)
        if mode == "repo":
            scopes = [""]
            if not token:
                raise PolicyError("repo-wide lease requires --approval-token (kind repo_lease)")
            if self.key is None:
                raise PolicyError("no approval key configured")
            verify_approval(
                self.key,
                token,
                clock=self.clock,
                store=self.store,
                subject="repo",
                kinds=("repo_lease",),
            )
        if not scopes:
            raise LeaseError("at least one path is required")
        ttl_value = int(ttl or (self.config.lease_ttl(mode) if self.config else 1800))
        if self.config and ttl_value > self.config.max_ttl():
            raise LeaseError(f"ttl {ttl_value}s exceeds max_ttl {self.config.max_ttl()}s")
        if ttl_value <= 0:
            raise LeaseError("ttl must be positive")

        with self.store.immediate() as conn:
            now = self.clock.now()
            rows = conn.execute(
                "SELECT task_id, path, mode, holder, expires_at FROM leases "
                "WHERE state = 'active' AND expires_at > ?",
                (now,),
            ).fetchall()
            conflicts = []
            for row in rows:
                if row["mode"] == "read" and mode == "read":
                    continue
                if any(overlaps(scope, row["path"]) for scope in scopes):
                    conflicts.append(dict(row))
            if conflicts:
                raise LeaseConflict(conflicts)
            group = "L-" + secrets.token_hex(6)
            expires_at = now + ttl_value
            for scope in scopes:
                conn.execute(
                    "INSERT INTO leases(group_id, task_id, path, mode, holder, lineage, created_at, expires_at, state)"
                    " VALUES (?,?,?,?,?,?,?,?,'active')",
                    (group, task_id, scope, mode, holder, lineage, now, expires_at),
                )
            self.store.event(
                "lease.acquired",
                conn=conn,
                task_id=task_id,
                group_id=group,
                actor=holder,
                mode=mode,
                paths=scopes,
                ttl=ttl_value,
                expires_at=expires_at,
            )
        return {
            "group": group,
            "task": task_id,
            "holder": holder,
            "mode": mode,
            "paths": scopes,
            "ttl": ttl_value,
            "expires_at": int(expires_at),
        }

    # -- lifecycle ---------------------------------------------------------
    def renew(self, group: str, ttl: int | None = None, holder: str | None = None) -> dict:
        with self.store.immediate() as conn:
            rows = conn.execute(
                "SELECT * FROM leases WHERE group_id = ? AND state = 'active'", (group,)
            ).fetchall()
            if not rows:
                raise LeaseError(f"no active lease group {group}")
            if holder and rows[0]["holder"] != holder:
                raise LeaseError(f"lease {group} is held by {rows[0]['holder']}, not {holder}")
            wanted = int(ttl or (rows[0]["expires_at"] - rows[0]["created_at"]))
            if self.config and wanted > self.config.max_ttl():
                raise LeaseError(f"ttl {wanted}s exceeds max_ttl")
            expires_at = self.clock.now() + wanted
            conn.execute("UPDATE leases SET expires_at = ? WHERE group_id = ?", (expires_at, group))
            self.store.event(
                "lease.renewed",
                conn=conn,
                task_id=rows[0]["task_id"],
                group_id=group,
                actor=holder or rows[0]["holder"],
                expires_at=expires_at,
            )
        return {"group": group, "expires_at": int(expires_at), "paths": [row["path"] for row in rows]}

    def release(self, group: str) -> int:
        with self.store.immediate() as conn:
            row = conn.execute(
                "SELECT task_id, holder FROM leases WHERE group_id = ? AND state = 'active' LIMIT 1", (group,)
            ).fetchone()
            if row is None:
                return 0
            cur = conn.execute(
                "UPDATE leases SET state = 'released' WHERE group_id = ? AND state = 'active'", (group,)
            )
            self.store.event(
                "lease.released",
                conn=conn,
                task_id=row["task_id"],
                group_id=group,
                actor=row["holder"],
            )
            return int(cur.rowcount)

    def active(self) -> list[dict]:
        rows = self.store.conn.execute(
            "SELECT * FROM leases WHERE state = 'active' ORDER BY group_id, path"
        ).fetchall()
        now = self.clock.now()
        groups: dict[str, dict] = {}
        for row in rows:
            group = groups.setdefault(
                row["group_id"],
                {
                    "group": row["group_id"],
                    "task": row["task_id"],
                    "holder": row["holder"],
                    "mode": row["mode"],
                    "lineage": row["lineage"],
                    "expires_at": int(row["expires_at"]),
                    "expired": row["expires_at"] <= now,
                    "paths": [],
                },
            )
            group["paths"].append(row["path"])
        return list(groups.values())

    def expire_suspensions(self, ledger) -> list[str]:
        """After the grace window a suspended task goes back to the pool (reassignable)."""

        released = []
        for task in ledger.list("suspended"):
            info = ledger.suspension(task) or {}
            if float(info.get("until", 0)) > self.clock.now():
                continue
            try:
                ledger.transition(task["id"], "proposed", actor="reaper",
                                  note=f"grace expired (was {info.get('holder')}): reassignable")
                released.append(task["id"])
            except Exception:  # noqa: BLE001
                continue
        return released

    def reap(self, ledger=None) -> list[str]:
        """Expire stale leases and push affected tasks back into ``proposed``."""

        now = self.clock.now()
        with self.store.immediate() as conn:
            rows = conn.execute(
                "SELECT DISTINCT task_id, group_id FROM leases WHERE state = 'active' AND expires_at <= ?",
                (now,),
            ).fetchall()
            if not rows:
                rows = []
            conn.execute("UPDATE leases SET state = 'expired' WHERE state = 'active' AND expires_at <= ?", (now,))
            for row in rows:
                self.store.event(
                    "lease.expired",
                    conn=conn,
                    task_id=row["task_id"],
                    group_id=row["group_id"],
                    actor="governance:reaper",
                )
        tasks = sorted({row["task_id"] for row in rows})
        if ledger is None:
            return tasks
        self.expire_suspensions(ledger)
        grace = (self.config.resume_grace_minutes() * 60) if self.config else 7200
        for task_id in tasks:
            try:
                task = ledger.load(task_id)
            except Exception:  # noqa: BLE001 - stale ledger rows must not stop the reaper
                continue
            if task.get("state") not in ("claimed", "in_progress", "blocked"):
                continue
            used = int(task["attempt"]["used"])
            cap = int(task["attempt"]["max"])
            holder = (task.get("lease") or {}).get("holder") or "unknown"
            if used >= cap:
                try:
                    ledger.transition(task_id, "abandoned", actor="reaper",
                                      note="unrecoverable: attempts exhausted while disconnected")
                except Exception:  # noqa: BLE001
                    ledger.set_lease(task_id, None)
                continue
            ledger.set_lease(task_id, None)
            ledger.suspend(task_id, holder=holder, until=self.clock.now() + grace,
                           reason="lease_expired", actor="reaper")
        return tasks

    def snapshot(self, target: Path) -> Path:
        """Export active leases so the repo keeps a reviewable audit trail."""

        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(entry, sort_keys=True, ensure_ascii=False) for entry in self.active()]
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return target
