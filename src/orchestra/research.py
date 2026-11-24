"""Research rounds: accept the last round, triage Later, promote one, work one round.

One round is deliberately boring:

  1. accept   - every `next` item with a merged task must pass its own acceptance command
  2. triage   - cheap feasibility on every `later` item (accept runnable, scope exists, scope free)
  3. promote  - at most one feasible `later` becomes `next` and gets a task
  4. work     - exactly one supervisor loop round (the agent does the change)
  5. record   - append the round to roadmap/STATE.md

The human sets `--rounds`, `--budget` and `--hours`; the driver refuses to invent more.
See docs/11-rounds.md.
"""

from __future__ import annotations

import time
from pathlib import Path

from .loop import run_loop
from .roadmap import (
    RoadmapError,
    accept_item,
    best_candidate,
    get,
    load,
    promote_item,
    save,
    triage_all,
)
from .runner import metrics_snapshot

STATE_RELATIVE = Path("roadmap") / "STATE.md"


def _stop_requested(ctx, stop_file: Path | None) -> str | None:
    path = Path(stop_file) if stop_file else ctx.repo / ".orchestra" / "STOP"
    if not path.is_absolute():
        path = ctx.repo / path
    return f"stop file present: {path}" if path.exists() else None


def _record_round(ctx, number: int, entry: dict) -> None:
    path = ctx.repo / STATE_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    lines = []
    if fresh:
        lines += ["# Research rounds (engine-owned)", ""]
    lines += [
        f"## Round {number} - {ctx.clock.iso()}",
        "",
        f"- accepted: {', '.join(entry['accepted']) or '-'}",
        f"- promoted: {entry['promoted'] or '-'}",
        f"- worked: {entry['loop'].get('rounds_done', 0)} loop round(s), "
        f"review={entry['loop'].get('review', 0)}, failed={entry['loop'].get('failed', 0)}",
        f"- snapshot: wip={entry['snapshot']['wip']} "
        f"gates={entry['snapshot']['gates']['passed_24h']}pass/{entry['snapshot']['gates']['failed_24h']}fail",
        "",
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def run_research(
    ctx,
    *,
    rounds: int = 1,
    budget: float | None = None,
    hours: float | None = None,
    intent: str | None = None,
    agent_command: list[str] | None = None,
    env_extra: dict | None = None,
    holder: str = "research",
    stop_file: Path | None = None,
    idle_sleep: float = 0.0,
    log=print,
) -> dict:
    started = time.monotonic()
    summary: dict = {"rounds": [], "stopped_reason": None, "needs_human": [], "done": [], "promoted": []}
    if budget is not None:
        ctx.store.budget_set("research:round", float(budget))

    for number in range(1, max(1, rounds) + 1):
        stop = _stop_requested(ctx, stop_file)
        if stop:
            summary["stopped_reason"] = stop
            break
        if hours is not None and (time.monotonic() - started) > hours * 3600:
            summary["stopped_reason"] = f"time budget exhausted after {summary.get('elapsed', 0)}s"
            break
        if budget is not None:
            ok, detail = ctx.store.budget_charge([("research:round", 1.0)])
            if not ok:
                summary["stopped_reason"] = f"round budget exhausted ({detail.get('spent')}/{detail.get('ceiling')})"
                break

        entry: dict = {"round": number, "accepted": [], "promoted": None}
        try:
            # 1. accept the previous round's Done claim
            for item in load(ctx.repo):
                if item.get("status") != "next":
                    continue
                merged = [task for task in ctx.ledger.list("merged") if task.get("roadmap") == item["id"]]
                if not merged:
                    continue
                result = accept_item(ctx, item["id"])
                entry["accepted"].append(item["id"])
                summary["done"].append(item["id"])
                log(f"round {number}: accepted {item['id']} ({', '.join(result['tasks'])})")
        except RoadmapError as exc:
            summary["needs_human"].append(str(exc))
            summary["stopped_reason"] = f"acceptance needs a human: {exc}"
            break

        # 2. triage every later item, 3. promote at most one
        entry["triage"] = triage_all(ctx.repo)
        for verdict in entry["triage"]:
            items = load(ctx.repo)
            item = get(items, verdict["id"])
            item["feasibility"] = {"score": verdict["score"], "checks": verdict["checks"], "at": ctx.clock.iso()}
            save(ctx.repo, items)

        has_next = any(item.get("status") == "next" for item in load(ctx.repo))
        if not has_next:
            candidate = best_candidate(ctx.repo)
            if candidate is None:
                summary["stopped_reason"] = "no feasible later item: roadmap needs a human"
                break
            promoted = promote_item(ctx, candidate["id"], intent=intent)
            entry["promoted"] = candidate["id"]
            summary["promoted"].append(candidate["id"])
            log(f"round {number}: promoted {candidate['id']} -> task {promoted['task']['id']}")

        # 4. exactly one loop round
        entry["loop"] = run_loop(
            ctx,
            max_rounds=1,
            idle_sleep=idle_sleep,
            holder=holder,
            agent_command=agent_command,
            env_extra=env_extra,
            log=log,
        )
        entry["snapshot"] = metrics_snapshot(ctx)
        _record_round(ctx, number, entry)
        summary["rounds"].append(entry)
        summary["elapsed"] = round(time.monotonic() - started, 2)
        if entry["loop"].get("stopped"):
            summary["stopped_reason"] = entry["loop"]["stopped_reason"]
            break

    summary["elapsed"] = round(time.monotonic() - started, 2)
    return summary