# 10 · 循环开发：supervisor loop 的设计与实测

本文件回答两件事：`orchestra loop` 怎么保证"能一直干活又不会失控"，以及**用它在本仓库上真跑一轮**得到了什么。

结论先说：机制成立，但第一轮就踩到两个只有真跑才会暴露的问题（worktree 残留、worktree 里是旧版 agent 脚本）；另一个机制（预算封套）按设计拦住了循环。全部数据来自本仓库 `.orchestra/` 与 `ledger/`，可复核。

## 1. 为什么是"外层循环"，不是"让 agent 自己继续"

agent 单次运行是无状态的。持续工作 = 外层驱动 + 状态外置（账簿/租约/事件）+ 停止条件。让 agent 在会话里自己"继续下一轮"的问题：上下文膨胀、行为漂移、进程一死全丢、无法审计。

所以 `loop` 是一个普通进程，每轮都是一个**可丢弃**的完整回合：

```
reap → 选任务 → claim（准入）→ worktree + brief → reset → agent → gate
     → 通过: review ｜ 失败: return(原因) + 耗尽: escalate ｜ 每轮: metrics 快照
```

进程随时可以被 Ctrl-C / kill；下一轮从账簿和租约重建，最多丢一轮未提交的工作。

## 2. 每轮的五条硬约束（都有测试）

| 约束 | 实现 | 测试 |
| --- | --- | --- |
| 停止开关立即生效 | `.orchestra/STOP` 存在则本轮开始前退出 | `test_stop_file_prevents_any_round` |
| 失败必须留下原因 | `task return`：释放租约 + 写 `dead_approaches` + 可能需要冷却 | `test_scope_violation_is_recorded_and_escalates` |
| 失败不污染下一轮 | `reset_worktree`：每轮开始 `git reset --hard` + `clean -fd`，失败后也重置 | `test_failed_round_leaves_a_clean_worktree` |
| 预算/冷却/持有者上限拒绝就退避 | 退出码 `5` + `{"reason": ...}`，循环 sleep 后重试，不消耗尝试预算 | `test_per_holder_limit_refuses_the_second_claim`、`test_cooldown_blocks_the_same_holder_only` |
| 基础设施故障不等于判负 | git/agent 无法启动 → 退出码 `6`，任务不动、尝试不涨 | `test_missing_agent_binary_is_a_fault_not_a_verdict`、`test_missing_worktree_is_a_fault` |

尝试预算耗尽 → `escalated`；`escalated` 不会再被循环领取（只有治理层能 `escalated → proposed`）。

## 3. 一次实测：让 loop 在本仓库上做一次真改动

任务与配置：`ledger/tasks/T-0007.json`、`config/orchestra.toml`（`impl` 门 = `compileall`，`merge` 门 = 全量测试）。

| 步骤 | 结果 | 耗时 |
| --- | --- | --- |
| 第 1 轮（stub 故意越界改 `docs/09-...`） | `scope` 失败 → `dead_approaches += scope_violation` → 任务回 `proposed`，租约释放 | 1.06s |
| 第 2 轮（stub 读 dead 后改 `docs/07-...`） | 门禁全绿 → `review` | 0.62s |
| 提交（带 `Task-Id: T-0007`） | 溯源门 PASS | — |
| `gate run --stage merge`（在 worktree 里跑全量测试） | 64 用例 OK，9/9 步全绿，风险 T0 | 49.3s |
| `approved → merged` + `git merge --ff-only` | 合并进 main，租约清空 | — |
| `status --append` / `trend` | 12 条快照；门禁 4 通过 / 9 失败 | — |

**这里最值钱的一条**：第 2 轮的 stub 是因为读到了 `brief.json` 里的 `dead_approaches` 才改对了范围。也就是说"死路随任务下发"不是文档口号，它在实测里直接改变了一次行为。

## 4. 实测暴露的三个问题（都真的发生了）

1. **worktree 残留污染下一轮**（严重）。失败回合把越界改动留在 worktree 里，下一轮 diff 仍包含它 → scope 永远失败 → 被误判为 escalate。修法：每轮开始与失败后都 `reset_worktree`。T-0001 就是被这个 bug 送进 `escalated` 的，已在账簿里如实记录并改成 `abandoned`。
2. **worktree 里跑的是 main 上的旧版 agent 脚本**。我把 stub 改了但没提交，循环在 worktree 里执行的是旧版（没有 `LOOP_DEMO_TARGET` 分支）→ diff 为空。教训：agent 入口应当用**绝对路径**指向受控副本，或先提交再跑；这条已写进 docs/08。
3. **预算封套真的会停掉循环**。`intent:I-0001` 花到 10/10 后，第 2 轮连续 3 次 `admission_refused`（`budget_exceeded`）并退避。把上限抬到 100 后立刻恢复。这正是期望行为——但它意味着"跑长循环"必须先把封套配到位，否则循环会安静地空转。

## 5. 这套循环的已知边界

- **不做合并**：loop 停在 `review`；`merge` 门与 `git merge` 仍由人执行。理由是 T1/T2 需要人签，合并顺序属 M3（合并队列）。
- **单机**：租约、冷却、预算都在一个 SQLite 里。
- **worker 是黑盒**：循环只认退出码与 diff；agent 内部多轮自我修复不在控制面内（也无需知道）。
- **没有优先级**：按任务号先进先出；分片/紧急度留给 M3 的调度台。

## 6. 怎么用

```powershell
# 一次（cron 友好）
python -m orchestra.cli --repo . loop --once --agent-cmd "python my_agent.py --brief {brief}"

# 连续，10 轮，空转等 30 秒
python -m orchestra.cli --repo . loop --max-rounds 10 --idle-sleep 30 `
  --agent-cmd "python my_agent.py --brief {brief}" --agent-env MY_FLAG=1

# kill switch
python -m orchestra.cli --repo . stop      # 下一轮开始前退出
python -m orchestra.cli --repo . resume
```

`{brief}` / `{worktree}` / `{task}` 会在每轮替换；`--agent-env KEY=VALUE` 注入给 agent；凭据类的环境变量一律被剥掉（`GITHUB_TOKEN`、`ANTHROPIC_API_KEY`、`AWS_*` 等）。

## 7. 判断这套东西是否值得继续用的信号

- **值得**：`dead_approaches` 在多数任务上非空（说明循环真的在自我纠正），且 `review/rounds_done` 稳定在 1 以上。
- **该停**：某任务反复 `scope_violation` 且 dead 已经不涨（说明 brief 没被读，或 agent 不收敛），或 `budget_exceeded` 成为主要失败原因（说明封套配错，不是 agent 的问题）。
