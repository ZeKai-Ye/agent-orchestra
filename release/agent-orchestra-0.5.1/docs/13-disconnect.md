# 13 · 断联、重续与任务回收

一个 agent 断线是常态，不是异常。这一节定义断线之后**任务归谁、多久、怎么回来**。

## 1. 三个问题的答案

| 问题 | 答案 |
| --- | --- |
| 断线后任务立刻回到池子吗？ | 不。先进入 `suspended`（**待重连**），为原持有者保留 `resume_grace_minutes`（默认 120 分钟） |
| 谁都抢不了，会不会卡死？ | 宽限期一过，`reap` 把它放回 `proposed`（**可再分配**），任何 agent 都能领 |
| 无法恢复的怎么办？ | 尝试预算已耗尽 → `abandoned`，并写明 "unrecoverable: attempts exhausted while disconnected" |
| 重连的 agent 怎么知道发生了什么？ | `task status --task T --holder H` 直接给结论：`yours` / `suspended-for-you` / `reassignable` / `suspended-for-other` / `held-by:<别人>` / `terminal:<状态>` |

## 2. 状态机增量

```text
claimed / in_progress / blocked ──(lease TTL 到期，reap)──→ suspended
suspended ──(同一 holder 在宽限期内 resume)──→ claimed        # 尝试计数不变
suspended ──(宽限期过，reap)──→ proposed                       # 任何人可领
suspended ──(尝试预算已耗尽)──→ abandoned                      # 无法恢复
```

`suspension` 字段随任务落盘：`{holder, until, reason, at}`。它**不消耗尝试次数**——断线是环境问题，不是 agent 的判断错误（与退出码 6 的语义一致）。

## 3. 两条命令

```powershell
# 重连后先问一句：我的任务还在吗？
python -m orchestra.cli --repo . task status --task T-0007 --holder agent-7
#   T-0007  suspended-for-you  attempt 0/3

# 还在宽限期内 → 直接续上（重新拿租约，历史里留下 resumed after disconnect）
python -m orchestra.cli --repo . task resume --task T-0007 --holder agent-7
#   T-0007 resumed by agent-7 (attempt 0/3)

# 如果不是你（宽限期已过、别人领了、或任务已终态），status 会说出来；resume/claim 都会被拒
```

拒绝的语义是分开的，脚本可以分支：

| 情况 | 结果 |
| --- | --- |
| 别人持有 | `claim` → 退出码 3（租约冲突） |
| 仍为你保留 | `claim`（任何 holder）→ 退出码 5，`reason=suspended`、带 `holder` 与 `until` |
| 你尝试 resume 但已过期 | 退出码 1，提示 "suspension expired ... it is reassignable now" |
| 尝试预算耗尽 | `reap` 直接 `abandoned`，不再进入 suspended |

## 4. git claim 模式下的对应关系

`claim`（`refs/claims/<task>`）的 TTL 与本地租约同源：ref 过期后被 `reap` 删除，任务照上面的规则进入 `suspended`。所以外部 agent 的流程是：

```
重连 → task status --holder <you>
     → suspended-for-you  → task resume --task T --holder <you>
     → reassignable/别人持有 → 放弃这个任务，去 claim 下一个
```

## 5. 边界

- **宽限期是本地时钟**：多机部署时以维护者机器的时钟为准（`reap` 在哪台跑就以哪台算）。
- **`suspended` 不阻止别人读**：`read` 租约与 `status` 照常；只有写被冻结。
- **没有"部分进度交接"**：`suspend` 只保留任务与死路记录，不保留 worktree；续上后从当前提交重新开始（worktree 每轮重置）。
- **没有自动归因**：断线与"agent 主动放弃"在账簿里都可读（前者 `reason=lease_expired`，后者 `task return --reason ...`），但没有自动区分"网络问题 vs 模型卡住"。
