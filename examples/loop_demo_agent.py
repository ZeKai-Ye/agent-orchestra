#!/usr/bin/env python3
"""Deterministic stand-in for an agent, used by the loop dogfooding run (docs/10-loop.md).

It is a test double, not a model: the subject of the experiment is the loop, not the
quality of the edits. The two behaviours are deliberate:

  attempt 1  edits a file outside the lease, so the scope gate rejects it and the loop
             records a dead approach;
  attempt 2+ reads brief.dead_approaches, learns that the previous attempt died on
             `scope_violation`, and does the real edit inside the lease.

That is the feedback path of docs/09 in miniature: the desk hands out what already died.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

README_BLOCK = """
## 循环开发（orchestra loop）

`orchestra loop` 是 supervisor，每轮固定 `reap → claim → worktree → agent → gate → review`：

- 失败按原因写进任务的 `dead_approaches`，下一轮的 brief 会带上它，避免重走死路；
- 尝试预算耗尽自动 `escalate`，不会无限重试；
- 准入拒绝（WIP / 每持有者上限 / 预算 / 冷却）退避后重试；
- 门禁自身异常返回 `6`（infrastructure fault），不计入尝试预算；
- `orchestra stop` 写 kill switch，循环在下一轮开始前退出。

```powershell
python -m orchestra.cli --repo . loop --once --agent-cmd "python my_agent.py --brief {brief}"
python -m orchestra.cli --repo . loop --max-rounds 10 --idle-sleep 30 --agent-cmd "python my_agent.py --brief {brief}"
python -m orchestra.cli --repo . stop     # kill switch；下一轮不再领取任务
```
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--brief", required=True)
    args = parser.parse_args()

    brief = json.loads(Path(args.brief).read_text(encoding="utf-8"))
    lease = brief.get("lease_paths") or []
    dead = brief.get("dead_approaches") or []

    if not dead:
        target = Path("docs/09-case-pequalnp.md")
        target.write_text(
            target.read_text(encoding="utf-8") + "\n<!-- attempt 1: wandered outside the lease -->\n",
            encoding="utf-8",
        )
        print(f"attempt 1: edited {target}, which is outside the lease {lease}")
        return 0

    reasons = ", ".join(entry.get("reason", "?") for entry in dead)
    readme = Path("README.md")
    original = readme.read_text(encoding="utf-8")
    if "循环开发（orchestra loop）" not in original:
        readme.write_text(original + README_BLOCK, encoding="utf-8")
    print(f"attempt {len(dead) + 1}: read dead_approaches ({reasons}) and edited README.md inside the lease")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
