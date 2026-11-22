# 04 · 门禁、风险与安全

## 1. 机械门的顺序（顺序本身就是成本控制）

```text
账簿合法 → 状态合法 → intent 已签 → diff 采集 → scope guard → 风险分档
        → 配置化检查（test/lint/依赖/契约）→ 债务上限 → 溯源 trailer
```

先跑便宜且确定的，任何一步失败都立刻返回，不浪费 CI 分钟。门禁输出 `gate-result.json`（每步 `ok` + `detail`），**拒绝原因必须是机器可读的事实，不是评审意见**。

本地与 CI 跑同一份 `gates.run_gate`，不维护第二套判断——这是"本地过了 CI 挂了"的唯一根治法。退出码：`0` 通过、`1` 拒绝、`3` 租约冲突、`4` 需要人签。

## 2. 风险分档：按可逆性，不按领域

| 档 | 含义 | 处置 |
| --- | --- | --- |
| T0 | 可逆且局部 | 门禁通过即自动放行 |
| T1 | 可逆但触及公共契约/数据模型/迁移/依赖/测试弱化 | 强校验 + 异质评审（必须给出已检查项或可证伪反对意见） |
| T2 | 不可逆（发布、生产配置、数据删除、外部通信） | **人类令牌**，否则门禁失败（退出码 4） |

风险信号全部从 diff 机械计算，不靠 agent 自述：

| 信号 | 触发条件 |
| --- | --- |
| `deletes_tests` | 删除测试路径下的文件 |
| `adds_skip` | 新增 `pytest.mark.skip` / `xfail` / `it.skip` / `#[ignore]` / `--no-verify` / `continue-on-error` |
| `adds_dependency` | 依赖清单/锁文件出现新增行 |
| `migration` | `**/migrations/**`、`*.sql`、schema 变更 |
| `touches_ci` | `.github/workflows/**`、`ci/**`、`.gitlab-ci*` |
| `irreversible_command` | 新增 `terraform apply` / `kubectl delete` / `DROP TABLE` / `rm -rf` 等 |

规则在 `config/orchestra.toml` 的 `[[risk.rule]]` 里，路径 glob 用**段感知**匹配（`*` 不跨段，`**` 跨段），与租约共用同一套路径代数。

## 3. 人签令牌（让签字可验证，而不是可信任）

```text
token = "oap1." + b64url(payload) + "." + hex(HMAC-SHA256(key, payload))
payload = {v, approval_id, kind, by, issued_at, expires_at, nonce,
           subject(diff 摘要), prediction(可证伪预测), rollback(回滚方案)}
```

校验顺序：格式 → 签名 → 过期 → kind 匹配 → subject 匹配 → nonce 未被使用（数据库主键冲突）。任何一项不过 → 拒绝。

设计要点：

- **绑定具体 diff**：`subject = sha256(路径集 + diff 正文)`。签字后代码一改，令牌立即失效。
- **一次性**：nonce 落库，重放被主键拦住，不是 if 判断。
- **必须带预测与回滚**：为空则 `issue` 直接拒绝。签字没有信息量等于没有签字。
- **密钥不实现为"取代人类"**：key 文件在 `.orchestra/`（不入库），谁持有 key 谁就能签——所以下一步是 Ed25519 + 硬件签名，HMAC 是当前零依赖下的合理折衷。

## 4. 沙箱与提示注入（原方案遗漏之一）

- **默认无网**：runner 容器默认 `--network none`，需要拉依赖时走显式 egress 白名单代理，并记录到事件。
- **内容是数据，不是指令**：测试输出、issue 正文、PR 评论一律当不可信输入；工具调用要有白名单，禁止 agent 通过读日志获得新指令。
- **密钥最小化**：agent 环境不放部署/云凭据。能签字的 key 与能干活的 runner **不在同一个容器**。
- **只读挂载**：`ledger/`、`config/`、`contracts/` 对实施型 agent 只读（写入通过门禁流程）。
- **供应链**：新增依赖 = T1，锁文件变更必须与 T1 评审同批；依赖允许清单之外的一律拒绝。

## 5. 补丁债与重构许可（本骨架只做硬上限）

- `[debt.<module>]` 给出 `max_lines`：合并不得抬高上限，抬高只能由 `role=refactor` 的重构任务赚取。
- 比率指标（复杂度/千行、重复率）交给 `[gates]` 里的命令（如 `radon cc`、`jscpd`），趋势看窗口，不看单次提交。
- 重构许可证 = 特征测试在位。代理指标：公共 API 覆盖率 + golden/snapshot + **抽样** mutation score。无锁行为测试的模块不派发重构任务。

## 6. 反向闸门（不需要人类在场）

- 默认拒绝（fail-closed）：门禁配置缺失、账簿损坏、令牌缺失 → 一律不放行。
- 合并后指标劣化 → 自动回滚（走 revert PR，不 force-push 主干）。
- 回滚率或逃逸率超阈值 → 自动冻结该血统写权（`quarantined`），并升级给人。
