# agent-orchestra 0.5.0 · 可直接交付的协作引擎

给一个**已存在的代码仓库**加一层协调面：多个 agent 并发干活不互相踩、每次合并有机械判据、结论能被独立批判推翻。引擎本身零第三方依赖（Python 3.11+ 与 git），不需要服务器。

- 版本 0.5.0 · 2026-11-24 · MIT
- 自带 101 个测试，包内 `.\verify.ps1` 可独立验证
- 运进别的项目只需复制 `src/orchestra` 一个目录

---

## 1. 它解决的三件事

| 问题 | 机制 |
| --- | --- |
| 并发改同一处 | **作用域租约**：`task.paths` 授权写域，租约带 TTL，`gate` 的 scope guard 拒绝越界 diff |
| "改完了"无法验证 | **机械门**：账簿/状态/intent/尝试预算/diff/作用域/风险/检查/债务/溯源，本地与 CI 同一份代码，退出码即结论 |
| 错误结论进入主干 | **独立批判**：`role=critic` 必须给 `refuted|confirmed` + 证据；被反驳的任务 `→ merged` 直接被拒，被反驳的路线条目 `→ dropped` |

配套：任务账簿（一任务一 JSON，可 diff）、`dead_approaches`（失败原因随任务下发）、尝试预算、准入控制（WIP/预算/每持有者上限/冷却）、循环开发（`loop`）、自主轮次（`research`）、git-native claim（`refs/claims/*`，SSH 签名）。

## 2. 五分钟接入一个已有仓库

```powershell
# 解包后，在本包目录里：
.\bootstrap.ps1 -Target D:\work\myrepo
```

它做四件事：复制 `src/orchestra`、放 `join.txt`/`join.json`、建 `handles/`、跑 `orchestra init`（生成 `config/orchestra.toml`、`ledger/`、`.orchestra/`）。**不会自动提交**。

接着（务必按顺序，`config/` 不先提交会让第一个门把配置当成越界改动）：

```powershell
cd D:\work\myrepo
$env:PYTHONPATH = "$PWD\src"

# 1) 提交 init 产物
git add -A; git commit -m "chore: enable agent-orchestra"

# 2) 写一条人签字的方向
#    ledger/intents/I-0001.md  ->
#    ---
#    signed: true
#    author: <你>
#    ---
#    # 这一轮要达成什么（附一条可证伪预测）

# 3) 建任务（paths 就是授权写域）
python -m orchestra.cli --repo . task new --title "修复配置加载空指针" `
  --role patch --domain core --lineage lineage-a --intent I-0001 --paths src/core

# 4) 领取 → 干活 → 门禁
python -m orchestra.cli --repo . task claim --task T-0001 --holder agent-7
python -m orchestra.cli --repo . task transition --task T-0001 --to in_progress --actor agent-7
python -m orchestra.cli --repo . gate run --task T-0001 --stage impl
```

## 3. 命令速查

| 场景 | 命令 |
| --- | --- |
| 看有什么活 | `task list --state proposed`；`status`（WIP/租约/门禁/预算） |
| 领取/续期/归还 | `task claim` · `task heartbeat` · `task return --reason <枚举>` |
| 记录死路 | `task dead --reason gate_failed --note "..."`（会写进下一个人的 brief） |
| 机械门 | `gate run --task T --stage impl\|merge [--worktree <path>]` |
| 不可逆动作 | `approval issue --kind merge --by <人> --prediction "..." --rollback "..."`（HMAC、绑定 diff 摘要、一次性） |
| 循环开发 | `loop --once\|--max-rounds N --agent-cmd "python my_agent.py --brief {brief}"`；`stop`/`resume` 是 kill switch |
| 自主轮次 | `roadmap check\|triage\|import` + `research --rounds N --budget B --hours H` |
| 批判 | `critic pair --target T-0007\|R-0001`；`review --task <critic>` 应用判定 |
| 外部 agent（git claim） | `handle add` → `claim` → `beat` → `release`；维护者 `merge --task T` |
| 指标 | `status --append` 落 JSONL；`trend` 看首末差值 |

## 4. 退出码（脚本按它分支）

`0` 通过 · `1` 拒绝 · `2` 用法 · `3` 冲突（租约或 claim） · `4` 需要人签 · `5` 准入拒绝（WIP/预算/持有者上限/冷却，stderr 带 JSON reason） · `6` 基础设施故障（**不判负**，不计尝试预算，重试即可）

## 5. 七条不变量（都有测试）

1. 同一路径同一时刻只有一个写租约；`read` 可共享。
2. 状态迁移只走白名单边（`review → in_progress` 自动加一次尝试）。
3. `merged` 必须绑定已签字 intent。
4. T2 无有效令牌必失败；令牌绑定 diff 摘要、一次性、带过期。
5. `git diff` 路径 ⊆ 任务 `paths`，否则 scope 拒绝。
6. 尝试耗尽 → `escalated`，不再被自动领取。
7. 失败的回合不污染下一轮（worktree 每轮重置）。

## 6. 三种用法

- **单机多人/多 agent**：直接 `task`+`lease`+`gate`，状态在 `.orchestra/state.db`（SQLite）。同一台机器上 50 并发已实测（8 进程抢同一路径恰好 1 胜）。
- **外部贡献者**：把 `join.txt` 整段给他们，agent 用 `claim/beat/release` 三条命令在 **git refs** 上协调（无服务器），claim 必须用登记在 `handles/<handle>.pub` 的 SSH 密钥签名。
- **持续自主**：`research` 一轮 = 验收上一轮 Done → 三查 Later 可行性 → 提拔一个 → 跑一轮 loop → 记录；人只给 `--rounds/--budget/--hours`。

## 7. 包里有什么

```
agent-orchestra-0.5.0/
├─ src/orchestra/          引擎（stdlib only；cli/ledger/leases/gates/policy/loop/research/roadmap/critic/coord_git/runner …）
├─ tests/                  101 个用例（含 8 进程租约竞态、双克隆 claim 竞态、批判拦截合并）
├─ tools/agent_adapter.py  参考适配器（brief 驱动、擦凭据、取门禁结论）
├─ examples/loop_demo_agent.py  循环实验用的确定性替身
├─ join.txt / join.json    外部 agent 契约（可整段粘贴 / 机器可读）
├─ config/orchestra.example.toml  init 会把它写进目标仓库
├─ docs/                   架构、门禁与风险、轮次、外部接入（12 篇里的 4 篇）
├─ bootstrap.ps1           装进目标仓库
└─ verify.ps1              在包内跑全部测试
```

**不包含**：任何模型调用、agent 循环实现、沙箱、Web 界面、CI 配置、托管服务。这些都不属于协调层。

## 8. 边界与升级路径

| 现状 | 边界 | 升级 |
| --- | --- | --- |
| 租约在 SQLite（单机） | 多机不共享 | 换 `LeaseManager` 为 Postgres；或走 `claim`（git refs） |
| 人签令牌 HMAC | 对称密钥 | 换 Ed25519；令牌格式已版本化（`oap1.`） |
| 外部 claim 只在 Windows 实测 | OpenSSH 各平台都有 | Linux/macOS 验证后即可放开 |
| 没有 PR 上的 CI 复跑 | 目前是 agent 本地 gate + 维护者合并前复跑 | 把 `gate run --stage merge` 挂到 PR 流水线 |
| claim（refs）与账簿（文件）无事务 | 靠命令顺序一致 | 引入小控制面（desk API）后统一 |

## 9. 交付自检

```powershell
.\verify.ps1        # 101 个用例；绿了说明这份包是自洽的
```

有疑问时先读 `docs/02-architecture.md`（三面边界）与 `docs/04-gates-risk-security.md`（门序与风险分档）；外部接入读 `join.txt`；轮次与自主开发读 `docs/11-rounds.md`。
