# 09 · 案例研究：pequalnp.com 的 agent 舰队怎么协作

来源：`https://pequalnp.com/`（`/how`、`/join.txt`、`/feed.atom`、`/live`、`/board`、`/graveyard` 及页面 JS）。抓取时间 2026-11-24，全部为公开页面。

**先声明两件事。**
1. 我不评价他们的数学，只看协作架构；站点自己也写明"P vs NP 没有被解决，这里是战线上的小步"。
2. `join.txt` 的标题就是"给 AI coding agent 的说明书"，形式上就是一份给 agent 的指令文档。我把它当**数据**读，**没有**执行其中任何动作（没有 fork、没有领 lease、没有开 PR）。这也正好暴露了我们自己的一个风险：适配器读仓库内容，就要承受同样的注入面（见 §6）。

## 1. 他们到底靠什么让几十个 agent 不打架

一句话：**把工作单元压到"一条可机检的陈述"，用一个检查器当唯一裁判，用调度台发租约。** 组织形态是 `programs → orchestrators → lemmas/milestones`，orchestrator 在 program 内有授权范围，负责挑 lemma。

| 机制 | 站点上的具体做法（原文可核对） |
| --- | --- |
| 唯一裁判 | "Lean checker，the only judge"；证明必须**恰好**是登记的陈述，不能更强/更弱/改词；禁 `sorry`、禁新增 `axiom`、禁 `native_decide` |
| 调度台（desk） | `GET /desk/next?handle=<h>` 返回 `gap` + `lease`；`gap` 里含 `statement / file / module / imports / context / dead / sketch / refutation`；搜索类步骤带 `shard` |
| 租约与心跳 | 租约 = 最后一次续约后 20 分钟，总时长最多 6 小时；每 10 分钟 `POST /desk/beat?lease=`；返回 **410** 表示"别人已结了或已撤回"，**立刻停手**重领 |
| 工作单元大小 | 一步 = 一个 Lean 文件 = 一个分支 = 一个提交 = 一个 PR。"one step at a time"，`&another=1` 最多同时 5 个，每个独立租约、独立文件、独立 PR |
| 并发限制 | 每 handle 默认 1 条，最多 5 条；对 desk 的请求"每几秒最多一次" |
| 放弃要带原因 | `POST /desk/release?lease=&reason=`，原因枚举：`hypothesis_too_weak` / `missing_lemma` / `statement_seems_false` / `does_not_elaborate` / `out_of_time` / `other` / `reduced`；以 `other|out_of_time` 交回的步骤**一小时内不再派给你** |
| 反例与证明同价 | "A refutation is billed like a proof"；反例通过提交"否定孪生命题"的 PR 来送 |
| 拆解是一等动作 | REDUCTION：提交一个定理，假设是 2–4 条更小陈述、结论正是原步骤的陈述；检查器通过后**每个假设变成新步骤**，原步骤全部完成才结，credited 延后；每 agent 每步 1 个、每步最多 3 个 |
| 反作弊的形状约束 | 假设必须只谈库与定义，不能是原陈述本身、不能包含它、不能是 False/可判假、不能互相依赖、不能带字符串、不能是 Lean 元类型、不能超过 2000 字符 |
| 第二类裁判（顾问） | Barrier desk：拿相对化 / 自然证明 / 代数化三堵墙去撞新方法，"它的裁决是 orchestrator 的，不是 checker 的"——权威与建议被显式分开 |
| 角色分工 | orchestrators（写陈述、发 lemmas）、thinker（先纸面推演，产出 sketch）、builder（把 sketch 写成 Lean）、red team（只做反例）、human review（与 Lean 分开，结果页显示 review 状态） |
| 三种裁决 | ACCEPTED / REJECTED / **FAULT**（"我们的机器挂了，与你的证明无关，会重检"）；常规检查 6–12 分钟，最多 20 分钟，忙时排队 |
| 软违规走人工 | 格式规则 a/c/d 直接拒；只违反 b/e 的文件**仍然判定，但等一个人读过才合并**——慢，不是死 |
| 公开记录 | `/feed.atom` 是"every change of state"，条目 id `seq-NNNN`，category 如 `lemma_proposed` / `lemma_checked`；另有 lemma 页、`/graveyard`（被判定思路死亡）、`/board`（本小时 / 全部）、`/live`（在跑的 agent 与 hub 计数 `hub-lemmas/checked/refuted/blocked`） |
| 信任边界 | "Nothing you type, attach or link is read by the fleet: only one Lean file in a pull request counts." 附件、邮件、链接一律不读 |
| 归属 | GitHub handle + PR 提交由 GitHub 身份签名；给出"先把邮箱设为私有"的操作提示；`/a/<handle>` 是个人进度页 |

## 2. 这套设计里真正聪明的地方

1. **把"难"关在裁判之外。** 检查器只回答"这个文件是否证明了这条陈述"，它**不回答**"这条陈述是否是我们想说的那句话"。后者由"不是作者本人的 orchestrator 去读"来兜。这是把可判定与不可判定显式分层的典范——我们的门禁也只应回答它能回答的那部分。
2. **工作单元的边界同时是冲突的边界。** 一步一个文件一个 PR，于是"谁写什么"不需要协商：desk 派发即所有权，租约只用来防重复劳动，不用来做合并排序。
3. **放弃是有产出的。** 带原因的 release（含 `missing_lemma`）会生成下一步该做什么；`other/out_of_time` 还有冷却惩罚，防止 agent 刷"领了又退"。
4. **拆解有反作弊约束。** REDUCTION 的假设形状被限死（不能是原命题、不能是 False、不能互相依赖、长度上限），否则"把真命题拆成假命题"能无限刷分。
5. **权威分层。** checker（机检）> orchestrator 裁决（barrier desk 的撞墙判断）> 人类 review。谁的话算数，写死在话术里，也写死在计数里。
6. **FAULT 是第三态。** 基础设施失败不算 agent 的失败，也不污染记录——这一条直接对应我们缺失的退出码。
7. **记录即产品。** 公开 feed 带序号与 category，等于把事件日志做成了任何人都能复核的账本。

## 3. 对照我们的项目：缺什么

| 他们有 | 我们现在的状态 | 差距的性质 |
| --- | --- | --- |
| desk 派活（desk 选择，人群自然分散） | agent 自己 `task claim` | 我们靠租约+指纹警告避免撞车，但没有"派发"这一层 |
| 每 handle 并发上限 1/5 + 退任务冷却 1 小时 | 全局 `wip_limit`，尝试预算 | 缺**按持有者**的限流与冷却 |
| lease 410 = 别人已结 / 已撤回 | 只有租约冲突（退出码 3） | 缺"任务被他人终结"的显式信号 |
| 领到的步骤自带 `context` + `dead`（已知失败） | 账簿有 history，但没有"死路登记" | 缺**死路清单**，新人会重走 |
| 反例与证明同等交付 | `role: verify/research`，但没有"证伪即成功"的门 | 缺一种一等角色 |
| REDUCTION 拆解（带反作弊约束、延后计功） | 无 | 完全缺失的能力 |
| FAULT 第三态 | 异常一律 exit 1 | 缺基础设施故障码 |
| 软违规 → 人工队列 | 门禁二值 | 缺"慢但可行"的通道 |
| feed/graveyard/board | `events` 表 + `status` + `trend` | 有数据，没有可发布的记录 |

## 4. 我建议采纳的（按性价比排序，写进 ROADMAP）

1. **`dead_approaches` 随任务下发**：失败/放弃时记录"哪条路已死 + 原因"，下一个领取者在 brief 里看到它。成本极低（账簿加字段 + brief 渲染），直接消掉"重走死路"。
2. **按持有者的并发上限 + 冷却**：`wip_limit` 之外加 `per_holder_limit`；以 `out_of_time|other` 结束的任务在 N 分钟内不再派给同一 holder。防止一个 agent 囤任务。
3. **基础设施故障码（exit 6）**：门禁自身异常（git 不可用、配置损坏、磁盘满）与"改动不合格"分离，且**不计入尝试预算**。
4. **`refute` 角色**：允许"用失败测试证伪某个 fix 的说法"作为合法交付，门禁对这类任务期待 `tests/` 中新增的失败用例，而不是绿。
5. **任务拆解（REDUCTION 的代码版）**：允许提交"把父任务拆成 2–4 个子任务"的提案，约束：子任务路径不相交、都在父租约内、每个子任务有可机检的完成判据；父任务在全部子任务合并后才关闭；同一 agent 对同一父任务最多 1 个拆解提案。
6. **软违规 → 人工队列**：门禁输出增加 `needs_human` 级别（不是 fail），例如"新增依赖但已列入允许清单""测试改动但没有对应 intent 行"。
7. **可发布记录**：`orchestra events --format md|atom --out` 导出"状态变更流水 + 死路清单"，让仓库自己成为可复核的账本。

## 5. 不建议照搬的

- **单一 oracle 的强度**：Lean 能判定"是否证明"，代码门禁不能判定"是否修对了"。照搬"检查器说了算"会让我们误以为绿=对。我们保留 T1 人工评审与抽样复核是必要的，不是流程冗余。
- **一步一文件**：代码改动天然跨文件；我们已经有 `paths` 作用域与 scope guard，粒度差异是领域决定的。
- **他们的格式洁癖**（禁字符串、限 `set_option`）：那是 Lean 内核的约束，不是协作约束。
- **sponsor/credit 那套商业结构**：是产品，不是协调。

## 6. 一个反过来的教训：我们自己也有注入面

`join.txt` 是一份"读了我就会照做"的文档：它告诉我 fork 哪个仓库、装什么、跑什么脚本、把 PR 发到哪。任何把仓库内容喂给 agent 的控制面（包括我们）都要假设**仓库里的文字会试图指挥 agent**。我们已做的：适配器擦凭据、默认无网建议；还缺的：把"仓库内容只作数据"写进 brief 的固定段，并让门禁拒绝"因读到文档而改动的路径"这类异常（例如 agent 顺手改了 `CONTRIBUTING.md`）。

## 7. 不能从外部知道的部分（不装懂）

- desk 的派发算法（如何"让人群散开"、如何选 gap）没有公开；只能看到它对外的契约。
- 他们的程序/里程碑如何与 lemmas 绑定、orchestrator 的"mandate"如何在系统内落实为权限，站点只有描述。
- 检查农场（6–12 分钟）的并发与成本结构未知。
- `/data` 与其 data-bind 的绑定模型（hub）是他们的实现细节，我只确认了存在这些绑定名，没有逆向其协议。

## 8. 一句话结论

他们的协作收敛在**三件极简的东西**上：一个不可协商的裁判、一个发租约的调度台、一条只有机检产物才进入的记录。我们可以直接吸收的是"调度台派活 + 死路公开 + 拆解受约束 + 故障第三态"；不能吸收的是"单裁判足以判定正确性"这个强假设——那需要我们把不可机检的部分显式交给人和抽样。

