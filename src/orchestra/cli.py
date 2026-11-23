"""Command line surface of the orchestrator.

Exit codes are part of the contract (agents and CI branch on them):
0 pass, 1 refused/failed, 2 usage, 3 lease conflict, 4 needs human approval.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .clock import clock_from_env
from .gates import GateError, changed_files, diff_text, git, resolve_base, run_gate
from .leases import LeaseConflict, LeaseError, LeaseManager
from .ledger import LedgerError, StateError, TaskLedger
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
from .store import Store

OK, FAIL, USAGE, CONFLICT, NEEDS_APPROVAL, ADMISSION = 0, 1, 2, 3, 4, 5


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
    tasks = ctx.ledger.list()
    counts: dict[str, int] = {}
    for task in tasks:
        counts[task["state"]] = counts.get(task["state"], 0) + 1
    active = ctx.leases.active()
    now = ctx.clock.now()
    window = now - 86400
    payload = {
        "repo": str(ctx.repo),
        "tasks": {"total": len(tasks), "by_state": counts},
        "wip": counts.get("claimed", 0) + counts.get("in_progress", 0) + counts.get("review", 0),
        "wip_limit": ctx.config.wip_limit() if ctx.config else None,
        "leases": {
            "active": len(active),
            "expired_pending_reap": sum(1 for item in active if item["expired"]),
            "expired_24h": ctx.store.count_events("lease.expired", window),
            "renewed_24h": ctx.store.count_events("lease.renewed", window),
        },
        "gates": {
            "passed_24h": ctx.store.count_events("gate.passed", window),
            "failed_24h": ctx.store.count_events("gate.failed", window),
        },
        "budget": ctx.store.budget_state(),
        "recent_events": ctx.store.recent_events(10),
    }
    if getattr(ctx.args, "append", False) or getattr(ctx.args, "out", None):
        target = Path(ctx.args.out) if ctx.args.out else ctx.repo / ".orchestra" / "metrics" / "snapshots.jsonl"
        snapshot = {key: value for key, value in payload.items() if key != "recent_events"}
        snapshot["at"] = int(ctx.clock.now())
        snapshot["iso"] = ctx.clock.iso()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot, sort_keys=True, ensure_ascii=False) + "\n")
        payload["snapshot"] = str(target)

    text = (
        f"tasks={len(tasks)} wip={payload['wip']}/{payload['wip_limit']} "
        f"leases={payload['leases']['active']} "
        f"gates={payload['gates']['passed_24h']}pass/{payload['gates']['failed_24h']}fail"
    )
    _print(ctx.args, payload, text)
    return OK


def cmd_worktree_create(ctx: Ctx) -> int:
    task = ctx.ledger.load(ctx.args.task)
    base = ctx.args.base or resolve_base(ctx.repo)
    worktree = ctx.repo / ".orchestra" / "worktrees" / task["id"]
    branch = f"agent/{task['lineage']}/{task['id']}"
    git(ctx.repo, "worktree", "add", "-b", branch, str(worktree), base)
    brief_dir = ctx.repo / ".orchestra" / "briefs"
    brief_dir.mkdir(parents=True, exist_ok=True)
    brief = {
        "task": task,
        "branch": branch,
        "worktree": str(worktree),
        "lease_paths": task["paths"],
        "gates": {"impl": ctx.config.gate_commands("impl") if ctx.config else []},
        "instructions": [
            "stay inside lease_paths; the scope gate rejects anything else",
            "prove success with the gate, not with prose",
            f"commit with trailer: Task-Id: {task['id']}",
        ],
    }
    brief_path = brief_dir / f"{task['id']}.json"
    brief_path.write_text(json.dumps(brief, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    _print(ctx.args, {"worktree": str(worktree), "branch": branch, "brief": str(brief_path)}, str(worktree))
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
    if task["state"] != "proposed":
        raise LedgerError(f"{task['id']} is {task['state']}, only proposed tasks can be claimed")
    if ctx.config:
        wip = sum(1 for item in ctx.ledger.list() if item["state"] in ("claimed", "in_progress", "review"))
        if wip >= ctx.config.wip_limit():
            raise AdmissionError(
                f"WIP limit reached ({wip}/{ctx.config.wip_limit()}); admission refuses the claim",
                {"reason": "wip_limit", "wip": wip, "limit": ctx.config.wip_limit()},
            )
    duplicates = ctx.ledger.find_by_fingerprint(task["fingerprint"], exclude=task["id"])
    lease = ctx.leases.acquire(
        task_id=task["id"],
        holder=args.holder,
        paths=task["paths"],
        mode=args.mode,
        ttl=args.ttl,
        lineage=task["lineage"],
        token=args.token,
    )
    cost = args.cost if args.cost is not None else (ctx.config.claim_cost() if ctx.config else 0.0)
    charged: list[str] = []
    try:
        if cost > 0:
            scopes = [
                (f"intent:{task.get('intent') or 'none'}", cost),
                (f"lineage:{task['lineage']}", cost),
            ]
            ok, detail = ctx.store.budget_charge(scopes)
            if not ok:
                raise AdmissionError(
                    "budget envelope exhausted: scope={scope} spent={spent:.2f} + {amount:.2f} > {ceiling:.2f}".format(
                        **detail
                    ),
                    detail,
                )
            charged = [scope for scope, _amount in scopes]
        note = "claimed"
        if duplicates:
            note += f"; duplicate-suspects={','.join(t['id'] for t in duplicates)}"
            print(
                f"warning: same fingerprint already active: {', '.join(t['id'] for t in duplicates)}",
                file=sys.stderr,
            )
        ctx.ledger.transition(task["id"], "claimed", actor=args.holder, note=note, extra={"lease": lease})
    except BaseException:
        # The lease was taken before the budget was charged; never leak it on refusal.
        ctx.leases.release(lease["group"])
        raise
    payload = {
        "task": task["id"],
        "lease": lease,
        "duplicates": [t["id"] for t in duplicates],
        "budget_charged": charged,
        "cost": cost,
    }
    _print(args, payload, lease["group"])
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
    except (LedgerError, StateError, LeaseError, PolicyError, GateError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return FAIL
    except subprocess.CalledProcessError as exc:  # pragma: no cover - defensive
        print(f"command failed: {exc}", file=sys.stderr)
        return FAIL
    finally:
        ctx.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
