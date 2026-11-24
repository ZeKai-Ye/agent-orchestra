# 14 · 本机多 agent 实战手册

这份文档只讲一件事：**在你自己的项目里，怎么让 N 个 CLI agent 并行干活，而你不会被它们的互相踩踏淹死。**

前面的文档讲"为什么这么设计"，这份讲"今天下午怎么跑起来"。文中每条命令都在 Windows + Python 3.12 + git 2.51 上实跑过；没跑过的会明确标注"未验证"。

---

## 1. 先看你机器上现在能用的 agent

2026-02 实测（本机：16 逻辑核 / 31.5 GB / Windows）：

| CLI | 状态 | 说明 |
|---|---|---|
| `opencode`（`opencode-cli.exe`） | **可用** | OpenCode 桌面版自带 CLI，v2.2.1。`run --auto "<prompt>"` 非交互执行，模型/供应商已配好 |
| `gemini` | **不可用** | 调用即报 `IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals`——Google 已停用该档，需迁移 Antigravity |
| `claude` / `codex` / `aider` / `gh` | 未安装 | 装好后把 `-Agent` 换掉即可，命令见第 5 节 |
| `ollama` | 已装，但**不是** coding agent | 只有模型，没有"改文件+跑命令+提交"的回路，别直接拿来当 worker |

`opencode-cli.exe` 的实际路径：

```
C:\Users\<你>\AppData\Local\Programs\ai.opencode.desktop\resources\opencode-cli.exe
```

它不在 PATH 上。`scripts/agent_run.py` 会自动探测这个位置，所以下面的命令不用手写全路径。

---

## 2. 一次性准备（每个项目做一次）

```powershell
cd E:\opencode-playfield\projects\<your-project>
git init                       # orchestra 要求仓库自包含
$env:PYTHONPATH = "E:\opencode-playfield\projects\agent-orchestra\src"
python -m orchestra.cli --repo . init
git add -A; git commit -m "chore: orchestra init"   # 必须先提交，否则第一个门判越界
```

然后补两样东西，否则门禁一定失败（这是设计，不是 bug）：

1. **意图文档**：`docs/intents/I-0001.md`，写清"这个项目要解决什么、什么算完成"。门禁会检查任务引用的 intent 是否存在。
2. **门禁命令**：`config/orchestra.toml` 里的 `impl` / `merge`。

```toml
[gates]
impl = ["python -m compileall -q src"]
merge = ["python -m unittest discover -s tests -t ."]
```

`impl` 在一轮结束时跑（快，能挡住语法错和越界），`merge` 在合并前跑（全量，慢）。

---

## 3. 任务怎么切——并行度从哪来

**关键认知：并行度不是由 CPU 决定的，是由路径租约决定的。**

- 一条任务 = 一次对若干路径的租约（`--paths a.py,b.py` 或 `--paths src/parser/`）。
- 两个任务路径相交 → 第二个直接 `exit 5`（`admission refused: wip_limit`/租约冲突），不排队、不等待。
- 所以切分原则是**按目录/模块切**，一条任务一个"谁都不会同时改"的文件集。

```powershell
python -m orchestra.cli --repo . task new --title "parser: 支持嵌套括号" --paths src/parser/ --intent I-0001
python -m orchestra.cli --repo . task new --title "cli: 新增 dry-run 开关"  --paths src/cli.py  --intent I-0001
```

租约写在 SQLite（单机原子），任务书写在 `ledger/tasks/T-xxxx.json`（可 diff、可 review）。这两份东西是唯一共享状态，agent 之间不通信。

---

## 4. 起 N 个 worker

```powershell
.\scripts\local-fleet.ps1 -Agents 3 -Rounds 2
```

它会做这些事：解析出 agent 可执行文件 → 为每个 worker 起一条独立的后台 `orchestra loop`（`--holder worker-i`）→ 每轮：领任务 → 建 worktree → 调 `scripts/agent_run.py` → agent 干活并提交 → 跑 `impl` 门 → 过则进 `review`，不过则回 `proposed` 并在 `dead_approaches` 里留下原因。

配套命令：

```powershell
.\scripts\local-fleet.ps1 -Status    # status + task list + lease list + 每个 worker 的 pid 是否还活着
.\scripts\local-fleet.ps1 -Logs      # 每个 worker 日志的最后 15 行
.\scripts\local-fleet.ps1 -Stop      # 写 .orchestra/STOP：worker 跑完当前轮后退出，不打断正在写的文件
```

**N 取多少？** 本机 16 核 / 31.5 GB，瓶颈不在 CPU，在"每个 agent 的 API 速率 + 拿到的可用任务数"。

- 2–4 是甜点。3 个 worker 一轮的墙钟时间主要被最慢的那个 agent 决定。
- 没有足够多**路径不相交**的任务时，多起的 worker 只会拿到 `exit 5` 然后空转（空转 3×max_rounds 次后自己退出）。
- 想压满就用 `-Rounds 5` 让每个 worker 连轴转，而不是 `-Agents 8`。

`-Timeout` 默认 1800 秒：单个 agent 调用的硬上限，超时计 `out_of_time`（会进冷却）。

---

## 5. 换 agent / 换模型

默认（自动探测 OpenCode CLI）：

```powershell
.\scripts\local-fleet.ps1 -Agents 3
```

指定模型：

```powershell
.\scripts\local-fleet.ps1 -Agents 2 -Model anthropic/claude-sonnet-4-5
```

换别的 CLI（装好之后）：

```powershell
.\scripts\local-fleet.ps1 -Agent claude            # claude -p <prompt> --permission-mode auto_edit
.\scripts\local-fleet.ps1 -Agent codex             # codex exec --skip-git-repo-check <prompt>
.\scripts\local-fleet.ps1 -Agent "C:\path\x.exe"   # 任意可执行，prompt 作最后一个位置参数
```

`scripts/agent_run.py` 里按 agent 名分派参数（`opencode` / `gemini` / `claude` / `codex`，其它一律"prompt 追加在末尾"）。**只接了实测过的参数**；没验证过的组合请先用 `--dry-run` 看一眼 argv：

```powershell
python scripts\agent_run.py .orchestra\briefs\T-0001.json --dry-run
python scripts\agent_run.py .orchestra\briefs\T-0001.json --print-prompt   # 看 agent 到底读到什么
```

---

## 6. 单个 agent 手动跑（日常最常用）

不想起编队、就想让一个 agent 干一件活：

```powershell
python -m orchestra.cli --repo . loop --once --holder me `
  --agent-cmd "python E:\opencode-playfield\projects\agent-orchestra\scripts\agent_run.py {brief}"
```

`{brief}` 由 supervisor 替换成 `.orchestra/briefs/<task>.json`。**worktree 里检出的是 main 上的旧版脚本，所以 agent 入口必须写绝对路径**——这条坑踩过一次。

---

## 7. 结果去哪了、怎么收

- agent 的提交在**独立 worktree**（`.orchestra/worktrees/T-0002`）和分支 `agent/<lineage>/T-0002` 上，`main` 一动不动。
- 过了 `impl` 门的任务停在 `review`，**不会**自动合并。
- 收一条：

```powershell
git -C .orchestra/worktrees/T-0002 log --oneline -3
git -C .orchestra/worktrees/T-0002 diff main --stat
python -m orchestra.cli --repo . gate run --task T-0002 --stage merge --worktree .orchestra/worktrees/T-0002
```

`gate run --stage merge` 退出 0 之后，合并与状态推进由你（或 T0 维护者的 `orchestra merge` 轮询）决定。

---

## 8. 故障处置表

| 症状 | 原因 | 处置 |
|---|---|---|
| `exit 5` + `admission_refused` | WIP 满 / 同 holder 上限 / 预算 / 冷却 / 任务悬挂 | 看 `status`；`--json` 里有 `reason` |
| `exit 5` + `suspended` | 该任务属于断联的别人 | 别抢，等宽限期，或让对方 `task resume` |
| 一轮 `agent failed (other)` | agent 退出码非 0（没配好、额度耗尽、超时） | `-Logs` 看输出；`dead_approaches` 里会留记录 |
| `gate failed (intent, debt: core)` | 仓库缺 `docs/intents/I-xxxx.md` 或债务规则没写 | 补文档；这是"仓库没准备好"，不是 agent 的错 |
| 任务卡在 `in_progress`、进程没了 | worker 被杀 / 断电 / 笔记本合盖 | 等 TTL（`[leases] ttl_minutes`）后 `lease reap`；同一 holder 在宽限内可 `task resume` |
| 想立刻全停 | — | `.\\scripts\\local-fleet.ps1 -Stop`（写 `.orchestra/STOP`，跑完当前轮退出） |

**日志为空？** 后台 Python 的 stdout 是块缓冲，进程被强杀时缓冲区丢失。想看实时输出就把 `-u` 加进 `loop` 的参数（`python -u -m orchestra.cli ...`），或直接看 worktree 里的 git 状态。

---

## 9. 一天的工作流

1. **早上**：`python -m orchestra.cli --repo . roadmap check` → 从 `roadmap/items.jsonl` 里挑 2–3 条互不冲突的，`roadmap promote` 成任务。
2. **上午**：`.\scripts\local-fleet.ps1 -Agents 3 -Rounds 3`，去干别的。
3. **中途**：`-Status` 看有没有 `exit 5` 堆积（说明任务切分有问题，不是 agent 有问题）。
4. **下午**：收 `review`——读 diff、跑 `gate run --stage merge`、人签、合并。
5. **收工**：`status --append` + `trend`，看这一天的通过率和失败原因分布。

---

## 10. 边界：别指望它做的事

- **不判断语义正确性**。门禁证明的是"命令跑过了 + 没越界 + 有提交"，不是"这个功能对"。正确性是你 review diff 的工作。
- **不自动合并**。停在 `review` 是设计（ADR 0006）。
- **不给 agent 凭据**。`scrub_env` 会摘掉 `ANTHROPIC_API_KEY`/`GITHUB_TOKEN`/`AWS_*` 等；agent 拿不到 token，所以它也推不了远端。
- **不解决"两个 agent 抢同一份设计意见"**。路径租约能防文件冲突，防不了逻辑冲突。
- 多机协作走 `docs/12-join.md`（git claims + SSH 签名），单机不需要。

---

## 11. 本机实测记录（2026-02）

在临时仓库 `fleet-smoke` 上跑通的链路：

```
task new (world.txt) → loop --once → claim → worktree → agent_run.py
  → opencode-cli run --auto <prompt>  (156 s)
  → commit a8657c7 "Append task line to world.txt"  (world.txt | 1 +)
  → gate: intent/debt 检查失败（临时仓库没有 docs/intents/）→ 任务回 proposed，dead_approaches 记一笔
```

这证明的是**接线正确**：brief → prompt → agent → 提交 → 门禁判定，全链路真实发生。门禁"失败"是因为临时仓库没准备意图文档，正好演示了门禁确实在起作用。

同一台机器上 `gemini` 用同一条命令直接报 `IneligibleTierError`，退出码 1——所以本文默认用 opencode。
