"""The session report: counts are counted, signals carry their numbers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestra import report  # noqa: E402
from .support import NOW, RepoCase, cli  # noqa: E402


class FakeCtx:
    def __init__(self, repo: Path, tasks: list[dict], events: list[dict]) -> None:
        self.repo = repo
        self.config = None
        self.ledger = self
        self.store = self
        self._tasks = tasks
        self._events = events

    def list(self):  # ledger.list()
        return self._tasks

    def events_since(self, since: float, limit: int = 20000):  # store.events_since()
        return [event for event in self._events if event["at"] >= since][:limit]


def event(kind: str, at: float, **payload) -> dict:
    return {"at": at, "kind": kind, "task_id": payload.pop("task_id", "T-0001"),
            "group_id": None, "actor": payload.pop("actor", "loop"), "payload": payload}


class ReportFactsTests(unittest.TestCase):
    def facts(self, events, tasks=None):
        ctx = FakeCtx(Path("C:/tmp/demo"), tasks or [], events)
        return report.collect(ctx, since=0.0, until=NOW)

    def test_counts_every_failure_kind(self):
        facts = self.facts(
            [
                event("gate.failed", NOW - 30, stage="impl", failed=["scope"]),
                event("gate.failed", NOW - 20, stage="impl", failed=["scope"]),
                event("gate.passed", NOW - 10, stage="impl"),
                event("admission.refused", NOW - 5, reason="wip_limit"),
                event("agent.finished", NOW - 4, exit=0, timed_out=False, seconds=31.5),
            ]
        )
        self.assertEqual(facts["counts"]["gate_failed"], 2)
        self.assertEqual(facts["counts"]["gate_passed"], 1)
        self.assertEqual(facts["gate_failed"], {"impl:scope": 2})
        self.assertEqual(facts["admission"], {"wip_limit": 1})
        self.assertEqual(facts["exits"], {"exit 0": 1})
        # `--all` must clamp to the first event, not to 1970
        self.assertEqual(facts["since"], NOW - 30)

    def test_signals_only_speak_with_numbers(self):
        facts = self.facts(
            [
                event("gate.failed", NOW - 30, stage="impl", failed=["scope"]),
                event("gate.failed", NOW - 20, stage="impl", failed=["scope"]),
                event("gate.passed", NOW - 10, stage="impl"),
            ],
            tasks=[{"id": "T-0001", "state": "proposed", "domain": "core", "paths": ["a.py"],
                    "dead": [{"reason": "scope_violation", "note": "failed: scope", "at": "x"},
                             {"reason": "scope_violation", "note": "failed: scope", "at": "x"}]}],
        )
        lines = report.signals(facts)
        self.assertTrue(any("impl:scope" in line for line in lines))
        self.assertTrue(any("scope_violation" in line and "2 次" in line for line in lines))
        text = report.render(facts)
        self.assertIn("## 7. 这份报告看不到的东西", text)
        self.assertIn("没有 token / 费用计数", text)

    def test_empty_window_says_so_instead_of_guessing(self):
        facts = self.facts([])
        self.assertEqual(report.signals(facts), [])
        text = report.render(facts)
        self.assertIn("样本不足，不给结论", text)
        self.assertTrue(any("事件表在窗口内为空" in gap for gap in facts["gaps"]))


class ReportCliTests(RepoCase):
    def test_report_writes_a_file_for_real_ledger(self):
        self.init_orchestra()
        self.intent("I-0001")
        self.make_task(["a.py"], title="report me")
        out = self.repo / "reports" / "session.md"
        cli(self.repo, "report", "--all", "--out", str(out), expect=0)
        text = out.read_text(encoding="utf-8")
        self.assertIn("# 并发会话报告", text)
        self.assertIn("T-0001", text)
        self.assertIn("## 6. 给架构评审的信号", text)

    def test_bad_since_is_a_usage_error(self):
        self.init_orchestra()
        cli(self.repo, "report", "--since", "last tuesday", expect=1)


if __name__ == "__main__":
    unittest.main()
