# 11 · 自主轮次：把 ROADMAP 变成可驱动的队列

目标：**人只给三个信封**（跑几轮、多少预算、多少小时），机器自己完成"验收上一轮 → 研究并扩展 Later → 确认新 Later 可行 → 提拔一个进 Next → 干活"。

这一版（v0.4.0）把这套流程做成了 `orchestra research`；`ROADMAP.md` 继续是给人看的叙事，机器队列在 `roadmap/items.jsonl`。

## 1. 两个介质，一条不变量

| 介质 | 谁写 | 用途 |
| --- | --- | --- |
| `ROADMAP.md` | 人（或人审过的 agent） | 叙事、理由、排序的"为什么" |
| `roadmap/items.jsonl` | 引擎 + 人 | 队列：每行一个条目，机器只认这个 |
| `roadmap/STATE.md` | 引擎 | 每轮流水，覆盖写 |

不变量：**条目只有通过 `roadmap accept` 才能变成 `done`**，而它要求 (a) 有引用该条目的 `merged` 任务，(b) 条目自己的验收命令退出 0。谁都不能自己给自己判 Done——这一条把"checkbox 文化"挡在门外。

## 2. 条目形状

```json
{"id":"R-0012","status":"later","title":"评估现成 merge queue","accept":"python -m unittest tests.test_merge_queue",
 "scope":["docs/12-merge-queue.md"],"size":"S","origin":"research:round-3"}
```

`status`：`later → next → done`（或 `dropped`）。`accept` 必填且必须能在仓库里跑；`scope` 必填（决定租约与 gate 的 scope guard）；`size` 只影响可行性打分。

## 3. 一轮的五步

```
1 accept   上一个 next 条目若有 merged 任务 → 跑它的验收命令；不过 → 停，交给人
2 triage   对每个 later 条目做三查 + 打分，写回 feasibility
3 promote  只挑一个可行条目 later → next，并建任务（paths=scope, roadmap=id）
4 work     跑一个 loop 轮（agent 改代码、门禁、review）
5 record   追加 roadmap/STATE.md + 指标快照 + 预算扣 1
```

对应命令：`orchestra research --rounds N --budget B --hours H --intent I-0001 --agent-cmd "..."`。

## 4. 可行性三查（机械、便宜、可解释）

| 检查 | 判据 | 失败含义 |
| --- | --- | --- |
| `accept-runnable` | 验收命令的第一个词可解析（内置 `python/pwsh/git` 或 `shutil.which`） | 条目写了个跑不起来的命令 |
| `scope-exists` | 每个 scope 路径或其父目录存在 | 条目指向还不存在的文件 |
| `scope-free` | 不与当前 `next` 条目的 scope 重叠 | 两个条目会抢同一片路径 |

`score = 0.4·runnable + 0.4·scope-exists + 0.2·scope-free`，`L` 尺寸 −0.1；**可行 = 前两项都过且 score ≥ 0.6**。不可行就绝不提拔——这是"确认新 later 可行"的落地方式，不是靠人读一遍。

## 5. 人的控制面

- `--rounds N`：最多几轮；`--budget B`：每轮扣 1 单位，超了就停；`--hours H`：墙钟上限。
- `orchestra stop`：kill switch，下一轮开始前退出。
- 任何一步"需要人"（验收不过、没有可行条目、队列结构不合法）都会以 `needs_human` 停下并**不**继续推进——宁可停，不要瞎推。

## 6. 用法

```powershell
# 人：写队列（或让 agent 写）
python -m orchestra.cli --repo . roadmap add --title "评估现成 merge queue" `
  --accept "python -m unittest tests.test_roadmap" --scope docs/06-references.md --size S
python -m orchestra.cli --repo . roadmap check
python -m orchestra.cli --repo . roadmap triage

# 机器：按信封跑
python -m orchestra.cli --repo . research --rounds 3 --budget 6 --hours 2 `
  --intent I-0001 --agent-cmd "python my_agent.py --brief {brief}"
```

## 7. 这一版没有做的（边界）

- **没有"研究 agent"**：它不会自己读代码提出新 Later；研究任务仍是 agent 的活，产出写进 `items.jsonl`（用 `roadmap add --origin research:round-N` 标记来源）。
- **不并行**：一次只有一个 `next`，这是刻意的（避免抢占 scope，也让验收顺序确定）。
- **不自动改 `ROADMAP.md` 叙事**：机器只动队列与 STATE.md，叙事由人合并，防止机器改写"为什么"。
- **可行性是必要条件不是充分条件**：三查只看"跑得起来"，看不出"值不值得做"。价值排序仍要人（或后续用指标驱动）。

## 8. 测试

`tests/test_roadmap.py` 15 例，覆盖：队列校验、重复 next 拒绝、不可行条目不提拔、一次只一个 next、验收必须绑定 merged 任务、验收命令失败不改状态、一轮 research 的完整流转、stop/budget 停止、上一轮先验收再提拔。全量 `Ran 80 tests ... OK`。

## 9. v0.4.1 增补：research 与 critic 两个交付物角色

队列条目本身也有盲点：**三查看不出"判据是空的"**。一次实测里，`accept: python -c "print(1)"`、`scope: docs` 的条目拿了 1.0 分，完全可行——但它证明不了任何事。所以补两种任务角色，各自有门禁能读的交付物：

| 角色 | 必须产出 | 门禁判定 | 后续动作 |
| --- | --- | --- | --- |
| `research` | `roadmap/proposals/<task-id>.md`，每条 `title/accept/scope/size` 一块 | 缺文件或块缺 `accept`/`scope` → `deliverable` 步失败 | `roadmap import` 把合法块变成 `later` 条目，非法块记为 rejected |
| `critic` | `reviews/<task-id>.md`，含 `verdict(target/reason/evidence)` | verdict 不在 `refuted|confirmed`、缺 target、反驳缺 evidence → 失败 | `orchestra review --task` 应用结论 |

**反例的力量在最后一步**：被 `refuted` 的任务写进 `review_verdict=refuted`，`→ merged` 直接被状态机拒绝（"answer the refutation first"）；被 `refuted` 的条目标为 `dropped`，三查再也不会提拔它。也就是说，反驳不是评论区，是闸门。

### 两轮实测（本仓库，2026-11-24）

| 轮次 | 任务 | 产物 | 门禁 | 结果 |
| --- | --- | --- | --- | --- |
| 1 | `T-0008` research | `roadmap/proposals/T-0008.md`：一条真判据 + 一条空判据 | `deliverable` PASS（1.0s）→ review | `roadmap import` 生成 `R-0003`、`R-0004`；三查给两条都打 1.0（**盲点**） |
| 2 | `T-0009` critic（target `R-0004`） | `reviews/T-0009.md`：`refuted` + evidence="打印 1 不可能失败" | `deliverable` PASS（0.98s）→ review | `review` 应用 → `R-0004` 变 `dropped`，三查池里只剩 R-0001/2/3 |

这两轮说明的是机制而不是智能：*空判据能骗过三查，但骗不过一个被要求给反例的独立任务；而它的反驳会直接改状态机，不会停在评论里*。

### 仍然是人的部分

- 谁来做 critic：目前由编排者（人或调度脚本）创建 `--role critic --target ...` 的任务。自动为每个 `next` 条目配一个 critic 是下一步（需要先定义"够不够、换不换人"的成本规则）。
- `confirmed` 只是记录，不等于通过：条目仍需 `roadmap accept`（merged 任务 + 判据）才能 `done`。
- 反例质量取决于 critic 的能力；机制只保证"反驳一定有证据、一定有后果"。
## 10. v0.4.2：critic 自动配对与轮换

上一节里 critic 还得靠人创建。现在由引擎自动配：

| 触发 | 规则 |
| --- | --- |
| 条目进队列（research 导入或人工 `roadmap add`） | 每个 `S/M/L` 条目配一个 critic（`[critic] required_sizes` 可关掉某个尺寸） |
| 任务到达 `review` 且带 `roadmap` 或 `role=research` | 为这个任务配一个 critic（target = 任务号） |

**轮换规则**：批判者来自 `[critic] pool`（默认 `critic-a`/`critic-b`）；同一持有者**连续两次 `confirmed`** 后跳过它（`max_confirm_streak = 2`），换下一个；一次 `refuted` 立刻把 streak 归零（说明这个人真的在干活）。池子全饱和时仍会配对，但结果里标 `rotated=true`，也就是"没人可换了，且在硬上"。

状态在 SQLite 的 `critic_stats(holder, confirm_streak, last_verdict, last_at)`，`review --task` 应用判定时更新；`critic status` 可查池子与开着的批判任务。

### 一次真实回合（本仓库，2026-11-24）

| 步骤 | 结果 |
| --- | --- |
| `roadmap promote --item R-0005 --intent I-0001` | 建任务 `T-0011`，`R-0005 → next` |
| `research --rounds 1 --agent-cmd worker` | 门禁全绿 → `review`（1.0s） |
| 自动配对 | **`T-0012` critic → target `T-0011`，holder `critic-a`** |
| `critic status` | `critic-a streak=0`、`critic-b streak=0`；open：`T-0009→R-0004`、`T-0012→T-0011` |

也就是说：**结论（T-0011 的 merge）现在默认卡在一个尚未表态的批判者后面**；批判者若 `refuted`，T-0011 直接不能 merge。

### 边界

- 配对数 ≠ 对抗质量：`confirmed` 连续两次就换人只是"防止橡皮章"的下限，不是质量证明。
- critic 只在 `review` 阶段出现，不会去审未提交的 worktree。
- 若 `[critic] required_sizes` 把某尺寸关掉，那条目仍可由人工 `critic pair` 补审。