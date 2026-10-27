# 当前状态（docs/STATUS）

> 最后实测：2026-10-28 02:50（本地 Windows，Python 3.14）。
> **真实数字以本页为准**；README/旧文档里的历史数字（如 78 / 166 passed）已过期，不再作为口径。

## 1. 测试（实测）

| 套件 | 命令 | 结果 | 说明 |
| --- | --- | --- | --- |
| 根 `tests/` | `python -m pytest tests -q` | **244 passed / 9 skipped**（43.34s） | 9 条 `tests/test_webui.py` 旧 `/agi` 用例依赖已移除的 `agi.core`，已加 `skipif` 跳过（legacy 8081，非主链） |
| `agi/tests/` | `python -m pytest agi/tests -q` | **2002 passed / 1 skipped**（840.99s ≈14min） | 全绿；此前记录的 `test_proactive_budget` 抖动本次未复现 |

- 测试数字与 `docs/plans` 旧记录中的 600/678/166/78 不同是正常的：项目已合并、测试集扩大。
- 发布前最低要求：**两套都绿**（根 `tests` 9 skipped 为 legacy、不算主链）。

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
| M1 Data Studio / 验收 | 完成 | SQLite 浏览/SQL/行编辑/备份（工作台 803 已接线并实测；idea 仓库 34 tests） |
| 文档收口 P1-1 | 本次完成 | `docs/INDEX.md`、`docs/STATUS.md`、plans 合并、快照、链接检查 |

## 5. 文档现状

- **唯一入口**：[`INDEX.md`](INDEX.md)；计划看 [`plans/README.md`](plans/README.md)（42 份，15–59，缺 17/27/55）。
- 根 `plans/` 已并入 `docs/plans/`；根 `agi/*.md` 笔记已移入 `docs/archive/agi-notes/`。
- 收口前全量快照：`docs/archive/20261028-docs-snapshot/`（含 MANIFEST.json）。
- 旧 `docs/README-legacy.md` 已移入 archive；根 README/usage 中的过期测试数字已改为引用本页。
- 链接检查：`python tools/check_docs_links.py`（当前 active docs 0 broken；`--fix` 可修 `plans/` → `docs/plans/`）。

## 6. 已知问题 / 待办

1. 根 `tests/test_webui.py` 的 9 条旧 `/agi` 用例已 `skipif`（依赖已移除的 `agi.core`）；如需恢复需重写为 802 控制台。
2. 8081/8082 仅保留历史/调试用途，不写入主链文档的操作步骤。
3. 真实发送（2026-10-28 05:40 更新）：
   - **换用 wechatauto-replica（Apache-2.0，1.2.8.1）作为发送后端**：`send_backend=sender` + `send_module=wechatauto`，`wxauto_python=E:/agent/venv_wx312/Scripts/python.exe`。
   - 原因：Python 3.13 下 comtypes/UIA 枚举返回空（wxauto4 内置 uia 失效）；**Python 3.12 下 uiautomation 2.0.29 正常**，因此 wechatauto 需要独立 3.12 venv。
   - 实测：体检账号识别、16/16 数据库可解密、控件树已物化；CLI `发` 与网关 `/api/approve` 均真实送达（`status=sent, module=wechatauto, chat_name=文件传输助手`）。
   - wxauto4 代码与配置保留：改回 `send_module=wxauto4` + 原 venv 即可（等 wxauto 更新）。
   - 旧 UIA 自研后端（uia_sender）保留为 `send_backend=uia` 备选。

## 7. 记忆引用闭环（P1-3）

prompt 里实际注入的记忆行带稳定短引用，模型回复可引用、guard 再校验，形成防幻觉闭环：

- **注入**：`agi/selflearn/chat.py::_prompt` 在 `memory.render_memories` 的行首加
  `[M-<hash8>]`（hash 自 memory 表 id，稳定、不泄露完整 id；最多注入 10 条，只注入实际进 prompt 的）；
  示例：`- [M-4c0f75d0] [user/preference] 我喜欢晚上写东西（依据：…）`
- **校验**：`agi/selflearn/guard.py::check(..., citation_ids=...)` 调用
  `citation.validate_citations`；本次注入的短 id 视为有效，回复里指向不存在/未注入的
  `[M-xxx]` 按 citation 原策略**删除引用**（不删整句），并写 `raw_events(kind="citation_invalid")` 审计。
- **开关**：`memory_citation_enabled`（默认 true；env `MEMORY_CITATION_ENABLED` 优先）——
  关闭时不注入 id、不调用 inject、不写新的 citation 审计，prompt 与旧逻辑一致。
- **测试**：`agi/tests/test_memory_citation.py`（11 条：短 id 稳定、≤10 注入、
  allowed 校验、无效引用删除+审计、开关关闭无注入/无新审计）；
  回归：`pytest agi/tests -q -k "chat or guard or citation or memory"` → **354 passed**。
