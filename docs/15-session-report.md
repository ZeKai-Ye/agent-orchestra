# 15 · 会话报告：把一次并发开发"留下来"

## 1. 要解决的问题

并发开发时，事实散在四个地方：

| 在哪 | 有什么 | 缺什么 |
|---|---|---|
| `ledger/tasks/*.json` | 当前状态、历史、死路、尝试次数 | 只看得到"现在",看不出节奏 |
| `.orchestra/state.db` 的 `events` | 租约、门禁、评审事件 | 没有进 git，session 结束就可能被清 |
| `.orchestra/gate-results/` | 单次门禁的输出 | 只有最后一次 |
| `.orchestra/metrics/snapshots.jsonl` | 指标快照 | 要 `status --append` 才有 |

结论：**跑完一轮之后，你手里没有一份能给"懂架构的人/agent"看的东西。** 把原始日志直接丢过去也不行——那是几千行噪声，评审者只能靠猜。

`orchestra report` 就是补这一份：**计数过的事实 + 明说不出来的部分**。

## 2. 用法

会话结束后（或跑到一半想看一眼）：

```powershell
# 最近 24 小时
python -m orchestra.cli --repo . report

# 指定窗口：小时数或 ISO 时间戳
python -m orchestra.cli --repo . report --hours 8
python -m orchestra.cli --repo . report --since 2026-11-24T05:00:00Z

# 全量（从第一条事件算起，不是从 1970 年）
python -m orchestra.cli --repo . report --all

# 指定输出位置；默认 reports/<UTC 时间戳>-session.md
python -m orchestra.cli --repo . report --out reports/2026-11-24-session.md
```

命令把输出路径打到 stdout，机器友好的摘要用 `--json`（含 `counts` 和 `signals` 数组）。

## 3. 报告里有什么

七节，顺序固定：

1. **当时的配置** —— WIP 上限、每 holder 上限、TTL、宽限期、冷却、impl/merge 门禁命令。没有这一节，所有失败率都无法解释。
2. **数字** —— 任务数、事件数、agent 调用、门禁通过/失败、准入拒绝。
3. **为什么失败** —— 门禁失败步骤（`impl:scope` 这种粒度）、死路原因、准入拒绝原因、agent 退出码，各带次数和一条示例。
4. **任务账簿** —— 每任务的状态、域、尝试 `used/max`、死路数、路径、最后一条历史。
5. **时间线** —— 最近 400 条事件，倒序。
6. **给架构评审的信号** —— 见下节。
7. **这份报告看不到的东西** —— 见第 6 节。

## 4. 信号是怎么来的（以及为什么可以少信它）

信号**只在样本够的时候出现**（`MIN_SAMPLE = 3`），而且每条都带推导它的数字：

- `门禁失败 13/20 次，高于通过率；最高频失败步骤是 impl:scope（5 次）`
- `死路原因 scope_violation 出现 5 次（示例：failed: scope）。同一原因重复出现，说明问题在任务契约或 brief`
- `准入拒绝共 12 次，其中 wip_limit 9 次。这是并发度设定的直接反馈`
- `最慢 holder worker-2 的单轮中位耗时 156s（样本 3 轮）`

样本不足时，第 6 节只写一句：**"样本不足，不给结论"**。这是刻意的——在小样本上做归因，比不做归因更糟。

## 5. 为了让报告有话说，新增了三种事件

之前 loop 只把"agent 退出码"和"准入拒绝"打到 stdout，session 一关就没了。现在写进 `events` 表：

| 事件 | 何时 | 关键字段 |
|---|---|---|
| `agent.finished` | 每次 agent 调用结束 | `exit`、`timed_out`、`seconds` |
| `admission.refused` | 准入被拒（exit 5） | `reason`（`wip_limit` / `holder_limit` / `budget_exceeded` / `cooldown` / `suspended`）、`detail` |
| `loop.finished` | supervisor 退出 | `rounds`、`review`、`failed`、`idle`、`stopped_reason` |

旧事件（`lease.*`、`gate.*`、`review.applied`、`roadmap.*`）不变，报告照样读它们，所以**历史数据也进得了报告**——本仓库第一次生成报告就用到了 v0.1 时代的门禁失败记录。

## 6. 边界（报告不会替你做的事）

- **没有 token / 费用**：预算按"轮"计，不按钱计。想知道一轮多少钱，得自己从 agent CLI 侧统计。
- **没有 diff**：报告只记路径和统计。判断代码质量仍然要看 worktree。
- **单机**：只覆盖本机这一份 SQLite。多机协作（`docs/12`）下，各机器各出一份，或由 T0 汇总。
- **不判断"设计对不对"**：它只说发生了什么、失败集中在哪。要不要改架构，是人（或另一个 agent）的事。

## 7. 推荐的工作流

1. 在项目 A 里跑编队：`.\\scripts\\local-fleet.ps1 -Agents 3 -Rounds 3 -Model <模型 id>`（模型必填，见 `docs/14`）。
2. 收工或中途想看时：`python -m orchestra.cli --repo . report --hours 8`。
3. 提交：`git add reports && git commit -m "log: session <日期>"`。报告是纯文本，diff 友好，也能跨机器看到。
4. 把那份 Markdown（或路径）交给架构评审的 agent（比如我），说清楚"这是项目 A 的会话报告，请据此更新架构"。评审方拿到的就不是日志噪声，而是一份带计数和缺口的证据。
