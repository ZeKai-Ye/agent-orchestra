# 03 · 数据模型与协作协议

## 1. 实体

### 1.1 任务账簿 `ledger/tasks/<id>.json`（进仓库、一任务一文件）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | int | 目前 1 |
| `id` | str | `T-0001` 形式，可自定义 slug |
| `title` | str | 一句话说清要改什么 |
| `role` | enum | `patch` / `refactor` / `test` / `verify` / `research` / `coordinate` |
| `domain` | str | 与 `[domains.*]` 对齐（域=所有权=租约域=CI 分片） |
| `lineage` | str | 生产方血统（模型+提示版本），只用于路由与统计，不用于授权 |
| `intent` | str \| null | 关联 `ledger/intents/<id>.md`，`merged` 必需且已签字 |
| `state` | enum | 见 §2 |
| `paths` | str[] | 授权写域（scope guard 的唯一依据） |
| `deps` | str[] | 任务依赖 |
| `fingerprint` | str | `sha256(canonical{paths, failure_signature})`，用于**建议性**去重 |
| `failure_signature` | str | 失败特征（异常类型+消息模板），避免语义相似度误判 |
| `attempt` | obj | `{used, max}`，超预算只能升级 |
| `risk` | obj | `{class, signals, reasons, digest, blast_radius}`，由门禁机械写入 |
| `lease` | obj \| null | `{group, holder, mode, paths, ttl, expires_at}` |
| `revision` | int | 每次落盘 +1，用于冲突排查 |
| `history` | obj[] | `{at, from, to, actor, note}`，追加不修改 |

写入方式：临时文件 + `os.replace`（原子），键排序（可 diff、冲突面小）。

### 1.2 intent `ledger/intents/<id>.md`（人写的方向）

```markdown
---
signed: true
author: kai
expires: 2026-12-31
---
# 让配置加载不再抛空指针
可证伪预测：该异常在 24h 内归零。
```

`signed` 不是 `true` 时，任何 `approved → merged` 都被拒绝——这条在代码里，不在流程文档里。

### 1.3 租约（SQLite，`leases` 表）

`group_id, task_id, path, mode, holder, lineage, created_at, expires_at, state`。一组租约共享 `group_id`，批量续期/释放在同一事务里完成。

### 1.4 事件（`events` 表）

`at, kind, task_id, group_id, actor, payload(JSON)`，kind 目前：`lease.acquired/renewed/released/expired`、`gate.passed/failed`。指标全部从这里算，不另建埋点。

### 1.5 一次性 nonce（`approvals_used` 表）

`nonce PRIMARY KEY`。令牌重放由主键冲突拦住——这是数据库层面的保证，不是 if 判断。

## 2. 状态机

```text
proposed ──→ claimed ──→ in_progress ──→ review ──→ approved ──→ merged
    │            │            │  ↑           │  │
    │            │            │  └───────────┘  │        (打回：attempt++)
    │            ├──────────→ proposed          │
    ├──────────→ abandoned                      │
    └──(governance only)──→ quarantined         │
任何非终态 ──→ escalated（超预算/需人决）        └──→ abandoned
escalated ──→ proposed（治理重置）   merged/abandoned 为终态
```

- 边是白名单，非法迁移直接非零退出。
- `quarantined` 只有 `--actor governance:*` 能进（血统冻结）。
- `review → in_progress` 自动 `attempt.used += 1`；`used > max` 直接拒绝并要求 escalate。

## 3. 租约算法

```text
acquire(task, holder, paths, mode, ttl, token?):
  if mode == repo: 需要 kind=repo_lease 的人签令牌（subject="repo"）
  scopes = normalize_all(paths) 并按字典序排序        # 固定加锁顺序 → 无死锁
  BEGIN IMMEDIATE                                     # 抢写锁，等价 CAS
     conflicts = 活跃租约(expires_at > now) 中与前缀重叠者
                  （read 与 read 之间不算冲突）
     if conflicts: ROLLBACK; 抛 LeaseConflict（退出码 3）
     INSERT 每个 scope 一行，共享 group_id
     写事件 lease.acquired
  COMMIT
```

- **重叠判定**同一套 `overlaps()`：`src` 与 `src/core/app.py` 冲突，`src/a` 与 `src/ab` 不冲突。
- **续期**：`expires_at = now + ttl`，超过 `max_ttl` 拒绝。
- **回收**：`expires_at <= now` 的活跃租约标 `expired`，事件落库，任务回到 `proposed`（`blocked` 例外，只清 lease）。
- **审计**：`lease snapshot` 把活跃租约导出成 `ledger/leases.snapshot.jsonl`，让仓库里能看到"谁当时拿着什么"。

### 3.5 预算封套（准入控制）

`budget` 表按 scope 记账，claim 时用两个 scope：`intent:<id>` 与 `lineage:<name>`。两者在**一个事务内**同时扣费，任一超限则整体拒绝（退出码 `5`），并把已经拿到的租约释放、任务留在 `proposed`。没有登记 `ceiling` 的 scope 视为无限额，只记录花费——所以不加封套的仓库行为与 v0.1 完全一致，只是多了账。

### 3.6 任务文件锁

一任务一文件，每次迁移都是 read-modify-write。所有写入走 `TaskLedger.update(task_id, mutator)`：它持有 `.orchestra/locks/task-<id>.lock` 上的 OS 咨询锁（POSIX `fcntl.flock` / Windows `msvcrt.locking`），id 分配则持一把 `ledger.lock`。锁只在本机有意义，与 SQLite 的单机假设一致（ADR 0004）。锁文件不进 `ledger/`，也不会出现在 `git status` 里。

### 3.7 死路清单（`dead`）

任务的 `dead` 是一个有上限（20 条）的追加数组：`{at, reason, note, actor}`。写入途径只有两个：`task dead`（手工记录）与 `task return`（循环交回任务时自动记录）。`reason` 是枚举（`gate_failed` / `scope_violation` / `agent_failed` / `no_progress` / `missing_lemma` / `out_of_time` / `blocked_externally` / `submitted` / `reduced` / `other`），乱填会被拒。

它不是日志，是**给下一个领取者的输入**：`worktree create` 与循环都会把它写进 `brief.json` 的 `dead_approaches`。docs/10 的实测里，正是这条让第二轮改对了范围。

### 3.8 冷却（`cooldowns` 表）

`(holder, task_id) → until`。以 `out_of_time` / `other` 交回任务时写入，时长由 `[scheduler] cooldown_minutes` 决定。领取时命中冷却 → 退出码 `5`，`reason=cooldown`，其它持有者不受影响。这个表在 SQLite 而不是任务文件里，因为它是"谁暂时别碰哪个任务"的瞬时状态，且会被频繁改写。

## 4. 指纹去重（建议性，不是闸门）

`fingerprint = sha256(canonical({"paths": 排序后的路径, "failure": 失败签名}))`。`claim` 时若已有同指纹的活跃任务，**只警告并记入 history**，不阻塞领取。

原因：硬阻塞会让"同一测试失败"无人认领，也会误杀"两个 agent 竞争同一修复"这种有价值的场景。是否合并由协调者（`role=coordinate`）裁决。

## 5. 七条不变量（测试逐个断言）

1. 同一路径同一时刻至多一个写租约。
2. 只允许白名单状态迁移。
3. `merged` 必须绑定已签字 intent。
4. T2 无有效令牌 → 门禁失败（退出码 4）。
5. `git diff` 路径 ⊆ `task.paths`（scope guard）。
6. 尝试次数超预算 → 拒绝继续，要求 escalate。
7. 租约过期可回收，任务自愈回 `proposed`。

## 6. 交接与协调

- 交接物 = 提交 + 门禁结果 + 账簿 history。**对话不是交接物。**
- 跨域改动只能通过契约变更（`contracts/` 版本化 + ADR），不允许 agent 之间私下协商。
- 人签不是"看一遍点确定"：令牌里必须带 `prediction`（可证伪预测）与 `rollback`（回滚方案），两者为空则 `issue` 直接拒绝。
