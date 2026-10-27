# 当前状态（docs/STATUS）

> 最后实测：2026-10-28 02:20（本地 Windows，Python 3.14）。
> **真实数字以本页为准**；README/旧文档里的历史数字（如 78 / 166 passed）已过期，不再作为口径。

## 1. 测试（实测）

| 套件 | 命令 | 结果 | 说明 |
| --- | --- | --- | --- |
| 根 `tests/` | `python -m pytest tests -q` | **244 passed / 9 failed**（56.79s） | 9 个失败集中在 `tests/test_webui.py` 的旧 `/agi` 页：模块 `agi.core` 已移除（legacy 8081），不是当前主链 |
| `agi/tests/` | `python -m pytest agi/tests -q` | **1975 passed / 1 skipped / 1 failed**（1298.58s ≈21m38s） | 失败：`test_proactive_budget.py::test_llm_can_speak_even_below_score_and_state`（期望纯文本、实际被加上 emoji/slang；疑似环境/规则配置差异，待单独修） |

- 测试数字与 `docs/plans` 旧记录中的 600/678/166/78 不同是正常的：项目已合并、测试集扩大。
- 发布前最低要求：**先跑 `agi/tests`，确认除已知 1 条外全绿；根 `tests` 的 9 条旧 `/agi` 失败不算主链**。

## 2. 服务与端口（主链 / 非主链）

| 端口 | 进程 | 定位 |
| --- | --- | --- |
| **801** | `python -m agi.wechat_gateway.server` | 主链：微信网关（收发/状态/调试 API） |
| **802** | `python tools/console/server.py --port 802` | 主链：控制台/工作台 UI |
| 8123 | `llama-server ... soul_qwen25_05b_v1b-q4_k_m.gguf` | 可选：本地 0.5B 内状态（shadow） |
| 8125 | `python tools/soul_serve/serve_local3b.py` | 可选：本地 3B（摘要/提取） |
| 8081 | `python web_app.py`（旧根网页 `/agi`） | **非主链/legacy**：`agi.core` 已移除，仅历史对照 |
| 8082 | `python -m agi.selflearn.web` | **非主链/调试**：selflearn 自带网页，日常入口是 802 |

## 3. 当前架构一句话

微信消息经**双后端**（wxauto4 receiver；或 wx_core MonitorBackend 的 WCDB 直读 + OCR 回退）→ 801 网关归一化/闸门 → `agi.selflearn.chat`（记忆/心情/内状态/人格/护栏）→ DeepSeek API 生成 → 发送。
详见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

## 4. 里程碑（简表）

| 阶段 | 状态 | 代表产物 |
| --- | --- | --- |
| W1–W4 | 已完成 | 内状态、四臂/六臂实验、灵魂线、LoRA 与评估 |
| P 系列（P0–P38） | 大部分完成 | 网关护栏、媒体/贴纸、B站、主动节奏、引擎/模型启停、每日语料、日记/自我叙事、记忆衰减 |
| M0 工作台 | 完成 | 服务/配置/Git/文件 API + 802 控制台 |
| M1 Data Studio / 验收 | 进行中 | SQLite 浏览/SQL/行编辑/备份（后端已就绪，路由待接） |
| 文档收口 P1-1 | 本次完成 | `docs/INDEX.md`、`docs/STATUS.md`、plans 合并、快照、链接检查 |

## 5. 文档现状

- **唯一入口**：[`INDEX.md`](INDEX.md)；计划看 [`plans/README.md`](plans/README.md)（42 份，15–59，缺 17/27/55）。
- 根 `plans/` 已并入 `docs/plans/`；根 `agi/*.md` 笔记已移入 `docs/archive/agi-notes/`。
- 收口前全量快照：`docs/archive/20261028-docs-snapshot/`（含 MANIFEST.json）。
- 旧 `docs/README-legacy.md` 已移入 archive；根 README/usage 中的过期测试数字已改为引用本页。
- 链接检查：`python tools/check_docs_links.py`（当前 active docs 0 broken；`--fix` 可修 `plans/` → `docs/plans/`）。

## 6. 已知问题 / 待办

1. 根 `tests/test_webui.py` 的 9 条旧 `/agi` 用例：依赖已移除的 `agi.core`；要么删/跳过，要么重写为 802 控制台，不在本次文档任务范围。
2. `agi/tests/test_proactive_budget.py` 的 1 条失败：期望无 emoji/slang，实际输出被追加；先怀疑测试环境/配置（simple/legacy 风格或护栏配置）差异。
3. Data Studio（M1-A）后端已实现，7 条测试通过；**路由尚未接 main.py**（Lead 统一接线）。
4. 8081/8082 仅保留历史/调试用途，不写入主链文档的操作步骤。
