# 12 · 让陌生人的 agent 一句粘贴就开始（git-native join）

`join.txt` 是给人复制给 agent 的说明书；`join.json` 是机器可读的同款契约。背后没有服务器：
**claim 就是 git ref**。

## 1. 协议（四条命令）

| 动作 | 命令 | 底层 |
| --- | --- | --- |
| 领 | `orchestra claim --task T-0007 --handle <h> --sign-key <key>` | `git push <commit>:refs/claims/T-0007`（已存在则失败 = CAS） |
| 续 | `orchestra beat --task T-0007 --handle <h> --sign-key <key>` | `--force-with-lease=refs/claims/T-0007:<old>` |
| 还 | `orchestra release --task T-0007 --handle <h> --reason <枚举>` | 同上 lease + 删除 ref |
| 收 | `orchestra reap`（定时任务） | 读 claim 的 `expires_at`，过期即删 |

claim commit 里只有一个文件 `claims/<task>.json`（task、handle、paths、base、started/expires、ttl）。**它必须由该 handle 的 SSH 密钥签名**：`handles/<handle>.pub` 会被拼成 allowed_signers，`git verify-commit` 决定"是谁签的"；吊销 = 删除公钥文件。

## 2. 为什么这样够用

- **不需要服务**：远端仓库就是仲裁者；冲突由 git 的 ref 语义解决，不靠我们写锁。
- **可审计**：谁在什么时候领了什么，都在 `refs/claims/*` 的历史里。
- **和现有机制对齐**：`paths` 抄任务的作用域，`gate` 照旧拒绝越界 diff；每 handle 最多 2 个并行 claim（`per_holder_limit`）。
- **迁移留缝**：将来搬到 desk API，只换 `coord_git` 模块，命令与 join.txt 不变。

## 3. 维护者侧的合并

`orchestra merge --task T-0007`：claim 必须已释放（或过期）→ 在 detached worktree 里跑 merge 门 → 只有 `risk=T0` 才 `git merge --ff-only` 并推进账本；`T1/T2` 返回退出码 4，要求评审或人签。这就是你说的"本地轮询合并"：可以挂定时任务，也可以手动一条条点。

## 4. 验证（真实双克隆竞态）

`tests/test_join.py`（需 `ssh-keygen`）：bare origin + alice/bob 两个克隆各自持密钥与 handle，同时 claim 同一任务 → **恰好一个 0、一个 3**，origin 只有一个 claim ref；非持有者 beat → 3；未签名 claim → 1；TTL 到期 `reap` 后可被另一个 handle 重领；claim 仍活着时 `merge` → 3。

## 5. 边界（这一版没做）

- **CI**：gate 目前由 agent 本地跑、维护者合并前再跑一遍；`join.txt` 里写的是本地流程。
- **速率与冷却**：只做了"每 handle 并行 claim 数"，没有按时间的限流；匿名刷单仍可能。
- **claim 与账簿的双写**：claim 在 refs，任务状态在 `ledger/tasks/*.json`，两者靠命令顺序保证一致，没有事务。
- **Windows 之外的签名路径**没测（OpenSSH 都在，但只在本机验证过）。