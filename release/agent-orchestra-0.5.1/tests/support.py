"""Shared fixtures: throwaway git repos, CLI invocation, ledger helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

PY = sys.executable
NOW = 1700000000
TEST_ROOT = Path(os.environ.get("PI_SCRATCH_DIR") or tempfile.gettempdir()) / "orchestra-tests"


def cli(repo, *args, now: int | None = NOW, expect: int | None = None, env_extra: dict | None = None):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    if now is not None:
        env["ORCHESTRA_NOW"] = str(now)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [PY, "-m", "orchestra.cli", "--repo", str(repo), *[str(a) for a in args]],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"expected exit {expect}, got {proc.returncode} for {args}\n"
            f"stdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return proc


def git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout


class RepoCase(unittest.TestCase):
    def setUp(self) -> None:
        TEST_ROOT.mkdir(parents=True, exist_ok=True)
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=str(TEST_ROOT)))
        self.repo = self.tmp / "repo"
        (self.repo / "src" / "core").mkdir(parents=True)
        (self.repo / "src" / "core" / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        git(self.repo, "init", "-b", "main")
        self.commit("chore: init")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- repo ---------------------------------------------------------------
    def commit(self, message: str) -> None:
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "--no-gpg-sign", "-m", message)

    def branch(self, name: str = "agent/lineage-a/T-0001") -> None:
        git(self.repo, "checkout", "-q", "-b", name)

    def write(self, relative: str, text: str) -> Path:
        path = self.repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # -- ledger -------------------------------------------------------------
    def init_orchestra(self):
        cli(self.repo, "init", expect=0)
        self.commit("chore: orchestra init")

    def intent(self, intent_id: str = "I-0001", signed: bool = True) -> str:
        self.write(
            f"ledger/intents/{intent_id}.md",
            f"---\nsigned: {'true' if signed else 'false'}\nauthor: kai\n---\n\n# intent {intent_id}\n",
        )
        return intent_id

    def make_task(self, paths=None, intent: str | None = "I-0001", **kwargs) -> dict:
        args = [
            "task",
            "new",
            "--title",
            kwargs.pop("title", "fix a bug"),
            "--role",
            kwargs.pop("role", "patch"),
            "--lineage",
            kwargs.pop("lineage", "lineage-a"),
            "--paths",
            *(paths or ["src/core"]),
        ]
        if intent:
            args += ["--intent", intent]
        for key, value in kwargs.items():
            args += [f"--{key.replace('_', '-')}", value]
        out = cli(self.repo, *args, "--json", expect=0).stdout
        return json.loads(out)