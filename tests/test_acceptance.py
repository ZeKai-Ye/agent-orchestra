"""Acceptance criteria: created with the task, frozen, and visible to the agent."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .support import PY, SRC, RepoCase, cli

SCRIPT = SRC.parent / "scripts" / "agent_run.py"


class AcceptanceCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()


class AcceptanceTests(AcceptanceCase):
    def test_task_new_refuses_without_criteria(self):
        proc = cli(self.repo, "task", "new", "--title", "x", "--paths", "a.py", expect=1)
        self.assertIn("--acceptance", proc.stderr)
        self.assertIn("nobody wrote down", proc.stderr)

    def test_criteria_are_parsed_digested_and_frozen(self):
        task = self.make_task(
            ["a.py"],
            title="carry criteria",
            acceptance=[
                "A1|check|python -c 'print(1)'|prints one",
                "A2|judge|the message names a file|reviewer reads the diff",
            ],
        )
        self.assertEqual(task["schema_version"], 2)
        self.assertEqual([entry["id"] for entry in task["acceptance"]], ["A1", "A2"])
        self.assertEqual(task["acceptance"][0]["class"], "check")
        self.assertEqual(task["acceptance"][1]["note"], "reviewer reads the diff")
        self.assertTrue(task["acceptance_digest"].startswith("sha256:"))

    def test_a_bad_criterion_line_is_refused(self):
        proc = cli(
            self.repo, "task", "new", "--title", "x", "--paths", "a.py",
            "--acceptance", "A1|vibes|feels right", expect=1,
        )
        self.assertIn("check", proc.stderr)

    def test_brief_and_prompt_carry_the_criteria(self):
        task = self.make_task(["a.py"], title="carry to the agent",
                              acceptance=["A1|judge|the behaviour is described|"])
        cli(self.repo, "loop", "--once", expect=0)
        brief = json.loads((self.repo / ".orchestra" / "briefs" / f"{task['id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(brief["acceptance"][0]["id"], "A1")
        self.assertEqual(brief["acceptance_digest"], task["acceptance_digest"])

        proc = subprocess.run(
            [PY, str(SCRIPT), str(self.repo / ".orchestra" / "briefs" / f"{task['id']}.json"), "--print-prompt"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Acceptance criteria", proc.stdout)
        self.assertIn("A1 [judge] the behaviour is described", proc.stdout)
        self.assertIn(f"notes/{task['id']}.md", proc.stdout)

    def test_editing_a_criterion_after_creation_fails_the_gate(self):
        task = self.make_task(["a.py"], acceptance=["A1|judge|as described|"])
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-1", expect=0)
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "in_progress", expect=0)

        path = self.repo / "ledger" / "tasks" / f"{task['id']}.json"
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["acceptance"][0]["criterion"] = "something easier"
        path.write_text(json.dumps(edited, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")

        proc = cli(self.repo, "gate", "run", "--task", task["id"], "--stage", "impl", expect=1)
        self.assertIn("acceptance_digest", proc.stdout + proc.stderr)

    def test_promote_turns_the_roadmap_accept_into_a_check(self):
        cli(self.repo, "roadmap", "add", "--title", "do the thing", "--accept",
            f"{PY} -c pass", "--scope", "src/core", expect=0)
        cli(self.repo, "roadmap", "triage", expect=0)
        promoted = json.loads(cli(self.repo, "roadmap", "promote", "--item", "R-0001", "--json", expect=0).stdout)
        task = promoted["task"] if isinstance(promoted.get("task"), dict) else promoted
        task_id = task["id"] if isinstance(task, dict) else str(task)
        stored = json.loads((self.repo / "ledger" / "tasks" / f"{task_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["acceptance"][0]["class"], "check")
        self.assertIn("pass", stored["acceptance"][0]["criterion"])
