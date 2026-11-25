"""v0.5.0: git-native claims - two clones race, only one wins (ssh signing required)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from .support import NOW, PY, SRC, TEST_ROOT

HAVE_SSH = shutil.which("ssh-keygen") is not None and shutil.which("git") is not None


def run(args, cwd, env_extra=None, expect=None, input_text=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    env["ORCHESTRA_NOW"] = str(NOW)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [str(a) for a in args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, input=input_text,
    )
    if expect is not None and proc.returncode != expect:
        raise AssertionError(f"expected {expect}, got {proc.returncode}: {args}\n{proc.stdout}\n{proc.stderr}")
    return proc


def cli(repo, *args, expect=None, env_extra=None, now=None):
    extra = {"ORCHESTRA_NOW": str(now)} if now is not None else None
    if env_extra:
        extra = {**(extra or {}), **env_extra}
    return run([PY, "-m", "orchestra.cli", "--repo", repo, *args], cwd=repo, expect=expect, env_extra=extra)


@unittest.skipUnless(HAVE_SSH, "needs ssh-keygen and git")
class JoinTests(unittest.TestCase):
    def setUp(self):
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.base = Path(tempfile.mkdtemp(prefix="join-", dir=str(TEST_ROOT)))
        self.origin = self.base / "origin.git"
        self.git(self.base, "init", "-q", "--bare", str(self.origin))
        self.alice = self.base / "alice"
        self.git(self.base, "clone", "-q", str(self.origin), str(self.alice))
        self.git(self.alice, "checkout", "-q", "-b", "main")
        self.git(self.alice, "config", "user.name", "alice")
        self.git(self.alice, "config", "user.email", "alice@example.com")
        (self.alice / "src" / "core").mkdir(parents=True)
        (self.alice / "src" / "core" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        cli(self.alice, "init", expect=0)
        (self.alice / "ledger" / "intents").mkdir(parents=True, exist_ok=True)
        (self.alice / "ledger" / "intents" / "I-0001.md").write_text("---\nsigned: true\n---\n", encoding="utf-8")
        self.task = json.loads(
            cli(self.alice, "task", "new", "--title", "fix the app", "--paths", "src/core",
                "--intent", "I-0001", "--acceptance", "A1|judge|the app works|", "--json", expect=0).stdout
        )
        self.alice_key = self.keypair("alice")
        self.bob_key = self.keypair("bob")
        cli(self.alice, "handle", "add", "--handle", "alice", "--key", str(self.alice_key) + ".pub", expect=0)
        self.git(self.alice, "add", "-A")
        self.git(self.alice, "commit", "-q", "-m", "chore: seed")
        self.git(self.alice, "push", "-q", "-u", "origin", "main")

        self.bob = self.base / "bob"
        self.git(self.base, "clone", "-q", "-b", "main", str(self.origin), str(self.bob))
        self.git(self.bob, "config", "user.name", "bob")
        self.git(self.bob, "config", "user.email", "bob@example.com")
        cli(self.bob, "handle", "add", "--handle", "bob", "--key", str(self.bob_key) + ".pub", expect=0)
        self.git(self.bob, "add", "-A")
        self.git(self.bob, "commit", "-q", "-m", "chore: register bob")
        self.git(self.bob, "push", "-q")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def git(self, cwd, *args) -> str:
        return run(["git", *args], cwd=cwd, expect=0).stdout

    def keypair(self, name: str) -> Path:
        key = self.base / f"{name}_key"
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", key], cwd=self.base, expect=0)
        return key

    def claim(self, repo, handle, key, task=None, expect=None, now=None):
        return cli(repo, "claim", "--task", task or self.task["id"], "--handle", handle,
                   "--sign-key", str(key), "--json", expect=expect, now=now)

    def refs(self) -> list[str]:
        return run(["git", "for-each-ref", "refs/claims", "--format=%(refname)"], cwd=self.origin,
                   expect=0).stdout.split()

    # -- tests --------------------------------------------------------------
    def test_race_has_exactly_one_winner(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["ORCHESTRA_NOW"] = str(NOW)
        procs = [
            subprocess.Popen(
                [PY, "-m", "orchestra.cli", "--repo", str(repo), "claim", "--task", self.task["id"],
                 "--handle", handle, "--sign-key", str(key), "--json"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            )
            for repo, handle, key in ((self.alice, "alice", self.alice_key), (self.bob, "bob", self.bob_key))
        ]
        codes = [proc.wait() for proc in procs]
        self.assertEqual(sorted(codes), [0, 3], codes)
        self.assertEqual(self.refs(), ["refs/claims/T-0001"])

    def test_renew_requires_the_holder(self):
        self.claim(self.alice, "alice", self.alice_key, expect=0)
        proc = cli(self.alice, "beat", "--task", self.task["id"], "--handle", "alice",
                   "--sign-key", str(self.alice_key), "--json", expect=0, now=NOW + 120)
        self.assertEqual(json.loads(proc.stdout)["renewals"], 1)
        stolen = cli(self.bob, "beat", "--task", self.task["id"], "--handle", "bob", "--sign-key", str(self.bob_key))
        self.assertEqual(stolen.returncode, 3, stolen.stderr)
        self.assertIn("held by alice", stolen.stderr)

    def test_unsigned_claim_is_refused(self):
        proc = cli(self.alice, "claim", "--task", self.task["id"], "--handle", "alice")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("must be signed", proc.stderr)

    def test_expired_claim_can_be_reaped_and_retaken(self):
        self.claim(self.alice, "alice", self.alice_key, expect=0)
        later = NOW + 5 * 3600
        reaped = json.loads(cli(self.alice, "reap", "--json", expect=0, now=later).stdout)["reaped"]
        self.assertEqual([item["task"] for item in reaped], [self.task["id"]])
        self.assertEqual(self.refs(), [])
        self.claim(self.bob, "bob", self.bob_key, expect=0, now=later)

    def test_merge_refuses_while_the_claim_is_live(self):
        cli(self.alice, "task", "claim", "--task", self.task["id"], "--holder", "alice", expect=0)
        for state in ("in_progress", "review"):
            cli(self.alice, "task", "transition", "--task", self.task["id"], "--to", state, "--actor", "alice", expect=0)
        self.claim(self.alice, "alice", self.alice_key, expect=0)
        branch = f"agent/local/{self.task['id']}"
        self.git(self.alice, "checkout", "-q", "-b", branch, "main")
        self.git(self.alice, "commit", "-q", "--allow-empty", "-m", f"work\n\nTask-Id: {self.task['id']}")
        self.git(self.alice, "checkout", "-q", "main")
        proc = cli(self.alice, "merge", "--task", self.task["id"])
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("still claimed", proc.stderr)


if __name__ == "__main__":
    unittest.main()