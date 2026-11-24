"""M2: admission control. A claim either fits the envelope or is refused with a reason."""

from __future__ import annotations

import json
import unittest

from .support import RepoCase, cli


class AdmissionTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()

    def config(self, text: str) -> None:
        self.write("config/orchestra.toml", text)

    def budget(self, scope: str, ceiling: float) -> dict:
        return json.loads(
            cli(self.repo, "budget", "set", "--scope", scope, "--ceiling", str(ceiling), "--json", expect=0).stdout
        )

    def detail(self, proc) -> dict:
        return json.loads(proc.stderr[proc.stderr.index("{"):])

    def claimed(self, task: dict, holder: str = "agent-1", **kwargs):
        return cli(self.repo, "task", "claim", "--task", task["id"], "--holder", holder, "--json", **kwargs)

    # -- budget -------------------------------------------------------------
    def test_claim_charges_intent_and_lineage(self):
        task = self.make_task(paths=["src/core"])
        self.claimed(task, expect=0)
        rows = {row["scope"]: row for row in json.loads(cli(self.repo, "budget", "list", "--json").stdout)["budget"]}
        self.assertEqual(rows["intent:I-0001"]["spent"], 1.0)
        self.assertEqual(rows["lineage:lineage-a"]["spent"], 1.0)

    def test_budget_refusal_is_exit_5_and_releases_the_lease(self):
        self.budget("intent:I-0001", 1.0)
        first = self.make_task(paths=["src/core"])
        second = self.make_task(paths=["src/other"])
        self.claimed(first, expect=0)

        proc = self.claimed(second, holder="agent-2")
        self.assertEqual(proc.returncode, 5, proc.stderr)
        self.assertIn("admission refused", proc.stderr)
        detail = json.loads(proc.stderr[proc.stderr.index("{"):])
        self.assertEqual(detail["reason"], "budget_exceeded")
        self.assertEqual(detail["scope"], "intent:I-0001")
        self.assertEqual(detail["ceiling"], 1.0)

        leases = json.loads(cli(self.repo, "lease", "list", "--json").stdout)["leases"]
        self.assertEqual([lease["task"] for lease in leases], [first["id"]], "refused claim must not keep a lease")
        self.assertEqual(json.loads(cli(self.repo, "task", "show", "--task", second["id"], "--json").stdout)["state"], "proposed")

    def test_cost_zero_does_not_charge(self):
        task = self.make_task(paths=["src/core"])
        proc = cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-1", "--cost", "0", "--json", expect=0)
        self.assertEqual(json.loads(proc.stdout)["budget_charged"], [])
        self.assertEqual(json.loads(cli(self.repo, "budget", "list", "--json").stdout)["budget"], [])

    # -- WIP ----------------------------------------------------------------
    def test_wip_limit_refuses_with_reason(self):
        self.config("[scheduler]\nwip_limit = 1\n")
        first = self.make_task(paths=["src/core"])
        second = self.make_task(paths=["src/other"])
        self.claimed(first, expect=0)

        proc = self.claimed(second, holder="agent-2")
        self.assertEqual(proc.returncode, 5, proc.stderr)
        detail = json.loads(proc.stderr[proc.stderr.index("{"):])
        self.assertEqual(detail, {"reason": "wip_limit", "wip": 1, "limit": 1})
        leases = json.loads(cli(self.repo, "lease", "list", "--json").stdout)["leases"]
        self.assertEqual(len(leases), 1)


if __name__ == "__main__":
    unittest.main()