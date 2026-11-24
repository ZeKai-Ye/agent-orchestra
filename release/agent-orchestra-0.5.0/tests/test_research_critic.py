"""v0.4.1: research deliverables (proposals) and critic verdicts (refutation blocks conclusions)."""

from __future__ import annotations

import json
import unittest

from .support import RepoCase, cli

PROPOSALS = """# proposals from a research round

---
title: Evaluate an existing merge queue
accept: python -m unittest tests.test_roadmap
scope: docs/06-references.md
size: S
---

---
title: A vacuous proposal that only looks runnable
accept: python -c "print(1)"
scope: docs
size: S
---
"""

REFUTATION = """# verdict

---
verdict: refuted
target: {target}
evidence: the accept command prints 1 and touches nothing; it cannot fail
reason: acceptance command is vacuous
---
"""


class RoleCase(RepoCase):
    def prepare(self, role: str, paths=("roadmap",), target: str | None = None, max_attempts="2") -> dict:
        self.init_orchestra()
        self.intent()
        kwargs = {"max_attempts": max_attempts}
        if target:
            kwargs["target"] = target
        task = self.make_task(paths=list(paths), role=role, **kwargs)
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-1", expect=0)
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "in_progress", "--actor", "agent-1", expect=0)
        self.branch(f"agent/lineage-a/{task['id']}")
        return task

    def gate(self, task, expect=0):
        return cli(self.repo, "gate", "run", "--task", task["id"], "--stage", "impl", "--json", expect=expect)


class ResearchGateTests(RoleCase):
    def test_missing_proposals_fail_the_deliverable(self):
        task = self.prepare("research")
        payload = json.loads(self.gate(task, expect=1).stdout)
        step = next(item for item in payload["steps"] if item["name"] == "deliverable")
        self.assertFalse(step["ok"])
        self.assertIn("proposals", step["detail"])

    def test_valid_proposals_pass_and_import(self):
        task = self.prepare("research")
        self.write(f"roadmap/proposals/{task['id']}.md", PROPOSALS)
        payload = json.loads(self.gate(task).stdout)
        self.assertTrue(payload["ok"], payload)
        result = json.loads(cli(self.repo, "roadmap", "import", "--task", task["id"], "--json", expect=0).stdout)
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(result["created"][0]["origin"], f"research:{task['id']}")
        self.assertEqual(result["created"][0]["status"], "later")

    def test_malformed_proposal_fails_the_gate(self):
        task = self.prepare("research")
        self.write(f"roadmap/proposals/{task['id']}.md", "---\ntitle: no verdict\nscope: docs\n---\n")
        payload = json.loads(self.gate(task, expect=1).stdout)
        step = next(item for item in payload["steps"] if item["name"] == "deliverable")
        self.assertIn("malformed", step["detail"])

    def test_import_rejects_malformed_blocks(self):
        task = self.prepare("research")
        self.write(
            f"roadmap/proposals/{task['id']}.md",
            "---\ntitle: good one\naccept: python -c \"print(1)\"\nscope: docs\n---\n\n"
            "---\ntitle: bad one\nscope: docs\n---\n",
        )
        result = json.loads(cli(self.repo, "roadmap", "import", "--task", task["id"], "--json", expect=0).stdout)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("accept missing", result["rejected"][0]["problems"])

    def test_import_without_any_valid_proposal_exits_1(self):
        task = self.prepare("research")
        self.write(f"roadmap/proposals/{task['id']}.md", "---\ntitle: bad\nscope: docs\n---\n")
        proc = cli(self.repo, "roadmap", "import", "--task", task["id"])
        self.assertEqual(proc.returncode, 1, proc.stderr)


class CriticGateTests(RoleCase):
    def test_missing_verdict_fails_the_deliverable(self):
        task = self.prepare("critic", paths=("reviews",), target="R-0001")
        payload = json.loads(self.gate(task, expect=1).stdout)
        step = next(item for item in payload["steps"] if item["name"] == "deliverable")
        self.assertFalse(step["ok"])

    def test_refutation_without_evidence_fails(self):
        task = self.prepare("critic", paths=("reviews",), target="R-0001")
        self.write(f"reviews/{task['id']}.md", "---\nverdict: refuted\ntarget: R-0001\nreason: no\n---\n")
        payload = json.loads(self.gate(task, expect=1).stdout)
        step = next(item for item in payload["steps"] if item["name"] == "deliverable")
        self.assertIn("evidence", step["detail"])

    def test_refuted_item_is_dropped(self):
        item = json.loads(
            cli(self.repo, "roadmap", "add", "--title", "vacuous", "--accept", 'python -c "print(1)"',
                "--scope", "docs", "--json", expect=0).stdout
        )
        task = self.prepare("critic", paths=("reviews",), target=item["id"])
        self.write(f"reviews/{task['id']}.md", REFUTATION.format(target=item["id"]))
        self.assertTrue(json.loads(self.gate(task).stdout)["ok"])
        result = json.loads(cli(self.repo, "review", "--task", task["id"], "--json", expect=0).stdout)
        self.assertEqual(result["verdict"], "refuted")
        self.assertEqual(result["applied"]["status"], "dropped")
        listed = {row["id"]: row for row in json.loads(cli(self.repo, "roadmap", "list", "--json").stdout)["items"]}
        self.assertEqual(listed[item["id"]]["status"], "dropped")


class RefutationBlocksMergeTests(RoleCase):
    def test_refuted_task_cannot_merge(self):
        self.init_orchestra()
        self.intent()
        work = self.make_task(paths=["src/core"], intent="I-0001")
        cli(self.repo, "task", "claim", "--task", work["id"], "--holder", "agent-1", expect=0)
        for state in ("in_progress", "review", "approved"):
            cli(self.repo, "task", "transition", "--task", work["id"], "--to", state, "--actor", "agent-1", expect=0)

        critic = self.prepare("critic", paths=("reviews",), target=work["id"])
        self.write(f"reviews/{critic['id']}.md", REFUTATION.format(target=work["id"]))
        self.assertTrue(json.loads(self.gate(critic).stdout)["ok"])
        cli(self.repo, "review", "--task", critic["id"], expect=0)

        proc = cli(self.repo, "task", "transition", "--task", work["id"], "--to", "merged", "--actor", "human:kai")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("refuted", proc.stderr)
        self.assertEqual(json.loads(cli(self.repo, "task", "show", "--task", work["id"], "--json").stdout)["state"], "approved")


if __name__ == "__main__":
    unittest.main()