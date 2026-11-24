"""M2: a killed agent must not block a path, and concurrent writers must not lose updates."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest

from .support import NOW, PY, SRC, RepoCase, cli, git

from orchestra.clock import FakeClock
from orchestra.fslock import LockTimeout, advisory_lock

from pathlib import Path


class KillTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.intent()
        self.task = self.make_task(paths=["src/core"])

    def spawn_holder(self) -> subprocess.Popen:
        """An agent that claims the task and then hangs, like a real worker mid-task."""

        code = (
            "import sys, time\n"
            "from orchestra.cli import main\n"
            f"rc = main(['--repo', {str(self.repo)!r}, 'task', 'claim', '--task', {self.task['id']!r},"
            " '--holder', 'agent-killed'])\n"
            "sys.stdout.flush()\n"
            "time.sleep(300)\n"
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["ORCHESTRA_NOW"] = str(NOW)
        return subprocess.Popen([PY, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_killed_holder_is_reaped_after_ttl(self):
        proc = self.spawn_holder()
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            state = json.loads(cli(self.repo, "task", "show", "--task", self.task["id"], "--json", expect=0).stdout)["state"]
            if state == "claimed":
                break
            time.sleep(0.2)
        self.assertEqual(state, "claimed", "the holder never claimed the task")
        proc.kill()
        proc.wait(timeout=30)

        # TTL is the only thing that frees the path now: nothing released the lease.
        leases = json.loads(cli(self.repo, "lease", "list", "--json").stdout)["leases"]
        self.assertEqual(len(leases), 1, "the dead agent still holds the lease (expected before reaping)")

        later = NOW + 4000
        reaped = json.loads(cli(self.repo, "lease", "reap", "--json", now=later, expect=0).stdout)["reclaimed"]
        self.assertEqual(reaped, [self.task["id"]])
        self.assertEqual(json.loads(cli(self.repo, "lease", "list", "--json", now=later).stdout)["leases"], [])
        self.assertEqual(
            json.loads(cli(self.repo, "task", "show", "--task", self.task["id"], "--json", now=later).stdout)["state"],
            "proposed",
        )
        # A different agent can now take the same path.
        cli(self.repo, "task", "claim", "--task", self.task["id"], "--holder", "agent-2", now=later, expect=0)


class ConcurrencyTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()
        self.task = self.make_task(paths=["src/core"])

    def test_concurrent_attempts_do_not_lose_updates(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["ORCHESTRA_NOW"] = str(NOW)
        workers = 6
        procs = [
            subprocess.Popen(
                [PY, "-m", "orchestra.cli", "--repo", str(self.repo), "task", "attempt",
                 "--task", self.task["id"], "--actor", f"agent-{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            for i in range(workers)
        ]
        codes = [proc.wait() for proc in procs]
        self.assertEqual(codes, [0] * workers)
        task = json.loads(cli(self.repo, "task", "show", "--task", self.task["id"], "--json").stdout)
        self.assertEqual(task["attempt"]["used"], workers, "a lost update means the task lock is not working")


class LockTests(RepoCase):
    def test_second_acquire_times_out(self):
        path = self.tmp / "lock" / "one.lock"
        with advisory_lock(path):
            with self.assertRaises(LockTimeout):
                with advisory_lock(path, timeout=0.15, interval=0.01):
                    pass

    def test_lock_is_released_after_the_block(self):
        path = self.tmp / "lock" / "two.lock"
        with advisory_lock(path):
            pass
        with advisory_lock(path, timeout=1.0):
            pass


class TrendTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.init_orchestra()

    def test_status_append_and_trend(self):
        cli(self.repo, "status", "--append", "--json", expect=0)
        self.make_task(paths=["src/core"])
        cli(self.repo, "status", "--append", "--json", now=NOW + 3600, expect=0)
        payload = json.loads(cli(self.repo, "trend", "--json", expect=0).stdout)
        self.assertEqual(payload["snapshots"], 2)
        self.assertEqual(payload["hours"], 1.0)
        deltas = {item["metric"]: item["delta"] for item in payload["series"]}
        self.assertEqual(deltas["tasks.total"], 1)
        self.assertEqual(deltas["wip"], 0)

    def test_trend_without_snapshots_is_refused(self):
        proc = cli(self.repo, "trend")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no metric snapshots", proc.stderr)


if __name__ == "__main__":
    unittest.main()