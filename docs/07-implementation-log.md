# 07 · 实现记录：做了什么、试过什么、放弃了什么

这份文档是**流水账**，不是方案。方案在 01–05，这里只记录事实：哪一行代码真在跑、哪个想法试过、哪个想法被自己推翻。目的是下一次改动前不用重新踩一遍。

版本：v0.1.0（提交 `1a1e943 chore: initial scaffold`，本文件为后续提交）。

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
- 任务文件的并发写保护（两个进程同时改同一任务会丢更新）。当前靠"一任务一持有者 + 租约"避免，属已知薄弱点。
- 跨仓库协作、子模块、monorepo 特化。

## 5. 已知限制（会在下一版暴露的）

| 限制 | 触发条件 | 计划 |
| --- | --- | --- |
| SQLite 单机 | 多台机器跑 agent | M2 换 Postgres，接口已 isolate 在 `LeaseManager` |
| HMAC 对称密钥 | 需要"人持有私钥、机器只验签" | M5 换 Ed25519，令牌格式已版本化（`oap1.`） |
| 关键词级风险信号 | 想要 AST 级判断（如"删了断言但保留了 test 名"） | M6 接 tree-sitter |
| 无故障注入 | 想知道 agent 崩溃/断网时是否自愈 | M2 加 chaos 用例（kill -9 后 `lease reap`） |
| 指标只有快照 | 想看趋势 | M2 把 `status --json` 落成 JSONL 时间序列 |

## 6. 复现本文所有结论

```powershell
powershell -File .\scripts\verify.ps1        # 37 用例 + 真实门禁演示
$env:PYTHONPATH = "$PWD\src"
python -m unittest tests.test_core.LeaseTests.test_eight_agents_race_for_one_path -v
python -m unittest tests.test_gates -v
```
