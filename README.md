# IDEA · SoulAgent 工作台（Workbench）

> 项目的「IDE」：在浏览器里管理 SoulAgent 的服务、配置、数据库、记忆、对话流水线、训练工厂与 Git。
> 设计文档见 SoulAgent 仓库：`docs/specs/11-工作台-Workbench.md`、`docs/specs/10-训练工厂-TrainLab.md`。

## 当前阶段：M0（骨架）

已完成的目标范围：

- [x] FastAPI 后端（803，OpenAI 无关；本机 127.0.0.1）
- [x] Token 鉴权（默认 `8888`，可用环境变量 `IDEA_TOKEN` 覆盖）
- [x] **服务管理**：gateway 801 / console 802 / soul 8123 / local3b 8125 的查看、启动、停止、日志
- [x] **配置中心**：schema 驱动（类型/范围/说明/分组）、校验、保存、热生效、审计
- [x] **Git Studio（最小版）**：状态、diff、提交、自动提交（每次保存配置自动 commit）
- [x] 审计日志（sqlite，记录每次变更 before/after）
- [x] Web 前端（原生 JS，无需构建）
- [x] 测试（pytest）

## 快速开始

```bat
cd C:\Users\Administrator\Desktop\idea
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 803
```

浏览器打开 <http://127.0.0.1:803>，右上角填 token（默认 `8888`）。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `IDEA_TOKEN` | `8888` | 工作台令牌 |
| `SOULAGENT_HOME` | `C:\Users\Administrator\Desktop\SoulAgent` | 被管理的项目根目录 |
| `AGENT_HOME` | `E:\agent` | 模型/数据/日志根目录 |

## 目录

```
idea/
├── app/            后端（FastAPI）
│   ├── main.py
│   ├── paths.py
│   ├── config_schema.py
│   ├── config_store.py
│   ├── services.py
│   ├── gitstudio.py
│   └── audit.py
├── web/            前端（index.html + app.js + style.css）
├── tests/
├── scripts/        run.bat / run.sh
└── requirements.txt
```

## 路线图

- M0 骨架（当前）
- M1 数据工作室 + 记忆工作室 + Git 自动推送
- M2 对话工作室（流水线编辑器 + 试跑）
- M3 模型中心 + 训练工厂（TrainLab）
- M4 UI 工作室
- M5 桌面化（Windows → macOS → Linux）
- M6 开源化（CI/文档/示例/许可）

## 开源

MIT（待定）。私有数据不进入本仓库；被管理的 SoulAgent 数据在 `E:\agent`，不在本仓。
