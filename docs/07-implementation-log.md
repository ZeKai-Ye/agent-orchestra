# 07 · 实现记录：做了什么、试过什么、放弃了什么

这份文档是**流水账**，不是方案。方案在 01–05，这里只记录事实：哪一行代码真在跑、哪个想法试过、哪个想法被自己推翻。目的是下一次改动前不用重新踩一遍。

版本：v0.2.0（v0.1.0 对应提交 `1a1e943 chore: initial scaffold`；M2 增补见 §7）。

## 1. 当前实现（可运行、有测试）

| 能力 | 位置 | 证据 | 限制 |
| --- | --- | --- | --- |
| 任务账簿（一任务一 JSON、原子写、revision） | `ledger.py` | `test_create_and_claim` | 无跨文件事务 |
| 状态机（10 态、白名单边、治理专属态） | `ledger.py` | `test_illegal_transition`、`test_quarantine_is_governance_only` | 无并发锁：同一任务文件并发写会丢更新 |
| intent 签字闸门 | `ledger.py` | `test_merge_requires_signed_intent` | front-matter 手写，不做签名验证 |
| 尝试预算 | `ledger.py` | `test_attempt_budget` | 计数在 `review → in_progress` 时自增，`task attempt` 为手动补录 |
| 结构化指纹去重（建议性） | `ledger.py` | `test_fingerprint_dedup_is_structured` | 只有结构化键，无语义相似度 |
| 路径租约：前缀重叠、四种模式、TTL、续期、回收、快照 | `leases.py` | `test_same_path_is_exclusive`、`test_readers_share`、`test_ttl_expiry_and_reap`、`test_eight_agents_race_for_one_path` | 单机 SQLite |
| 8 进程真并发抢同一路径：1 胜 7 败（退出码 3） | `leases.py` + `store.immediate` | 同上 | 未测 500 并发（下一里程碑） |
| WIP 准入控制 | `cli.cmd_task_claim` | 手工验证 | 预算扣减已实现但未接 claim |
| 风险分档（T0/T1/T2，6 类信号） | `policy.py` | `RiskTests` 6 个用例 | 信号是关键词匹配，非 AST 级 |
| 人签令牌（HMAC、绑定 diff 摘要、一次性 nonce、过期、必须带预测与回滚） | `policy.py` | `ApprovalTests` 5 个用例 | 对称密钥，非公钥 |
| 机械门（账簿/状态/intent/尝试/diff/scope/风险/检查/债务/溯源） | `gates.py` | `GateTests` 9 个用例 | 检查项命令来自配置，未内置语言工具链 |
| diff scope guard | `gates.py` | `test_scope_guard_blocks_writes_outside_lease` | 只覆盖已提交 + 未跟踪文件，不解析子模块 |
| 债务硬上限 | `gates.py` | `test_debt_budget_blocks_growth` | 只数字符行，不算复杂度/重复度 |
| 溯源 trailer 校验 | `gates.py` | `test_merge_requires_task_id_trailer` | 只查 `Task-Id:`，未验签 |
| 指标快照（WIP / 租约 / 门禁 / 预算 / 事件） | `cli.cmd_status` | `test_status_reports_wip_and_leases` | 无时间序列，无导出 |
| worktree + brief 生成 | `cli.cmd_worktree_create` | `verify.ps1` 手工路径 | 未在测试里跑（需要多余 git 仓库） |
| 一键自检 | `scripts/verify.ps1` | 全绿输出 | Windows/PowerShell 优先 |

测试规模：37 个用例，覆盖 `paths / ledger / leases / policy / gates / cli`。最近一次全绿：`Ran 37 tests ... OK`（约 21 秒）。

## 2. 试过、保留的决策

| 决策 | 当时的备选 | 为什么选现在这个 |
| --- | --- | --- |
| 租约放 SQLite 事务 | 仓库内 `leases.lock` 文件 | 文件锁并发即冲突、无 CAS；改 DB 后 8 进程竞态一次通过 |
| 机器写状态用 JSON | YAML（原方案） | YAML 无往返语义保证，且本机无 PyYAML；JSON 排序键 + `os.replace` 可 diff 可原子 |
| 配置用 TOML 单文件 | 4 个 YAML/TOML 分文件 | 配置漂移点从 4 个降到 1 个；`tomllib` 是标准库 |
| 风险信号按 diff 文本 + 路径算 | 让 agent 自报风险等级 | 自报必然乐观；机械计算可被测试固定住 |
| 令牌绑定 `sha256(路径集 + diff)` | 令牌只绑 approval_id | 不绑内容 = 签字后代码还能改，等于没签 |
| 指纹只警告不阻塞 | 同指纹直接拒绝领取 | 硬阻塞会让故障无人认领，也误杀竞争修复 |
| 门禁与 scope guard 同一份代码供本地/CI 用 | 本地轻、CI 重 | 两套判断必然漂移（"本地过了 CI 挂了"） |

## 3. 试过、改掉的（留档，避免再走）

1. **`--json` 只放在全局位置**：`orchestra task new --json` 直接 argparse 报错。现在 `main()` 先扫出 `--json`/`--repo` 再解析，任何位置都接受。
2. **`orchestra init` 的产物不提交**：`config/orchestra.toml` 未跟踪时，第一个门把它算成"越界改动"，导致 impl 门假失败。修法两层：`verify.ps1` 里先提交 init 产物；文档写明这一步。
3. **`transition` 之后再读 `task["lease"]` 去释放租约**：`transition` 已把 lease 置空，导致 merged 后租约残留（`test_full_flow_to_merged` 抓到的）。改成先读旧账簿再迁移。
4. **T2 审批被拒时返回 `1`**：agent 无法区分"改坏了"和"等签字"。现在只要 T2 未获有效令牌（缺失或校验失败）就统一返回 `4`。
5. **`diff_text` 用 `git diff base` 全量**：含 `ledger/` 与 `.orchestra/`，导致门禁写账簿后审批摘要漂移（令牌永远对不上）。现在用 pathspec 排除这两个目录。
6. **风险规则直接 `fnmatch`**：`src/*.py` 会跨段匹配 `src/core/app.py`。改成段感知匹配（`*` 不跨段，`**` 跨段）。
7. **测试把改动提交到 `main`**：`git diff main` 变空，"diff" 步直接失败。改成每个用例切 `agent/<lineage>/<id>` 分支。

## 4. 明确没做（不是遗漏，是边界）

- 真实分片 CI、消费者契约测试执行、mutation 执行、OTel 导出、Postgres 租约、具体模型适配器、Web 签字台。
- ~~任务文件的并发写保护~~：v0.2.0 已解决，走 `fslock`（见 §7 与 ADR 0004）。
- 跨仓库协作、子模块、monorepo 特化。

## 5. 已知限制（会在下一版暴露的）

| 限制 | 触发条件 | 计划 |
| --- | --- | --- |
| SQLite 单机 | 多台机器跑 agent | M2 换 Postgres，接口已 isolate 在 `LeaseManager` |
| HMAC 对称密钥 | 需要"人持有私钥、机器只验签" | M5 换 Ed25519，令牌格式已版本化（`oap1.`） |
| 关键词级风险信号 | 想要 AST 级判断（如"删了断言但保留了 test 名"） | M6 接 tree-sitter |
| 无故障注入 | 想知道 agent 崩溃/断网时是否自愈 | M2 加 chaos 用例（kill -9 后 `lease reap`） |
| 指标只有快照 | 想看趋势 | **已解决**：`status --append` + `trend`（§7） |

## 6. 复现本文所有结论

```powershell
powershell -File .\scripts\verify.ps1        # 37 用例 + 真实门禁演示
$env:PYTHONPATH = "$PWD\src"
python -m unittest tests.test_core.LeaseTests.test_eight_agents_race_for_one_path -v
python -m unittest tests.test_gates -v
```

## 8. v0.3.0 增补（M2.5：supervisor loop + 死路清单 + 冷却 + 故障第三态）

### 8.1 新增能力

| 能力 | 位置 | 证据 | 限制 |
| --- | --- | --- | --- |
| `orchestra loop`：每轮 reap→claim→reset→agent→gate→review | `loop.py`、`runner.py` | `tests/test_loop.py` 14 例 | 停在 review，不合并 |
| kill switch（`stop`/`resume`，文件标记） | `cli.cmd_stop/resume` | `test_stop_file_prevents_any_round` | 只在本轮边界生效 |
| 死路清单随 brief 下发 | `ledger.add_dead`、`runner.create_worktree` | `test_brief_carries_dead_approaches` | 上限 20 条，不做语义合并 |
| 每持有者上限 + 冷却 | `runner.claim_task`、`store.cooldowns` | `test_per_holder_limit_*`、`test_cooldown_*` | 冷却只按 (holder, task) 记 |
| 失败不污染下一轮 | `runner.reset_worktree` | `test_failed_round_leaves_a_clean_worktree` | 会丢弃 agent 的未提交产物（有意） |
| 退出码 6：基础设施故障 | `cli.main`、`runner.run_agent` | `test_missing_agent_binary_is_a_fault_not_a_verdict`、`test_missing_worktree_is_a_fault` | 租约留在原地，靠 TTL 回收 |

### 8.2 本轮踩到并修掉的（含真跑暴露的）

10. **失败轮次污染下一轮**（实测发现，严重）：越界改动留在 worktree，下一轮 scope 永远失败 → 被误 escalate。加 `reset_worktree`（每轮开始 + 失败后）。T-0001 就是被这个 bug 送进 `escalated` 的，账簿里如实留档，后改为 `abandoned`。
11. **循环跑的是 worktree 里的旧版 agent 脚本**：我改了 stub 但没提交，worktree 检出的是 main 上的旧版 → diff 为空。改用绝对路径调用受控副本，并写进 docs/08。
12. **`budget_exceeded` 静默停住循环**：`intent:I-0001` 到 10/10 后连续 3 次 `admission_refused`。这是"工作正常"，但意味着跑长循环前必须先配封套——已写进 docs/10 §4。

### 8.3 一次真跑（dogfooding）的数据

`T-0007`：第 1 轮越界 → scope 失败（1.06s）；第 2 轮读 dead 后改对范围 → review（0.62s）；提交带 `Task-Id`；merge 门在 worktree 里跑全量测试 49.3s 全绿；`approved → merged` + `git merge --ff-only`。全文见 [10-loop.md](10-loop.md)。

### 8.4 边界

- 循环不做合并、不做优先级、不做跨机；worker 内部多轮自我修复不在控制面。
- `escalated` 只能由治理层重置，循环不会自己捡回来。

## 7. v0.2.0 增补（M2：准入、韧性、证据、适配器）

### 7.1 新增能力

| 能力 | 位置 | 证据 | 限制 |
| --- | --- | --- | --- |
| 预算封套（`intent:` / `lineage:` 两个 scope，一次事务全有或全无） | `store.budget_charge`、`cli.cmd_task_claim` | `test_claim_charges_intent_and_lineage` | 单位是任意数，不做货币换算 |
| 准入拒绝：退出码 `5` + 机器可读 reason，租约自动释放 | `policy.AdmissionError`、`cli.main` | `test_budget_refusal_is_exit_5_and_releases_the_lease`、`test_wip_limit_refuses_with_reason` | 只有 WIP 与预算两类 |
| `budget set/list`、`claim --cost` | `cli` | `test_cost_zero_does_not_charge` | 无预算变更审计流（只有 spent 累计） |
| 任务文件 OS 锁 + id 分配锁 | `fslock.py`、`ledger.update/_task_lock` | `test_concurrent_attempts_do_not_lose_updates`（6 进程全中）、`LockTests` | 单机；7 层嵌套调用会超时而不是死等 |
| 崩溃回收：agent 被 kill 后靠 TTL 释放 | `leases.reap` | `test_killed_holder_is_reaped_after_ttl`（真 kill 子进程） | 回收依赖有人跑 `lease reap`，没有守护进程 |
| 指标时间序列与趋势 | `cli.cmd_status --append`、`cli.cmd_trend` | `test_status_append_and_trend` | 只存快照，不做聚合/导出 |
| `gate run --worktree`：diff 在 worktree、账簿在主仓 | `gates.run_gate(worktree=...)` | `test_full_loop_with_a_fake_agent` | 只支持同一 git 目录下的 worktree |
| 参考适配器（读 brief、擦凭据、跑 agent、取门禁结论） | `tools/agent_adapter.py` | `AdapterTests` 4 例 | 用假 agent 验证；真 CLI agent 未接入（M3 第一项） |

测试规模：37 → **51**，全绿约 80 秒（worktree 与 kill 用例较慢）。

### 7.2 本轮踩到并修掉的

8. **`budget list` 的格式串**：`f"{'-' if row['ceiling'] is None else row['ceiling']:.2f}"` 的条件表达式整体吃了 `:.2f`，ceiling 为空时对 `"-"` 做浮点格式化直接抛错。改成先算 `ceiling` 字符串。
9. **`status --append` 引用了不存在的变量**：`cmd_status` 里只绑了 `ctx.args`，我按习惯写了 `args`，`NameError` 把 status 整个打挂。这类错误只会在"新加分支"里出现，靠现有用例回归才发现——所以 §4 的"一不变量一用例"之外，还加了一条：**动共享命令就顺手跑一遍它的最小用例**。

### 7.3 已知限制（更新后）

已解决：任务文件并发写、指标只有快照、无故障注入。

仍存在：SQLite 单机；HMAC 对称密钥；风险信号是关键词级；适配器只验证到假 agent；回收无守护进程（靠定时 `reap`）。

### 7.4 刻意没做

没有自己去写 merge queue。`docs/06` 查到 `uber/submitqueue` 与 `funador/claude-code-merge-queue` 已经解决同一问题，M3 的第一项是**评估后选型**，而不是直接开写——这是本轮唯一改变原计划顺序的地方。

<!-- loop attempt 2: inside the lease after dead=scope_violation -->

## v0.5.2 增补（本机多 agent 编队）

新增两个**脚本**（不是库代码，不改状态机）：`scripts/agent_run.py` 把 brief 变成 prompt 并调用本机 CLI agent；`scripts/local-fleet.ps1` 起 N 条独立 supervisor。

- `agent_run.py`：读 `.orchestra/briefs/<task>.json` → 生成 prompt（任务、租约路径、门禁命令、dead_approaches、六条规矩）→ 在 worktree 里跑 agent。`--print-prompt` / `--dry-run` 可离线检查。agent 分派：`opencode` → `run --auto <prompt>`；`gemini` → `-p <prompt> --skip-trust --approval-mode <mode>`；`claude` → `-p <prompt> --permission-mode <mode>`；`codex` → `exec --skip-git-repo-check <prompt>`；其它 → prompt 追加在末尾。`.cmd/.bat` 自动包一层 `cmd /c`，`.ps1` 包 `powershell -File`。
- `local-fleet.ps1`：`-Agents N -Rounds M` 每 worker 一条 `loop --holder worker-i --timeout T`，日志落 `.orchestra/logs/worker-i.log`，pid 落 `.orchestra/fleet.json`；`-Status` / `-Stop`（写 STOP 文件）/ `-Logs`。agent 未指定时按 `%LOCALAPPDATA%\Programs\ai.opencode.desktop\resources\opencode-cli.exe` → PATH(`opencode`/`claude`/`codex`/`gemini`) 顺序探测。

**实测事实（2026-02，本机 16 核 / 31.5 GB）**：

- `gemini` CLI 在本机**已不可用**：`IneligibleTierError: This client is no longer supported for Gemini Code Assist for individuals`，exit 1。同一台机器的 `opencode-cli.exe`（v2.2.1，`run --auto`）正常工作。
- 端到端在临时仓库跑通：`task new → loop --once → claim → worktree → agent_run.py → opencode (156 s) → commit a8657c7 → impl 门判定`。门禁因临时仓库缺 `docs/intents/` 而失败并回退任务——门在起作用，不是接线错误。
- 踩到并修掉的坑：`local-fleet.ps1` 曾把 `PYTHONPATH` 指向**目标仓库**的 `src`（而不是 orchestra 自己的 `src`），子进程因此 `ModuleNotFoundError`。目标仓库不是 orchestra 源码树，这个错对真实用法是必现的。
- 已知限制：后台子进程被父会话回收时任务停在 `in_progress`（靠 TTL + `lease reap` 或 `task resume` 收尾）；后台 Python 的 stdout 是块缓冲，强杀时日志为空——这两条都写进了 `docs/14` 的故障表。

## v0.5.3 增补（会话报告：并发开发留痕）

新增 `orchestra report`（`src/orchestra/report.py` + `cli.py` 子命令 + `store.events_since`）：把账簿、事件表、配置聚成一份 Markdown（默认 `reports/<UTC>-session.md`）。

- 七节固定结构：配置 / 数字 / 失败归因（门禁步骤、死路、准入、agent 退出）/ 任务账簿 / 时间线 / 信号 / 缺口。
- 信号有门槛：样本 < 3 不给结论；每条信号自带推导它的数字。**这条规则是刻意的**，小样本归因比不归因更糟。
- 新增三种事件，否则报告无话可说：`agent.finished`（exit/timed_out/seconds）、`admission.refused`（reason/detail）、`loop.finished`（rounds/review/failed/idle）。
- `--all` 的窗口从**第一条事件**算起（实测踩坑：初版输出 `1970-01-01 → now（498752 小时）`）。

**实测**：

- 在本仓库（v0.1 起的真实历史）生成成功，55 条事件、12 个任务；信号输出 `门禁失败 13/20 次…最高频失败步骤是 impl:scope（5 次）`——与 `T-0001` 的两条 `scope_violation` 死路记录一致。
- 在临时仓库跑一轮真实 agent（opencode 退出 1）后，报告正确记下 `exit 1` 一次并聚合进信号。
- 5 个新测试（`tests/test_report.py`）：计数、信号门槛、空窗口明说、真实账簿出文件、非法 `--since` 退出码 1。
- 期间修掉的坑：`cmd_report` 追加在 `if __name__ == "__main__"` 之后导致 `NameError`——`__main__` 守卫必须留在文件末尾。

## v0.5.4 增补（loop 轮内续约）

**问题**：`loop` 在 agent 跑的那一整轮里从不续约（grep `renew`/`heartbeat` 在 loop.py 里一处都没有），而 agent 默认 `--timeout` 也是 1800s、租约 TTL 也是 1800s。一轮超过 TTL 时自己的租约会先过期，被别的 worker `reap` 收走后任务易主，而原 worker 还在往同一个 worktree 里写。

**做法**（`runner.LeaseHeartbeat` + `loop.run_loop`）：

- 心跳线程每 `interval` 秒调一次 `leases.renew(group, holder=holder)`；`interval` 由 `--heartbeat` 指定，默认 `ttl/3`，夹在 [1, 60] 秒。
- 线程用自己的 `Store`（自己开 sqlite 连接）：**sqlite3 连接不能跨线程**，共享会直接抛 `ProgrammingError`。
- `renew` 抛 `LeaseError`（组不存在 = 被 reap；holder 不匹配 = 被抢）→ 置 `lost=True` 并停止续约。`sqlite3.Error` 只记 `transient_errors`，因为数据库忙不等于租约丢了。
- agent 返回后、门禁跑完后各检查一次 `lost`：命中就 `_abandon_lost_lease()`——写 `lease.lost` 事件、重置 worktree、**完全不动账簿**（任务已经属于别人），本轮 `outcome=lease_lost`、计入 `summary["lease_lost"]`，然后继续下一轮。
- `--heartbeat 0`（默认）＝自动；`loop --json` 摘要里出现 `lease_lost` 计数，纯文本输出非零时追加 `lease_lost=N`。

**为什么不把租约 TTL 直接调大**：TTL 是"掉线多久才算掉线"的判定窗口，调大等于把误判窗口一起调大。续约修的是"活着的人被判定掉线"，不该用它掩盖"死了的人还占着"。

**测试**（`tests/test_heartbeat.py`，真时钟）：

- 慢 agent（sleep 1.2s，`--heartbeat 0.3`）：`lease.renewed` ≥ 1、`lease.expired` = 0、`lease_lost` = 0 → 说明续约真的在发生。
- 被抢：agent 自己用独立连接 `UPDATE leases SET holder='intruder'` 再 sleep 1.5s → `lease_lost = 1`、`outcome=lease_lost`、任务没进 `review`、`gate.passed` = 0 → 说明丢租约就真停手，不是只记一笔日志。

## v0.5.5 增补（多 agent 启动必须指定模型）

**要求**：本机跑多 agent 编队时，模型必须显式确定，不允许隐式落到 agent 的默认模型上。

**实现**（`scripts/local-fleet.ps1`）：

- `-Model` 必填：不传直接 `Write-Error` 拒绝启动，并列出 `opencode models` 的前 12 个（共 N 个）。
- 传 `-Model auto` 是**显式**接受 agent 默认：打 `Write-Warning`，之后 `fleet.json` 与 `-Status` 标注 `(agent default)`。
- 模型 id 对照 `opencode models` 校验，拼错当场报错。
- 新增 `-DryRun`：打印 agent / model / 参数行但不启动进程；新增 `-Yes` 跳过确认（非交互场景，例如脚本或自动化测试）。
- 实际模型写进 `.orchestra/fleet.json` 的 `model` 字段，`-Status` 每个 worker 一行显示 → "现在跑的是哪个模型"永远可查。
- `scripts/agent_run.py` 在没收到 `-m` 时向 stderr 打一行警告（单 agent 手动跑也看得见）。

**实测**（`fleet-smoke` 临时仓库）：

| 场景 | 结果 |
|---|---|
| `-Agents 1`（无 `-Model`） | 拒绝，列出 "Available (first 12 of 96)"，退出码非 0 |
| `-Model no-such-model` | 拒绝：`not in the list this agent reports (96 models)` |
| `-Model opencode-go/deepseek-v4-pro -DryRun` | 打印 `model: opencode-go/deepseek-v4-pro`，argv 里带 `-m` |
| `-Model auto -DryRun` | 警告 + `model: (agent default)`，argv 里不带 `-m` |

**顺带纠正一个错误结论**：上一版文档写"本机没有 `anthropic/*`"。实测同一台机器相邻两次 `opencode models` 分别返回 **81** 和 **96** 个模型，provider 组成也不同（`anthropic/*` 出现过）。这个目录是动态的，任何写死的数字或"有没有某 provider"的断言都会过期，已改为"以命令输出为准"。

## v0.6.0 增补（验收标准成为任务的字段：L1 + L2）

**问题（用户实录）**：多 agent 并发开发后质量验收不成立——**执行方和评审方都不知道验收标准是什么**。追下去发现标准本来存在，只是在交接处丢了：`roadmap/items.jsonl` 每条都有 `accept`，`triage` 还会检查 `accept-runnable`，但 `promote_item()` 调 `ledger.create()` 时**没传**，任务、brief、评审三方全看不到。

**做法**（本轮做 L1 + L2，L3/L4 见 `docs/03` 7.4）：

- 新模块 `src/orchestra/acceptance.py`：解析 `A1|check|<命令>|<备注>` / `A1|judge|<要求>|<备注>`（可省 id、可省备注、裸字符串= check 命令），算 `acceptance_digest`，渲染成 brief/prompt 用的文本。只分 `check`（命令判定）与 `judge`（人/critic 判定）两类——不属于任何一类的要求不是标准。
- `ledger`：`_create(acceptance=...)` 解析并存储 `acceptance` + `acceptance_digest`；`validate()` 重算摘要，**改过标准即 `acceptance_digest does not match the criteria`**，门禁的 `ledger` 步骤直接失败。`SCHEMA_VERSION` 1→2，`ACCEPTED_SCHEMA_VERSIONS=(1,2)` 让 12 个历史任务继续可读。
- `cli task new`：`--acceptance`（可重复）必填，否则 exit 1；`--no-acceptance` 是显式弃权；输出行加 `criteria=N`。
- `roadmap.promote_item`：把 `accept` 转成 `A1|check|<accept>|<标题>`；条目没有 accept 就拒绝 promote（不再产生"没人能评审"的任务）。
- `runner.create_worktree`：brief 增加 `acceptance` 与 `acceptance_digest`。
- `scripts/agent_run.py`：prompt 增加 "Acceptance criteria" 段与第 7 条规则——`check` 要跑、`judge` 要用 file:line 论证，证据写进 `notes/<task>.md`。

**实测**（`acc-smoke` 临时仓库 + 6 个新测试）：

| 检查 | 结果 |
|---|---|
| `task new` 不带 `--acceptance` | exit 1，提示写明"nobody wrote down" |
| 带两条标准创建 | `schema_version=2`、`A1/A2` 解析正确、`acceptance_digest=sha256:...` |
| `loop --once` 后读 brief | `acceptance` 与 `acceptance_digest` 均在 |
| `agent_run.py --print-prompt` | 出现 "Acceptance criteria"、`A1 [judge] …`、`notes/T-0001.md` |
| 手改任务里的 criterion 再跑门禁 | `FAIL ledger acceptance_digest does not match the criteria`，exit 1 |
| `roadmap promote` | 任务的 A1 是 `check`，命令来自条目的 `accept` |

全量 121 个测试通过。

**已知边界**：`check` 类条目还没有进 gate 自动执行（L3）；评审仍是单条 verdict（L4）；`critic.py` 直接创建的 critic 任务没有 acceptance。这三条都写在 `docs/03` 7.4，不装作已完成。
