"""Session report: what actually happened, in one file you can hand to a reviewer.

The ledger says what state every task is in *now*; the events table says what
happened; neither is a document. This module joins them into one Markdown file
that answers the only questions an architecture review can act on:

    what was attempted, what failed, why it failed, and what that implies.

Rules this file follows, because a report that lies is worse than no report:

* every number is counted, never estimated;
* a "signal" is only emitted when the sample supports it (min_sample), and it
  always prints the numbers it was derived from;
* what the data cannot show is listed under "gaps" instead of being guessed.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from collections import Counter
from pathlib import Path

REPORT_DIR = Path("reports")
SCHEMA = 1

# A signal is worthless on a tiny sample: say nothing rather than something wrong.
MIN_SAMPLE = 3


def _utc(ts: float) -> str:
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hm(ts: float) -> str:
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%m-%d %H:%M")


def _head(repo: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return "unknown"
    return out.stdout.strip() or "unknown"


def _share(part: int, whole: int) -> str:
    return f"{part}/{whole}" if whole else f"{part}/0"


def collect(ctx, *, since: float, until: float, limit: int = 20000) -> dict:
    """Join the ledger, the event log and the config into plain facts."""

    from . import __version__

    tasks = [task for task in ctx.ledger.list() if isinstance(task, dict)]
    events = ctx.store.events_since(since, limit=limit)
    if since <= 0:
        # `--all` means "since the first thing that happened", not 1970.
        stamps = [float(event["at"]) for event in events] or [float(until)]
        since = min(stamps)

    gate_failed: Counter = Counter()
    gate_passed: Counter = Counter()
    dead_reasons: Counter = Counter()
    dead_examples: dict[str, str] = {}
    admission: Counter = Counter()
    exits: Counter = Counter()
    durations: dict[str, list[float]] = {}
    timeline: list[dict] = []

    for event in events:
        kind = event["kind"]
        if kind not in ("lease.acquired",):
            timeline.append(
                {
                    "at": event["at"],
                    "kind": kind,
                    "task": event.get("task_id") or "-",
                    "actor": event.get("actor") or "-",
                    "detail": _detail(kind, event.get("payload") or {}),
                }
            )
        if kind == "gate.failed":
            for step in event["payload"].get("failed") or ["(unknown step)"]:
                gate_failed[f"{event['payload'].get('stage', '?')}:{step}"] += 1
        elif kind == "gate.passed":
            gate_passed[str(event["payload"].get("stage", "?"))] += 1
        elif kind == "admission.refused":
            admission[str(event["payload"].get("reason", "unknown"))] += 1
        elif kind == "agent.finished":
            payload = event["payload"]
            if payload.get("timed_out"):
                exits["timeout"] += 1
            else:
                exits[f"exit {payload.get('exit')}"] += 1
            seconds = payload.get("seconds")
            if isinstance(seconds, (int, float)):
                durations.setdefault(str(event.get("actor") or "-"), []).append(float(seconds))

    for task in tasks:
        for dead in task.get("dead") or []:
            reason = str(dead.get("reason", "unknown"))
            if float(dead.get("at_ts") or 0) and float(dead.get("at_ts")) < since:
                continue
            dead_reasons[reason] += 1
            dead_examples.setdefault(reason, str(dead.get("note", ""))[:80])

    config = {}
    if ctx.config:
        config = {
            "wip_limit": ctx.config.wip_limit(),
            "per_holder_limit": ctx.config.per_holder_limit(),
            "lease_ttl_seconds": ctx.config.lease_ttl("write"),
            "resume_grace_minutes": ctx.config.resume_grace_minutes(),
            "cooldown_minutes": ctx.config.cooldown_minutes(),
            "impl_gates": ctx.config.gate_commands("impl"),
            "merge_gates": ctx.config.gate_commands("merge"),
        }

    return {
        "schema": SCHEMA,
        "version": __version__,
        "repo": ctx.repo.name,
        "head": _head(ctx.repo),
        "generated_at": until,
        "since": since,
        "until": until,
        "hours": round((until - since) / 3600.0, 2),
        "config": config,
        "counts": {
            "tasks": len(tasks),
            "events": len(events),
            "gate_passed": sum(gate_passed.values()),
            "gate_failed": sum(gate_failed.values()),
            "agents": sum(exits.values()),
            "admission_refused": sum(admission.values()),
        },
        "gate_failed": dict(gate_failed.most_common()),
        "gate_passed": dict(gate_passed.most_common()),
        "dead": dict(dead_reasons.most_common()),
        "dead_examples": dead_examples,
        "admission": dict(admission.most_common()),
        "exits": dict(exits.most_common()),
        "durations": {who: sorted(values) for who, values in durations.items()},
        "tasks": [_task_row(task) for task in tasks],
        "timeline": timeline[-400:],
        "gaps": _gaps(events, tasks),
    }


def _detail(kind: str, payload: dict) -> str:
    if kind == "gate.failed":
        return f"{payload.get('stage', '?')} failed: " + ", ".join(payload.get("failed") or [])
    if kind == "admission.refused":
        return str(payload.get("reason", ""))
    if kind == "agent.finished":
        if payload.get("timed_out"):
            return "timed out"
        return f"exit {payload.get('exit')} in {payload.get('seconds', '?')}s"
    if kind in ("lease.acquired", "lease.released"):
        paths = payload.get("paths") or []
        return ", ".join(paths) if paths else str(payload.get("reason", ""))
    if kind == "review.applied":
        return str(payload.get("verdict", ""))
    if kind == "roadmap.promoted":
        return str(payload.get("item", ""))
    return ""


def _task_row(task: dict) -> dict:
    attempt = task.get("attempt") or {}
    history = task.get("history") or []
    last = history[-1] if history else {}
    return {
        "id": task.get("id"),
        "state": task.get("state"),
        "domain": task.get("domain"),
        "attempts": f"{attempt.get('used', 0)}/{attempt.get('max', 0)}",
        "dead": len(task.get("dead") or []),
        "paths": task.get("paths") or [],
        "title": task.get("title") or "",
        "last": f"{last.get('to', '?')}: {last.get('note', '')}".strip(": "),
    }


def signals(facts: dict) -> list[str]:
    """Only say what the counters support; each line carries its own numbers."""

    out: list[str] = []
    counts = facts["counts"]
    rounds = counts["agents"]

    if counts["gate_failed"] and counts["gate_failed"] + counts["gate_passed"] >= MIN_SAMPLE:
        total = counts["gate_failed"] + counts["gate_passed"]
        if counts["gate_failed"] * 2 >= total:
            top = next(iter(facts["gate_failed"].items()), ("?", 0))
            out.append(
                f"门禁失败 {_share(counts['gate_failed'], total)} 次，高于通过率；"
                f"最高频失败步骤是 `{top[0]}`（{top[1]} 次）。先修门禁/契约，再加并发。"
            )
    if facts["dead"]:
        top_reason, top_count = next(iter(facts["dead"].items()))
        if top_count >= 2:
            out.append(
                f"死路原因 `{top_reason}` 出现 {top_count} 次"
                f"（示例：{facts['dead_examples'].get(top_reason, '')}）。"
                "同一原因重复出现，说明问题在任务契约或 brief，不在 agent 的能力。"
            )
    if facts["admission"]:
        top_reason, top_count = next(iter(facts["admission"].items()))
        out.append(
            f"准入拒绝共 {counts['admission_refused']} 次，其中 `{top_reason}` {top_count} 次。"
            "这是并发度设定的直接反馈：拒绝多说明并行度超过了可切分的任务数。"
        )
    if facts["exits"].get("timeout"):
        out.append(
            f"agent 超时 {facts['exits']['timeout']} 次；`--timeout` 对当前模型可能偏小。"
        )
    slowest = None
    for who, values in facts["durations"].items():
        if len(values) >= 2:
            median = values[len(values) // 2]
            if slowest is None or median > slowest[1]:
                slowest = (who, median)
    if slowest:
        out.append(
            f"最慢 holder `{slowest[0]}` 的单轮中位耗时 {slowest[1]:.0f}s"
            f"（样本 {len(facts['durations'][slowest[0]])} 轮）。轮次墙钟时间由它决定。"
        )
    if rounds and facts["durations"]:
        out.append(
            f"本窗口共 {rounds} 次 agent 调用、{counts['gate_passed']} 次过门、"
            f"{counts['gate_failed']} 次门禁失败。"
        )
    return out


def _gaps(events: list[dict], tasks: list[dict]) -> list[str]:
    gaps = [
        "没有 token / 费用计数：预算按「轮」计，不按钱计。",
        "没有 diff 内容：报告只记路径与统计，判断代码质量仍需看 worktree。",
        "单机 SQLite：多机协作下这份报告只覆盖本机那一份账簿。",
    ]
    if not events:
        gaps.append("事件表在窗口内为空：要么没跑过，要么 `.orchestra/state.db` 被清过。")
    if not any(task.get("dead") for task in tasks):
        gaps.append("没有任何 dead_approaches：失败原因分布这一节没有样本。")
    return gaps


def render(facts: dict) -> str:
    counts = facts["counts"]
    lines: list[str] = []
    add = lines.append

    add(f"# 并发会话报告 · {facts['repo']}")
    add("")
    add(f"- 生成时间（UTC）：{_utc(facts['generated_at'])}")
    add(f"- 窗口：{_utc(facts['since'])} → {_utc(facts['until'])}（{facts['hours']} 小时）")
    add(f"- orchestra {facts['version']} · 仓库 HEAD `{facts['head']}` · 报告 schema {facts['schema']}")
    add("")

    add("## 1. 当时的配置")
    add("")
    if facts["config"]:
        add("| 项 | 值 |")
        add("|---|---|")
        for key, value in facts["config"].items():
            add(f"| `{key}` | {json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value} |")
    else:
        add("（config 未加载）")
    add("")

    add("## 2. 数字")
    add("")
    add("| 指标 | 值 |")
    add("|---|---|")
    add(f"| 任务总数 | {counts['tasks']} |")
    add(f"| 事件数（窗口内） | {counts['events']} |")
    add(f"| agent 调用 | {counts['agents']} |")
    add(f"| 门禁通过 | {counts['gate_passed']} |")
    add(f"| 门禁失败 | {counts['gate_failed']} |")
    add(f"| 准入拒绝 | {counts['admission_refused']} |")
    add("")

    add("## 3. 为什么失败")
    add("")
    add("### 3.1 门禁失败步骤")
    add("")
    add(_counter_table(facts["gate_failed"], ("步骤", "次数"), empty="窗口内没有门禁失败。"))
    add("")
    add("### 3.2 死路原因（dead_approaches）")
    add("")
    add(
        _counter_table(
            facts["dead"],
            ("原因", "次数"),
            extra={k: v for k, v in facts["dead_examples"].items()},
            empty="没有记录到死路。",
        )
    )
    add("")
    add("### 3.3 准入拒绝（admission）")
    add("")
    add(_counter_table(facts["admission"], ("reason", "次数"), empty="窗口内没有被拒绝的准入请求。"))
    add("")
    add("### 3.4 agent 退出")
    add("")
    add(_counter_table(facts["exits"], ("退出", "次数"), empty="窗口内没有 agent 调用。"))
    add("")

    add("## 4. 任务账簿")
    add("")
    if facts["tasks"]:
        add("| 任务 | 状态 | 域 | 尝试 | 死路 | 路径 | 最后一条历史 |")
        add("|---|---|---|---|---|---|---|")
        for row in facts["tasks"]:
            paths = ", ".join(row["paths"]) or "-"
            add(
                f"| `{row['id']}` | {row['state']} | {row['domain']} | {row['attempts']} | "
                f"{row['dead']} | {paths} | {row['last']} |"
            )
    else:
        add("（账簿里没有任务）")
    add("")

    add("## 5. 时间线（倒序取最近 400 条）")
    add("")
    if facts["timeline"]:
        add("| 时间 | 事件 | 任务 | 施动者 | 详情 |")
        add("|---|---|---|---|---|")
        for item in reversed(facts["timeline"]):
            add(
                f"| {_hm(item['at'])} | {item['kind']} | {item['task']} | "
                f"{item['actor']} | {item['detail']} |"
            )
    else:
        add("（窗口内没有事件）")
    add("")

    add("## 6. 给架构评审的信号")
    add("")
    found = signals(facts)
    if found:
        for line in found:
            add(f"- {line}")
    else:
        add("- 样本不足，不给结论。（这是故意的：小样本上的归因比没有归因更糟。）")
    add("")

    add("## 7. 这份报告看不到的东西")
    add("")
    for gap in facts["gaps"]:
        add(f"- {gap}")
    add("")
    return "\n".join(lines)


def _counter_table(counter: dict, headers: tuple[str, str], *, extra: dict | None = None, empty: str) -> str:
    if not counter:
        return empty
    rows = ["| " + " | ".join(headers) + " | 备注 |", "|---|---|---|"]
    for key, value in counter.items():
        note = (extra or {}).get(key, "")
        rows.append(f"| `{key}` | {value} | {note} |")
    return "\n".join(rows)


def write(facts: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(facts), encoding="utf-8")
    return out
