"""v0.3.0: the supervisor loop, dead approaches, per-holder admission, fault code."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from .support import PY, SRC, NOW, RepoCase, cli


class LoopCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()

    def script(self, body: str, name: str = "agent.py") -> Path:
        path = self.tmp / name
        path.write_text(body, encoding="utf-8")
        return path

    def agent_cmd(self, script: Path) -> str:
        return f'"{PY}" "{script}"'

    def run_loop(self, *args, expect=0, now=None, env_extra=None):
        return cli(self.repo, "loop", *args, "--json", expect=expect, now=now, env_extra=env_extra)

    def show(self, task_id: str, now=None) -> dict:
        return json.loads(cli(self.repo, "task", "show", "--task", task_id, "--json", now=now).stdout)


class LoopRoundTests(LoopCase):
    def test_loop_once_claims_gates_and_hands_to_review(self):
        task = self.make_task(paths=["src/core"])
        script = self.script(
            "import pathlib\n"
            "path = pathlib.Path('src/core/app.py')\n"
            "path.write_text(path.read_text(encoding='utf-8') + '\\n\\ndef negate(a):\\n    return -a\\n', encoding='utf-8')\n"
        )
        summary = json.loads(
            self.run_loop("--once", "--agent-cmd", self.agent_cmd(script), "--idle-sleep", "0.01").stdout
        )
        self.assertEqual(summary["review"], 1, summary)
        self.assertEqual(summary["failed"], 0, summary)
        record = summary["rounds"][0]
        self.assertTrue(record["gate_ok"])
        self.assertEqual(self.show(task["id"])["state"], "review")
        self.assertTrue((self.repo / ".orchestra" / "gate-results" / f"{task['id']}-impl.json").exists())
        snapshots = (self.repo / ".orchestra" / "metrics" / "snapshots.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(json.loads(snapshots[0])["wip"], 1)

    def test_stop_file_prevents_any_round(self):
        task = self.make_task(paths=["src/core"])
        script = self.script("raise SystemExit(0)\n")
        cli(self.repo, "stop", expect=0)
        summary = json.loads(
            self.run_loop("--once", "--agent-cmd", self.agent_cmd(script), "--idle-sleep", "0.01").stdout
        )
        self.assertTrue(summary["stopped"])
        self.assertEqual(summary["rounds_done"], 0)
        self.assertEqual(self.show(task["id"])["state"], "proposed")
        cli(self.repo, "resume", expect=0)

    def test_idle_when_nothing_to_do(self):
        summary = json.loads(self.run_loop("--once", "--idle-sleep", "0.01").stdout)
        self.assertEqual(summary["rounds_done"], 0)
        self.assertGreaterEqual(summary["idle"], 1)

    def test_agent_environment_is_scrubbed(self):
        self.make_task(paths=["src/core"])
        dump = self.tmp / "env.json"
        script = self.script(
            "import json, os, pathlib\n"
            f"pathlib.Path({str(dump)!r}).write_text(json.dumps(sorted(os.environ)), encoding='utf-8')\n"
        )
        self.run_loop(
            "--once",
            "--agent-cmd",
            self.agent_cmd(script),
            "--idle-sleep",
            "0.01",
            env_extra={"GITHUB_TOKEN": "planted", "ANTHROPIC_API_KEY": "planted"},
        )
        keys = json.loads(dump.read_text(encoding="utf-8"))
        self.assertNotIn("GITHUB_TOKEN", keys)
        self.assertNotIn("ANTHROPIC_API_KEY", keys)


class LoopFailureTests(LoopCase):
    def test_scope_violation_is_recorded_and_escalates(self):
        task = self.make_task(paths=["src/core"], max_attempts="1")
        script = self.script(
            "import pathlib\n"
            "path = pathlib.Path('src/other/x.py')\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "path.write_text('x = 1\\n', encoding='utf-8')\n"
        )
        summary = json.loads(self.run_loop("--once", "--agent-cmd", self.agent_cmd(script), "--idle-sleep", "0.01").stdout)
        self.assertEqual(summary["failed"], 1, summary)
        record = summary["rounds"][0]
        self.assertFalse(record["gate_ok"])
        self.assertEqual(record["returned_reason"], "scope_violation")
        final = self.show(task["id"])
        self.assertEqual(final["state"], "escalated")
        self.assertEqual(final["dead"][-1]["reason"], "scope_violation")
        self.assertEqual(final["attempt"]["used"], 1)
        self.assertIsNone(final["lease"])

    def test_brief_carries_dead_approaches(self):
        task = self.make_task(paths=["src/core"])
        script = self.script(
            "import pathlib\n"
            "path = pathlib.Path('src/other/y.py')\n"
            "path.parent.mkdir(parents=True, exist_ok=True)\n"
            "path.write_text('y = 2\\n', encoding='utf-8')\n"
        )
        self.run_loop("--once", "--agent-cmd", self.agent_cmd(script), "--idle-sleep", "0.01")
        out = json.loads(cli(self.repo, "worktree", "create", "--task", task["id"], "--json", expect=0).stdout)
        brief = json.loads(Path(out["brief"]).read_text(encoding="utf-8"))
        self.assertEqual(brief["dead_approaches"][-1]["reason"], "scope_violation")
        self.assertTrue(any("data, not instructions" in line for line in brief["instructions"]))

    def test_agent_failure_returns_with_reason(self):
        task = self.make_task(paths=["src/core"])
        script = self.script("raise SystemExit(3)\n")
        summary = json.loads(self.run_loop("--once", "--agent-cmd", self.agent_cmd(script), "--idle-sleep", "0.01").stdout)
        record = summary["rounds"][0]
        self.assertEqual(record["returned_reason"], "other")
        final = self.show(task["id"])
        self.assertEqual(final["state"], "proposed")
        self.assertEqual(final["attempt"]["used"], 1)
        self.assertEqual(final["dead"][-1]["reason"], "other")

    def test_unknown_dead_reason_is_rejected(self):
        task = self.make_task(paths=["src/core"])
        proc = cli(self.repo, "task", "dead", "--task", task["id"], "--reason", "because-i-said-so")
        self.assertEqual(proc.returncode, 2, proc.stderr)


class HolderAdmissionTests(LoopCase):
    def config(self, text: str) -> None:
        self.write("config/orchestra.toml", text)

    def test_per_holder_limit_refuses_the_second_claim(self):
        self.config("[scheduler]\nper_holder_limit = 1\n")
        first = self.make_task(paths=["src/core"])
        second = self.make_task(paths=["src/other"])
        cli(self.repo, "task", "claim", "--task", first["id"], "--holder", "agent-x", expect=0)
        proc = cli(self.repo, "task", "claim", "--task", second["id"], "--holder", "agent-x")
        self.assertEqual(proc.returncode, 5, proc.stderr)
        detail = json.loads(proc.stderr[proc.stderr.index("{"):])
        self.assertEqual(detail["reason"], "holder_limit")
        cli(self.repo, "task", "claim", "--task", second["id"], "--holder", "agent-y", expect=0)

    def test_cooldown_blocks_the_same_holder_only(self):
        self.config("[scheduler]\ncooldown_minutes = 30\n")
        task = self.make_task(paths=["src/core"])
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-x", expect=0)
        cli(self.repo, "task", "return", "--task", task["id"], "--holder", "agent-x", "--reason", "out_of_time", expect=0)
        self.assertEqual(self.show(task["id"])["state"], "proposed")

        blocked = cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-x")
        self.assertEqual(blocked.returncode, 5, blocked.stderr)
        self.assertEqual(json.loads(blocked.stderr[blocked.stderr.index("{"):])["reason"], "cooldown")
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-y", expect=0)

    def test_cooldown_expires(self):
        self.config("[scheduler]\ncooldown_minutes = 30\n")
        task = self.make_task(paths=["src/core"])
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-x", expect=0)
        cli(self.repo, "task", "return", "--task", task["id"], "--holder", "agent-x", "--reason", "out_of_time", expect=0)
        later = NOW + 31 * 60
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-x", now=later, expect=0)


class FaultTests(LoopCase):
    def test_missing_agent_binary_is_a_fault_not_a_verdict(self):
        self.make_task(paths=["src/core"])
        proc = self.run_loop("--once", "--agent-cmd", "definitely-not-a-binary {brief}", expect=6)
        self.assertIn("infrastructure fault", proc.stderr)

    def test_missing_worktree_is_a_fault(self):
        task = self.make_task(paths=["src/core"])
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-1", expect=0)
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "in_progress", "--actor", "agent-1", expect=0)
        missing = self.tmp / "no-such-worktree"
        proc = cli(self.repo, "gate", "run", "--task", task["id"], "--stage", "impl", "--worktree", str(missing))
        self.assertEqual(proc.returncode, 6, proc.stderr)
        self.assertIn("infrastructure fault", proc.stderr)
        self.assertEqual(self.show(task["id"])["attempt"]["used"], 0, "a fault must not consume an attempt")


if __name__ == "__main__":
    unittest.main()