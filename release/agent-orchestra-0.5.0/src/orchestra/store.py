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

CREATE TABLE IF NOT EXISTS cooldowns (
    holder  TEXT NOT NULL,
    task_id TEXT NOT NULL,
    until   REAL NOT NULL,
    PRIMARY KEY (holder, task_id)
);

CREATE TABLE IF NOT EXISTS critic_stats (
    holder       TEXT PRIMARY KEY,
    confirm_streak INTEGER NOT NULL DEFAULT 0,
    last_verdict TEXT,
    last_at      REAL
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
    def budget_set(self, scope: str, ceiling: float | None) -> None:
        """Register or clear the envelope of a scope (spent is preserved)."""

        with self.immediate() as conn:
            conn.execute(
                "INSERT INTO budget(scope, spent, ceiling) VALUES (?, 0, ?) "
                "ON CONFLICT(scope) DO UPDATE SET ceiling = ?",
                (scope, ceiling, ceiling),
            )

    def budget_charge(self, charges: list[tuple[str, float]]) -> tuple[bool, dict]:
        """Charge several scopes in one transaction; all or nothing.

        Returns ``(ok, detail)``. On refusal ``detail`` names the scope that broke
        its envelope, so the caller can emit a machine-readable reason instead of
        a sentence.
        """

        prepared: list[tuple[str, float, float | None]] = []
        with self.immediate() as conn:
            for scope, amount in charges:
                row = conn.execute(
                    "SELECT spent, ceiling FROM budget WHERE scope = ?", (scope,)
                ).fetchone()
                spent = 0.0 if row is None else float(row["spent"])
                ceiling = None if row is None else row["ceiling"]
                total = spent + float(amount)
                if ceiling is not None and total > float(ceiling):
                    return False, {
                        "reason": "budget_exceeded",
                        "scope": scope,
                        "spent": spent,
                        "amount": float(amount),
                        "ceiling": float(ceiling),
                    }
                prepared.append((scope, total, ceiling))
            for scope, total, ceiling in prepared:
                conn.execute(
                    "INSERT INTO budget(scope, spent, ceiling) VALUES (?,?,?) "
                    "ON CONFLICT(scope) DO UPDATE SET spent = ?",
                    (scope, total, ceiling, total),
                )
        return True, {"charged": [scope for scope, _amount, _ceiling in prepared]}

    def budget_spend(self, scope: str, amount: float, ceiling: float | None = None) -> bool:
        """Single-scope convenience wrapper around :meth:`budget_charge`."""

        if ceiling is not None:
            self.budget_set(scope, ceiling)
        return self.budget_charge([(scope, amount)])[0]

    def budget_state(self) -> list[dict]:
        rows = self.conn.execute("SELECT scope, spent, ceiling FROM budget ORDER BY scope").fetchall()
        return [dict(row) for row in rows]

    # -- critic rotation ----------------------------------------------------
    def critic_streak(self, holder: str) -> int:
        row = self.conn.execute("SELECT confirm_streak FROM critic_stats WHERE holder = ?", (holder,)).fetchone()
        return 0 if row is None else int(row["confirm_streak"])

    def critic_last_verdict(self, holder: str) -> str | None:
        row = self.conn.execute("SELECT last_verdict FROM critic_stats WHERE holder = ?", (holder,)).fetchone()
        return None if row is None else row["last_verdict"]

    def critic_record(self, holder: str, verdict: str) -> None:
        """A `confirmed` extends the streak; a `refuted` resets it (that critic did work)."""

        with self.immediate() as conn:
            row = conn.execute("SELECT confirm_streak FROM critic_stats WHERE holder = ?", (holder,)).fetchone()
            current = 0 if row is None else int(row["confirm_streak"])
            streak = current + 1 if verdict == "confirmed" else 0
            conn.execute(
                "INSERT INTO critic_stats(holder, confirm_streak, last_verdict, last_at) VALUES (?,?,?,?) "
                "ON CONFLICT(holder) DO UPDATE SET confirm_streak = ?, last_verdict = ?, last_at = ?",
                (holder, streak, verdict, self.clock.now(), streak, verdict, self.clock.now()),
            )
            self.event("critic.verdict", actor=holder, verdict=verdict, streak=streak)

    # -- per-holder admission (loops and fleets) ---------------------------
    def holder_leases(self, holder: str, now: float) -> int:
        """How many distinct lease groups a holder currently owns."""

        row = self.conn.execute(
            "SELECT COUNT(DISTINCT group_id) AS n FROM leases "
            "WHERE holder = ? AND state = 'active' AND expires_at > ?",
            (holder, now),
        ).fetchone()
        return int(row["n"])

    def cooldown_until(self, holder: str, task_id: str) -> float:
        row = self.conn.execute(
            "SELECT until FROM cooldowns WHERE holder = ? AND task_id = ?", (holder, task_id)
        ).fetchone()
        return 0.0 if row is None else float(row["until"])

    def cooldown_set(self, holder: str, task_id: str, until: float) -> None:
        with self.immediate() as conn:
            conn.execute(
                "INSERT INTO cooldowns(holder, task_id, until) VALUES (?,?,?) "
                "ON CONFLICT(holder, task_id) DO UPDATE SET until = ?",
                (holder, task_id, float(until), float(until)),
            )
            self.event(
                "cooldown.set",
                conn=conn,
                task_id=task_id,
                actor=holder,
                until=float(until),
            )

    def cooldown_list(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT holder, task_id, until FROM cooldowns ORDER BY until"
        ).fetchall()
        return [dict(row) for row in rows]
