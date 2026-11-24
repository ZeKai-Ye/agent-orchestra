"""v0.5.4: the loop renews its lease mid-round, and gives up when it is stolen."""

from __future__ import annotations

import json
import sqlite3

from .support import PY, RepoCase, cli


class HeartbeatCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()

    def script(self, body: str, name: str = "agent.py"):
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path

    def agent_cmd(self, script) -> str:
        return f'"{PY}" "{script}"'

    def run_loop(self, *args, expect=0):
        # now=None: a real clock, because renewal is about wall time
        return cli(self.repo, "loop", *args, "--json", expect=expect, now=None)

    def event_count(self, kind: str) -> int:
        conn = sqlite3.connect(str(self.repo / ".orchestra" / "state.db"))
        try:
            row = conn.execute("SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def task_state(self, task_id: str) -> str:
        data = json.loads((self.repo / "ledger" / "tasks" / f"{task_id}.json").read_text(encoding="utf-8"))
        return data["state"]


class HeartbeatTests(HeartbeatCase):
    def test_slow_agent_keeps_its_lease(self):
        self.make_task(["notes.txt"], title="slow round")
        slow = self.script("import time\ntime.sleep(1.2)\n")
        summary = json.loads(
            self.run_loop("--once", "--agent-cmd", self.agent_cmd(slow), "--idle-sleep", "0.01",
                          "--heartbeat", "0.3").stdout
        )
        self.assertEqual(summary["lease_lost"], 0, summary)
        self.assertGreaterEqual(self.event_count("lease.renewed"), 1, "the round must heartbeat")
        self.assertEqual(self.event_count("lease.expired"), 0, "no lease may expire mid-round")

    def test_stolen_lease_abandons_the_round_without_touching_the_ledger(self):
        self.make_task(["notes.txt"], title="stolen round")
        thief = self.script(
            "import pathlib, sqlite3, time\n"
            "repo = pathlib.Path.cwd().parents[2]\n"
            "conn = sqlite3.connect(str(repo / '.orchestra' / 'state.db'), timeout=30)\n"
            "conn.execute(\"UPDATE leases SET holder = 'intruder' WHERE state = 'active'\")\n"
            "conn.commit()\n"
            "conn.close()\n"
            "time.sleep(1.5)\n"
        )
        summary = json.loads(
            self.run_loop("--once", "--agent-cmd", self.agent_cmd(thief), "--idle-sleep", "0.01",
                          "--heartbeat", "0.2").stdout
        )
        self.assertEqual(summary["lease_lost"], 1, summary)
        record = summary["rounds"][0]
        self.assertEqual(record["outcome"], "lease_lost", record)
        # the ledger belongs to whoever holds the lease now: we must not have moved it
        self.assertNotEqual(self.task_state(record["task"]), "review")
        self.assertEqual(self.event_count("lease.lost"), 1)
        self.assertEqual(self.event_count("gate.passed"), 0, "a lost lease must not reach the gate")
