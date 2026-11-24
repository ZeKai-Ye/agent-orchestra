"""M2: worktree + brief, and the reference adapter that drives one CLI agent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from .support import PY, SRC, RepoCase, TEST_ROOT, cli

ADAPTER = Path(__file__).resolve().parents[1] / "tools" / "agent_adapter.py"


class AdapterTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()
        self.task = self.make_task(paths=["src/core"])
        cli(self.repo, "task", "claim", "--task", self.task["id"], "--holder", "agent-1", expect=0)
        cli(self.repo, "task", "transition", "--task", self.task["id"], "--to", "in_progress", "--actor", "agent-1", expect=0)
        self.worktree = self.repo / ".orchestra" / "worktrees" / self.task["id"]

    def adapter(self, *args, expect=0, env_extra=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [PY, str(ADAPTER), "--repo", str(self.repo), "--task", self.task["id"], *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        if expect is not None and proc.returncode != expect:
            raise AssertionError(f"expected exit {expect}, got {proc.returncode}\n{proc.stdout}\n{proc.stderr}")
        return proc

    def test_worktree_and_brief_are_created(self):
        out = cli(self.repo, "worktree", "create", "--task", self.task["id"], "--json", expect=0).stdout
        payload = json.loads(out)
        self.assertTrue(Path(payload["worktree"]).is_dir())
        self.assertEqual(payload["branch"], f"agent/lineage-a/{self.task['id']}")
        brief = json.loads(Path(payload["brief"]).read_text(encoding="utf-8"))
        self.assertEqual(brief["lease_paths"], ["src/core"])
        self.assertEqual(brief["task"]["id"], self.task["id"])
        self.assertTrue(any("Task-Id" in line for line in brief["instructions"]))

    def test_dry_run_scrubs_credentials(self):
        cli(self.repo, "worktree", "create", "--task", self.task["id"], expect=0)
        proc = self.adapter("--dry-run", "--json", env_extra={"GITHUB_TOKEN": "planted", "AWS_SECRET_ACCESS_KEY": "x"})
        plan = json.loads(proc.stdout)
        self.assertTrue(plan["worktree_exists"])
        self.assertEqual(plan["lease_paths"], ["src/core"])
        self.assertIsNone(plan["argv"])
        self.assertIn("GITHUB_TOKEN", plan["removed_env"])
        self.assertIn("AWS_SECRET_ACCESS_KEY", plan["removed_env"])
        self.assertGreater(plan["prompt_chars"], 0)

    def test_full_loop_with_a_fake_agent(self):
        cli(self.repo, "worktree", "create", "--task", self.task["id"], expect=0)
        script = self.tmp / "fake_agent.py"
        script.write_text(
            "import pathlib\n"
            "path = pathlib.Path('src/core/app.py')\n"
            "path.write_text(path.read_text(encoding='utf-8') + '\\n\\ndef negate(a):\\n    return -a\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        proc = self.adapter(
            "--agent-cmd",
            f'"{PY}" "{script}"',
            "--orchestra-path",
            str(SRC),
            "--json",
            expect=0,
        )
        plan = json.loads(proc.stdout)
        self.assertEqual(plan["agent_exit"], 0)
        self.assertTrue(plan["gate_ok"], plan.get("failed_steps"))
        self.assertEqual(plan["gate_exit"], 0)
        self.assertEqual(plan["risk_class"], "T0")
        edited = (self.worktree / "src" / "core" / "app.py").read_text(encoding="utf-8")
        self.assertIn("def negate", edited)

    def test_missing_brief_is_refused(self):
        proc = self.adapter("--dry-run", expect=1)
        self.assertIn("no brief", proc.stderr + proc.stdout)


if __name__ == "__main__":
    unittest.main()