"""SQLite coordination store: leases, audit events, budget ledger, used nonces.

Deliberately *not* in git: a repo file used as a lock is a single-writer hot spot
with merge conflicts and no atomic compare-and-swap (see docs/01-feasibility-review.md
correction #2). One SQLite file with WAL and ``BEGIN IMMEDIATE`` gives real
serialisation; ``lease snapshot`` exports a reviewable copy back into the repo.

Single-host assumption: multiple agent containers on one machine are fine
(shared file). Multi-host needs Postgres; ``LeaseManager`` is the only module
that talks to these tables.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .clock import Clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS leases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT    NOT NULL,
    task_id     TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    mode        TEXT    NOT NULL,
    holder      TEXT    NOT NULL,
    lineage     TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL,
    expires_at  REAL    NOT NULL,
    state       TEXT    NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_leases_state ON leases(state, expires_at);
CREATE INDEX IF NOT EXISTS idx_leases_group ON leases(group_id);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL    NOT NULL,
    kind      TEXT    NOT NULL,
    task_id   TEXT,
    group_id  TEXT,
    actor     TEXT,
    payload   TEXT    NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, at);

CREATE TABLE IF NOT EXISTS approvals_used (
    nonce      TEXT PRIMARY KEY,
    approval_id TEXT NOT NULL,
    digest     TEXT NOT NULL DEFAULT '',
    used_at    REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS budget (
    scope   TEXT PRIMARY KEY,
    spent   REAL NOT NULL DEFAULT 0,
    ceiling REAL
);
"""


class Store:
    def __init__(self, db_path: Path, clock: Clock) -> None:
        self.db_path = Path(db_path)
        self.clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:  # pragma: no cover - close is best effort
            pass

    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        """Write transaction: take the write lock up front, so CAS is real."""

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # -- audit events -------------------------------------------------------
    def event(
        self,
        kind: str,
        *,
        conn: sqlite3.Connection | None = None,
        task_id: str | None = None,
        group_id: str | None = None,
        actor: str | None = None,
        **payload: Any,
    ) -> None:
        target = conn or self.conn
        target.execute(
            "INSERT INTO events(at, kind, task_id, group_id, actor, payload) VALUES (?,?,?,?,?,?)",
            (
                self.clock.now(),
                kind,
                task_id,
                group_id,
                actor,
                json.dumps(payload, sort_keys=True, ensure_ascii=False),
            ),
        )

    def recent_events(self, limit: int = 50, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if kind:
            sql += " WHERE kind = ?"
            params.append(kind)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [
            {
                "at": row["at"],
                "kind": row["kind"],
                "task_id": row["task_id"],
                "group_id": row["group_id"],
                "actor": row["actor"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    def count_events(self, kind: str, since: float) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind = ? AND at >= ?", (kind, since)
        ).fetchone()
        return int(row["n"])

    # -- approval nonces (single use) --------------------------------------
    def use_nonce(self, nonce: str, approval_id: str, digest: str) -> bool:
        """Mark a nonce used. Returns False when it was already spent."""

        try:
            with self.immediate() as conn:
                conn.execute(
                    "INSERT INTO approvals_used(nonce, approval_id, digest, used_at) VALUES (?,?,?,?)",
                    (nonce, approval_id, digest, self.clock.now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def nonce_used(self, nonce: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM approvals_used WHERE nonce = ?", (nonce,)
        ).fetchone()
        return row is not None

    # -- budget (admission control) ----------------------------------------
    def budget_spend(self, scope: str, amount: float, ceiling: float | None = None) -> bool:
        """Pre-charge a budget scope. Returns False when the ceiling would break."""

        with self.immediate() as conn:
            row = conn.execute("SELECT spent, ceiling FROM budget WHERE scope = ?", (scope,)).fetchone()
            spent = 0.0 if row is None else float(row["spent"])
            limit = ceiling if ceiling is not None else (None if row is None else row["ceiling"])
            if limit is not None and spent + amount > float(limit):
                return False
            conn.execute(
                "INSERT INTO budget(scope, spent, ceiling) VALUES (?,?,?) "
                "ON CONFLICT(scope) DO UPDATE SET spent = ?",
                (scope, spent + amount, limit, spent + amount),
            )
            return True

    def budget_state(self) -> list[dict]:
        rows = self.conn.execute("SELECT scope, spent, ceiling FROM budget ORDER BY scope").fetchall()
        return [dict(row) for row in rows]
