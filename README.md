# 小鱼 · 会聊天的本地伴侣

[![CI](https://img.shields.io/badge/CI-GitHub%20Actions-blue.svg)](.github/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)

> 微信里真实收发消息的本地 AI 伴侣：有长期记忆、自己的心情、会主动找人、会写日记、
> 会上网冲浪，也会被你的反馈一点点改变。**大模型负责「会说」，这个项目负责「是谁」**——
> 记忆、情绪、人格、关系、欲望都在模型外面，逐层注入、逐层约束。
>
> 本文的边界声明：下面「已实现 / 还没做到 / 外部依赖」三段按代码现状写（核对日期 2026-10-29），
> 数字可用第 6 节的命令复算；不要拿宣传口径替代事实。

## 1. 已实现（能在代码里核对）

- **微信真收发**：装一个 801 网关；收消息有两条后端可选（wxauto4 `WeChatReceiver`，或
  `wx_core.MonitorBackend` 的 WCDB 数据库直读 + OCR 回退），发消息只有 `Gateway._send` 一个入口，
  持全局发送锁、串行化 reactive/主动/人工三条路径。
- **发送闸门**：STOP 文件 → 引擎开关 → 白名单（空白名单 fail-closed）→ 内容检查 → `send_mode`
  （off/confirm 草稿/auto）→ 节流（最小间隔、每小时 40、每天 200）→ 夜间静默 → 发送器子进程。
- **长期记忆**：记忆抽取/合并/冲突处理/检索 + 类海马体衰减（分层、半衰期、dormant 200 天后才可能清理，默认 dry-run）。
- **自己的情绪**：每轮判心情（`moodDelta` + 7 维情绪 + reason + confidence），基线 mood 70，
  mood 半衰期 3h、7 情绪 90min；心情影响说话方式与主动程度。
- **人格**：`persona_layers.py` 三层 core（人工批准）/ evolving（证据累积+限速）/ relations。
- **主动**：主链是网关常驻循环（见 [`docs/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) 第 6 节），
  由 pacing（对等）× initiative（熟悉度/心情/信任）× 内驱力队列共同决定，不是定时群发。
- **日记 / 周复盘**：每天 23:00 写第一人称日记；周日 23:30 后做周复盘并提炼进核心人格层。
- **上网**：天气/新闻/网页搜索（`agi/integrations/web_lookup.py`）；B站素材摄取（见「外部依赖」）。
- **可观测**：802 控制台能看每条回复的 prompt/记忆/内状态/心情/生成/润色/护栏/耗时（`turn_traces`）。
- **测试**：根 `tests/` + `agi/tests/` 两套离线测试；CI 跑密钥扫描与两套测试
  （`.github/workflows/ci.yml`）。

## 2. 还没做到 / 明确的限制（不要当成已实现）

- **增量训练还没真正开始**：语料门禁要求 ≥300 条可训练样本，2026-10-27 的记录是 **96/300**，
  `agi/data/train/history.jsonl` 的 `last_training=null`（未训练、未合并、未切线上）；
  目前唯一跑通过的是 W4-B 的 20 步 smoke（见 `docs/plans/24`），没有产出线上 adapter。
  仓库内**没有自动训练、自动上线路径**：`tools/train/train_gate.py` 默认只做计划，
  真训练要人工 `--execute`，结果只标 `candidate`。
- **本地小模型不是主模型**：主对话仍是 DeepSeek API；本地 0.5B 只产内状态（shadow），
  本地 3B 只做摘要/关键词/提取。「像她」目前主要靠记忆/人格/护栏，不是权重。
- **兜底发送 `unverified` ≠ 送达**：`sendclass.py` 里 `unverified` 只表示「调用返回 ok 但没有送达证据」
  （如窗口兜底 `verified=False`），不计 sent、不重置熔断；对外说明不能写成「已送达」。
- **群聊未支持**；引用/拍一拍/@ 是启发式识别。
- **公众号全文**受微信 IP 级风控：只能搜标题摘要，或用户自己复制正文。
- **8081/8082 是 legacy**：`web_app.py`（旧 wx_box 网页）与 `agi.selflearn.web`（8082 调试页）都不在主链，
  日常入口只有 801 网关 + 802 控制台；`/agi/core*` 旧接口已返回 410（`agi_core_removed`）。
- **没有实现的「意识」**：这里做的是行为与叙事上的连续自我，不宣称意识。

## 3. 外部依赖（仓库里没有、必须自备）

| 依赖 | 说明 | 缺失时 |
| --- | --- | --- |
| Windows 10/11 + 微信 PC 客户端 | 收发都靠 UIA/窗口操作，Windows-only | 无法收发 |
| Python **3.12+**（主进程可用 3.14） | 仓库在 3.12 / 3.14 上验证过 | 无法运行 |
| 独立 **Python 3.12 venv** | 发送器（wechatauto/wxauto4）跑在这个 venv 的子进程里（Python 3.14 主进程不 import wx 系依赖） | 发送失败/降级 |
| DeepSeek API key | 主对话模型；用环境变量 `DEEPSEEK_API_KEY` 注入（见第 5 节） | 只能走规则兜底 |
| `~/.dsh/bili-duty` 插件（仓库外） | B站后台刷视频/读字幕评论/写日报与凭证；本仓库只读它的产物 | B站/冲浪素材降级，主链不受影响 |
| 本地 llama-server（可选） | 0.5B 内状态（8123）/ 3B 提取（8125） | 内状态关闭，聊天照常 |
| 模型资产（仓库外） | `E:\agent` 下的 GGUF/adapter/日志；路径由 `agi/paths.py` 解析 | 本地模型相关能力不可用 |

## 4. 快速开始

```bat
:: 1) 一键桌面版（确保 801/802 在跑并打开控制台窗口）
启动桌面版.bat

:: 2) 或者手动分开起
C:\Python314\python.exe -m agi.wechat_gateway.server          :: 网关 801
C:\Python314\python.exe tools\console\server.py --port 802    :: 控制台 802
C:\Python314\python.exe tools\soul_serve\serve_local3b.py     :: 本地 3B 8125（可选）
E:\agent\llama\cuda\llama-server.exe -m E:\agent\output\gguf\soul_qwen25_05b_v1b-q4_k_m.gguf --port 8123  :: 内状态 8123（可选）

:: 3) 桌面 GUI（可选；--fake 不连微信）
C:\Python314\python.exe app.py --fake
```

打开 <http://127.0.0.1:802>。管理令牌不是固定默认值：空/弱口令（如 `8888`/`admin`/`123456`）
启动时会被**自动轮换成 32 位随机 token**（`agi/wechat_gateway/control.py` 的 F3 逻辑），
去 `agi/data/wechat/config.json` 的 `admin_token` 或控制台设置页读当前值；管理 API 用
`X-Admin-Token` 请求头认证。
注意：**白名单为空时网关拒绝发送**（fail-closed），先在控制台「设置」里配置 `allowed_contacts`。

## 5. 配置与密钥（明文 key 请尽快迁到 env）

- 配置：`agi/data/wechat/config.json`（白名单、主动、静默、限流、send_mode 等，默认值见
  `agi/wechat_gateway/config.py` DEFAULTS）；运行数据都在 `agi/data/**`，**不入库、不打包**。
- 密钥取值顺序（`agi/selflearn/llm.py`）：`DEEPSEEK_API_KEY` 环境变量 → `agi/data/secrets.json`
  （`SOULAGENT_SECRETS` 可换路径）→ `web_config.json`（兼容旧部署，命中会告警一次）。
- 迁移建议：先 `python tools/rotate_secrets.py --dry-run` 看计划，再设环境变量并轮换旧 key；
  不要把 key 写进任何会被提交/外发的文件。
- 人格设定书：`agi/persona/<名字>/`（安装/卸载：`python -m tools.persona.install_xiaoyu`）。

## 6. 如何验证本文档里的数字

```bat
:: 全量终验：两套测试 + 静态检查 + 生产文件未被测试污染，输出证据表
C:\Python314\python.exe scripts\agentb_verify.py

:: 测试用例数（collect）、代码规模、legacy 归档量——都是现场算的
C:\Python314\python.exe tools\status_report.py

:: 跑两套测试并把真实 passed/failed/耗时回填 docs/STATUS.md（agi/tests 约 14~20 分钟）
C:\Python314\python.exe tools/status_report.py --run

:: 导入终验 JSON（不手抄数字）
C:\Python314\python.exe tools/status_report.py --from-agentb docs/agentb_verify_result.json

:: 文档数字是否过期（过期退出 1）
C:\Python314\python.exe tools/status_report.py --check

:: 架构守卫（死代码/import 环/行数预算/分层）
C:\Python314\python.exe tools/check_architecture.py

:: 文档链接检查（docs/ 内断链为 0 才退出 0）
C:\Python314\python.exe tools/check_docs_links.py
```

真实测试数字以 [`docs/STATUS.md`](docs/quality/STATUS.md) 的 `STATUS:AUTO` 区块为准（由
`scripts/agentb_verify.py` 全量终验 + `tools/status_report.py --from-agentb` 生成，不手抄）；
本文只给方法和口径。安全与留存相关工具见 [`docs/INDEX.md`](docs/INDEX.md) 第 2 节
（`rotate_secrets.py` 密钥迁移、`prune_chat_logs.py` 留存清理，二者默认 dry-run）。

## 7. 目录

```
SoulAgent/
├── agi/                   核心包：selflearn/ inner/ llm/ integrations/ wechat_gateway/ soul/ persona/
├── tools/                 控制台(802)/ 桌面版/ soul_serve(8125)/ train/ persona/ 校验工具
├── wx_core/               wxauto4 封装、WCDB 直读、OCR、发送底座
├── webui/ ui/             legacy 网页端 / Tk 桌面 GUI
├── Demo/                  独立工具实现（tools_impl 被 tools/ 动态 import）
├── legacy/                死代码归档（只 mv 不删；清单见 legacy/README.md）
├── tests/ docs/ scripts/  测试 / 文档 / 发布脚本
├── app.py web_app.py      桌面 GUI / legacy 网页端入口
└── README.md
```

模型与运行数据（GGUF/adapter/日志）放在 **`E:\agent`**，与项目目录分离；详见 [`docs/PATHS.md`](docs/guides/PATHS.md)。

## 8. 文档

- [架构说明](docs/architecture/ARCHITECTURE.md)（双后端、801/802、发送闸门、主动链、数据层）
- [文档总入口](docs/INDEX.md)
- [当前状态与可复现测试数字](docs/quality/STATUS.md)
- [迭代计划索引](docs/plans/README.md)
- [使用与运维说明](docs/guides/README-usage.md)
- [死代码归档清单](legacy/README.md)
- [历史/归档](docs/archive/)