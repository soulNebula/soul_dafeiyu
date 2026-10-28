# IDEA 目录结构（STRUCTURE）

> 配合 [README](../README.md) / [RUN](RUN.md) 阅读。目标是让新接手的人 5 分钟内知道
> 每个目录该改什么、不该改什么。

## 1. 顶层

| 路径 | 职责 | 修改注意 |
| --- | --- | --- |
| `app/` | 803 FastAPI 后端与各业务纯函数 | 业务接口优先改对应 `*studio.py`，`main.py` 只做路由接线 |
| `web/` | 单页前端（`index.html` / `style.css`） | 前端全部逻辑集中在 index 的内联 JS；不引入构建链 |
| `tests/` | 离线 pytest 套件 | 全部 tmp/伪装 backend，不访问真实 SoulAgent 数据 |
| `scripts/` | 启动、停止、冒烟、验收 | 只做本机运维编排，不承载业务逻辑 |
| `docs/` | 运行/结构文档 | `RUN.md` 启动链路；`STRUCTURE.md` 本文件 |
| `reports/` | 验收报告 | `latest.md` 最新；`archive/` 历史只归档不删除 |
| `.data/` | 运行时数据（gitignore） | 含截图、SQLite、日志、训练/语料/蒸馏任务状态 |
| `.venv/` | 本机 Python 环境（gitignore） | 不入库 |
| `.github/workflows/ci.yml` | CI（Python 3.12 离线 pytest） | 新增依赖同步 `requirements.txt` |

## 2. `app/` 后端

| 文件 | 负责 | 主要接口前缀 |
| --- | --- | --- |
| `main.py` | FastAPI app、token 依赖、路由接线、静态资源 | `/health`、`/api/wb/*` |
| `trainstudio.py` | 数据集发现/过滤/预览、训练任务、导出、聊天库发现 | `/api/wb/train/*` |
| `corpusstudio.py` | 语料上传、异步 OCR/解析、items 选择、保存数据集 | `/api/wb/corpus/*` |
| `annotstudio.py` | 标注建议、InnerState 校验、写 soul 数据集 | `/api/wb/annot/*` |
| `distillstudio.py` | 蒸馏任务（mock/real）、成本估算、日志、取消、数据集清单 | `/api/wb/distill/*` |
| `datastudio.py` | 扫描 SQLite、schema/rows/SQL/update/备份、友好名/隐藏排序 | `/api/wb/db/*` |
| `modelstudio.py` | 模型/适配器/GGUF 清单、运行状态、导入、下载、合并、转换 | `/api/wb/models/*` |
| `files_api.py` | 文件树/读写/新建（最小文件能力） | `/api/wb/files/*` |
| `services.py` / `config_store.py` / `audit.py` 等 | 最小 M0：服务、配置、审计 | `/api/wb/services|config|audit` |

约定：
- 业务逻辑尽量做纯函数（可单测），路由只解包参数、做审计；
- 所有写操作必须 token + 审计；涉及外部模型/训练/OCR 的耗时任务用子进程/线程；
- 数据路径统一来自 `app.paths`（`SOULAGENT_HOME` / `AGENT_HOME`），不要在模块里硬编码用户目录。

## 3. `web/` 前端

- `index.html`：单页应用，所有视图在 `<section id="view-*">`，路由由 hash/`?view=` 驱动；
- `style.css`：暗色主题、卡片/表单/表格/气泡/进度条；只追加类，避免影响既有视图；
- 视图清单：`welcome / overview / services / config / data / train / distill / corpus / annotate / models`；
- 全局终端面板：右侧停靠、拖拽变宽、`body.term-open` 让主区域留白；内联终端不受影响；
- 中文化：枚举/状态统一走 `zh(cat, v)` 映射，兜底原值；API 仍用英文值。

## 4. `tests/`

- `test_*studio*.py`：各工作室纯函数 + HTTP handler 级测试；
- `test_console*`：不在此仓库（802 控制台在 `SoulAgent/tools/console`）；
- 全部用 `tmp_path` 注入 `paths.*_home`、mock subprocess/LLM/网络；
- 运行：`.venv\Scripts\python.exe -m pytest tests -q`。

## 5. `scripts/`

| 脚本 | 用途 |
| --- | --- |
| `start_all.bat/.sh` | 按端口启动 801/802/803/8123/8125（已监听则跳过） |
| `stop_all.bat` | 按 801/802/803/8123/8125 监听 PID 停止，不动其他端口 |
| `smoke_all.py` | 全服务 HTTP 冒烟（803/801/802/8123/8125），打印 PASS/FAIL |
| `acceptance.py` | 生成验收报告到 `reports/` |
| `port_pid.py` / `wait_port.py` | 端口/PID 小工具 |

## 6. `.data/` 运行数据

```text
.data/
├─ screens/          # 验收截图（只保留每功能一张代表图）
├─ distill_jobs/     # 蒸馏任务 meta/log
├─ corpus/           # 语料任务 raw/items
├─ trainjobs/        # 训练任务 meta/log
├─ db_backups/       # 数据工作室备份
├─ *.sqlite3         # 本机运行库（gitignore）
└─ server.log 等      # 运行日志
```

`.data/` 是**本机状态**，不随仓库分发；清空它会丢任务/截图/临时数据。

## 7. 报告

- `reports/latest.md`：最新验收摘要；
- `reports/archive/`：历史报告（按时间保留，不删除）；
- 不要在根目录留临时 `*.log`/`*.tmp`/`*.bak`；调试截图只放 `.data/screens/`，
  完成验收后按 `<页面>_<状态>.png` 收敛成一张代表图。
