#!/usr/bin/env python3
"""Reference adapter: drive one CLI agent through an orchestrator task brief.

This file is deliberately outside the package: adapters may only use the CLI, never
``import orchestra.*`` (docs/08 section 3.3). Copy it into your own repository and
change ``--agent-cmd``.

Contract
  in : <repo>/.orchestra/briefs/<task>.json  (task, worktree, lease_paths, gates)
  out: a diff inside lease_paths + a gate verdict

Examples
  python tools/agent_adapter.py --repo R --task T-0001 --dry-run
  python tools/agent_adapter.py --repo R --task T-0001 --agent-cmd "claude -p {prompt}"
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

SECRET_MARKERS = (
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "APPROVAL_KEY",
    "PRIVATE_KEY",
    "AWS_",
    "GH_",
    "GITHUB_",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


def scrub_env(env: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Remove credentials: an agent container must not be able to sign or deploy."""

    kept: dict[str, str] = {}
    removed: list[str] = []
    for key, value in env.items():
        if any(marker in key.upper() for marker in SECRET_MARKERS):
            removed.append(key)
            continue
        kept[key] = value
    return kept, sorted(removed)


def split_command(text: str) -> list[str]:
    if os.name == "nt":
        parts = shlex.split(text, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return shlex.split(text)


def load_brief(repo: Path, task: str) -> tuple[dict, Path]:
    path = repo / ".orchestra" / "briefs" / f"{task}.json"
    if not path.exists():
        raise SystemExit(
            f"no brief for {task}: {path}\n"
            f"run: python -m orchestra.cli --repo {repo} worktree create --task {task}"
        )
    brief = json.loads(path.read_text(encoding="utf-8"))
    for key in ("task", "worktree", "lease_paths"):
        if key not in brief:
            raise SystemExit(f"brief is missing {key!r}: {path}")
    return brief, path


def build_prompt(brief: dict, brief_path: Path) -> str:
    task = brief["task"]
    lines = [
        f"Task {task['id']}: {task['title']}",
        f"Role: {task['role']}  Domain: {task.get('domain', 'core')}",
        "",
        "Allowed write scope (anything else fails the scope gate):",
        *[f"  - {path}" for path in brief["lease_paths"]],
        "",
        "Definition of done:",
        "  - change only the paths above",
        f"  - commit with trailer: Task-Id: {task['id']}",
        f"  - `orchestra gate run --task {task['id']} --stage impl --worktree .` must exit 0",
        "",
        f"Brief: {brief_path}",
    ]
    return "\n".join(lines)


def emit(args, payload: dict, text: str) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drive one CLI agent through an orchestrator brief")
    parser.add_argument("--repo", required=True, help="the managed repository (holds ledger and briefs)")
    parser.add_argument("--task", required=True)
    parser.add_argument("--agent-cmd", default=None, help='command template, e.g. "claude -p {prompt}"')
    parser.add_argument("--orchestra-path", default=None, help="path to the orchestrator src (for the gate call)")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--dry-run", action="store_true", help="print the resolved plan, run nothing")
    parser.add_argument("--no-gate", action="store_true", help="skip the gate verdict")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    brief, brief_path = load_brief(repo, args.task)
    worktree = Path(brief["worktree"])
    prompt = build_prompt(brief, brief_path)
    env, removed = scrub_env(os.environ)

    plan: dict = {
        "task": args.task,
        "worktree": str(worktree),
        "brief": str(brief_path),
        "lease_paths": brief["lease_paths"],
        "worktree_exists": worktree.exists(),
        "removed_env": removed,
        "prompt_chars": len(prompt),
    }
    if args.agent_cmd:
        plan["argv"] = split_command(
            args.agent_cmd.replace("{prompt}", prompt).replace("{worktree}", str(worktree)).replace("{task}", args.task)
        )
    else:
        plan["argv"] = None

    if not worktree.exists():
        plan["error"] = "worktree missing; run `worktree create` again"
        emit(args, plan, f"REFUSED {plan['error']}")
        return 1
    if args.dry_run or not plan["argv"]:
        emit(
            args,
            plan,
            "\n".join(
                [
                    f"task      : {args.task}",
                    f"worktree  : {worktree}",
                    f"lease     : {', '.join(brief['lease_paths'])}",
                    f"argv      : {plan['argv'] or '(no --agent-cmd: dry run only)'}",
                    f"scrubbed  : {', '.join(removed) or '(nothing)'}",
                ]
            ),
        )
        return 0

    try:
        proc = subprocess.run(
            plan["argv"],
            cwd=str(worktree),
            env=env,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        plan["error"] = f"agent exceeded {args.timeout}s and was killed"
        plan["agent_exit"] = None
        emit(args, plan, f"FAILED {plan['error']}")
        return 1
    plan["agent_exit"] = proc.returncode

    if args.no_gate or proc.returncode != 0:
        ok = proc.returncode == 0
        emit(args, plan, f"agent exit {proc.returncode} (gate skipped)")
        return 0 if ok else 1

    gate_env = dict(os.environ)
    if args.orchestra_path:
        gate_env["PYTHONPATH"] = os.pathsep.join(
            [args.orchestra_path, gate_env.get("PYTHONPATH", "")] if gate_env.get("PYTHONPATH") else [args.orchestra_path]
        )
    gate = subprocess.run(
        [
            args.python,
            "-m",
            "orchestra.cli",
            "--repo",
            str(repo),
            "gate",
            "run",
            "--task",
            args.task,
            "--stage",
            "impl",
            "--worktree",
            str(worktree),
            "--json",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=gate_env,
        timeout=args.timeout,
    )
    result: dict = {}
    if gate.stdout.strip().startswith("{"):
        try:
            result = json.loads(gate.stdout)
        except json.JSONDecodeError:
            result = {}
    plan["gate_exit"] = gate.returncode
    plan["gate_ok"] = result.get("ok")
    plan["failed_steps"] = [step["name"] for step in result.get("steps", []) if not step.get("ok")]
    plan["risk_class"] = (result.get("risk") or {}).get("class")

    text = "\n".join(
        [
            f"task      : {args.task}",
            f"agent exit: {proc.returncode}",
            f"gate      : {'PASS' if result.get('ok') else 'FAIL'} (exit {gate.returncode}, risk {plan['risk_class']})",
            f"failed    : {', '.join(plan['failed_steps']) or '-'}",
        ]
    )
    emit(args, plan, text)
    if result.get("ok"):
        return 0
    return gate.returncode or 1


if __name__ == "__main__":
    raise SystemExit(main())