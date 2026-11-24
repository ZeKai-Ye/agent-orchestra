"""v0.5.1: an agent that loses its connection can resume; a dead one releases the task."""

from __future__ import annotations

import json
import unittest

from .support import NOW, RepoCase, cli


class DisconnectCase(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()

    def config(self, text: str) -> None:
        self.write("config/orchestra.toml", text)
        self.commit("test: config")

    def claimed(self, paths=("src/core",), holder="agent-1", **kwargs) -> dict:
        task = self.make_task(paths=list(paths), **kwargs)
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", holder, expect=0)
        cli(self.repo, "task", "transition", "--task", task["id"], "--to", "in_progress", "--actor", holder, expect=0)
        return task

    def state(self, task_id: str, now=None) -> dict:
        return json.loads(cli(self.repo, "task", "show", "--task", task_id, "--json", now=now).stdout)

    def reap(self, now, expect=0):
        return cli(self.repo, "lease", "reap", "--json", now=now, expect=expect)

    def status(self, task_id, holder, now=None):
        return json.loads(cli(self.repo, "task", "status", "--task", task_id, "--holder", holder,
                              "--json", now=now, expect=0).stdout)


class SuspendTests(DisconnectCase):
    def test_reap_suspends_instead_of_requeueing(self):
        task = self.claimed()
        self.reap(NOW + 4000)
        after = self.state(task["id"], now=NOW + 4000)
        self.assertEqual(after["state"], "suspended")
        self.assertEqual(after["suspension"]["holder"], "agent-1")
        self.assertGreater(after["suspension"]["until"], NOW + 4000)
        self.assertIsNone(after["lease"])

    def test_same_holder_resumes_inside_the_grace(self):
        task = self.claimed()
        self.reap(NOW + 4000)
        resumed = json.loads(
            cli(self.repo, "task", "resume", "--task", task["id"], "--holder", "agent-1",
                "--json", now=NOW + 4000, expect=0).stdout
        )
        self.assertEqual(resumed["state"], "claimed")
        self.assertEqual(resumed["lease"]["holder"], "agent-1")
        after = self.state(task["id"], now=NOW + 4000)
        self.assertEqual(after["attempt"]["used"], 0, "a disconnect must not cost an attempt")

    def test_another_holder_cannot_resume(self):
        task = self.claimed()
        self.reap(NOW + 4000)
        proc = cli(self.repo, "task", "resume", "--task", task["id"], "--holder", "agent-2", now=NOW + 4000)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("suspended for agent-1", proc.stderr)

    def test_claim_is_refused_while_suspended(self):
        task = self.claimed()
        self.reap(NOW + 4000)
        proc = cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-2", now=NOW + 4000)
        self.assertEqual(proc.returncode, 5, proc.stderr)
        detail = json.loads(proc.stderr[proc.stderr.index("{"):])
        self.assertEqual(detail["reason"], "suspended")
        self.assertEqual(detail["holder"], "agent-1")

    def test_after_the_grace_the_task_is_reassignable(self):
        self.config("[scheduler]\nresume_grace_minutes = 10\n")
        task = self.claimed()
        self.reap(NOW + 4000)
        later = NOW + 4000 + 11 * 60
        self.reap(later)
        after = self.state(task["id"], now=later)
        self.assertEqual(after["state"], "proposed")
        self.assertIn("grace expired", after["history"][-1]["note"])
        cli(self.repo, "task", "claim", "--task", task["id"], "--holder", "agent-2", now=later, expect=0)

    def test_unrecoverable_task_is_abandoned(self):
        task = self.claimed(max_attempts="1")
        cli(self.repo, "task", "attempt", "--task", task["id"], "--actor", "agent-1", expect=0)
        self.reap(NOW + 4000)
        after = self.state(task["id"], now=NOW + 4000)
        self.assertEqual(after["state"], "abandoned")
        self.assertIn("unrecoverable", after["history"][-1]["note"])


class StatusTests(DisconnectCase):
    def test_status_tells_a_reconnecting_agent_what_happened(self):
        task = self.claimed()
        self.assertEqual(self.status(task["id"], "agent-1")["verdict"], "yours")
        self.reap(NOW + 4000)
        self.assertEqual(self.status(task["id"], "agent-1", now=NOW + 4000)["verdict"], "suspended-for-you")
        self.assertEqual(self.status(task["id"], "agent-2", now=NOW + 4000)["verdict"], "suspended-for-other")
        cli(self.repo, "task", "resume", "--task", task["id"], "--holder", "agent-1", now=NOW + 4000, expect=0)
        self.assertEqual(self.status(task["id"], "agent-2", now=NOW + 4000)["verdict"], "held-by:agent-1")


if __name__ == "__main__":
    unittest.main()