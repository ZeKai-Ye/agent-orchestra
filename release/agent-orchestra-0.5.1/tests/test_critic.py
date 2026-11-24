"""v0.4.2: automatic critic pairing and reviewer rotation."""

from __future__ import annotations

import json
import unittest

from .support import PY, RepoCase, cli

CONFIRM = """---
verdict: confirmed
target: {target}
reason: checked the claim against the repository
---
"""

REFUTE = """---
verdict: refuted
target: {target}
evidence: python -c "print(1)" cannot fail
reason: vacuous
---
"""


class CriticCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()

    def item(self, size="S", title="an item"):
        return json.loads(
            cli(self.repo, "roadmap", "add", "--title", title, "--accept", 'python -c "print(1)"',
                "--scope", "docs", "--size", size, "--json", expect=0).stdout
        )

    def pair(self, target, size="S", expect=0):
        return cli(self.repo, "critic", "pair", "--target", target, "--size", size, "--intent", "I-0001",
                   "--json", expect=expect)

    def verdict(self, target, body, expect=0):
        task = json.loads(self.pair(target).stdout)["task"]
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", task["lineage"], expect=0)
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "in_progress", "--actor", task["lineage"], expect=0)
        self.write(f"reviews/{task['id']}.md", body.format(target=target))
        gate = cli(self.repo, "gate", "run", "--task", task["id"], "--stage", "impl", "--json", expect=0)
        payload = json.loads(gate.stdout)
        failed = [step["name"] + ": " + step["detail"] for step in payload["steps"] if not step["ok"]]
        self.assertTrue(payload["ok"], failed)
        return task, json.loads(cli(self.repo, "review", "--task", task["id"], "--json", expect=expect).stdout)

    def streaks(self):
        payload = json.loads(cli(self.repo, "critic", "status", "--json", expect=0).stdout)
        return {row["holder"]: row["confirm_streak"] for row in payload["pool"]}

    def config(self, text: str) -> None:
        self.write("config/orchestra.toml", text)
        self.commit("test: config")


class PairingTests(CriticCase):
    def test_pair_creates_a_critic_task(self):
        item = self.item()
        payload = json.loads(self.pair(item["id"]).stdout)
        self.assertTrue(payload["paired"])
        task = payload["task"]
        self.assertEqual(task["role"], "critic")
        self.assertEqual(task["target"], item["id"])
        self.assertEqual(task["paths"], ["reviews"])
        self.assertEqual(task["lineage"], "critic-a")

    def test_size_can_opt_out(self):
        self.config("[critic]\nrequired_sizes = [\"M\"]\n")
        item = self.item(size="S")
        payload = json.loads(self.pair(item["id"], expect=1).stdout)
        self.assertFalse(payload["paired"])
        self.assertIn("does not require", payload["reason"])

    def test_invalid_target_is_refused(self):
        proc = cli(self.repo, "critic", "pair", "--target", "X-1")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("must be T-0000 or R-0000", proc.stderr)


class RotationTests(CriticCase):
    def test_two_confirms_rotate_the_reviewer(self):
        item = self.item()
        self.verdict(item["id"], CONFIRM)
        self.verdict(item["id"], CONFIRM)
        self.assertEqual(self.streaks()["critic-a"], 2)
        payload = json.loads(self.pair(item["id"]).stdout)
        self.assertEqual(payload["holder"], "critic-b")
        self.assertFalse(payload["rotated"])

    def test_refutation_resets_the_streak(self):
        item = self.item()
        self.verdict(item["id"], CONFIRM)
        self.verdict(item["id"], REFUTE)
        self.assertEqual(self.streaks()["critic-a"], 0)

    def test_saturated_pool_still_pairs_but_flags_rotation(self):
        self.config("[critic]\npool = [\"only-one\"]\nmax_confirm_streak = 1\n")
        item = self.item()
        self.verdict(item["id"], CONFIRM)
        payload = json.loads(self.pair(item["id"]).stdout)
        self.assertTrue(payload["rotated"])
        self.assertEqual(payload["holder"], "only-one")


class AutoPairingTests(CriticCase):
    def test_research_round_pairs_a_critic_for_the_worked_task(self):
        cli(self.repo, "roadmap", "add", "--title", "edit the app", "--accept", 'python -c "print(1)"',
            "--scope", "src/core", "--size", "S", expect=0)
        path = self.tmp / "agent.py"
        path.write_text(
            "import pathlib\n"
            "p = pathlib.Path('src/core/app.py')\n"
            "p.write_text(p.read_text(encoding='utf-8') + '\\n\\ndef extra():\\n    return 1\\n', encoding='utf-8')\n",
            encoding="utf-8",
        )
        summary = json.loads(
            cli(self.repo, "research", "--rounds", "1", "--intent", "I-0001",
                "--agent-cmd", f'"{PY}" "{path}"', "--json", expect=0).stdout
        )
        self.assertEqual(summary["rounds"][0]["loop"]["review"], 1, summary)
        critic = summary["rounds"][0]["critic"]
        self.assertTrue(critic["paired"], summary)
        self.assertTrue(critic["task"]["target"].startswith("T-"))
        tasks = json.loads(cli(self.repo, "task", "list", "--json").stdout)["tasks"]
        self.assertTrue(any(task["role"] == "critic" for task in tasks))

if __name__ == "__main__":
    unittest.main()