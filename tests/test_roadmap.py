"""v0.4.0: the roadmap queue and the research round driver."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from .support import PY, NOW, RepoCase, cli


class RoadmapCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()

    def add(self, title="do a thing", accept='python -c "print(1)"', scope=("src/core",), status="later", **kwargs):
        args = ["roadmap", "add", "--title", title, "--accept", accept, "--scope", *scope, "--status", status, "--json"]
        for key, value in kwargs.items():
            args += [f"--{key.replace('_', '-')}", str(value)]
        return json.loads(cli(self.repo, *args, expect=0).stdout)

    def check(self, expect=0):
        return cli(self.repo, "roadmap", "check", expect=expect)

    def queue(self):
        item_filter = None
        return {item["id"]: item for item in json.loads(cli(self.repo, "roadmap", "list", "--json").stdout)["items"]}

    def merged_task(self, roadmap_id: str, paths=("src/core",)) -> dict:
        task = json.loads(
            cli(self.repo, "task", "new", "--title", f"{roadmap_id} work", "--paths", *paths,
                "--intent", "I-0001", "--roadmap", roadmap_id,
                "--acceptance", "A1|judge|the roadmap item is addressed|", "--json", expect=0).stdout
        )
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-1", expect=0)
        for state in ("in_progress", "review", "approved", "merged"):
            cli(self.repo, "task", "transition", "--task", task["id"], "--to", state, "--actor", "human:kai", expect=0)
        return task


class QueueTests(RoadmapCase):
    def test_add_and_check(self):
        item = self.add(title="first", status="later")
        self.assertTrue(item["id"].startswith("R-"))
        self.assertEqual(item["status"], "later")
        proc = self.check()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok", proc.stdout)

    def test_check_rejects_a_second_next_item(self):
        self.add(status="next")
        self.add(title="second", status="next")
        proc = self.check(expect=1)
        self.assertIn("more than one next item", proc.stdout + proc.stderr)

    def test_check_rejects_an_item_without_a_verdict(self):
        proc = cli(self.repo, "roadmap", "add", "--title", "no verdict", "--accept", "", "--scope", "src/core")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("accept command is required", proc.stderr)


class TriageTests(RoadmapCase):
    def test_triage_scores_and_lists_blockers(self):
        self.add(title="ok", scope=("src/core",))
        item = self.add(title="missing scope", scope=("nope/missing",))
        results = {entry["id"]: entry for entry in json.loads(cli(self.repo, "roadmap", "triage", "--json", expect=0).stdout)["triage"]}
        self.assertTrue(results[self.queue()["R-0001"]["id"]]["feasible"])
        self.assertFalse(results[item["id"]]["feasible"])
        self.assertFalse(results[item["id"]]["checks"]["scope-exists"])

    def test_promote_refuses_an_infeasible_item(self):
        item = self.add(accept="definitely-not-a-binary --x", scope=("src/core",))
        proc = cli(self.repo, "roadmap", "promote", "--item", item["id"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("not feasible", proc.stderr)


class PromoteTests(RoadmapCase):
    def test_promote_moves_item_and_creates_a_task(self):
        item = self.add(title="promote me")
        result = json.loads(cli(self.repo, "roadmap", "promote", "--item", item["id"], "--intent", "I-0001", "--json", expect=0).stdout)
        self.assertEqual(result["item"]["status"], "next")
        self.assertEqual(result["task"]["roadmap"], item["id"])
        self.assertEqual(result["task"]["paths"], ["src/core"])
        self.assertEqual(result["task"]["state"], "proposed")

    def test_only_one_next_item_at_a_time(self):
        first = self.add(title="first")
        second = self.add(title="second", scope=("src",))
        cli(self.repo, "roadmap", "promote", "--item", first["id"], expect=0)
        proc = cli(self.repo, "roadmap", "promote", "--item", second["id"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("still next", proc.stderr)


class AcceptTests(RoadmapCase):
    def test_accept_requires_a_merged_task(self):
        item = self.add(title="no work done")
        cli(self.repo, "roadmap", "promote", "--item", item["id"], expect=0)
        proc = cli(self.repo, "roadmap", "accept", "--item", item["id"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("no merged task", proc.stderr)

    def test_accept_runs_the_items_own_verdict(self):
        item = self.add(title="real work", accept='python -c "print(42)"')
        promoted = json.loads(cli(self.repo, "roadmap", "promote", "--item", item["id"], "--json", expect=0).stdout)
        work = self.merged_task(item["id"])
        self.assertNotEqual(work["id"], promoted["task"]["id"])
        result = json.loads(cli(self.repo, "roadmap", "accept", "--item", item["id"], "--json", expect=0).stdout)
        self.assertEqual(result["item"]["status"], "done")
        self.assertEqual(result["item"]["evidence"]["tasks"], [work["id"]])

    def test_failing_verdict_does_not_mark_done(self):
        item = self.add(title="broken verdict", accept='python -c "raise SystemExit(3)"')
        cli(self.repo, "roadmap", "promote", "--item", item["id"], expect=0)
        self.merged_task(item["id"])
        proc = cli(self.repo, "roadmap", "accept", "--item", item["id"])
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertEqual(self.queue()[item["id"]]["status"], "next")


class ResearchTests(RoadmapCase):
    def agent(self, body: str) -> str:
        path = self.tmp / "agent.py"
        path.write_text(body, encoding="utf-8")
        return f'"{PY}" "{path}"'

    def research(self, *args, expect=0):
        return cli(self.repo, "research", *args, "--json", expect=expect)

    def test_one_round_promotes_and_works(self):
        self.add(title="edit the app", scope=("src/core",))
        agent = self.agent(
            "import pathlib\n"
            "p = pathlib.Path('src/core/app.py')\n"
            "p.write_text(p.read_text(encoding='utf-8') + '\\n\\ndef extra():\\n    return 1\\n', encoding='utf-8')\n"
        )
        summary = json.loads(self.research("--rounds", "1", "--intent", "I-0001", "--agent-cmd", agent).stdout)
        self.assertEqual(summary["promoted"], ["R-0001"])
        self.assertEqual(summary["rounds"][0]["loop"]["review"], 1, summary)
        self.assertEqual(self.queue()["R-0001"]["status"], "next")
        state = (self.repo / "roadmap" / "STATE.md").read_text(encoding="utf-8")
        self.assertIn("## Round 1", state)
        self.assertIn("promoted: R-0001", state)

    def test_stop_file_stops_before_any_round(self):
        self.add(title="edit the app")
        cli(self.repo, "stop", expect=0)
        summary = json.loads(self.research("--rounds", "3").stdout)
        self.assertEqual(summary["rounds"], [])
        self.assertIn("stop file", summary["stopped_reason"])

    def test_round_budget_stops_the_run(self):
        self.add(title="edit the app")
        agent = self.agent("raise SystemExit(0)\n")
        summary = json.loads(self.research("--rounds", "5", "--budget", "1", "--intent", "I-0001", "--agent-cmd", agent).stdout)
        self.assertEqual(len(summary["rounds"]), 1)
        self.assertIn("round budget exhausted", summary["stopped_reason"])

    def test_previous_round_is_accepted_before_promoting_the_next(self):
        item = self.add(title="first", accept='python -c "print(1)"')
        self.add(title="second", scope=("src",))
        cli(self.repo, "roadmap", "promote", "--item", item["id"], expect=0)
        self.merged_task(item["id"])
        agent = self.agent("raise SystemExit(0)\n")
        summary = json.loads(self.research("--rounds", "1", "--intent", "I-0001", "--agent-cmd", agent).stdout)
        self.assertEqual(summary["done"], [item["id"]])
        self.assertEqual(self.queue()[item["id"]]["status"], "done")
        self.assertEqual(summary["promoted"], ["R-0002"])

    def test_no_feasible_item_asks_for_a_human(self):
        self.add(title="impossible", accept="definitely-not-a-binary", scope=("src/core",))
        summary = json.loads(self.research("--rounds", "1").stdout)
        self.assertIn("no feasible later item", summary["stopped_reason"])


if __name__ == "__main__":
    unittest.main()