# IDEA · SoulAgent 工作台

> 浏览器里的 **模型 / 训练 / 数据** 工作台，外加最小 M0（服务管理、配置、概览）。
> 微信控制台在 **802**（`SoulAgent/tools/console`），本仓库只负责工作台本身（**803**）。

## 定位

IDEA 是 SoulAgent 的本地开发工作台：帮你看清「模型、训练数据、服务状态、运行库数据」，
并把常用操作（启停服务、浏览/检索数据、创建训练任务、生成蒸馏语义数据、清洗语料、人工标注）
收进一个页面。所有操作都走本机回环地址 + admin token；不上传数据、不做云端托管。

## 快速启动

```bat
cd C:\Users\Administrator\Desktop\idea
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 803
```

打开 <http://127.0.0.1:803/?token=8888>。

- 默认 token `8888`，可用 `IDEA_TOKEN` 覆盖；
- 需要微信网关（801）在跑时，概览/服务页才有实时数据；802 控制台独立运行；
- 一键启停/冒烟见 [docs/RUN.md](docs/RUN.md)。

## 页面

| 视图 | 作用 |
| --- | --- |
| `welcome` | 欢迎页与快捷任务入口（默认） |
| `overview` | 服务、模型、最近训练概览 |
| `services` | 801/802/803/8123/8125 状态、启停、日志 |
| `config` | 基础/模型/训练必要配置；保存前备份 + 审计 |
| `data` | 数据工作室：数据文件下拉（中文分组/折叠旧库）、数据表浏览、搜索/翻页、底部高级 SQL |
| `train` | 训练工厂：数据集预览、从聊天记录导出、过滤、LoRA/全参数微调、任务日志 |
| `distill` | 蒸馏语义数据：模拟/真实生成、成本确认、任务进度/日志/取消、已有数据集 |
| `corpus` | 语料清洗：上传、OCR/解析进度、你/我勾选、保存为对话/指令数据集 |
| `annotate` | 用户标注：微信式对话气泡、InnerState 滑杆/下拉/星标、保存 soul 数据集 |
| `models` | 模型中心：GGUF/HuggingFace/LoRA 适配器、运行状态、导入/下载/删除 |

全局终端面板从右侧滑出（可拖拽调宽、记住宽度），不属于页面内容。

## 目录结构

```text
idea/
├─ app/                 # FastAPI 后端 + 各工作室纯函数模块
│  ├─ main.py           # 路由接线（803 入口）
│  ├─ trainstudio.py    # 训练工厂：数据集/任务/导出
│  ├─ corpusstudio.py   # 语料清洗：上传/解析/保存
│  ├─ annotstudio.py    # 用户标注：建议/校验/写 soul 数据集
│  ├─ distillstudio.py  # 蒸馏语义数据：mock/real 生成任务
│  ├─ datastudio.py     # 数据工作室：库/表/行/SQL/备份
│  ├─ modelstudio.py    # 模型中心：清单/运行/导入下载/合并转换
│  └─ files_api.py 等   # 文件/服务/配置等最小 M0
├─ web/                 # 无框架前端（index.html + style.css）
├─ tests/               # 离线 pytest（不连真实服务）
├─ scripts/             # 启动/停止/冒烟/验收脚本
├─ docs/                # RUN.md（启动链路）、STRUCTURE.md（目录职责）
├─ reports/             # latest.md（最新验收）；archive/（旧报告）
├─ .data/               # 运行数据（忽略；含 screens/ 截图、日志、数据库）
├─ requirements.txt     # 运行/测试依赖
├─ LICENSE              # MIT
└─ .github/workflows/ci.yml
```

详细职责见 [docs/STRUCTURE.md](docs/STRUCTURE.md)。

## 测试

```bat
cd C:\Users\Administrator\Desktop\idea
.venv\Scripts\python.exe -m pytest tests -q
```

- 测试全部离线：用 tmp 路径/伪装 backend，不写真实 `SoulAgent` 数据、不联网；
- 冒烟（需要 801/802/803/8123/8125 在跑）：

```bat
.venv\Scripts\python.exe scripts\smoke_all.py
```

## 截图

`.data/screens/` 只保留每个页面/功能一张代表图，命名 `<页面>_<状态>.png`：

```text
welcome_default.png     overview_default.png    services_default.png
config_default.png      data_files_default.png  train_default.png
models_default.png           corpus_progress.png     annotate_chat.png
distill_form.png        distill_done.png        terminal_open.png
```

截图用 headless Edge 生成（示例）：

```bat
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --headless=new ^
  --disable-gpu --window-size=1600,1200 --virtual-time-budget=9000 ^
  --screenshot=.data\screens\overview_default.png ^
  "http://127.0.0.1:803/?token=8888&view=overview"
```

`.data/` 整体被 gitignore，截图只是本机验收材料，不入库。

## 报告与归档

- `reports/latest.md`：最近一次全量验收报告；
- `reports/archive/`：历史验收报告（按时间保留，不删除）；
- `reports/archive/screens/`：旧验收报告配套截图；
- 旧版本临时截图/调试截图已清理，避免与当前页面混淆。

## 依赖 / CI

- `requirements.txt`：fastapi、uvicorn、httpx、pytest、python-multipart（上传接口需要）；
  `huggingface_hub`/`torch` 为可选运行时依赖（未安装时对应功能降级，不阻塞主流程）。
- CI：`.github/workflows/ci.yml`（Python 3.12，离线 pytest）。
- 许可证：MIT（`LICENSE`）。

## 已移除的功能

对话工作室、记忆工作室、朋友圈、微信草稿/发送模式、Git Studio、通用文件树/编辑器/Monaco
不再由 IDEA 提供（朋友圈/模型管理等已迁移到 802 控制台或拆分到独立页面）。历史报告仍保留
在 `reports/archive/` 供查证。
