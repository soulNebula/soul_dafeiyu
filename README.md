# IDEA · SoulAgent 工作台（Workbench）

> 项目的「IDE」：在浏览器里管理 SoulAgent 的服务、配置、数据库、记忆、对话流水线、训练工厂与 Git。
> 设计文档见 SoulAgent 仓库：`docs/specs/11-工作台-Workbench.md`、`docs/specs/10-训练工厂-TrainLab.md`。

## 当前阶段：M0.1（VS Code 风格骨架）

已完成的目标范围：

- [x] FastAPI 后端（803，本机 127.0.0.1）+ Token 鉴权（默认 `8888`）
- [x] **VS Code 风格 UI**：标题栏 / 活动栏 / 侧栏 / 编辑器标签页 / 底部面板 / 状态栏
- [x] **单页欢迎界面**：顶部任务栏（打开文件夹/新建文件/运行/源代码管理/服务/设置/总览/连接）+ 项目状态卡 + 常用任务
- [x] **资源管理器**：文件树 + **打开文件夹选择器**（可添加任意本机目录为根）、点击打开、Ctrl+S 保存
- [x] **顶部菜单**：☰ 打开文件夹/新建文件/保存/服务/设置/连接/关于（已移除不可用的命令输入框）
- [x] **连接弹窗**：左下角钥匙或菜单打开，填令牌重连
- [x] **代码编辑器**：Monaco（CDN 加载，离线自动退回 textarea）
- [x] **代码工作区**：运行 Python（可选 4 个解释器，含训练 venv312/torch），输出进「终端」面板
- [x] **代码模板**：PyTorch 环境自检 / LoRA SFT 训练骨架，一键生成到 `scratch/`
- [x] **Git Studio**：变更列表、diff 查看、分支下拉/新建切换、提交/推送、历史
- [x] **服务管理**：801/802/8123/8125 启停/重启/日志
- [x] **设置（按域分页）**：每个配置分组独立一页，保存即校验+备份+审计
- [x] 审计日志（sqlite，before/after）
- [x] 测试（pytest）

## 快速开始

```bat
cd C:\Users\Administrator\Desktop\idea
scripts\run.bat
```

浏览器打开 <http://127.0.0.1:803>，右上角填 token（默认 `8888`）。

## 界面速览（VS Code 风格）

```
标题栏：菜单 / 项目名 / 令牌连接
活动栏：资源管理器 | 源代码管理 | 运行 | 服务 | 设置 | 总览 | 训练(M3) | 数据(M1)
侧栏  ：文件树 / Git 变更 / 运行任务 / 服务卡片 / 设置分组
编辑区：Monaco 编辑器 + 标签页；设置页按域独立渲染；总览页
底部  ：终端（运行输出）| 输出 | 审计
状态栏：分支 · 服务运行数 · python/torch · 连接状态
```

## 代码工作区怎么用

1. 侧栏「运行」→ 选择解释器（含 `E:/agent/venv312`，带 torch 2.6 + CUDA）
2. 点「新建训练脚本（模板）」生成 `scratch/pytorch_train_lora.py`
3. 打开文件 → 点「运行当前 Python 文件」→ 输出实时进终端
4. 改完 Ctrl+S 保存；提交走 Git 侧栏

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
