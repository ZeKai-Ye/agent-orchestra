"""Supervisor loop: keep a worker busy, one round at a time.

The loop owns the retry policy; the agent owns nothing but the diff. Each round is
independently recoverable: kill the process at any point and the ledger plus the
lease TTL are enough to resume. See docs/10-loop.md and ADR 0006.
"""

from __future__ import annotations

import time
from pathlib import Path

from .gates import run_gate
from .leases import LeaseConflict
from .ledger import LedgerError
from .policy import AdmissionError
from .runner import (
    append_snapshot,
    claim_task,
    create_worktree,
    metrics_snapshot,
    reset_worktree,
    return_task,
    run_agent,
)

STOP_DEFAULT = Path(".orchestra") / "STOP"


def _stop_path(ctx, stop_file: Path | None) -> Path:
    if stop_file is not None:
        path = Path(stop_file)
        return path if path.is_absolute() else ctx.repo / path
    return ctx.repo / STOP_DEFAULT


def next_task(ctx, *, domain: str | None, holder: str) -> dict | None:
    """Oldest eligible task the holder is allowed to take right now."""

    candidates = ctx.ledger.list("proposed")
    if domain:
        candidates = [task for task in candidates if task["domain"] == domain]
    for task in candidates:
        if ctx.store.cooldown_until(holder, task["id"]) > ctx.clock.now():
            continue
        return task
    return None


def run_loop(
    ctx,
    *,
    domain: str | None = None,
    max_rounds: int = 1,
    idle_sleep: float = 5.0,
    stop_file: Path | None = None,
    agent_command: list[str] | None = None,
    env_extra: dict | None = None,
    holder: str = "loop",
    timeout: float = 1800.0,
    log=print,
) -> dict:
    stop = _stop_path(ctx, stop_file)
    summary: dict = {
        "rounds": [],
        "stopped_reason": None,
        "review": 0,
        "failed": 0,
        "idle": 0,
        "reaped": [],
    }
    rounds_done = 0
    idle_iterations = 0
    idle_cap = max(1, max_rounds * 3)

    while rounds_done < max_rounds and idle_iterations < idle_cap:
        if stop.exists():
            summary["stopped_reason"] = f"stop file present: {stop}"
            log(f"loop: {summary['stopped_reason']}")
            break

        reaped = ctx.leases.reap(ctx.ledger)
        if reaped:
            summary["reaped"].extend(reaped)
            log(f"loop: reaped {', '.join(reaped)}")

        if ctx.config:
            wip_states = ("claimed", "in_progress", "review")
            wip = sum(1 for item in ctx.ledger.list() if item["state"] in wip_states)
            if wip >= ctx.config.wip_limit():
                summary["idle"] += 1
                idle_iterations += 1
                log(f"loop: idle (WIP {wip}/{ctx.config.wip_limit()})")
                if not agent_command:
                    break
                time.sleep(idle_sleep)
                continue

        task = next_task(ctx, domain=domain, holder=holder)
        if task is None:
            summary["idle"] += 1
            idle_iterations += 1
            log("loop: idle (no eligible task)")
            if idle_iterations >= idle_cap:
                break
            time.sleep(idle_sleep)
            continue

        started = time.monotonic()
        record = {"round": rounds_done + 1, "task": task["id"], "outcome": None}
        try:
            claim = claim_task(ctx, task, holder)
        except LeaseConflict as exc:
            record["outcome"] = "lease_conflict"
            record["detail"] = str(exc)
            summary["rounds"].append(record)
            rounds_done += 1
            idle_iterations += 1
            log(f"loop: {task['id']} lease conflict, skipping")
            continue
        except AdmissionError as exc:
            record["outcome"] = "admission_refused"
            record["detail"] = exc.detail or {"message": str(exc)}
            summary["rounds"].append(record)
            summary["idle"] += 1
            idle_iterations += 1
            log(f"loop: admission refused ({exc.detail.get('reason', str(exc))}), backing off")
            time.sleep(idle_sleep)
            continue
        except LedgerError as exc:
            record["outcome"] = "refused"
            record["detail"] = str(exc)
            summary["rounds"].append(record)
            rounds_done += 1
            continue

        record["lease"] = claim["lease"]["group"]
        ctx.ledger.transition(task["id"], "in_progress", actor=holder, note="agent started")
        prepared = create_worktree(ctx, task)
        reset_worktree(Path(prepared["worktree"]))
        record["worktree"] = prepared["worktree"]

        if agent_command is None:
            record["outcome"] = "claimed_only"
            summary["rounds"].append(record)
            rounds_done += 1
            log(f"loop: {task['id']} claimed (no --agent-cmd, so no work is done)")
            break

        argv = [
            token.replace("{worktree}", prepared["worktree"])
            .replace("{brief}", prepared["brief"])
            .replace("{task}", task["id"])
            for token in agent_command
        ]
        run = run_agent(argv, worktree=Path(prepared["worktree"]), timeout=timeout, env_extra=env_extra)
        record["agent_exit"] = run["exit"]
        if run["timed_out"] or run["exit"] != 0:
            reason = "out_of_time" if run["timed_out"] else "other"
            state = return_task(ctx, task["id"], holder, reason, note=f"agent exit {run['exit']}")
            reset_worktree(Path(prepared["worktree"]))
            record["outcome"] = "returned"
            record["returned_reason"] = reason
            record["state"] = state["state"]
            summary["failed"] += 1
            rounds_done += 1
            record["seconds"] = round(time.monotonic() - started, 2)
            summary["rounds"].append(record)
            log(f"loop: {task['id']} agent failed ({reason}) -> {state['state']}")
            continue

        gate = run_gate(
            repo=ctx.repo,
            task_id=task["id"],
            stage="impl",
            ledger=ctx.ledger,
            store=ctx.store,
            clock=ctx.clock,
            config=ctx.config,
            worktree=Path(prepared["worktree"]),
        )
        failed = [step["name"] for step in gate["steps"] if not step["ok"]]
        record["gate_ok"] = gate["ok"]
        record["failed_steps"] = failed
        if gate["ok"]:
            moved = ctx.ledger.transition(task["id"], "review", actor=holder, note="gate passed")
            record["outcome"] = "review"
            record["state"] = moved["state"]
            summary["review"] += 1
            log(f"loop: {task['id']} gate passed -> review")
        else:
            reason = "scope_violation" if "scope" in failed else "gate_failed"
            state = return_task(ctx, task["id"], holder, reason, note="failed: " + ", ".join(failed))
            reset_worktree(Path(prepared["worktree"]))
            record["outcome"] = "returned"
            record["returned_reason"] = reason
            record["state"] = state["state"]
            summary["failed"] += 1
            log(f"loop: {task['id']} gate failed ({', '.join(failed)}) -> {state['state']}")

        rounds_done += 1
        record["seconds"] = round(time.monotonic() - started, 2)
        summary["rounds"].append(record)
        append_snapshot(ctx, metrics_snapshot(ctx))

    summary["rounds_done"] = rounds_done
    summary["stopped"] = summary["stopped_reason"] is not None
    return summary