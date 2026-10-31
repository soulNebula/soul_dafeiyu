# 当前状态（docs/STATUS）

> 本页只放**可以现场复算**的事实。测试数字分两个口径，不要混用：
> - **collect 数**：`pytest --collect-only` 收集到的用例数，与运行环境无关，用来判断「测试面有没有缩水」；
> - **实测结果**（通过/失败/跳过/耗时）：真的跑一遍后的结果，会随机器/并发有所波动，必须带命令与时间。
>
> 表格列与 `scripts/agentb_verify.py` 的输出一致（套件 | 通过 | 失败 | 跳过 | 耗时 | 结果）。
> **全量终验以 `python scripts/agentb_verify.py` 为准**（它同时跑两套测试 + 静态检查 +
> 生产文件未被污染校验），其 JSON 可用 `python tools/status_report.py --from-agentb <json>` 导入；
> 自动区块本身由 `python tools/status_report.py --refresh/--run` 生成，
> `--check` 校验数字是否过期。**不要手改 `STATUS:AUTO` 区块里的数字**；正文只写方法与结论。

## 0. 测试与代码规模（自动生成）

<!-- STATUS:AUTO:BEGIN（由 tools/status_report.py 生成，勿手改）-->

最后生成：2026-11-01T01:09:44（Python 3.14.7）

| 套件 | 通过 | 失败 | 跳过 | 耗时 | 结果 |
| --- | --- | --- | --- | --- | --- |
| `tests/` | 301 | 0 | 0 | 45.29s | PASS |
| `agi/tests/` | 2298 | 0 | 4 | 435.75s | PASS |

collect 数（现场 `pytest --collect-only -q`，只表示用例面大小、不代表通过数）：`tests/`=301、`agi/tests/`=2310。

数据来源：`python scripts/agentb_verify.py`（全量终验，两套 + 静态检查 + 生产文件未被污染）或 `python tools/status_report.py --run`（单套件实测）；`--from-agentb` 可直接导入前者的 JSON，不手抄。

代码规模（同一命令可复算）：Python 模块 3620 个（agi 151 / selflearn 95 / agi/tests 193）；计划文档 46 份；legacy 归档 37 个文件 / 9151 行。

<!-- STATUS:AUTO:END -->

## 1. 服务与端口（主链 / 非主链）

| 端口 | 进程 | 定位 | 代码依据 |
| --- | --- | --- | --- |
| **801** | `python -m agi.wechat_gateway.server` | **主链**：微信网关（收/发/状态/调试 API） | `agi/wechat_gateway/config.py` DEFAULTS `"port": 801` |
| **802** | `python tools/console/server.py --port 802` | **主链**：控制台单页应用（日常操作入口） | `tools/console/server.py` `--port` 默认 802 |
| **803** | `cd idea && .venv\Scripts\python.exe -m uvicorn app.main:app --port 803` | **工作台**：训练/数据/语料清洗/标注/蒸馏 | `idea/app/main.py`（独立 git 历史，subtree 合入） |
| 8123 | `llama-server ... --port 8123` | 可选：本地 0.5B 内状态（shadow） | `agi/selflearn/mood_judge.py` 默认地址 |
| 8125 | `python tools/soul_serve/serve_local3b.py` | 可选：本地 3B（摘要/关键词/提取） | `tools/soul_serve/serve_local3b.py` |
| 8081 | `python web_app.py` | **legacy/非主链**：旧 wx_box 网页端 | `web_app.py` DEFAULT_PORT=8081 |
| 8082 | `python -m agi.selflearn.cli serve --port 8082` | **legacy/非主链**：selflearn 调试网页 | `agi/selflearn/cli.py` serve → `web.py` |

- 主链只有 801（真实收发）+ 802（操作台）+ 803（训练/数据工作台）；8081/8082 仅历史对照/调试。
- 桌面版（M5，Windows）：`python tools/desktop/launcher.py --tray`（桌面快捷方式「小鱼桌面版」= pythonw）
  一键托管 801/802/803 + 托盘 + Edge app 窗口；`--status/--stop/--autostart` 可用。
  不要用管理员身份启动（会触发 Edge 提权弹窗；启动器会自动回落普通权限浏览器）。详见 `docs/guides/桌面版.md`。
- Tk 桌面 GUI：`python app.py`（`--fake` 假后端演示）。
- 密钥：DeepSeek key 优先级 env → `agi/data/secrets.json` → `web_config.json`；802 模型配置页可
  直接保存/清除 key，只写本机 `secrets.json`（原子写、ACL、不入 git、不回传明文），热加载生效。

## 2. 当前架构一句话

微信消息经**双后端**（wxauto4 `WeChatReceiver`；或 `wx_core.MonitorBackend` 的 WCDB 直读 + OCR 回退）
→ 801 网关归一化/发送闸门 → `agi/selflearn/chat.py`（记忆/心情/内状态/人格/护栏）→ LLM（DeepSeek API 主）
→ `Gateway._send` → sender 子进程。自主层的主链是网关常驻主动循环，登记表见 `agi/proactive_registry.py`。
详见 [`ARCHITECTURE.md`](../architecture/ARCHITECTURE.md)。

## 3. 里程碑（简表；细节看对应 plans 编号）

| 阶段 | 状态 | 代表产物 |
| --- | --- | --- |
| W1–W4 | 已完成 | 内状态实验、灵魂线、LoRA smoke（20 步）、评测脚本 |
| P 系列（P0–P38） | 大部分完成 | 网关护栏、媒体/贴纸、B站摄取、主动节奏、引擎/模型启停、每日语料、日记/自我叙事、记忆衰减 |
| M0 工作台 802 | 完成 | 服务/配置/日志/素材/推理过程等 API + 单页控制台 |
| M1 工作台 803（IDEA） | 完成 | 模型中心/训练工厂/数据工作室/语料清洗/用户标注/蒸馏；已作为 `idea/` 合入主仓 |
| M5 桌面化（Windows） | 完成（仅 Windows） | `tools/desktop/` launcher+tray、三服务托管、Edge app 窗口、自启、状态/停止 |
| 802 消息分析台 | 完成 | 三栏总览/对话流、联系人库隔离、图片/表情/GIF、手工发送、监听开关 |
| 技术文章 PDF | 完成 | `docs/architecture/小鱼-AI伴侣-技术与实践.pdf`（14 页 / 7 张配图） |
| 架构收口（本次） | 完成 | 死代码归档（legacy/）、架构守卫 `tools/check_architecture.py`、主动链登记表、文档按代码重写 |
| 训练闭环 | **未开始（只到语料+门禁）** | 96/300 可训练样本、`last_training=null`；无自动训练/自动上线 |

## 4. 文档现状

- **唯一入口**：[`INDEX.md`](../INDEX.md)；计划看 [`plans/README.md`](../plans/README.md)（**45 份**，15–62，缺 17/27/55）。
- 根 `plans/` 已并入 `docs/plans/`；旧文档在 `docs/archive/`（含 2026-10-28 全量快照）。
- 死代码归档区：[`../legacy/README.md`](../../legacy/README.md)（36 个归档文件 / 9078 行，只 mv 不删、可搬回；
  该 README 不计入；`tools/status_report.py` 统计整个目录会比它多 1 个文件）。
- 链接检查：`python tools/check_docs_links.py`（`docs/` 内 0 断链才退出 0）。
- 架构守卫：`python tools/check_architecture.py`（死代码回归 / import 环 / 行数预算 / 分层，0=通过）。

## 5. 已知问题 / 待办（与代码一致）

1. **训练没真正开始**：语料门禁 `MIN_SAMPLES=300`，2026-10-27 记录 96/300、`last_training=null`；
   `tools/train/train_gate.py` 默认只做计划，真训练需人工 `--execute`，且只产出 `candidate` 不切线上。
2. **发送兜底语义**：`unverified` 只表示无送达证据，不等于送达（`agi/wechat_gateway/sendclass.py`）。
3. **8081/8082 仅历史/调试**；`/agi/core*` 在 `webui/server.py` 返回 410（`agi_core_removed`）。
4. **外部依赖**：B站能力依赖仓库外 `~/.dsh/bili-duty` 插件；主对话依赖 DeepSeek API key；
   发送依赖独立 Python 3.12 venv。缺失时分别降级，不代表功能已完成。
5. 大文件待拆：`tools/console/server.py`、`agi/selflearn/proactive.py`、`agi/wechat_gateway/server.py`
   已列入架构守卫的行数预算（基线不再增长，鼓励拆分）。
6. **桌面版仅 Windows**：依赖 UIA/微信窗口与 Edge app 参数；PyInstaller exe 在本机被安全软件拦截
   （0xc0000142），推荐 pythonw 快捷方式；首次打开 Edge 窗口约 20–30s（独立 profile 冷启动）。
7. **发送窗口自检**：微信最小化/收托盘会自动恢复（`wx_core/win.py::ensure_wechat_window_visible`）；
   微信未登录/退出仍会如实报「未找到微信窗口」。
8. **产品化未开工**：一键安装/803 bot 向导/802 登录/联系人 wxid/本地+云端双模型/训练数据闭环
   仍是规划（见 [`plans/63-产品化路线图-V1-V3.md`](../plans/63-产品化路线图-V1-V3.md)）；
   当前仍是「开发者手动建环境 + 全局单数据目录」形态。

## 6. 怎么更新本页

```bat
:: 全量终验（两套测试 + 静态检查 + 生产文件未被污染），并落 JSON 证据
C:\Python314\python.exe scripts\agentb_verify.py --json docs\agentb_verify_result.json

:: 把终验 JSON 导入本文档的 STATUS:AUTO 区块（不手抄数字）
C:\Python314\python.exe tools\status_report.py --from-agentb docs\agentb_verify_result.json

:: 只刷新 collect 数/代码规模/归档量（秒级）
C:\Python314\python.exe tools\status_report.py --refresh

:: 真跑两套测试并回填实测结果（agi/tests 约 14~20 分钟）
C:\Python314\python.exe tools\status_report.py --run

:: 校验本页数字是否过期（过期退出 1）
C:\Python314\python.exe tools\status_report.py --check
```