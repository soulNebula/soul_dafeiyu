# IDEA 启动 / 停止 / 冒烟（RUN）

> 工作台仓库：`C:\Users\Administrator\Desktop\idea`
> 被管理项目：`C:\Users\Administrator\Desktop\SoulAgent`
> 本文只说明本机开发环境的启动链路，不修改业务代码。

## 1. 端口与服务

| 端口 | 服务 | 启动方式 |
|---|---|---|
| 803 | IDEA 工作台（FastAPI） | `idea\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 803` |
| 801 | 微信网关 | 在 `SoulAgent` 下 `python -m agi.wechat_gateway.server` |
| 802 | 控制台 | 在 `SoulAgent` 下 `python tools/console/server.py --port 802` |
| 8123 | 灵魂模型 llama-server | `E:\agent\llama\cuda\llama-server.exe -m <gguf> --port 8123 ...` |
| 8125 | 本地 3B | 在 `SoulAgent` 下 `python tools/soul_serve/serve_local3b.py` |

日志默认在 `E:\agent\logs\`（可用 `AGENT_HOME` 覆盖）。

## 2. 一键启动

```bat
cd C:\Users\Administrator\Desktop\idea
scripts\start_all.bat
```

只启动部分服务：

```bat
scripts\start_all.bat --only gateway,console,workbench,models
scripts\start_all.bat --only workbench
```

- 端口已监听 → 打印“已在运行，跳过”，不会重复启动；
- 启动后最多等待 30 秒；失败会打印对应日志路径并返回非 0；
- `models` 同时启动 8123 和 8125；
- `start_all.sh` 为 macOS/Linux 预留版（命令/模型路径可用环境变量覆盖）。

## 3. 停止

```bat
scripts\stop_all.bat
```

- 只按 **801/802/803/8123/8125** 的监听 PID 杀进程，不碰其他端口；
- 先 dry-run 查看会杀谁：

```bat
scripts\stop_all.bat --dry-run
```

- 停止不会删除数据/日志；重启前确认没有其他实例占用同一端口。

## 4. 全服务冒烟

```bat
cd C:\Users\Administrator\Desktop\idea
.venv\Scripts\python.exe scripts\smoke_all.py
```

- 覆盖：
  - 803：`/health`、overview/config/services/db databases/train datasets/models running/models disk
  - 801：`/health`、`/status`（`X-Admin-Token`）
  - 802：`/api/health`
  - 8123/8125：`/v1/models`
- 输出 PASS/FAIL 表；全部通过退出码 0，否则 1。
- Token 不再有固定默认值：首次启动生成随机 token 写入 `idea/config.json`（已 gitignore），
  启动日志会打印一次带 token 的 URL；也可用 `--token` 或 `IDEA_TOKEN` 覆盖：

```bat
scripts\smoke_all.py --token <idea/config.json 里的 admin_token>
```

## 5. 常用环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SOULAGENT_HOME` | `%USERPROFILE%\Desktop\SoulAgent` | 被管理项目目录 |
| `AGENT_HOME` | `E:\agent` | 日志/模型根目录 |
| `IDEA_PYTHON` | `C:\Python314\python.exe` | 启动网关/控制台/3B 的 Python |
| `IDEA_TOKEN` | 读 `idea/config.json` 的 `admin_token` | 工作台 token（smoke 也读取；显式设置优先生效） |
| `LLAMA_SERVER` | `E:\agent\llama\cuda\llama-server.exe` | 灵魂模型可执行文件 |
| `SOUL_GGUF` | `E:\agent\output\gguf\soul_qwen25_05b_v1b-q4_k_m.gguf` | 灵魂模型权重 |

## 6. 常见问题

### 端口被占用

- 启动脚本会跳过已监听端口，不会重复拉起；
- 若想重启：`scripts\stop_all.bat`（先 `--dry-run` 确认 PID）后再 `start_all.bat`；
- 不要用 `taskkill /IM python.exe` 全杀，可能误伤其他服务。

### 803 起不来

- 确认 `.venv\Scripts\python.exe` 存在；没有则先按 README 建 venv 并装 `requirements.txt`；
- 看 `E:\agent\logs\workbench_803.log`；
- 检查 803 是否被旧工作台占用。

### 801/802 起不来

- 确认在 `SoulAgent` 目录下有 `agi/`、`tools/console/`；
- 看 `E:\agent\logs\gateway_run.log` / `console_802.log`；
- 801 依赖微信窗口/配置；微信未登录/窗口不可用时网关可能启动但功能不可用；
- 802 的 `/api/health` 在本机回环可访问；需要 token 的接口带 `X-Admin-Token`。

### 8123/8125 起不来

- 8123 需要 `E:\agent\llama\cuda\llama-server.exe` 和对应 GGUF；
- 8125 需要 `tools/soul_serve/serve_local3b.py` 依赖已安装；
- 分别看 `llama_8123.log` / `llama_8125.log`；
- 模型加载较慢时 smoke 会超时失败，等待服务就绪后重跑。

### smoke 报 401

- 工作台/网关/控制台使用 token；803 的 token 见 `idea/config.json` 的 `admin_token`（启动日志也会打印带 token 的 URL），
  或 `IDEA_TOKEN` 显式覆盖；网关 token 与 `agi/data/wechat/config.json` 的 `admin_token` 一致（控制台可在本机 `/api/token` 查看）。

### 微信窗口

- 801 的窗口/OCR 路径需要微信客户端已登录、窗口可恢复；
- 数据库直读依赖本机微信数据与权限；
- 无窗口时网关仍可能健康，但监听/发送能力降级或不可用。

## 7. 注意

- 本脚本只做本机开发启停/健康检查，不修改 `app/`、`SoulAgent` 代码；
- 不提交 `.venv`、日志、`config.json` 中的密钥；
- 自动化发送/读取微信数据有合规风险，先阅读 `SoulAgent/docs/风控与合规说明.md`。
