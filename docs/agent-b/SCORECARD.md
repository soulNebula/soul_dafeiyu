# SoulAgent 质量评分卡（Agent B / 2026-10-29）

> 本卡只写**能现场复算的证据**。每条分数后面给出「证据命令/文件」与「残余风险」；
> 数字随代码变化，以 `python scripts/agentb_verify.py` 的终验输出为准。
> 运行环境：Windows 10 + Python 3.14.7，worktree `SoulAgent-b`（分支 `agent-b/score7`）。

## 0. 结论

> **终验结果**（2026-10-29 04:13，`python scripts/agentb_verify.py --json docs/agentb_verify_result.json`）：
> `tests/` **301 passed / 0 failed / 0 skipped（59.5s）**；`agi/tests/` **2185 passed / 0 failed / 13 skipped（532.6s）**；
> 四条静态闸门全 PASS；生产敏感文件 sha256 前后一致（测试没写生产）。
> 13 条 skip 全部是**本机无该资产**的精确跳过：8 条 `model_registry`（缺 `local_models.json`）、
> 1 条 `media_choice`（缺真机上下文库）、3 条 `test_soul_eval` dry-run（缺 agent_home 下的模型目录）、
> 1 条 Windows-only（`SOULAGENT_CI` 口径）。没有「整文件 ignore」。

| # | 维度 | 改前 | 改后 | 一句话依据 |
| --- | --- | --- | --- | --- |
| 1 | 功能完成度 | 8.5 | 8.5 | 功能面未削减；新增交付状态/队列/兜底可观测，不新增对外功能承诺 |
| 2 | 主链健壮性 | 4.5 | **7.5** | 发送锁 + 冲刷不丢 + 送达真值 + 幂等去重 + WAL + 队列/兜底可观测，42 条新测试 |
| 3 | 安全与合规 | 5.5 | **7.5** | 明文 key 外部化、轮换/清理/扫描/ACL 四件工具 + 合规文档；剩余动作需本人执行 |
| 4 | 测试规模 | 7.5 | **8.0** | collect `tests`=301、`agi/tests`=2198（改前 270/2104，净增 125 条） |
| 5 | 测试可证伪性 | 4.5 | **7.5** | 生产写盘自证守卫 + 路径 env 隔离 + CI 真跑 + 4 个既有 flake 修实 |
| 6 | 架构清晰度 | 3.5 | **7.5** | 9000+ 行死代码归档、主动链收敛唯一入口、4 条自动守卫、文档按代码重写 |
| 7 | 可移植性 | 7.0 | **7.5** | 路径全 env 化、Windows-only 精确 skip、干净 checkout 可跑离线层 |
| 8 | 文档事实性 | 4.0 | **8.0** | README/ARCHITECTURE/STATUS/INDEX/OPENSOURCE 重写，143 链接 0 断链，数字可复算 |
| 9 | 叙事诚实度 | 5.0 | **7.5** | 「已实现/未完成/外部依赖」三段；训练 96/300、unverified≠送达、B站外置 |
| — | **综合** | ≈5.4 | **≈7.7** | 9 维平均 7.72；所有维度 ≥7 |

评分口径：每维满分 10，7 分 = 「可交付、有自动证据、残余风险已写明」，9 分以上要求
无已知结构性债。改前分数取自用户提供的基线表。

## 1. 主链健壮性 4.5 → 7.5

改前：单 worker 无发送锁、`flush` 先 pop 后发送（异常即静默丢）、去重只看文本、
回复落库即当「说过」、队列满静默丢、兜底 `unverified` 混入 `sent`、草稿无限增长。

改后（证据 = 测试名 + 代码）：

| 承诺 | 证据 |
| --- | --- |
| 所有真实外发串行（含子进程/UIA/记账） | `test_global_send_lock_serializes_concurrent_sends`（两线程最大并发=1）、`test_send_lock_is_reentrant_for_nested_paths`、`test_media_batch_waits_for_global_send_lock`；`server.py` `self._send_lock = threading.RLock()`（`_send` 壳 + `send_media_batch`） |
| 冲刷失败不丢、有退避与上限、丢弃必落盘 | `test_flush_pending_keeps_and_retries_on_failure`、`test_flush_pending_drops_after_limit_but_persists`（`pending_lost.jsonl`） |
| SQLite 并发加固 | `test_connect_enables_wal_busy_timeout_and_normal_sync`、`test_two_threads_write_50_rows_each_without_locked`（100/100 无 `database is locked`）；`db._SQLITE_PRAGMAS` |
| 幂等去重双向正确 | `test_same_text_from_different_turns_is_not_deduped`、`test_same_idem_key_within_window_is_blocked`；`_reply` 传 `turn_id:bubble_index` |
| 没发出去的话不当既成事实 | `test_mark_turn_delivery_blocked_hides_assistant_message`、`test_gateway_whitelist_block_marks_turn_blocked`（端到端：turn=blocked 且 assistant 不进 `recent_messages`）；`chat.mark_turn_delivery`、`memory.recent_messages` 过滤 `delivered=0` |
| 队列/兜底/草稿/弱令牌可观测 | `test_queue_overflow_is_counted_and_visible`、`test_window_text_fallback_is_unverified_not_sent`、`test_prune_removes_expired_keeps_fresh`、`test_weak_token_blocks_admin_api_once_started`、`test_approve_rejects_query_token_even_when_env_allows` |
| 媒体白名单 fail-closed | `test_media_batch_empty_allowlist_is_fail_closed`（`no_allowlist`） |

残余风险（已写进代码注释/文档，不藏）：① 仍是单 worker，队头阻塞只可观测未改并发模型
（`/status` `queue_size`/`in_flight_contact`）；② 冲刷到 3 次仍会最终丢弃（落盘可捞，
不是无限重试）；③ 入站队列满仍丢入站（计数+事件，无背压）；④ 送达过滤只覆盖 chat 组装
上下文主路径，diary/summary/proactive 等旁路仍能读到未送达文本。

## 2. 安全与合规 5.5 → 7.5

改后：
- 明文 key 根除（代码层）：`agi/selflearn/llm.py` 优先级 `DEEPSEEK_API_KEY` env >
  `SOULAGENT_WEB_CONFIG` > `web_config.json`；`agi/secrets_store.py` 唯一读取入口
  （env > `secrets.json` > 文件），损坏 fail-closed，日志/异常不打印 key。
- 四件工具：`tools/rotate_secrets.py`（--dry-run 默认，--apply 复制，--purge-plaintext
  校验后清空）、`tools/prune_chat_logs.py`（留存 TTL）、`tools/check_secrets.py`
  （扫描 git-tracked 文件，现 PASS）、`tools/harden_acl.py`（`--apply`/`--verify`）。
- 权限与留存：`SECURITY.md`、`docs/风控与合规说明.md`（v2026-10-29）写明
  unverified≠delivered、白名单 fail-closed、管理令牌弱口令拒绝、聊天留存 TTL。
- 52 条安全回归：`agi/tests/test_agentb_security.py`。

残余风险：① 生产 `web_config.json` 里的明文 key **仍在磁盘上**，需本人执行
`python tools/rotate_secrets.py --apply --purge-plaintext` 才算真正清掉（工具已 dry-run
验证不改文件）；② `check_secrets.py` 是模式匹配，不能证明「绝对无密钥」；③
`harden_acl.py --verify` 在未收紧 ACL 的机器上按设计退出 1，需本人 `--apply` 后复验。

## 3. 测试规模/可证伪 7.5 / 4.5 → 8.0 / 7.5

| 指标 | 改前 | 改后 |
| --- | --- | --- |
| `tests/` collect | 270 | **301**（+14 守卫 +17 架构） |
| `agi/tests/` collect | 2104 | **2198** |
| 其中本次新增 | — | send-core 42 + 安全 52 + 架构 17 + 隔离 14 |
| 终验实测 | 无统一入口 | `tests` 301 passed；`agi/tests` 2185 passed / 13 skipped / 0 failed |
| CI | `--ignore=tests/test_webui.py`，实际从不跑 | 两条真跑；`SOULAGENT_CI=1` 下 261 passed/40 skipped（Windows-only 精确 skip） |
| 不可重复用例 | test_proactive_budget 墙钟、test_urges 未 seed 概率闸门 | 均已修实并连跑验证（5×4 passed、10/10 passed） |

**可证伪性关键证据**：两套测试的 conftest 在会话开始/结束对生产敏感文件做
mtime_ns+size 快照，有变化即 fail；T2 用「人为改 config.json → 会话立刻红 → 按
sha256 还原」做了阳性对照。独立第三方脚本（仓库外）对 14 条敏感路径取
mtime+size+sha256，根测试跑前/跑后 `changed: 0`。

残余风险：① 本机无法真跑 ubuntu CI runner；② 并发跑两个 pytest 会话时守卫无法归因
（A 会话会看到 B 会话的写盘），已文档化为「终验必须单会话串行」。

## 4. 架构清晰度 3.5 → 7.5

- 死代码：`legacy/` 归档 36 文件 / 9078 行（Demo/snapshot 17、Demo/dev 10、scratch 3、
  agi/_audit_parts 6），只 mv 不 rm，`legacy/README.md` 逐项给原路径/证据/恢复命令。
- 主动链唯一入口：`agi/proactive_registry.py` 登记 9 个入口（active/legacy/manual），
  `run_proactive_tick` 全仓库唯一定义，`Gateway._proactive_cycle` 已改为委托它——
  **代码里真正在跑的链 = 登记表 primary 项**，不再「文档一套、代码一套」。
- 架构守卫：`tools/check_architecture.py` 4 条规则（死代码回归 / import 环 / 行数预算 /
  分层），`python tools/check_architecture.py` → PASS exit 0（444 模块 / 1479 边）；
  `tests/test_architecture.py` 17 passed（含正反用例与登记表一致性）。
- 文档：`docs/ARCHITECTURE.md` 按代码重写（双后端→801/802 主链→完整发送闸门顺序→
  主动链→数据库/控制台/B站/模型归属→目录树）。

残余风险：大文件债仍在——`tools/console/server.py` 4827 行、`agi/selflearn/proactive.py`
3056、`agi/wechat_gateway/server.py` 2751；已进行数预算 allowlist（基线不再增长），
但没拆。

## 5. 文档事实性 4.0 → 8.0 / 叙事 5.0 → 7.5

- README 重写为「已实现 / 未完成 / 外部依赖」三段；训练改口径为可核实事实：
  96/300 可训练样本、`last_training=null`、无自动训练/自动上线；唯一跑通的是 W4-B
  20 步 smoke（任务书里的「1–2 步」在仓库内无依据）。
- 术语统一：`unverified≠delivered`、白名单 fail-closed、管理令牌弱口令拒绝；B站能力
  明确依赖仓库外 `~/.dsh/bili-duty` 插件；Windows + 独立 3.12 venv；明文 key→env 迁移。
- 数字不再手抄：`tools/status_report.py` 从现场/终验 JSON 生成 `docs/STATUS.md` 自动
  区块；`--check` 过期退出 1。
- 链接：`python tools/check_docs_links.py` → 143 links / 0 broken。
- 计划目录：`docs/plans/README.md` 明确 43 份（缺 17/27/55），并声明「计划里的旧数字
  不作现役事实」。

残余风险：`docs/archive/` 里仍是历史快照（明确标注不作现役事实，未逐份重写）；
`docs/weather` 等个别人工文档仍可能引用旧路径。

## 6. 可移植性 7.0 → 7.5

- 路径全 env 化：`SOULAGENT_DATA_DIR/LOG_DIR/CONFIG_PATH/WEB_CONFIG` +
  `agi.paths.data_dir()/log_dir()`；23 个运行时常量默认值与改前逐字节一致
  （`checked 23 bad {}`），无 env 时生产行为零变化。
- `tests/test_isolation_guard.py` 静态扫描禁止 `agi/` 非测试代码再出现 `"agi"/"data"`
  字面量（白名单逐条写原因），防止回归。
- `SOULAGENT_CI=1` 通过 `windows_only` marker 精确 skip（ctypes.WinDLL/UIA/tkinter/
  字体/奇偶校验），不再整文件 ignore；`tests/test_friends.py` 顶层 import 链改成
  收集期安全。
- `requirements.txt` + `web_config.example.json` 给出干净 checkout 的离线层可跑路径。

残余风险：真实发送依赖独立 Python 3.12 venv（仓库不打包）；B站插件在仓库外；
ubuntu runner 未真跑。

## 7. 功能完成度 8.5 → 8.5（未削减，未虚增）

功能面没有删减：全部既有测试保留并通过（`tests` 301 passed、`agi/tests` 见终验）。
新增的是「可观测/可信」而非新功能：`/status` 暴露 `dropped_inbound/queue_size/
in_flight_contact/flush_retry/flush_dropped/drafts_pruned`，`turns.delivery_status`
可逐轮对账，`raw_events(kind=delivery)` 审计。训练/上线能力**没有**新增，
README 也不声称已有。

## 8. 现场复算命令

```bat
:: 1) 全量终验（两套测试 + 静态检查 + 生产文件未被污染），落 JSON 证据
C:\Python314\python.exe scripts\agentb_verify.py --json docs\agentb_verify_result.json

:: 2) 回填 docs/STATUS.md 自动区块（不手抄）
C:\Python314\python.exe tools\status_report.py --from-agentb docs\agentb_verify_result.json

:: 3) 四条静态闸门
C:\Python314\python.exe tools\check_secrets.py
C:\Python314\python.exe tools\check_architecture.py
C:\Python314\python.exe tools\check_docs_links.py
C:\Python314\python.exe tools\check_portability.py

:: 4) 分维度证据
C:\Python314\python.exe -m pytest tests -q
C:\Python314\python.exe -m pytest agi/tests -q
C:\Python314\python.exe -m pytest agi/tests/test_agentb_send_core.py agi/tests/test_agentb_delivery.py agi/tests/test_agentb_maintenance.py agi/tests/test_agentb_security.py tests/test_architecture.py tests/test_isolation_guard.py -q
```