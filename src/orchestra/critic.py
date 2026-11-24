"""Automatic critic pairing: every conclusion gets an adversarial reader.

Rules (docs/11 section 10):

* every roadmap item that enters the queue gets exactly one critic task;
* a task that reaches `review` with a roadmap item (or `role=research`) gets one;
* the critic is drawn from a small pool, and a holder whose last two verdicts were
  `confirmed` is skipped: a reviewer that keeps agreeing is rotated out, not trusted.
"""

from __future__ import annotations

from pathlib import Path

from .ledger import LedgerError

DEFAULT_POOL = ("critic-a", "critic-b")
DEFAULT_MAX_CONFIRM_STREAK = 2
DEFAULT_SIZES = ("S", "M", "L")
DEFAULT_SCOPE = ("reviews",)


def pool(config) -> list[str]:
    value = config.get("critic.pool", list(DEFAULT_POOL)) if config else list(DEFAULT_POOL)
    return [str(item) for item in value] or list(DEFAULT_POOL)


def max_confirm_streak(config) -> int:
    return int(config.get("critic.max_confirm_streak", DEFAULT_MAX_CONFIRM_STREAK)) if config else DEFAULT_MAX_CONFIRM_STREAK


def required_sizes(config) -> tuple[str, ...]:
    value = config.get("critic.required_sizes", list(DEFAULT_SIZES)) if config else list(DEFAULT_SIZES)
    return tuple(str(item).upper() for item in value)


def scope(config) -> list[str]:
    value = config.get("critic.scope", list(DEFAULT_SCOPE)) if config else list(DEFAULT_SCOPE)
    return [str(item) for item in value] or list(DEFAULT_SCOPE)


def streak(ctx, holder: str) -> int:
    return int(ctx.store.critic_streak(holder))


def pick(ctx) -> dict:
    """Least-loaded holder whose confirm streak is below the limit."""

    holders = pool(ctx.config)
    limit = max_confirm_streak(ctx.config)
    stats = {holder: ctx.store.critic_streak(holder) for holder in holders}
    eligible = [holder for holder in holders if stats[holder] < limit]
    rotated = not eligible
    if eligible:
        chosen = eligible[0]
    else:
        chosen = sorted(holders, key=lambda name: (stats.get(name, 0), holders.index(name)))[0]
    return {"holder": chosen, "rotated": rotated, "streaks": stats, "limit": limit}


def record(ctx, holder: str | None, verdict: str) -> None:
    if not holder:
        return
    ctx.store.critic_record(holder, verdict)


def pair(ctx, *, target: str, size: str = "S", intent: str | None = None, title: str | None = None) -> dict:
    """Create one critic task for a task or item target."""

    if not (target.startswith("T-") or target.startswith("R-")):
        raise LedgerError(f"critic target must be T-0000 or R-0000, got {target!r}")
    if size.upper() not in required_sizes(ctx.config):
        return {"paired": False, "reason": f"size {size} does not require a critic", "target": target}
    choice = pick(ctx)
    task = ctx.ledger.create(
        title=title or f"critic: try to refute {target}",
        role="critic",
        domain="core",
        lineage=choice["holder"],
        paths=scope(ctx.config),
        intent=intent,
        target=target,
        max_attempts=2,
    )
    ctx.store.event(
        "critic.paired",
        task_id=task["id"],
        actor="research",
        target=target,
        holder=choice["holder"],
        rotated=choice["rotated"],
    )
    return {"paired": True, "task": task, "holder": choice["holder"], "rotated": choice["rotated"],
            "streaks": choice["streaks"]}


def pair_items(ctx, items: list[dict], *, intent: str | None = None) -> list[dict]:
    """Pair one critic per freshly imported item (S/M/L all require one review)."""

    results = []
    for item in items:
        results.append(pair(ctx, target=item["id"], size=item.get("size", "S"), intent=intent, title=f"critic: {item['title']}"))
    return results


def open_critics(ctx) -> list[dict]:
    return [task for task in ctx.ledger.list() if task.get("role") == "critic" and task["state"] not in ("merged", "abandoned")]


def status(ctx) -> dict:
    holders = pool(ctx.config)
    return {
        "pool": [
            {"holder": holder, "confirm_streak": ctx.store.critic_streak(holder),
             "last_verdict": ctx.store.critic_last_verdict(holder)}
            for holder in holders
        ],
        "max_confirm_streak": max_confirm_streak(ctx.config),
        "open_critics": [{"task": task["id"], "target": task.get("target"), "state": task["state"]} for task in open_critics(ctx)],
    }