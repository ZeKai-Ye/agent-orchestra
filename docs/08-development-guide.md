# 08 · 长期开发指南

写给下一个改这份代码的人（也可能是三个月后的我）。目标：**加能力有固定动作，不做架构辩论；改决策有留痕，不靠记忆。**

## 1. 文档分层（各写各的，不混）

| 层 | 文件 | 什么时候改 |
| --- | --- | --- |
| 决策（冻结） | `adr/NNNN-*.md` | 做了一个会被后续代码依赖的选择时。改主意 = 新开 ADR 并附证伪条件 |
| 方案 | `docs/01`–`05` | 设计变化时。它们描述"应该怎样" |
| 事实 | `docs/07-implementation-log.md` | **每次合并都该动**：实现了什么、试了什么、放弃了什么 |
| 外部依据 | `docs/06-references.md` | 引入新参考或有新证据时；必须写明"支持/反驳了哪一条" |
| 路线 | `ROADMAP.md` + `docs/05` §3 | 里程碑状态变化时 |

规则：**代码行为变了但 07 没动 = 未完成**。这不是洁癖，07 是唯一能防止"下一个 agent 重走一遍死路"的东西。

## 2. 目录职责

```text
src/orchestra/          控制面本体（stdlib only，禁止引入第三方依赖，见 ADR 0002）
  cli.py                只做参数解析与输出，业务规则一律下沉
  ledger.py             任务正文与状态机（文件）
  leases.py             租约（事务）；多机替换点在这里
  policy.py             配置 + 风险 + 令牌
  gates.py              门禁编排；所有 git 访问都在这里
  store.py              SQLite schema 与事务原语（只被 leases/policy/gates 调用）
  fslock.py             OS 咨询锁；任务文件的临界区靠它串行化
  paths.py / clock.py   纯函数；不 import 其它模块
tools/agent_adapter.py  参考适配器（不属于包；只走 CLI 契约）
tests/                  一不变量一用例；用临时仓库，不 mock git
docs/                   方案与事实
adr/                    决策记录
examples/               可被测试当夹具的数据
scripts/verify.ps1      唯一入口，CI 也跑它
```

依赖方向单向：`cli → gates/ledger/leases/policy → store/paths/clock`。反过来 import 就是设计错误，别用 "只是临时" 说服自己。

## 3. 常见改动怎么做

### 3.1 加一个门禁步骤

1. 在 `gates.run_gate` 里加 `step(name, ok, detail)`，位置遵守"便宜且确定优先"。
2. 新增 `GateTests` 用例：一个通过、一个拒绝（拒绝要断言 `step["ok"] is False` 且 `detail` 可读）。
3. 若引入新的风险信号，在 `policy.detect_signals` 加信号 + 在模板 `[[risk.rule]]` 里登记。
4. 更新 `docs/04` 的门序表与 `docs/07` 的"当前实现"。

### 3.2 加一种租约模式

1. `leases.MODES` 加值；`acquire` 里决定它与别的模式的重叠规则（谁与谁能共存）。
2. 明确是否需要人签（照 `repo` 模式写），以及默认 TTL。
3. 用例必须覆盖：两种模式互相冲突/共存的四种组合、TTL 过期、回收后状态自愈。
4. 更新 `docs/03` §3 与 `docs/07`。

### 3.3 接一个 agent 适配器

适配器不属于控制面，放在使用方的仓库里。约定只有三条：

1. 读 `.orchestra/briefs/<task>.json`（任务、`lease_paths`、`gates.impl`、必需 trailer）。
2. 在指定 worktree 里干活，产出 diff；**不要**自己改 `ledger/` 以外的协调文件。
3. 成功后跑 `gate run --task ... --stage impl`，按退出码决定"换策略"还是"提交评审"。

适配器只允许依赖 CLI，不允许 import `orchestra.*`（否则内部重构会砸到所有适配器）。

### 3.4 改 schema

`ledger.SCHEMA_VERSION` 加 1，`validate` 里对旧版本给出可执行错误信息，写迁移函数并在 `task show` 时惰性升级。任何 schema 变更都需要 ADR——这是少数值得走流程的地方。

## 4. 测试规矩

- **一不变量一用例**，用例名直接写不变量（`test_same_path_is_exclusive`），不要写 `test_1`。
- 时间一律走 `FakeClock` 或 `ORCHESTRA_NOW`，禁止 `sleep`。
- git 用真实临时仓库（`RepoCase`），不 mock；`git diff` 的语义只能被真 git 固定住。
- 并发用例至少一次真多进程（`test_eight_agents_race_for_one_path`），线程级"并发"证明不了 SQLite 的行为。
- 涉及密钥的用例把 key 写到临时目录，绝不落进仓库。
- CI 顺序：`verify.ps1` → 全绿才合并；它同时是人的自检入口。

## 5. 发布

1. `CHANGELOG.md` 加 `## [x.y.z] - YYYY-MM-DD`，分类 Added/Changed/Fixed/Removed。
2. `pyproject.toml` 与 `src/orchestra/__init__.py` 版本同步。
3. 打 tag `vX.Y.Z`；小版本只加能力不改账簿 schema，改 schema 必须是大版本。
4. 发布说明里必须写"这一版会让哪些旧账簿/旧令牌失效"。

## 6. ADR 流程（三句话）

- 命名 `adr/NNNN-kebab-title.md`，从 `adr/template.md` 复制。
- 正文只写：背景、决定、被否决的选项、证伪条件（什么现象出现就该重开）。
- 通过后**不改正文**，要改就新开一篇并互相引用。

## 7. 上手顺序（约 30 分钟）

1. 读 `docs/01` §2（12 处修正）——知道为什么这样设计。
2. 跑 `scripts/verify.ps1`，读 `verify.ps1` 的 demo 段——知道系统长什么样。
3. 读 `leases.py` 的 `acquire` 与 `gates.run_gate` 的 `step` 序列——这是系统的心脏。
4. 读 `docs/07` §3（试过、改掉的）——避免重走七条死路。
5. 挑一个 `ROADMAP.md` 的 Next 项开工，开工前先写 ADR 或先写失败的用例。
