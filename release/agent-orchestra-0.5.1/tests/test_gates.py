"""Mechanical gates: scope guard, risk/T2 approval, debt budget, provenance, full flow."""

from __future__ import annotations

import json
import unittest

from .support import RepoCase, cli, git


class GateTests(RepoCase):
    def prepare(self, paths=("src/core",), intent="I-0001", **kwargs) -> dict:
        self.init_orchestra()
        self.intent(intent)
        task = self.make_task(paths=list(paths), intent=intent, **kwargs)
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-1", expect=0)
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "in_progress", "--actor", "agent-1", expect=0)
        self.branch(f"agent/lineage-a/{task['id']}")
        return task

    def gate(self, task, stage="impl", expect=0, extra=None):
        args = ["gate", "run", "--task", task["id"], "--stage", stage, "--json", *(extra or [])]
        return json.loads(cli(self.repo, *args, expect=expect).stdout)

    # -- scope -------------------------------------------------------------
    def test_scope_guard_blocks_writes_outside_lease(self):
        task = self.prepare()
        self.write("src/other/x.py", "x = 1\n")
        self.commit(f"feat: helper\n\nTask-Id: {task['id']}")
        payload = self.gate(task, expect=1)
        step = next(s for s in payload["steps"] if s["name"] == "scope")
        self.assertFalse(step["ok"])
        self.assertIn("src/other/x.py", step["detail"])

    def test_impl_gate_passes_inside_lease(self):
        task = self.prepare()
        self.write("src/core/app.py", "def add(a, b):\n    return a + b + 0\n")
        self.commit(f"fix: add nothing\n\nTask-Id: {task['id']}")
        payload = self.gate(task)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["risk"]["class"], "T0")
        self.assertFalse(payload["requires_approval"])
        stored = json.loads(cli(self.repo, "task", "show", "--task", task["id"], "--json").stdout)
        self.assertEqual(stored["risk"]["class"], "T0")
        self.assertEqual(stored["state"], "in_progress")

    # -- risk --------------------------------------------------------------
    def test_t2_requires_human_token_and_is_single_use(self):
        task = self.prepare(paths=("src/core", "deploy"))
        self.write("deploy/release.sh", "terraform apply -auto-approve\n")
        self.commit(f"chore: release\n\nTask-Id: {task['id']}")
        risk = json.loads(cli(self.repo, "risk", "classify", "--json", expect=0).stdout)
        self.assertEqual(risk["class"], "T2")

        blocked = self.gate(task, expect=4)
        self.assertTrue(blocked["requires_approval"])

        cli(self.repo, "approval", "keygen", expect=0)
        issued = json.loads(
            cli(
                self.repo,
                "approval",
                "issue",
                "--kind",
                "merge",
                "--by",
                "kai",
                "--subject",
                risk["digest"],
                "--prediction",
                "release script runs once",
                "--rollback",
                "git revert HEAD",
                "--json",
                expect=0,
            ).stdout
        )
        token = issued["token"]
        approved = self.gate(task, extra=["--token", token])
        self.assertTrue(approved["ok"])
        replayed = self.gate(task, expect=4, extra=["--token", token])
        self.assertTrue(replayed["requires_approval"])

    def test_token_for_other_subject_is_rejected(self):
        task = self.prepare(paths=("src/core", "deploy"))
        self.write("deploy/release.sh", "terraform apply -auto-approve\n")
        self.commit(f"chore: release\n\nTask-Id: {task['id']}")
        cli(self.repo, "approval", "keygen", expect=0)
        stale = json.loads(
            cli(
                self.repo,
                "approval",
                "issue",
                "--kind",
                "merge",
                "--by",
                "kai",
                "--subject",
                "sha256:0000",
                "--prediction",
                "p",
                "--rollback",
                "r",
                "--json",
                expect=0,
            ).stdout
        )
        payload = self.gate(task, expect=4, extra=["--token", stale["token"]])
        self.assertFalse(payload["ok"])

    # -- debt / attempts / provenance --------------------------------------
    def test_debt_budget_blocks_growth(self):
        task = self.prepare(paths=("src/core", "config"))
        self.write("config/orchestra.toml", '[debt.core]\npaths = ["src/core"]\nmax_lines = 2\n')
        self.write("src/core/app.py", "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n")
        self.commit(f"feat: sub\n\nTask-Id: {task['id']}")
        payload = self.gate(task, expect=1)
        failed = [s["name"] for s in payload["steps"] if not s["ok"]]
        self.assertIn("debt: core", failed)

    def test_attempt_budget_blocks_retry(self):
        task = self.prepare(max_attempts="1")
        self.write("src/core/app.py", "def add(a, b):\n    return a + b\n")
        self.commit(f"fix: retry\n\nTask-Id: {task['id']}")
        cli(self.repo, "task", "attempt", "--task", task["id"], "--actor", "agent-1", expect=0)
        payload = self.gate(task, expect=1)
        failed = [s["name"] for s in payload["steps"] if not s["ok"]]
        self.assertIn("attempt-budget", failed)

    def test_merge_requires_task_id_trailer(self):
        task = self.prepare()
        self.write("src/core/app.py", "def add(a, b):\n    return b + a\n")
        self.commit("fix: swap order")
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "review", "--actor", "agent-1", expect=0)
        payload = self.gate(task, stage="merge", expect=1)
        failed = [s["name"] for s in payload["steps"] if not s["ok"]]
        self.assertIn("provenance", failed)

        git(self.repo, "commit", "--amend", "--no-gpg-sign", "-m", f"fix: swap order\n\nTask-Id: {task['id']}")
        self.assertTrue(self.gate(task, stage="merge")["ok"])

    # -- end to end --------------------------------------------------------
    def test_full_flow_to_merged(self):
        task = self.prepare()
        self.write("src/core/app.py", "def add(a, b):\n    return a + b\n\n\ndef negate(a):\n    return -a\n")
        self.commit(f"feat: negate\n\nTask-Id: {task['id']}")
        self.assertTrue(self.gate(task)["ok"])
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "review", "--actor", "agent-1", expect=0)
        self.assertTrue(self.gate(task, stage="merge")["ok"])
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "approved", "--actor", "human:kai", expect=0)
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "merged", "--actor", "human:kai", expect=0)
        final = json.loads(cli(self.repo, "task", "show", "--task", task["id"], "--json").stdout)
        self.assertEqual(final["state"], "merged")
        self.assertIsNone(final["lease"])
        self.assertEqual(json.loads(cli(self.repo, "lease", "list", "--json").stdout)["leases"], [])
        status = json.loads(cli(self.repo, "status", "--json").stdout)
        self.assertEqual(status["tasks"]["by_state"]["merged"], 1)
        self.assertGreaterEqual(status["gates"]["passed_24h"], 2)

    def test_unknown_task_is_refused(self):
        self.init_orchestra()
        proc = cli(self.repo, "gate", "run", "--task", "T-9999", "--stage", "impl", "--json", expect=1)
        self.assertIn("unknown task", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()