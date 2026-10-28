# Agent B 改动说明（分支 `agent-b/score7`）

> 目标：在不打扰运行中的生产进程、不搞崩项目的前提下，把质量维度从基线
> （功能 8.5 / 主链 4.5 / 安全 5.5 / 测试规模 7.5 / 测试可证伪 4.5 / 架构 3.5 /
> 可移植 7 / 文档 4 / 叙事 5，综合≈5.4）提到全部 ≥7（见 `SCORECARD.md`）。
>
> **本分支的代码没有部署、没有重启任何服务、没有改主工作区
> `C:\Users\Administrator\Desktop\SoulAgent`。** 主仓的 801/802 等服务在会话期间
> 一直正常运行（健康检查见终验输出）。

## 1. 分支与提交

分支：`agent-b/score7`（基于 `8d1e444`，即 w4-soul HEAD）

| 提交 | 内容 |
| --- | --- |
| `e475b57` | 安全：密钥外部化（env > secrets > 文件）+ 留存 TTL + ACL `--verify` + 合规文档重写 |
| `f1c4d6f` | 测试隔离：路径 env 化 + 会话自证守卫 + CI 真跑 + 既有 flake 修实 |
| `0844850` | 主链：发送锁 / 冲刷不丢 / 送达真值 / 幂等去重 / WAL / 队列可观测 |
| `86a3507` | 安全测试：假 key 改运行期拼串，`check_secrets` 不再误报 |
| `cbde2ff` | 架构：死代码归档 `legacy/` + 架构守卫 + 主动链唯一入口 + 文档按代码重写 |
| 终验提交 | 终验 JSON + `docs/STATUS.md` 数字回填 + 本目录两份说明 |

回滚：`git reset --hard 8d1e444`（分支内操作，不影响其他分支/主工作区）。

## 2. 用户 P0/P1/P2 清单 → 处理结果

| # | 问题 | 处理 | 证据 |
| --- | --- | --- | --- |
| P0-1 | 明文 API key / db_keys / 聊天日志 | key 走 env > `secrets.json` > 文件；四件工具生成/轮换/扫描/收紧 ACL；留存 TTL | `SECURITY.md`、`docs/风控与合规说明.md`、`agi/tests/test_agentb_security.py` |
| P0-2 | 无人值守自动发送、兜底不可验证、force 后门 | `unverified` 独立口径不计 `sent`；`/api/approve`、`/api/draft/approve` 要 `confirm=true` + 白名单，拒绝 `?token=`；弱 `admin_token` 直接 403 | `test_window_text_fallback_is_unverified_not_sent`、`test_weak_token_blocks_admin_api_once_started`、`test_approve_rejects_query_token_even_when_env_allows` |
| P0-3 | 先落库后发送、没发出去也当说过 | `turns.delivery_status` + `messages.delivered` + `chat.mark_turn_delivery`，`memory.recent_messages` 过滤未送达 assistant 行 | `test_gateway_whitelist_block_marks_turn_blocked` |
| P0-4 | 单 worker、无发送锁、重复发送 | 全局 RLock 串行化全部真实外发；幂等去重键 `(contact, turn_id:bubble_index)` | `test_global_send_lock_serializes_concurrent_sends`、`test_same_idem_key_within_window_is_blocked` |
| P0-5 | 消息静默丢失 | 冲刷失败保留重试（指数退避、上限 3），最终丢弃落盘 `pending_lost.jsonl`；队列满计数+事件 | `test_flush_pending_keeps_and_retries_on_failure`、`test_queue_overflow_is_counted_and_visible` |
| P1-6 | 死模块 / 巨石文件 | 36 文件 / 9078 行归档到 `legacy/`；行数预算守卫；主动链收敛唯一入口 | `legacy/README.md`、`tools/check_architecture.py`、`agi/proactive_registry.py` |
| P1-7 | 文档过时 | README/ARCHITECTURE/STATUS/INDEX/OPENSOURCE/plans 按代码重写；数字自动生成 | `tools/status_report.py`、143 链接 0 断链 |
| P1-8 | CI 从不真跑 | 删 `--ignore`，两条真跑；`SOULAGENT_CI=1` 精确 skip Windows-only | `.github/workflows/ci.yml`、CI 模式 261 passed/40 skipped |
| P1-9 | 测试污染生产 | env 路径重定向（23 个常量）+ 会话前后 mtime/size 快照守卫，变化即 fail | `tests/conftest.py`、`agi/tests/conftest.py`、`tests/test_isolation_guard.py` |
| P2-10 | 可移植性宣称 | 干净 checkout 可跑离线层；真发送需独立 3.12 venv、B站需仓库外插件，文档明说 | `README.md`、`docs/OPENSOURCE.md` |
| P2-11 | 卫生 | `.gitignore` 补密钥/运行数据；`check_secrets.py` 现 PASS；假 key 改运行期拼串 | `tools/check_secrets.py` |

## 3. 接口/行为变化（部署前必须知道）

**默认行为零变化**（无 env、无新配置时与改前逐字节一致）：

- 日志仍落 `<repo>/agi/data/wechat/gateway.log`；DB 仍落 `<repo>/agi/data/selflearn/`。
- 旧库首次 `db.connect()` 自动迁移（WAL + 两个新列，`delivered` 默认 1），旧数据全部照旧可见。
- 送达回填只在有 `turn_id` 时发生；`dry_run`/`send_mode=draft` 仍走草稿。
- 草稿 TTL 默认 7 天（`draft_ttl_days` 可覆盖）。

**行为更严格的地方**（都是有意的安全收紧，均已有测试）：

- `admin_token` 为空/`8888`/长度 <8：网关 `start` 后管理 API 一律 403（此前只告警）。
  生产当前 token 32 字符，不受影响；若你的部署用弱 token，请先轮换再启动。
- `/api/approve`、`/api/draft/approve` 不再接受 `?token=` query（用
  `X-Admin-Token` 头）；`confirm=true` + 白名单仍是硬条件。
- 媒体批发送在**允许联系人列表为空**时 fail-closed（`no_allowlist`）。生产
  `allowed_contacts` 非空，不受影响。
- `unverified`（窗口/UIA 兜底）不计入 `sent`/`sent_today`，单列事件与 `/status`。

## 4. 部署后建议由本人执行的动作（本会话**没有**执行）

```bat
:: 1) 先 dry-run 看要做什么（不改文件）
C:\Python314\python.exe tools\rotate_secrets.py --dry-run

:: 2) 复制明文 key 到 secrets.json，并在校验通过后清空明文
C:\Python314\python.exe tools\rotate_secrets.py --apply --purge-plaintext

:: 3) 收紧数据/日志 ACL 并复核（未收紧时 --verify 按设计退出 1）
C:\Python314\python.exe tools\harden_acl.py --apply
C:\Python314\python.exe tools\harden_acl.py --verify

:: 4) 可选：把 key 放环境变量而不是 secrets.json
set DEEPSEEK_API_KEY=...

:: 5) 重启网关/控制台让新代码生效（不重启则仍是旧代码在跑）
::    确认 802 控制台 / 801 网关健康后再离开

:: 6) 聊天日志留存清理（默认 dry-run）
C:\Python314\python.exe tools\prune_chat_logs.py
```

## 5. 验证（一条命令出全套证据）

```bat
C:\Python314\python.exe scripts\agentb_verify.py --json docs\agentb_verify_result.json
```

它会：跑 `tests/` 与 `agi/tests/` 两套测试 → 跑四条静态闸门（密钥扫描/文档链接/
可移植性/架构守卫）→ 对生产敏感文件做 sha256 前后快照（证明测试没写生产）→
打印证据表（套件 | 通过 | 失败 | 跳过 | 耗时 | 结果）。`--quick` 跳过 `agi/tests`。

**最近一次终验（2026-10-29 04:13）**：`tests` 301 passed / 0 failed；`agi/tests`
2185 passed / 0 failed / 13 skipped（全部为「本机缺该资产」的精确 skip：8 model_registry、
1 media_choice、3 soul_eval 模型目录、1 Windows-only）；四条静态闸门全 PASS；
生产敏感文件未被修改。证据 JSON 在 `docs/agentb_verify_result.json`。

## 6. 已知残留风险（诚实清单）

1. 主链仍是单 worker：队头阻塞只做了可观测（`/status` `queue_size`/`in_flight_contact`），
   没有改并发模型（按「不冒险」原则）。
2. 冲刷到 3 次仍会最终丢弃（落盘 `pending_lost.jsonl` 可捞回，不是无限重试）；
   入站队列满仍丢入站（计数+事件，无背压）。
3. 送达过滤只覆盖 chat 组装上下文主路径；diary/summary/proactive 等旁路仍能读到
   未送达文本（不影响对话事实性，但不是全闭环）。
4. `check_secrets.py` 是模式匹配；`harden_acl --apply` 需本人执行后 `--verify` 才算完成；
   明文 key 在你执行 `--purge-plaintext` 前仍在磁盘上。
5. ubuntu CI runner 本机无法真跑；并发跑两个 pytest 会话会让「测试写生产」守卫
   无法归因（终验必须单会话串行，已写进 `docs/STATUS.md`）。
6. 大文件债：`tools/console/server.py` 4827 行、`agi/selflearn/proactive.py` 3056 行、
   `agi/wechat_gateway/server.py` 2751 行——已进行数预算、基线不再增长，但没拆。