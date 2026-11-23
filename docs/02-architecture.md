# 02 · 工程架构

## 1. 三个面，边界不许含糊

```text
                  ┌──────────── Orchestrator（单进程，Python CLI）────────────┐
  人签令牌 ─────→ │ 账簿 │ WIP/预算准入 │ 租约管理 │ 风险分档 │ 事件日志 │ 快照 │
                  └───────▲───────────────────────────────────▲──────────────┘
             brief.json   │                                   │ gate-result.json
                  ┌───────┴────────┐                 ┌────────┴─────────┐
                  │ Agent Runner   │                 │ Gate Runner       │
                  │ 现成 CLI agent │                 │ 本地与 CI 同一份  │
                  │ + git worktree │                 │ 代码，无第二套    │
                  └───────┬────────┘                 └────────┬─────────┘
                          └──────────→ git（证据面）←──────────┘
             ledger/tasks/*.json · ledger/intents · contracts · adr · config/orchestra.toml
```

- **控制面（唯一权威）**：SQLite WAL，存 `leases` / `events` / `approvals_used` / `budget`。所有子命令只是它的门面。租约不写进 git（见审核 2.2）。
- **执行面**：一任务一 `worktree`，分支 `agent/<lineage>/<task-id>`；`brief.json` 写死租约路径、动作集、必须跑的门，agent 只收 diff。**agent 不能自己宣告成功，只有 gate 能。**
- **证据面**：任务账簿一任务一 JSON（可 diff、可 merge、无单大表）、intent、契约版本、ADR、domain/债务配置、租约审计快照。没进仓库的视为不存在。

## 2. 组件职责（与代码一一对应）

| 组件 | 文件 | 职责 | 不做什么 |
| --- | --- | --- | --- |
| 门面 | `cli.py` | 子命令、退出码、全局 `--repo/--json` | 不藏业务规则 |
| 账簿 | `ledger.py` | 一任务一 JSON 原子写、状态机白名单、intent 签字读取、指纹 | 不做调度决策 |
| 租约 | `leases.py` | 有序加锁、TTL、心跳、回收、审计快照 | 不判断任务是否该做 |
| 存储 | `store.py` | 事务、事件、一次性 nonce、预算前置扣减 | 不存任务正文 |
| 政策 | `policy.py` | 配置读取、diff → 风险分档、人签令牌签发/校验 | 不执行命令 |
| 门禁 | `gates.py` | 机械门顺序执行、diff scope guard、债务上限、溯源 | 不修改代码 |
| 任务文件锁 | `fslock.py` | OS 咨询锁（POSIX `flock` / Windows `msvcrt`），只锁住 read-modify-write 的临界区 | 不做租约，不跨主机 |
| 适配器参考实现 | `tools/agent_adapter.py` | 读 brief、擦环境变量、跑 agent、取门禁结论 | 不属于包，不许 import `orchestra.*` |
| 路径/时钟 | `paths.py` / `clock.py` | 唯一一套路径代数；可注入时间 | — |

"唯一一套路径代数"是刻意的：`is_within` / `overlaps` / `glob_match` 同时服务租约冲突、scope guard、风险规则，避免三处各写一份判断导致边界不一致。

## 3. 与现成 CLI agent 的集成契约（最小、可替换）

runner 只承诺三件事：

1. **输入**：`brief.json`（任务、`lease_paths`、`gates.impl` 命令列表、必需 trailer）。agent 用什么模型、什么工具，控制面不关心。
2. **输出**：worktree 里的 diff + 一次 `gate run` 的结果。**没有 diff 就没有交付**，对话记录不算交付物。
3. **失败信号**：gate 退出码 `1`（拒绝）/ `3`（冲突）/ `4`（需要人签）/ `5`（准入拒绝：WIP 或预算）。agent 必须据此换策略或升级，不许原地重试（尝试预算会拦住）。

接入一个 agent = 写一个 30 行的 subprocess 适配器，读 brief、起进程、收 diff。控制面不引入任何模型 SDK。

## 4. 技术选型与理由

| 选择 | 理由 |
| --- | --- |
| Python 3.12 + stdlib only | 目标机器已有 Python 与 git；零依赖 = 任何 agent 容器都能跑，不必先解决装包 |
| SQLite(WAL) 而非 Redis/Postgres | 单机 50 并发足够；`BEGIN IMMEDIATE` 就是 CAS；少一个需要运维的进程 |
| 任务正文用 JSON、配置用 TOML | 机器写状态需要保证往返语义，YAML 不保证；人写配置用 `tomllib`（标准库） |
| 状态在 SQLite、账簿在 git | 瞬时状态要原子；长期证据要可 review。两件事不该用同一个介质 |
| PowerShell 脚本薄封装 | 本机工作流是 PowerShell；脚本只负责设 `PYTHONPATH` 和串命令 |

## 5. 运行形态与单机边界

- 单机：一个 Orchestrator 进程（或按需 CLI 调用）+ N 个 agent 容器共享 `$repo/.orchestra/state.db`。WAL 允许多读一写，写事务串行化，正是我们要的。
- 多机：换 Postgres。替换点只有 `LeaseManager`（与 `Store` 的四个方法），调用方不需要改——这是刻意留的接缝。
- 崩溃恢复：租约带 TTL，`lease reap` 把死 agent 的路径放回池子并把任务打回 `proposed`。不需要守护进程，定时跑一次即可。

## 6. 明确不做

不改模型、不写工具循环、不做向量记忆、不做自由对话协调。这些是 agent 自己的事；控制面只提供**可验证的协作骨架**。
