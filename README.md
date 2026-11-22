# agent-orchestra

多 Agent 协作开发的**协调控制面**：不改模型、不写 agent 循环，只负责一件事——让几十个并发的 CLI agent（Claude Code / Codex / PI-Desktop 等）在同一仓库里互相不踩、不重复、不越权，并且每个合并都能被机械验证。

本仓库同时是一份**可行性审核 + 工程化方案**（`docs/`）和一个**可运行的骨架**（`src/`），骨架实现了原方案里最贵也最关键的三件事：任务账簿、路径租约、机械门。

## 结论先说

原方案方向成立，可直接落地，但有 12 处必须修正、3 处应当否决、8 类风险被遗漏：

- §7 吞吐公式错：漏掉主干合并串行化与评审/人签供给，`槽位/时长×(1−失败率)` 不是瓶颈模型。
- 租约写进仓库锁文件不可行：单写热点 + 合并冲突 + 没有原子 CAS。
- 方案缺控制面：50 个 agent 各自轮询 forge API 会打爆配额，也无法公平调度。
- 相位顺序错：租约属于 P0；不可逆权限必须从 P0 起就不在 agent 手里，而不是 P4 才补。

详见 [docs/01-feasibility-review.md](docs/01-feasibility-review.md)。

## 快速开始

环境：Python 3.11+、git。**零第三方依赖**（stdlib `sqlite3` / `argparse` / `tomllib` / `hmac` / `unittest`），无需安装即可跑。

```powershell
# 被测仓库：初始化账簿与控制面配置
$env:PYTHONPATH = "$PWD\src"
python -m orchestra.cli --repo D:\work\myrepo init
# 把 init 产物（config/orchestra.toml）先提交：它和其他文件一样需要 review，
# 未提交时第一个门会把它算成"越界改动"。

# 人类签字的方向（intent）放进仓库，未签字不能合并
#   ledger/intents/I-0001.md ->  ---  signed: true  author: kai  ---

python -m orchestra.cli --repo D:\work\myrepo task new --title "修复配置加载的空指针" `
  --role patch --domain core --lineage lineage-a --intent I-0001 --paths src/core

# agent 领取任务：拿租约 + 状态迁移（WIP 上限、指纹查重都在这步）
python -m orchestra.cli --repo D:\work\myrepo task claim --task T-0001 --holder agent-7
python -m orchestra.cli --repo D:\work\myrepo task transition --task T-0001 --to in_progress --actor agent-7

# worktree + 任务简报（brief.json 里写死了租约路径与必须跑的门）
python -m orchestra.cli --repo D:\work\myrepo worktree create --task T-0001

# 机械门：账簿 → 状态 → intent → diff → scope → 风险 → 配置检查 → 债务 → 溯源
python -m orchestra.cli --repo D:\work\myrepo gate run --task T-0001 --stage impl

# 不可逆/高风险改动必须带人类令牌（HMAC、绑定 diff 摘要、一次性）
python -m orchestra.cli --repo D:\work\myrepo approval keygen
python -m orchestra.cli --repo D:\work\myrepo approval issue --kind merge --by kai `
  --subject sha256:xxxx --prediction "回滚率不升" --rollback "git revert HEAD"
```

退出码即契约：`0` 通过、`1` 拒绝、`2` 用法、`3` 租约冲突、`4` 需要人签。

## 本骨架真正强制的不变量

1. 同一路径同一时刻至多一个写租约（`read` 可共享）。
2. 状态迁移只走白名单边，非法迁移直接拒绝。
3. `merged` 必须绑定已签字 intent。
4. T2 无有效令牌则门禁必失败（令牌一次性、绑定 diff 摘要、带过期）。
5. `git diff` 路径必须 ⊆ 租约路径，否则 scope guard 拒绝。
6. 尝试次数超预算 → `escalated`，不许无限重试。
7. 租约过期可被回收，任务自动回到 `proposed`（死 agent 不会占着路径）。

## 目录

```text
agent-orchestra/
├─ docs/                                # 审核 + 工程方案（先读 01）
│  ├─ 01-feasibility-review.md           # 12 处修正 / 3 处否决 / 8 类遗漏风险
│  ├─ 02-architecture.md                 # 控制面/执行面/证据面、组件契约、单机边界
│  ├─ 03-data-model-and-protocol.md       # 实体字段、状态机、租约算法、7 条不变量
│  ├─ 04-gates-risk-security.md           # 门禁清单、风险规则、审批令牌、沙箱与供应链
│  └─ 05-metrics-and-roadmap.md           # 指标字典、容量模型、M0–M7 里程碑
├─ src/orchestra/
│  ├─ cli.py                             # 子命令门面（退出码是契约）
│  ├─ ledger.py                          # 任务账簿（一任务一 JSON）+ 状态机
│  ├─ leases.py                          # 路径租约：有序加锁、TTL、回收、快照
│  ├─ policy.py                          # 配置 + 风险分级 + 人签令牌
│  ├─ gates.py                           # 机械门 + diff scope guard + git helpers
│  ├─ store.py                           # SQLite(WAL) 事务与控制面表
│  ├─ paths.py / clock.py                # 路径代数 / 可注入时钟
│  └─ templates/orchestra.toml           # `orchestra init` 写入被测仓库的配置
├─ tests/                                # unittest：并发抢租约、状态机、风险夹具、全链路
├─ examples/task.example.json            # 任务账簿样例
└─ scripts/verify.ps1                    # 一条命令：跑测试 + 建示例仓库跑真实门禁
```

## 测试

```powershell
powershell -File .\scripts\verify.ps1
# 或
$env:PYTHONPATH = "$PWD\src"; python -m unittest discover -s tests -t . -v
```

37 个用例，其中最关键的一条是**8 个进程同时抢同一路径**：恰好 1 个成功、7 个退出码 3。这是"写租约能防互踩"的直接证据，不是设计口号。

## 边界（本轮不做，避免假交付）

真实分片 CI runner、契约消费者测试执行器、mutation 引擎、OTel 导出、多主机租约（Postgres）、具体模型适配器、Web 签字台。它们已经写成 [docs/05-metrics-and-roadmap.md](docs/05-metrics-and-roadmap.md) 里的 M3–M7，不在代码里装样子。

单机假设：SQLite 一份文件，多容器同主机可用；多主机要换 Postgres，替换点是 `leases.LeaseManager`。

## 状态

开发中（骨架 + 方案）。技术栈：Python 3.12 / SQLite(WAL) / git worktree / PowerShell 脚本。
