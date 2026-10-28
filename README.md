# IDEA · SoulAgent 工作台（Workbench）

> 浏览器里的 **模型 / 训练 / 数据** 工作台，外加最小 M0（服务管理、配置中心、概览）。
> 微信控制台在 802，不在本仓库；本工作台只做工作台自己的事。

## 运行

```bat
cd C:\Users\Administrator\Desktop\idea
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 803
```

打开 <http://127.0.0.1:803/?token=8888>。令牌默认 `8888`，可用 `IDEA_TOKEN` 覆盖。

## 页面（7 个）

| 页面 | 说明 |
| --- | --- |
| `welcome` | 欢迎/任务入口（默认页） |
| `overview` | 服务、模型、最近训练概览 |
| `services` | 801/802/803/8123/8125 启停、状态、日志 |
| `config` | 基础 / 模型 / 训练必要配置；保存前备份 + 审计 |
| `data` | SQLite 数据工作室：库/表浏览、分页、只读 SQL、受控写入、备份 |
| `train` | 训练工厂：数据集发现/预览、LoRA 任务创建/启动/日志、导出 |
| `models` | 模型中心：模型/适配器/GGUF、运行状态、磁盘 |

## API（精简后）

- 基础：`/health`、`/api/wb/env`、`/api/wb/overview`
- 服务：`/api/wb/services`、`/api/wb/services/{name}/{action}`、`/api/wb/services/{name}/logs`
- 配置：`/api/wb/config`、`/api/wb/config/validate`、`/api/wb/config/save`、`/api/wb/config/backups`、`/api/wb/config/restore`
- 数据：`/api/wb/db/databases`、`schema`、`rows`、`sql`、`update`、`backup`、`backups`
- 训练：`/api/wb/train/discover`、`datasets`、`assets`、`preview`、`filter`、`create`、`start`、`stop`、`jobs`、`job/{id}`、`job/{id}/log`、`export/preview`、`export`
- 模型：`/api/wb/models`、`running`、`adapters`、`disk`、`status`、`start`、`stop`、`merge`、`convert`
- 审计：`/api/wb/audit`

## 已删除

对话工作室、记忆工作室、朋友圈、草稿/发送模式、Git Studio、文件树/编辑器/运行 Python、Monaco。
这些功能不再由 IDEA 提供（朋友圈等迁移到 802 控制台）。

## 测试

```bat
.venv\Scripts\python.exe -m pytest tests -q
```

冒烟：`.venv\Scripts\python.exe scripts\smoke_all.py`
