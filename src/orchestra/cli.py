"""Command line surface of the orchestrator.

Exit codes are part of the contract (agents and CI branch on them):
0 pass, 1 refused/failed, 2 usage, 3 lease conflict, 4 needs human approval.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

from . import __version__
from .clock import clock_from_env
from .critic import pair as pair_critic, status as critic_status
from .gates import GateError, changed_files, diff_text, git, resolve_base, run_gate
from .leases import LeaseConflict, LeaseError, LeaseManager
from .ledger import DEAD_REASONS, LedgerError, StateError, TaskLedger
from .loop import run_loop
from .research import run_research
from .policy import (
    AdmissionError,
    ApprovalError,
    PolicyError,
    classify,
    generate_key,
    issue,
    load_config,
    read_key,
    verify,
    write_default_config,
)
from .roadmap import (
    RoadmapError,
    apply_verdict,
    import_proposals,
    accept_item,
    create as roadmap_create,
    load as roadmap_load,
    promote_item,
    triage_all,
    validate as roadmap_validate,
)
from .store import Store
from .runner import append_snapshot, claim_task, create_worktree, metrics_snapshot, return_task, split_command

OK, FAIL, USAGE, CONFLICT, NEEDS_APPROVAL, ADMISSION, FAULT = 0, 1, 2, 3, 4, 5, 6


def _print(args, payload: dict, text: str | None = None) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    elif text:
        print(text)


class Ctx:
    def __init__(self, args) -> None:
        self.args = args
        self.repo = Path(args.repo or ".").resolve()
        self.clock = clock_from_env()
        self.config = load_config(self.repo) if self.repo.exists() else None
        self._store: Store | None = None
        self._ledger: TaskLedger | None = None
        self._leases: LeaseManager | None = None

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(self.repo / ".orchestra" / "state.db", self.clock)
        return self._store

    @property
    def ledger(self) -> TaskLedger:
        if self._ledger is None:
            self._ledger = TaskLedger(self.repo, self.clock)
        return self._ledger

    @property
    def leases(self) -> LeaseManager:
        if self._leases is None:
            key = None
            try:
                key = read_key(self.repo)
            except ApprovalError:
                key = None
            self._leases = LeaseManager(self.store, self.clock, self.config, key)
        return self._leases

    def close(self) -> None:
        if self._store is not None:
            self._store.close()


# --------------------------------------------------------------------------
# init / status / worktree
# --------------------------------------------------------------------------
def cmd_init(ctx: Ctx) -> int:
    repo = ctx.repo
    repo.mkdir(parents=True, exist_ok=True)
    ctx.ledger.ensure_layout()
    config = write_default_config(repo, force=ctx.args.force)
    state_dir = repo / ".orchestra"
    state_dir.mkdir(parents=True, exist_ok=True)
    ignore = state_dir / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    _print(ctx.args, {"repo": str(repo), "config": str(config)}, f"initialised {repo}\nconfig: {config}")
    return OK


def cmd_status(ctx: Ctx) -> int:
    payload = metrics_snapshot(ctx)
    payload["recent_events"] = ctx.store.recent_events(10)
    if getattr(ctx.args, "append", False) or getattr(ctx.args, "out", None):
        target = Path(ctx.args.out) if ctx.args.out else None
        compact = {key: value for key, value in payload.items() if key != "recent_events"}
        payload["snapshot"] = str(append_snapshot(ctx, compact, target))
    text = (
        f"tasks={payload['tasks']['total']} wip={payload['wip']}/{payload['wip_limit']} "
        f"leases={payload['leases']['active']} "
        f"gates={payload['gates']['passed_24h']}pass/{payload['gates']['failed_24h']}fail"
    )
    _print(ctx.args, payload, text)
    return OK

def cmd_worktree_create(ctx: Ctx) -> int:
    task = ctx.ledger.load(ctx.args.task)
    prepared = create_worktree(ctx, task)
    _print(
        ctx.args,
        {"worktree": prepared["worktree"], "branch": prepared["branch"], "brief": prepared["brief"]},
        prepared["worktree"],
    )
    return OK

# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------

def cmd_task_new(ctx: Ctx) -> int:
    args = ctx.args
    task = ctx.ledger.create(
        title=args.title,
        role=args.role,
        domain=args.domain,
        lineage=args.lineage,
        paths=args.paths,
        intent=args.intent,
        owner=args.owner,
        max_attempts=args.max_attempts,
        deps=args.dep or (),
        failure_signature=args.failure_signature,
        task_id=args.id,
        roadmap=args.roadmap,
        target=args.target,
    )
    _print(args, task, f"{task['id']}\t{task['role']}\t{','.join(task['paths'])}")
    return OK


def cmd_task_list(ctx: Ctx) -> int:
    tasks = ctx.ledger.list(ctx.args.state)
    rows = [f"{t['id']}\t{t['state']}\t{t['role']}\t{t['lineage']}\t{','.join(t['paths'])}" for t in tasks]
    _print(ctx.args, {"tasks": tasks}, "\n".join(rows) or "(no tasks)")
    return OK


def cmd_task_show(ctx: Ctx) -> int:
    task = ctx.ledger.load(ctx.args.task)
    _print(ctx.args, task, json.dumps(task, indent=2, sort_keys=True, ensure_ascii=False))
    return OK


def cmd_task_claim(ctx: Ctx) -> int:
    args = ctx.args
    task = ctx.ledger.load(args.task)
    result = claim_task(
        ctx,
        task,
        args.holder,
        mode=args.mode,
        ttl=args.ttl,
        token=args.token,
        cost=args.cost,
    )
    if result["duplicates"]:
        print(f"warning: same fingerprint already active: {', '.join(result['duplicates'])}", file=sys.stderr)
    _print(args, result, result["lease"]["group"])
    return OK

def cmd_task_transition(ctx: Ctx) -> int:
    args = ctx.args
    before = ctx.ledger.load(args.task)
    group = ((before.get("lease") or {}).get("group")) if args.to in ("merged", "abandoned") else None
    task = ctx.ledger.transition(args.task, args.to, actor=args.actor, note=args.note)
    if group:
        ctx.leases.release(group)
    _print(args, task, f"{task['id']} -> {task['state']}")
    return OK


def cmd_task_attempt(ctx: Ctx) -> int:
    args = ctx.args
    task = ctx.ledger.bump_attempt(args.task, args.actor, args.note)
    _print(args, task, f"{task['id']} attempts {task['attempt']['used']}/{task['attempt']['max']}")
    return OK


def cmd_task_heartbeat(ctx: Ctx) -> int:
    args = ctx.args
    task = ctx.ledger.load(args.task)
    lease = task.get("lease") or {}
    if not lease.get("group"):
        raise LedgerError(f"{task['id']} holds no lease")
    renewed = ctx.leases.renew(lease["group"], args.ttl, args.holder)
    lease["expires_at"] = renewed["expires_at"]
    ctx.ledger.set_lease(task["id"], lease)
    _print(args, renewed, f"{task['id']} lease {renewed['group']} until {renewed['expires_at']}")
    return OK


# --------------------------------------------------------------------------
# leases
# --------------------------------------------------------------------------
def cmd_lease_acquire(ctx: Ctx) -> int:
    args = ctx.args
    lease = ctx.leases.acquire(
        task_id=args.task,
        holder=args.holder,
        paths=args.paths or [""],
        mode=args.mode,
        ttl=args.ttl,
        token=args.token,
    )
    _print(args, lease, json.dumps(lease, sort_keys=True))
    return OK


def cmd_lease_renew(ctx: Ctx) -> int:
    args = ctx.args
    renewed = ctx.leases.renew(args.group, args.ttl, args.holder)
    _print(args, renewed, json.dumps(renewed, sort_keys=True))
    return OK


def cmd_lease_release(ctx: Ctx) -> int:
    args = ctx.args
    count = ctx.leases.release(args.group)
    _print(args, {"group": args.group, "released": count}, f"released {count} scope(s) of {args.group}")
    return OK if count else FAIL


def cmd_lease_reap(ctx: Ctx) -> int:
    args = ctx.args
    reclaimed = ctx.leases.reap(ctx.ledger)
    _print(args, {"reclaimed": reclaimed}, f"reclaimed {len(reclaimed)} task(s): {', '.join(reclaimed) or '-'}")
    return OK


def cmd_lease_list(ctx: Ctx) -> int:
    entries = ctx.leases.active()
    rows = [
        f"{e['group']}\t{e['task']}\t{e['holder']}\t{e['mode']}\t{int(e['expires_at'])}\t{','.join(e['paths'])}"
        for e in entries
    ]
    _print(ctx.args, {"leases": entries}, "\n".join(rows) or "(no active leases)")
    return OK


def cmd_lease_snapshot(ctx: Ctx) -> int:
    target = Path(ctx.args.out) if ctx.args.out else ctx.repo / "ledger" / "leases.snapshot.jsonl"
    path = ctx.leases.snapshot(target)
    _print(ctx.args, {"snapshot": str(path)}, str(path))
    return OK


# --------------------------------------------------------------------------
# risk / gate / approval
# --------------------------------------------------------------------------
def cmd_risk_classify(ctx: Ctx) -> int:
    base = resolve_base(ctx.repo, ctx.args.base)
    changes = changed_files(ctx.repo, base)
    body = diff_text(ctx.repo, base)
    result = classify(changes, body, ctx.config.risk_rules(), protected=ctx.config.protected_patterns())
    payload = result.as_dict()
    payload["base"] = base
    _print(ctx.args, payload, f"{result.cls}\t{','.join(result.signals) or '-'}\t{result.digest}")
    return OK


def cmd_gate_run(ctx: Ctx) -> int:
    args = ctx.args
    token = args.token
    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    result = run_gate(
        repo=ctx.repo,
        task_id=args.task,
        stage=args.stage,
        ledger=ctx.ledger,
        store=ctx.store,
        clock=ctx.clock,
        config=ctx.config,
        base=args.base,
        token=token,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        worktree=Path(args.worktree) if args.worktree else None,
    )
    lines = [f"{'PASS' if step['ok'] else 'FAIL'}\t{step['name']}\t{step['detail']}" for step in result["steps"]]
    _print(args, result, "\n".join(lines) + f"\n=> {'PASS' if result['ok'] else 'FAIL'} risk={result['risk'].get('class', '-')}")
    if result["ok"]:
        return OK
    return NEEDS_APPROVAL if result["requires_approval"] else FAIL


def cmd_approval_keygen(ctx: Ctx) -> int:
    path = generate_key(ctx.repo, force=ctx.args.force)
    _print(ctx.args, {"key": str(path)}, str(path))
    return OK


def cmd_approval_issue(ctx: Ctx) -> int:
    args = ctx.args
    payload = issue(
        read_key(ctx.repo),
        kind=args.kind,
        by=args.by,
        clock=ctx.clock,
        ttl=args.ttl,
        subject=args.subject,
        prediction=args.prediction,
        rollback=args.rollback,
        approval_id=args.id,
    )
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    _print(args, payload, payload["token"])
    return OK


def cmd_approval_verify(ctx: Ctx) -> int:
    args = ctx.args
    token = args.token
    if args.token_file:
        raw = Path(args.token_file).read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            raw = json.loads(raw)["token"]
        token = raw
    payload = verify(
        read_key(ctx.repo),
        token,
        clock=ctx.clock,
        store=ctx.store,
        subject=args.subject,
        kinds=(args.kind,) if args.kind else None,
    )
    _print(args, payload, f"valid: {payload['approval_id']} by {payload['by']} until {payload['expires_at']}")
    return OK


# --------------------------------------------------------------------------
# budget / trend
# --------------------------------------------------------------------------
def cmd_budget_set(ctx: Ctx) -> int:
    args = ctx.args
    ctx.store.budget_set(args.scope, args.ceiling)
    state = {row["scope"]: row for row in ctx.store.budget_state()}
    _print(args, state, f"{args.scope}\tceiling={args.ceiling}")
    return OK


def cmd_budget_list(ctx: Ctx) -> int:
    rows = ctx.store.budget_state()
    lines = []
    for row in rows:
        ceiling = "-" if row["ceiling"] is None else f"{row['ceiling']:.2f}"
        lines.append(f"{row['scope']}\t{row['spent']:.2f}/{ceiling}")
    _print(ctx.args, {"budget": rows}, "\n".join(lines) or "(no envelopes registered)")
    return OK


def _metric(row: dict, dotted: str):
    node: object = row
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def cmd_trend(ctx: Ctx) -> int:
    args = ctx.args
    target = Path(args.out) if args.out else ctx.repo / ".orchestra" / "metrics" / "snapshots.jsonl"
    if not target.exists():
        raise LedgerError(f"no metric snapshots at {target}; run `status --append` first")
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.last:
        rows = rows[-args.last :]
    if not rows:
        raise LedgerError("snapshot file is empty")
    first, last = rows[0], rows[-1]
    keys = [
        "tasks.total",
        "wip",
        "leases.active",
        "leases.expired_24h",
        "gates.passed_24h",
        "gates.failed_24h",
    ]
    series = []
    for key in keys:
        start, end = _metric(first, key), _metric(last, key)
        delta = (end - start) if isinstance(start, (int, float)) and isinstance(end, (int, float)) else None
        series.append({"metric": key, "first": start, "last": end, "delta": delta})
    hours = None
    if isinstance(first.get("at"), (int, float)) and isinstance(last.get("at"), (int, float)):
        hours = round((last["at"] - first["at"]) / 3600, 2)
    payload = {
        "snapshots": len(rows),
        "from": first.get("iso"),
        "to": last.get("iso"),
        "hours": hours,
        "series": series,
    }
    text = "\n".join(
        [f"{len(rows)} snapshots over {hours}h"] + [f"{item['metric']:<22} {item['first']} -> {item['last']}  delta={item['delta']}" for item in series]
    )
    _print(args, payload, text)
    return OK


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchestra", description="Coordination control plane for many CLI agents")
    parser.add_argument("--repo", default=None, help="managed repository (default: .)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version=f"orchestra {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        node = sub.add_parser(name, help=help_text)
        node.add_argument("--repo", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        node.set_defaults(handler=handler)
        return node

    node = add("init", cmd_init, "create ledger layout and default config")
    node.add_argument("--force", action="store_true", help="overwrite config")

    status_node = add("status", cmd_status, "metrics snapshot (WIP, leases, gates, budget)")
    status_node.add_argument("--append", action="store_true", help="append the snapshot to the metrics JSONL")
    status_node.add_argument("--out", default=None, help="JSONL path (default: .orchestra/metrics/snapshots.jsonl)")

    trend = add("trend", cmd_trend, "read the metrics JSONL and report first/last deltas")
    trend.add_argument("--last", type=int, default=50)
    trend.add_argument("--out", default=None)

    worktree = add("worktree", lambda ctx: USAGE, "worktree helpers")
    worktree_sub = worktree.add_subparsers(dest="sub", required=True)
    wt_create = worktree_sub.add_parser("create", help="git worktree + task brief for an agent")
    wt_create.add_argument("--task", required=True)
    wt_create.add_argument("--base", default=None)
    wt_create.set_defaults(handler=cmd_worktree_create)

    task = add("task", lambda ctx: USAGE, "task ledger")
    task_sub = task.add_subparsers(dest="sub", required=True)

    p = task_sub.add_parser("new", help="create a task book")
    p.add_argument("--title", required=True)
    p.add_argument("--role", default="patch")
    p.add_argument("--domain", default="core")
    p.add_argument("--lineage", default="local")
    p.add_argument("--paths", nargs="+", required=True)
    p.add_argument("--intent", default=None)
    p.add_argument("--owner", default="")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--dep", nargs="*", default=None)
    p.add_argument("--failure-signature", default="")
    p.add_argument("--roadmap", default=None)
    p.add_argument("--target", default=None, help="the task or roadmap item this task is about")
    p.add_argument("--id", default=None)
    p.set_defaults(handler=cmd_task_new)

    p = task_sub.add_parser("list", help="list task books")
    p.add_argument("--state", default=None)
    p.set_defaults(handler=cmd_task_list)

    p = task_sub.add_parser("show", help="print one task book")
    p.add_argument("--task", required=True)
    p.set_defaults(handler=cmd_task_show)

    p = task_sub.add_parser("claim", help="claim a task: lease + state transition")
    p.add_argument("--task", required=True)
    p.add_argument("--holder", required=True)
    p.add_argument("--mode", default="write", choices=["read", "write", "module", "repo"])
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--token", default=None)
    p.add_argument("--cost", type=float, default=None, help="override the cost charged to the envelopes")
    p.set_defaults(handler=cmd_task_claim)

    p = task_sub.add_parser("transition", help="move a task through the state machine")
    p.add_argument("--task", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--actor", default="cli")
    p.add_argument("--note", default="")
    p.set_defaults(handler=cmd_task_transition)

    p = task_sub.add_parser("attempt", help="record a failed attempt")
    p.add_argument("--task", required=True)
    p.add_argument("--actor", default="cli")
    p.add_argument("--note", default="")
    p.set_defaults(handler=cmd_task_attempt)

    p = task_sub.add_parser("return", help="hand a task back: dead approach, cooldown, requeue")
    p.add_argument("--task", required=True)
    p.add_argument("--holder", required=True)
    p.add_argument("--reason", required=True, choices=list(DEAD_REASONS))
    p.add_argument("--note", default="")
    p.set_defaults(handler=cmd_task_return)

    p = task_sub.add_parser("dead", help="record an approach that died, for the next claimant")
    p.add_argument("--task", required=True)
    p.add_argument("--reason", required=True, choices=list(DEAD_REASONS))
    p.add_argument("--note", default="")
    p.add_argument("--actor", default="cli")
    p.set_defaults(handler=cmd_task_dead)

    p = task_sub.add_parser("heartbeat", help="renew the lease of a claimed task")
    p.add_argument("--task", required=True)
    p.add_argument("--holder", default=None)
    p.add_argument("--ttl", type=int, default=None)
    p.set_defaults(handler=cmd_task_heartbeat)

    lease = add("lease", lambda ctx: USAGE, "path leases")
    lease_sub = lease.add_subparsers(dest="sub", required=True)

    p = lease_sub.add_parser("acquire", help="take a lease on paths")
    p.add_argument("--task", required=True)
    p.add_argument("--holder", required=True)
    p.add_argument("--paths", nargs="*", default=None)
    p.add_argument("--mode", default="write", choices=["read", "write", "module", "repo"])
    p.add_argument("--ttl", type=int, default=None)
    p.add_argument("--token", default=None)
    p.set_defaults(handler=cmd_lease_acquire)

    p = lease_sub.add_parser("renew", help="extend a lease")
    p.add_argument("--group", required=True)
    p.add_argument("--holder", default=None)
    p.add_argument("--ttl", type=int, default=None)
    p.set_defaults(handler=cmd_lease_renew)

    p = lease_sub.add_parser("release", help="release a lease group")
    p.add_argument("--group", required=True)
    p.set_defaults(handler=cmd_lease_release)

    p = lease_sub.add_parser("reap", help="expire stale leases and requeue tasks")
    p.set_defaults(handler=cmd_lease_reap)

    p = lease_sub.add_parser("list", help="list active leases")
    p.set_defaults(handler=cmd_lease_list)

    p = lease_sub.add_parser("snapshot", help="export leases for audit")
    p.add_argument("--out", default=None)
    p.set_defaults(handler=cmd_lease_snapshot)

    budget = add("budget", lambda ctx: USAGE, "cost envelopes (admission control)")
    budget_sub = budget.add_subparsers(dest="sub", required=True)

    p = budget_sub.add_parser("set", help="register or clear the ceiling of a scope")
    p.add_argument("--scope", required=True, help="intent:<id> or lineage:<name>")
    p.add_argument("--ceiling", type=float, required=True)
    p.set_defaults(handler=cmd_budget_set)

    p = budget_sub.add_parser("list", help="show envelopes and spend")
    p.set_defaults(handler=cmd_budget_list)

    risk = add("risk", lambda ctx: USAGE, "risk classification")
    risk_sub = risk.add_subparsers(dest="sub", required=True)
    p = risk_sub.add_parser("classify", help="classify the current diff")
    p.add_argument("--base", default=None)
    p.set_defaults(handler=cmd_risk_classify)

    gate = add("gate", lambda ctx: USAGE, "mechanical gates")
    gate_sub = gate.add_subparsers(dest="sub", required=True)
    p = gate_sub.add_parser("run", help="run the mechanical gate for a task")
    p.add_argument("--task", required=True)
    p.add_argument("--stage", default="impl", choices=["impl", "merge"])
    p.add_argument("--base", default=None)
    p.add_argument("--token", default=None)
    p.add_argument("--token-file", default=None)
    p.add_argument("--out-dir", default=None)
    p.add_argument("--worktree", default=None, help="diff this worktree while the ledger stays in --repo")
    p.set_defaults(handler=cmd_gate_run)

    roadmap = add("roadmap", lambda ctx: USAGE, "the machine queue behind ROADMAP.md")
    roadmap_sub = roadmap.add_subparsers(dest="sub", required=True)

    p = roadmap_sub.add_parser("import", help="turn a research task's proposals into later items")
    p.add_argument("--task", required=True)
    p.add_argument("--from", dest="source", default=None, help="read the proposal file from this tree (e.g. a worktree)")
    p.set_defaults(handler=cmd_roadmap_import)

    p = roadmap_sub.add_parser("list", help="list items")
    p.add_argument("--status", default=None, choices=["later", "next", "done", "dropped"])
    p.set_defaults(handler=cmd_roadmap_list)

    p = roadmap_sub.add_parser("check", help="validate the queue (one next item, ids, shapes)")
    p.set_defaults(handler=cmd_roadmap_check)

    p = roadmap_sub.add_parser("add", help="append an item")
    p.add_argument("--title", required=True)
    p.add_argument("--accept", required=True, help="the command that decides it is done")
    p.add_argument("--scope", nargs="+", required=True)
    p.add_argument("--size", default="S", choices=["S", "M", "L"])
    p.add_argument("--status", default="later", choices=["later", "next", "done", "dropped"])
    p.add_argument("--origin", default="human")
    p.set_defaults(handler=cmd_roadmap_add)

    p = roadmap_sub.add_parser("triage", help="cheap feasibility on every later item")
    p.set_defaults(handler=cmd_roadmap_triage)

    p = roadmap_sub.add_parser("accept", help="mark an item done (needs a merged task + its acceptance command)")
    p.add_argument("--item", required=True)
    p.set_defaults(handler=cmd_roadmap_accept)

    p = roadmap_sub.add_parser("promote", help="move one feasible later item to next and create its task")
    p.add_argument("--item", required=True)
    p.add_argument("--intent", default=None)
    p.add_argument("--role", default="patch")
    p.set_defaults(handler=cmd_roadmap_promote)

    critic = add("critic", lambda ctx: USAGE, "adversarial reviewers")
    critic_sub = critic.add_subparsers(dest="sub", required=True)

    p = critic_sub.add_parser("pair", help="create one critic task for a task or item")
    p.add_argument("--target", required=True, help="T-0000 or R-0000")
    p.add_argument("--size", default="S", choices=["S", "M", "L"])
    p.add_argument("--intent", default=None)
    p.add_argument("--title", default=None)
    p.set_defaults(handler=cmd_critic_pair)

    p = critic_sub.add_parser("status", help="pool, confirm streaks and open critic tasks")
    p.set_defaults(handler=cmd_critic_status)

    review = add("review", cmd_review, "apply a critic task's verdict (a refutation blocks a conclusion)")
    review.add_argument("--task", required=True)
    review.add_argument("--from", dest="source", default=None)

    research = add("research", cmd_research, "one round: accept, triage, promote, work (honours rounds/budget/hours)")
    research.add_argument("--rounds", type=int, default=1)
    research.add_argument("--budget", type=float, default=None, help="round budget; 1 unit per round")
    research.add_argument("--hours", type=float, default=None, help="wall-clock budget for the whole run")
    research.add_argument("--intent", default=None)
    research.add_argument("--agent-cmd", default=None)
    research.add_argument("--agent-env", nargs="*", default=None)
    research.add_argument("--holder", default="research")
    research.add_argument("--idle-sleep", type=float, default=0.0)
    research.add_argument("--stop-file", default=None)
    approval = add("approval", lambda ctx: USAGE, "human approval tokens")
    approval_sub = approval.add_subparsers(dest="sub", required=True)

    p = approval_sub.add_parser("keygen", help="create the local signing key")
    p.add_argument("--force", action="store_true")
    p.set_defaults(handler=cmd_approval_keygen)

    p = approval_sub.add_parser("issue", help="sign an approval (humans only)")
    p.add_argument("--kind", required=True, choices=["repo_lease", "merge", "irreversible"])
    p.add_argument("--by", required=True)
    p.add_argument("--ttl", type=int, default=3600)
    p.add_argument("--subject", default="")
    p.add_argument("--prediction", required=True)
    p.add_argument("--rollback", required=True)
    p.add_argument("--id", default=None)
    p.add_argument("--out", default=None)
    p.set_defaults(handler=cmd_approval_issue)

    p = approval_sub.add_parser("verify", help="verify an approval token")
    p.add_argument("--token", default=None)
    p.add_argument("--token-file", default=None)
    p.add_argument("--subject", default="")
    p.add_argument("--kind", default=None)
    p.set_defaults(handler=cmd_approval_verify)

    loop = add("loop", cmd_loop, "supervisor: claim, run an agent, gate, review, repeat")
    loop.add_argument("--domain", default=None)
    loop.add_argument("--max-rounds", type=int, default=1)
    loop.add_argument("--once", action="store_true", help="one round and exit (cron-friendly)")
    loop.add_argument("--idle-sleep", type=float, default=5.0)
    loop.add_argument("--stop-file", default=None)
    loop.add_argument("--agent-cmd", default=None, help='e.g. "{python} my_agent.py {brief}"')
    loop.add_argument("--agent-env", nargs="*", default=None, help="KEY=VALUE pairs passed to the agent")
    loop.add_argument("--holder", default="loop")
    loop.add_argument("--timeout", type=float, default=1800.0)

    stop = add("stop", cmd_stop, "write the kill-switch file so a loop stops after this round")
    stop.add_argument("--file", default=None)
    stop.add_argument("--by", default="human")

    resume = add("resume", cmd_resume, "remove the kill-switch file")
    resume.add_argument("--file", default=None)

    return parser


def _extract(argv: list[str], name: str) -> tuple[str | None, list[str]]:
    """Accept global options anywhere (``orchestra task new --repo X ...``)."""

    value: str | None = None
    rest: list[str] = []
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == name and index + 1 < len(argv):
            value = argv[index + 1]
            index += 2
            continue
        if item.startswith(name + "="):
            value = item.split("=", 1)[1]
            index += 1
            continue
        rest.append(item)
        index += 1
    return value, rest


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in raw
    raw = [item for item in raw if item != "--json"]
    repo, raw = _extract(raw, "--repo")
    args = parser.parse_args(raw)
    args.repo = repo
    args.json = wants_json
    ctx = Ctx(args)
    try:
        return int(args.handler(ctx))
    except AdmissionError as exc:
        detail = f" {json.dumps(exc.detail, sort_keys=True, ensure_ascii=False)}" if exc.detail else ""
        print(f"admission refused: {exc}{detail}", file=sys.stderr)
        return ADMISSION
    except LeaseConflict as exc:
        print(f"lease conflict: {exc}", file=sys.stderr)
        return CONFLICT
    except ApprovalError as exc:
        print(f"approval refused: {exc}", file=sys.stderr)
        return NEEDS_APPROVAL
    except (GateError, OSError) as exc:
        print(f"infrastructure fault (not a verdict): {exc}", file=sys.stderr)
        return FAULT
    except (LedgerError, StateError, LeaseError, PolicyError, RoadmapError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return FAIL
    except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
        print(f"command failed: {exc}", file=sys.stderr)
        return FAIL
    finally:
        ctx.close()



# --------------------------------------------------------------------------
# return / dead / loop / stop
# --------------------------------------------------------------------------
def cmd_task_return(ctx: Ctx) -> int:
    args = ctx.args
    task = return_task(ctx, args.task, args.holder, args.reason, note=args.note)
    _print(args, task, f"{task['id']} -> {task['state']} ({args.reason})")
    return OK


def cmd_task_dead(ctx: Ctx) -> int:
    args = ctx.args
    task = ctx.ledger.add_dead(args.task, args.reason, args.note, actor=args.actor)
    _print(args, task, f"{task['id']} dead: {args.reason} ({len(task.get('dead', []))} recorded)")
    return OK


def _agent_env(items) -> dict:
    env: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise LedgerError(f"--agent-env expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        env[key] = value
    return env


def cmd_loop(ctx: Ctx) -> int:
    args = ctx.args
    command = split_command(args.agent_cmd) if args.agent_cmd else None
    summary = run_loop(
        ctx,
        domain=args.domain,
        max_rounds=1 if args.once else args.max_rounds,
        idle_sleep=args.idle_sleep,
        stop_file=Path(args.stop_file) if args.stop_file else None,
        agent_command=command,
        env_extra=_agent_env(args.agent_env),
        holder=args.holder,
        timeout=args.timeout,
        log=(lambda message: None) if args.json else print,
    )
    text = (
        f"rounds={summary['rounds_done']} review={summary['review']} "
        f"failed={summary['failed']} idle={summary['idle']}"
    )
    if summary["stopped_reason"]:
        text += f" stopped={summary['stopped_reason']}"
    _print(args, summary, text)
    return OK


def cmd_stop(ctx: Ctx) -> int:
    path = Path(ctx.args.file) if ctx.args.file else ctx.repo / ".orchestra" / "STOP"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"stopped at {ctx.clock.iso()} by {ctx.args.by}\n", encoding="utf-8")
    _print(ctx.args, {"stop_file": str(path)}, str(path))
    return OK


def cmd_resume(ctx: Ctx) -> int:
    path = Path(ctx.args.file) if ctx.args.file else ctx.repo / ".orchestra" / "STOP"
    removed = path.exists()
    if removed:
        path.unlink()
    _print(ctx.args, {"stop_file": str(path), "removed": removed}, f"{path} removed" if removed else "no stop file")
    return OK


# --------------------------------------------------------------------------
# roadmap queue / research rounds
# --------------------------------------------------------------------------
def cmd_roadmap_list(ctx: Ctx) -> int:
    items = roadmap_load(ctx.repo)
    if ctx.args.status:
        items = [item for item in items if item.get("status") == ctx.args.status]
    rows = [
        f"{item['id']}\t{item.get('status')}\t{item.get('size')}\t{item.get('title')}\t[{','.join(item.get('scope', []))}]"
        for item in items
    ]
    _print(ctx.args, {"items": items}, "\n".join(rows) or "(empty queue)")
    return OK


def cmd_roadmap_check(ctx: Ctx) -> int:
    items = roadmap_load(ctx.repo)
    problems: list[str] = []
    seen: set[str] = set()
    for item in items:
        problems += [f"{item.get('id')}: {problem}" for problem in roadmap_validate(item)]
        if item.get("id") in seen:
            problems.append(f"duplicate id {item.get('id')}")
        seen.add(item.get("id"))
    pending = [item["id"] for item in items if item.get("status") == "next"]
    if len(pending) > 1:
        problems.append(f"more than one next item: {', '.join(pending)}")
    _print(ctx.args, {"items": len(items), "problems": problems}, "ok" if not problems else "\n".join(problems))
    if problems:
        raise RoadmapError(f"{len(problems)} queue problem(s)")
    return OK


def cmd_roadmap_add(ctx: Ctx) -> int:
    args = ctx.args
    item = roadmap_create(
        ctx.repo,
        title=args.title,
        accept=args.accept,
        scope=args.scope,
        size=args.size,
        status=args.status,
        origin=args.origin,
    )
    _print(args, item, f"{item['id']}\t{item['status']}\t{item['title']}")
    return OK


def cmd_roadmap_triage(ctx: Ctx) -> int:
    results = triage_all(ctx.repo)
    rows = [
        f"{entry['id']}\t{entry['score']}\t{'feasible' if entry['feasible'] else 'blocked'}\t{entry['title']}"
        for entry in results
    ]
    _print(ctx.args, {"triage": results}, "\n".join(rows) or "(no later items)")
    return OK


def cmd_roadmap_accept(ctx: Ctx) -> int:
    result = accept_item(ctx, ctx.args.item)
    _print(ctx.args, result, f"{result['item']['id']} accepted as done ({', '.join(result['tasks'])})")
    return OK


def cmd_roadmap_promote(ctx: Ctx) -> int:
    args = ctx.args
    result = promote_item(ctx, args.item, intent=args.intent, role=args.role)
    _print(args, result, f"{args.item} -> next, task {result['task']['id']}")
    return OK


def cmd_research(ctx: Ctx) -> int:
    args = ctx.args
    command = split_command(args.agent_cmd) if args.agent_cmd else None
    summary = run_research(
        ctx,
        rounds=args.rounds,
        budget=args.budget,
        hours=args.hours,
        intent=args.intent,
        agent_command=command,
        env_extra=_agent_env(args.agent_env),
        holder=args.holder,
        stop_file=Path(args.stop_file) if args.stop_file else None,
        idle_sleep=args.idle_sleep,
        log=(lambda message: None) if args.json else print,
    )
    text = (
        f"rounds={len(summary['rounds'])} done={summary['done'] or '-'} "
        f"promoted={summary['promoted'] or '-'} elapsed={summary['elapsed']}s"
    )
    if summary["stopped_reason"]:
        text += f" stopped={summary['stopped_reason']}"
    _print(args, summary, text)
    return FAIL if summary["needs_human"] else OK


def cmd_roadmap_import(ctx: Ctx) -> int:
    args = ctx.args
    result = import_proposals(ctx, args.task, root=args.source)[0]
    created, rejected = result["created"], result["rejected"]
    text = f"imported {len(created)} item(s), rejected {len(rejected)}"
    if created:
        text += ": " + ", ".join(item["id"] for item in created)
    _print(args, result, text)
    return OK if created else FAIL


def cmd_review(ctx: Ctx) -> int:
    args = ctx.args
    result = apply_verdict(ctx, args.task, root=args.source)
    applied = result["applied"]
    text = f"{result['verdict']} applied to {result['target']}"
    if isinstance(applied, dict) and "status" in applied:
        text += f" -> {applied['status']}"
    _print(args, result, text)
    return OK


def cmd_critic_pair(ctx: Ctx) -> int:
    args = ctx.args
    result = pair_critic(ctx, target=args.target, size=args.size, intent=args.intent, title=args.title)
    if result.get("paired"):
        _print(args, result, f"critic {result['task']['id']} -> {args.target} ({result['holder']})")
        return OK
    _print(args, result, f"no critic paired: {result.get('reason')}")
    return FAIL


def cmd_critic_status(ctx: Ctx) -> int:
    payload = critic_status(ctx)
    rows = [f"{row['holder']}\tstreak={row['confirm_streak']}\tlast={row['last_verdict']}" for row in payload["pool"]]
    rows += [f"open\t{entry['task']}\t{entry['target']}\t{entry['state']}" for entry in payload["open_critics"]]
    _print(ctx.args, payload, "\n".join(rows) or "(no critics)")
    return OK

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
