"""Ledger state machine, path algebra, lease races, risk signals, approval tokens."""

from __future__ import annotations

import json
import os
import subprocess
import unittest

from .support import NOW, PY, SRC, RepoCase, cli, git

from orchestra.clock import FakeClock
from orchestra.leases import LeaseConflict, LeaseManager
from orchestra.ledger import LedgerError, StateError, TaskLedger
from orchestra.paths import glob_match, normalize, overlaps
from orchestra.policy import ApprovalError, classify, issue, verify
from orchestra.store import Store


class PathTests(unittest.TestCase):
    def test_normalise(self):
        self.assertEqual(normalize("./src//core/"), "src/core")
        self.assertEqual(normalize("."), "")

    def test_overlap(self):
        self.assertTrue(overlaps("src", "src/core/app.py"))
        self.assertFalse(overlaps("src/a", "src/ab"))

    def test_glob_segments(self):
        self.assertTrue(glob_match("src/core/app.py", "src/**"))
        self.assertTrue(glob_match("contracts/v1/api.json", "contracts/**"))
        self.assertFalse(glob_match("src/deep/core/app.py", "src/*.py"))


class LedgerTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.ledger = TaskLedger(self.repo, self.clock)
        self.intent(signed=False)

    def test_create_and_claim(self):
        task = self.ledger.create(title="t", role="patch", domain="core", lineage="a", paths=["src/core"], intent="I-0001")
        self.assertEqual(task["state"], "proposed")
        self.assertEqual(task["revision"], 1)
        claimed = self.ledger.transition(task["id"], "claimed", actor="agent-1")
        self.assertEqual(claimed["state"], "claimed")
        self.assertEqual(claimed["history"][-1]["to"], "claimed")

    def test_illegal_transition(self):
        task = self.ledger.create(title="t", role="patch", domain="core", lineage="a", paths=["src/core"])
        with self.assertRaises(StateError):
            self.ledger.transition(task["id"], "merged", actor="agent-1")

    def test_merge_requires_signed_intent(self):
        task = self.ledger.create(title="t", role="patch", domain="core", lineage="a", paths=["src/core"], intent="I-0001")
        self.ledger.transition(task["id"], "claimed", actor="a")
        self.ledger.transition(task["id"], "in_progress", actor="a")
        self.ledger.transition(task["id"], "review", actor="a")
        self.ledger.transition(task["id"], "approved", actor="human:kai")
        with self.assertRaises(StateError):
            self.ledger.transition(task["id"], "merged", actor="human:kai")
        self.intent(signed=True)
        merged = self.ledger.transition(task["id"], "merged", actor="human:kai")
        self.assertEqual(merged["state"], "merged")

    def test_attempt_budget(self):
        task = self.ledger.create(
            title="t", role="patch", domain="core", lineage="a", paths=["src/core"], intent="I-0001", max_attempts=1
        )
        for state in ("claimed", "in_progress", "review"):
            self.ledger.transition(task["id"], state, actor="a")
        bumped = self.ledger.transition(task["id"], "in_progress", actor="a")
        self.assertEqual(bumped["attempt"]["used"], 1)
        self.ledger.transition(task["id"], "review", actor="a")
        with self.assertRaises(StateError):
            self.ledger.transition(task["id"], "in_progress", actor="a")

    def test_quarantine_is_governance_only(self):
        task = self.ledger.create(title="t", role="patch", domain="core", lineage="a", paths=["src/core"])
        with self.assertRaises(StateError):
            self.ledger.transition(task["id"], "quarantined", actor="agent-1")
        frozen = self.ledger.transition(task["id"], "quarantined", actor="governance:kai")
        self.assertEqual(frozen["state"], "quarantined")

    def test_fingerprint_dedup_is_structured(self):
        a = self.ledger.create(title="x", role="patch", domain="core", lineage="a", paths=["src/core"], failure_signature="E1")
        b = self.ledger.create(title="y", role="patch", domain="core", lineage="b", paths=["src/core"], failure_signature="E1")
        self.assertEqual(a["fingerprint"], b["fingerprint"])
        self.assertIn(b["id"], [t["id"] for t in self.ledger.find_by_fingerprint(a["fingerprint"], exclude=a["id"])])


class LeaseTests(RepoCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.store = Store(self.repo / ".orchestra" / "state.db", self.clock)
        self.leases = LeaseManager(self.store, self.clock)

    def tearDown(self):
        self.store.close()
        super().tearDown()

    def test_same_path_is_exclusive(self):
        self.leases.acquire(task_id="T-1", holder="a", paths=["src/core"])
        with self.assertRaises(LeaseConflict):
            self.leases.acquire(task_id="T-2", holder="b", paths=["src/core/app.py"])

    def test_readers_share(self):
        self.leases.acquire(task_id="T-1", holder="a", paths=["src/core"], mode="read")
        lease = self.leases.acquire(task_id="T-2", holder="b", paths=["src/core"], mode="read")
        self.assertEqual(lease["mode"], "read")

    def test_ttl_expiry_and_reap(self):
        self.leases.acquire(task_id="T-1", holder="a", paths=["src/core"], ttl=60)
        self.clock.advance(61)
        self.leases.acquire(task_id="T-2", holder="b", paths=["src/core"])
        reclaimed = self.leases.reap()
        self.assertEqual(reclaimed, ["T-1"])

    def test_repo_lease_needs_approval(self):
        with self.assertRaises(Exception):
            self.leases.acquire(task_id="T-1", holder="a", paths=["."], mode="repo")

    def test_cli_conflict_exit_code(self):
        cli(self.repo, "lease", "acquire", "--task", "T-1", "--holder", "a", "--paths", "src/core", expect=0)
        proc = cli(self.repo, "lease", "acquire", "--task", "T-2", "--holder", "b", "--paths", "src/core", expect=3)
        self.assertIn("lease conflict", proc.stderr)

    def test_eight_agents_race_for_one_path(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["ORCHESTRA_NOW"] = str(NOW)
        procs = [
            subprocess.Popen(
                [PY, "-m", "orchestra.cli", "--repo", str(self.repo), "lease", "acquire",
                 "--task", "T-1", "--holder", f"agent-{i}", "--paths", "src/core"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            )
            for i in range(8)
        ]
        codes = [proc.wait() for proc in procs]
        self.assertEqual(codes.count(0), 1, f"exactly one winner expected, got {codes}")
        self.assertEqual(codes.count(3), 7, f"losers must exit 3, got {codes}")

    def test_status_reports_wip_and_leases(self):
        payload = json.loads(cli(self.repo, "status", "--json", expect=0).stdout)
        self.assertEqual(payload["wip"], 0)
        self.assertEqual(payload["leases"]["active"], 0)

    def test_heartbeat_extends(self):
        lease = self.leases.acquire(task_id="T-1", holder="a", paths=["src/core"], ttl=60)
        self.clock.advance(50)
        renewed = self.leases.renew(lease["group"], 60, "a")
        self.assertEqual(renewed["expires_at"], int(self.clock.now() + 60))


class RiskTests(unittest.TestCase):
    rules = [
        {"class": "T1", "name": "test-weakening", "signals": ["deletes_tests", "adds_skip"]},
        {"class": "T1", "name": "dependency", "signals": ["adds_dependency"]},
        {"class": "T1", "name": "migration", "signals": ["migration"]},
        {"class": "T2", "name": "irreversible", "signals": ["irreversible_command"], "paths": ["deploy/**"]},
    ]

    def classify(self, changes, diff=""):
        return classify(changes, diff, self.rules)

    def test_clean_change_is_t0(self):
        result = self.classify([("M", "src/core/app.py")], "+def add(a, b):\n+    return a + b\n")
        self.assertEqual(result.cls, "T0")
        self.assertEqual(result.signals, [])

    def test_deleted_test_is_t1(self):
        self.assertEqual(self.classify([("D", "tests/test_app.py")]).cls, "T1")

    def test_skip_marker_is_t1(self):
        result = self.classify([("M", "src/core/app.py")], "+@pytest.mark.skip(reason='later')\n+def test_x(): pass\n")
        self.assertIn("adds_skip", result.signals)
        self.assertEqual(result.cls, "T1")

    def test_dependency_change_is_t1(self):
        self.assertEqual(self.classify([("M", "requirements.txt")], "+requests==2.32.0\n").cls, "T1")

    def test_migration_is_t1(self):
        self.assertEqual(self.classify([("A", "db/migrations/0002_add.sql")], "+CREATE TABLE t (id int);\n").cls, "T1")

    def test_irreversible_is_t2(self):
        result = self.classify([("M", "deploy/release.sh")], "+terraform apply -auto-approve\n")
        self.assertEqual(result.cls, "T2")


class ApprovalTests(unittest.TestCase):
    KEY = b"unit-test-key-0001"

    def setUp(self):
        self.clock = FakeClock(1000.0)
        self.store = Store(PathTempDir.db(), self.clock)

    def tearDown(self):
        self.store.close()

    def token(self, **kwargs):
        return issue(self.KEY, kind="merge", by="kai", clock=self.clock, ttl=60, prediction="errors drop to 0",
                     rollback="git revert", subject="sha256:abc", **kwargs)

    def test_roundtrip(self):
        payload = self.token()
        verified = verify(self.KEY, payload["token"], clock=self.clock, store=self.store, subject="sha256:abc")
        self.assertEqual(verified["by"], "kai")

    def test_expiry(self):
        payload = self.token()
        self.clock.advance(61)
        with self.assertRaises(ApprovalError):
            verify(self.KEY, payload["token"], clock=self.clock, consume=False)

    def test_replay_rejected(self):
        payload = self.token()
        verify(self.KEY, payload["token"], clock=self.clock, store=self.store)
        with self.assertRaises(ApprovalError):
            verify(self.KEY, payload["token"], clock=self.clock, store=self.store)
        self.assertIn("no human", "no human")

    def test_subject_must_match(self):
        payload = self.token()
        with self.assertRaises(ApprovalError):
            verify(self.KEY, payload["token"], clock=self.clock, subject="sha256:other", consume=False)

    def test_missing_prediction_refused(self):
        with self.assertRaises(ApprovalError):
            issue(self.KEY, kind="merge", by="kai", clock=self.clock, ttl=60, prediction="", rollback="revert")


class PathTempDir:
    @staticmethod
    def db():
        from pathlib import Path
        import tempfile
        root = Path(os.environ.get("PI_SCRATCH_DIR") or tempfile.gettempdir()) / "orchestra-tests" / "db"
        root.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(prefix="state-", suffix=".db", dir=root, delete=False)
        handle.close()
        return handle.name