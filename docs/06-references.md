# 06 · 参考架构与论文

检索方式（可复现）：arXiv API `export.arxiv.org/api/query?search_query=...&sortBy=relevance`；GitHub Search API `api.github.com/search/repositories`。检索时间 2026-11-22。

**阅读深度声明**：arXiv 条目为标题 + 摘要级核对（未逐篇精读全文），GitHub 项目为描述 + star 级核对。凡是"我认为"的判断都标注了依据；没有依据的地方写"待验证"，不装作读过。

## 1. 多 Agent 软件工程：经验与失败分析

| 来源 | 一句话 | 对我们的意义 |
| --- | --- | --- |
| [2608.11965](https://arxiv.org/abs/2608.11965) *Developing LLM-based Multi-Agent Systems in Software Engineering: A Mixed-Method Experience Report* | 多 agent SE 系统的混合方法经验报告 | 支持审核 §2.3：真正的困难在协作与流程，不在单个 agent 智力 |
| [2608.24271](https://arxiv.org/abs/2608.24271) *Observability and Fault Injection for LLM-Based Multi-Agent Systems in Software Engineering* | 多 agent 的**可观测性与故障注入** | 直接支持被遗漏风险 #4：没有事件面就无法定位失败；我们的 `events` 表是这条的最小实现 |
| [2607.09510](https://arxiv.org/abs/2607.09510) *Failure as a Process: An Anatomy of CLI Coding Agent Trajectories* | CLI coding agent 轨迹的失败解剖 | 支持"按阶段失败信号归档"（我们的 `failure_signature` 指纹字段），也说明"重试"必须有预算 |
| [2607.03691](https://arxiv.org/abs/2607.03691) *Don't Blame the LLM: How Agent Harness Evolution Shapes Coding Agent Quality* | 效果很大程度取决于 **harness** 而非模型 | 支持我们的定位：控制面/harness 是独立可优化的层，不应被模型评测绑架 |
| [2510.03463](https://arxiv.org/abs/2510.03463) *ALMAS* / [2604.13120](https://arxiv.org/abs/2604.13120) *AgentForge* / [2602.01465](https://arxiv.org/abs/2602.01465) *Agyn* | 端到端多 agent SE 框架（角色分工、执行接地、团队化） | **反向提示**：这些框架都在做"角色 + 编排 + 执行"，与我们的 L2 工人层重叠；我们刻意只做协调与门禁，不重做 agent 循环 |

## 2. 合并队列、CI 吞吐与主干健康

| 来源 | 一句话 | 对我们的意义 |
| --- | --- | --- |
| [uber/submitqueue](https://github.com/uber/submitqueue) | 高性能 **speculative merge queue**，规模化保持主干常绿 | 直接支持审核 2.1：吞吐瓶颈在主干合并串行化，解法是批次 + 投机验证，不是加 CI 槽位 |
| [bors-ng/bors-ng](https://github.com/bors-ng/bors-ng) | GitHub PR 合并机器人（batch + 排队） | 批次合并的成熟先例；也是"不要把逐条合并当默认"的证据 |
| [funador/claude-code-merge-queue](https://github.com/funador/claude-code-merge-queue) | **面向并行 Claude Code agent 的本地合并队列** | 最接近本项目目标的既有实现（先验证 M1 是否够用再自研 M3） |
| 商业 merge queue（Graphite.dev / Aviator 等） | 商业产品，机制详见其文档 | 说明批次合并是真实需求；**本轮未取得可核对的公开实现细节**，不作技术依据 |

## 3. Agent 运行时、沙箱与并行执行

| 来源 | 一句话 | 对我们的意义 |
| --- | --- | --- |
| [SWE-agent/SWE-ReX](https://github.com/SWE-agent/SWE-ReX) | 大规模并行的沙箱化代码执行 | 支持执行面"一任务一隔离环境"；我们用的是 git worktree（更轻），沙箱留给 runner 容器 |
| [perplexityai/SPACE](https://github.com/perplexityai/SPACE)、[abshkbh/arrakis](https://github.com/abshkbh/arrakis) | 可插拔运行时 / 自托管 agent 沙箱 | 支持审核遗漏风险 #1：默认无网 + 工具白名单是运行时的责任，控制面只发最小权限 |
| [SWE-agent/mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent) | 100 行的极简 coding agent | 支持"M1 先薄后厚"：agent 侧不需要我们发明东西，控制面才是缺口 |
| [omnigent-ai/omnigent](https://github.com/omnigent-ai/omnigent) | 编排 Claude Code / Codex / Cursor / Pi 的 meta-harness | 与我们的集成契约（brief.json + 收 diff）同构；说明"适配器模式"是社区共识 |

## 4. 可验证性：测试选择、变异测试与基准效度

| 来源 | 一句话 | 对我们的意义 |
| --- | --- | --- |
| [2605.25356](https://arxiv.org/abs/2605.25356) *Names Are All You Need: Effective and Safe Regression Test Selection for Python* | 基于名称的 RTS，对本项目最贴边的分片 CI 依据 | 支持"按依赖图只跑受影响子集"，且给了可落地的 Python 方案（M3） |
| [2211.07782](https://arxiv.org/abs/2211.07782) *Test Impact Analysis on the Integration Level* | 集成层 TIA | 同上；两级集成下需要按模块边界做影响分析 |
| [2108.12469](https://arxiv.org/abs/2108.12469) *LaForge: Always-Correct and Fast Incremental Builds* | 增量构建的正确性形式化 | 支持"分片 CI 的正确性必须可证明，否则就是随机跳过测试" |
| [2404.13295](https://arxiv.org/abs/2404.13295) *Detecting Build Dependency Errors in Incremental Builds* | 依赖图错误会静默产生错误结果 | 提醒：分片 CI 的失败模式是**漏跑**，不是报错——需要依赖图自身的检查（M3 验收项） |
| [2404.09952](https://arxiv.org/abs/2404.09952) *LLMorpheus* / [2609.35841](https://arxiv.org/abs/2609.35841) | LLM 驱动的变异测试/变异体生成 | 支持审核 2.8：用**抽样** mutation score 当"行为锁"代理指标，而不是要求全量 |
| [2506.09289](https://arxiv.org/abs/2506.09289) *UTBoost*、[2410.06992](https://arxiv.org/abs/2410.06992) *SWE-Bench+*、[2505.23419](https://arxiv.org/abs/2505.23419) *SWE-bench Live* | 基准本身存在弱测试与数据泄漏 | **警示**：门禁靠"测试通过"就够了的假设很脆——测试可能本身就假。这支持"实现方不产验收判据"与抽样复核 |

## 5. 独立性与多样性（评审为什么不能同源）

| 来源 | 一句话 | 对我们的意义 |
| --- | --- | --- |
| [2410.03953](https://arxiv.org/abs/2410.03953) *LLM-TOPLA* | 通过**最大化多样性**提升集成效果 | 支持审核 1.1 的"独立性只能来自证据面差异"，并给出可操作度量（多样性而非"换模型"） |
| [2607.17384](https://arxiv.org/abs/2607.17384) *Quantifying Diversity of Thought* | 多样性对集成增益的定量规律 | 支持"异质评审"要有指标：我们观测的反对率就是它的廉价代理 |
| [2507.19115](https://arxiv.org/abs/2507.19115) *Automated Code Review using LLMs at Ericsson: An Experience Report* | 工业界 LLM 评审经验 | 支持评审输入面隔离：给推理链会污染独立判断；也提示评审者会"表扬式通过" |

## 6. 协作冲突与合并冲突

| 来源 | 一句话 | 对我们的意义 |
| --- | --- | --- |
| [1907.06274](https://arxiv.org/abs/1907.06274) *Predicting Merge Conflicts in Collaborative Software Development* | 合并冲突可预测（路径/作者/历史特征） | 支持"冲突从源头避免（租约）优于事后解决（git）"；也说明路径重叠是有效特征 |
| [2609.21146](https://arxiv.org/abs/2609.21146) | 分批-合并队列的排队论精确分析 | 支持容量模型：合并队列该用排队论建模，而不是 `槽位/时长` |

## 7. 外部结论对我们方案的 6 条修正

1. **先测后建**：`funador/claude-code-merge-queue` 已经做了"并行 agent 的本地合并队列"。M3 之前应先拿它当基线，避免重复造轮子（引擎不同但语义同构）。
2. **投机合并是吞吐解**：`uber/submitqueue` 表明主干瓶颈靠批次 + 投机验证解决，这与审核 2.1 一致，且证明我们"逐条合并"的否决是对的。
3. **可观测性是一等公民**：`2608.24271` 说明缺可观测性的多 agent 系统无法调试；我们已经把 `events` 当唯一指标来源，但**还缺故障注入能力**（新里程碑）。
4. **测试可能是假的**：UTBoost / SWE-Bench+ / SWE-bench Live 都指出弱测试与泄漏。因此"机械门通过"必须配"抽样人工复核 + 变异抽样"，否则门禁只会训练出通过率。
5. **多样性要度量，不要信仰**：LLM-TOPLA 与 diversity-of-thought 给了可测的代理量；我们的"评审反对率"是最便宜的指标，但需要基线。
6. **增量构建的正确性是硬问题**：LaForge / 依赖图错误检测说明分片 CI 会**静默漏跑**。M3 必须自带"依赖图自检"，这是原方案没有的验收项。

## 8. 与既有框架的分工（避免重复建设）

| 层 | 谁去做 | 我们做什么 |
| --- | --- | --- |
| 模型 / 工具循环 | Claude Code、Codex、mini-swe-agent、omnigent | 不做 |
| 沙箱 / 执行隔离 | SWE-ReX、SPACE、arrakis、容器 | 只定义"brief 里要写什么、收什么" |
| 合并队列 | submitqueue、bors-ng、Graphite | M3 先接现成，不够再自研 |
| **协作契约、租约、门禁、令牌、审计** | **没有现成等价物** | **这是我们的位置** |

最后一格是这份方案存在的理由：上面三层都在解决"怎么干活"，没有一层解决"几十个并发工人怎么不互相踩、谁有权做什么、合并凭什么算通过"。

## 9. 待验证（不装作读过）

- 商业 merge queue（Graphite、MergeQueue、Aviator）的确切机制与限流行为：需实际接入才能确认。
- `2608.*` 系列为预印本，方法细节与可复现性未审稿，引用只作方向性支持。
- 尚无"多 agent 并发写同一仓库"的公开对照实验能给出"路径租约 vs 合并队列"的定量收益；这条只能靠我们自己的 `lease.expired` / 冲突率指标逐步验证。
