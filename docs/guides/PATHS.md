# PATH 配置（P0-3）

SoulAgent 的「agent 数据根」不再写死 `E:\agent`。代码统一通过 `agi/paths.py` 取路径；
换机器/换盘只改配置或环境变量即可。

## 1. 解析优先级

`agi.paths.agent_home()` 依次尝试：

| 顺序 | 来源 | 示例 |
| --- | --- | --- |
| 1 | 环境变量 `AGENT_HOME` | `set AGENT_HOME=D:\agent` |
| 2 | 环境变量 `SOULAGENT_AGENT_HOME` | `set SOULAGENT_AGENT_HOME=D:\agent` |
| 3 | 配置文件 `agi/data/paths.json` 的 `agent_home` | `{"agent_home": "D:\\agent"}` |
| 4 | 默认值 `E:\agent`（老机器零配置保持可用） | — |

相对路径按 **仓库根目录** 解析（例如 `"my_agent"` → `<repo>/my_agent`）；
支持 `~` 和 `%VAR%`/`$VAR` 展开。

## 2. 派生目录

| 函数 | 默认位置 | 单项 env 覆盖 | `paths.json` 键 |
| --- | --- | --- | --- |
| `agent_home()` | `E:\agent` | `AGENT_HOME` / `SOULAGENT_AGENT_HOME` | `agent_home` |
| `models_dir()` | `<home>\models` | `AGENT_MODELS_DIR` | `models_dir` |
| `gguf_dir()` | `<home>\output\gguf` | `AGENT_GGUF_DIR` | `gguf_dir` |
| `adapters_dir()` | `<home>\output`（LoRA/训练产物根） | `AGENT_ADAPTERS_DIR` | `adapters_dir` |
| `output_dir()` | `<home>\output` | `AGENT_OUTPUT_DIR` | `output_dir` |
| `logs_dir()` | `<home>\logs` | `AGENT_LOGS_DIR` | `logs_dir` |
| `venv312_python()` | `<home>\venv312\Scripts\python.exe` | `AGENT_VENV312_PYTHON` | `venv312_python` |
| `llama_bin()` | `<home>\llama\cuda` | `AGENT_LLAMA_BIN` | `llama_bin` |
| `convert_dir()` | `<home>\llama\convert` | `AGENT_CONVERT_DIR` | `convert_dir` |
| `torch_lib_dir()` | `<home>\venv312\Lib\site-packages\torch\lib` | — | — |
| `tmp_dir()` | `<home>\tmp` | `AGENT_TMP_DIR` | `tmp_dir` |
| `qq_export_dir()` | `<home>\qq_export\c2c` | `AGENT_QQ_EXPORT_DIR` | `qq_export_dir` |
| `backups_dir()` | `<home>\backups` | `AGENT_BACKUPS_DIR` | `backups_dir` |

辅助函数：`agent_path(*parts)`（自动处理 `/` 和 `\` 分隔）、`repo_root`、`PATHS_JSON`、
`describe()`（诊断当前解析结果与来源）。

## 3. 配置文件示例

`agi/data/paths.json`（**本机配置，不入库**）：

```json
{
  "agent_home": "D:\\agent",
  "models_dir": "D:\\agent\\models",
  "gguf_dir": "D:\\agent\\output\\gguf",
  "logs_dir": "D:\\agent\\logs"
}
```

## 4. 迁移例子：`E:\agent` → `D:\agent`

1. 拷贝/移动数据：`robocopy E:\agent D:\agent /E`（或先复制再改配置验证）；
2. 二选一：
   - 临时（当前终端）：`set AGENT_HOME=D:\agent`（PowerShell：`$env:AGENT_HOME="D:\agent"`）
   - 永久：写 `agi/data/paths.json`：`{"agent_home": "D:\\agent"}`
3. 验证：
   ```bash
   python tools/check_portability.py
   python -c "from agi import paths; print(paths.describe())"
   ```
4. 如果只用单项目录不同（例如模型在 NAS），只覆盖对应项（`models_dir` / `gguf_dir` …）。

## 5. 已接入的代码

`agi/eval_data/tone_*.py`、`agi/selflearn/model_registry.py`、`agi/soul/evaluate.py`、
`agi/tests/test_downstream.py`、`agi/tests/test_soul_eval.py`、`tools/` 下的训练/评测/
本地服务/控制台/桌面启动脚本（`tools/build_*`、`tools/soul_serve/*`、`tools/train/*`、
`tools/console/server.py`、`tools/desktop/launcher.py`、`tools/local3b_quality_eval.py`）
以及 `tools/soul_serve/start_server.ps1`。旧机器不设任何配置仍走 `E:\agent`。

## 6. 可移植性冒烟

```bash
python tools/check_portability.py            # 代码 PASS/FAIL；文档旧路径 WARN
python tools/check_portability.py --strict-docs   # 文档旧路径也算 FAIL
```

检查项：
- 代码（`.py`/`.ps1`）无 `E:\agent` 硬编码（`agi/paths.py` 的默认值除外）；
- `requirements.txt` 每行可被 PEP 508 解析，且不再有 `pycryptodome==4.1.0`；
- `AGENT_HOME` → `SOULAGENT_AGENT_HOME` → `paths.json` → 默认 的 fallback 与实际派生目录；
- 退出码 0 = PASS。

## 7. 隐私 / 注意

`agi/data/paths.json` 只包含本机路径，不含密钥；不要提交它（`.gitignore` 已忽略
`agi/data/`）。如果目录不存在，各模块按旧逻辑在启动/写入时创建或给出明确报错。

## 8. 换机迁移清单（新电脑从零跑起来）

目标：把「代码 + 本机数据 + 本机密钥」三件事分清楚，**不要**把密钥/聊天记录跟着
仓库一起拷来拷去。按顺序做，任何一步失败就先停，不要跳步。

### 8.1 环境（Python 3.12+）

```bat
:: 1) 装 Python 3.12+（勾选 Add python.exe to PATH）
python -V
:: 2) 装运行时依赖（不写死解释器路径）
python -m pip install -r requirements.txt
:: 3) 跑离线测试确认代码本身可用（不碰微信、不联网）
python -m pytest tests -q
```

训练/评测/本地大模型这条线另外需要一个 **独立的 3.12 venv**（和主进程隔离）：

```bat
python -m venv E:\agent\venv312
E:\agent\venv312\Scripts\python.exe -m pip install -r requirements.txt
```

`venv312` 的位置由 `agi/paths.py::venv312_python()` 解析（默认 `<agent_home>\venv312`），
换机后要么保持默认，要么在 `agi/data/paths.json` 写 `"venv312_python"`。

### 8.2 数据根（AGENT_HOME）

| 项 | 新机器怎么做 |
| --- | --- |
| `AGENT_HOME` | `setx AGENT_HOME D:\agent`（或 PowerShell `$env:AGENT_HOME="D:\agent"`） |
| 或者 | 写 `agi/data/paths.json`：`{"agent_home": "D:\\agent"}` |
| 验证 | `python -c "from agi import paths; print(paths.describe())"` |

默认仍是 `E:\agent`（老机器零配置）。**完整优先级与派生目录见 §1/§2。**

### 8.3 密钥（两条路，二选一，推荐第 1 条）

```bat
:: 路 1（推荐）：环境变量，不落盘
setx DEEPSEEK_API_KEY "<你的 key>"
:: 路 2：迁移到本机密钥库（复制 + 收紧 ACL，默认不改旧文件）
python tools/rotate_secrets.py --dry-run
python tools/rotate_secrets.py --apply
python tools/harden_acl.py --apply
```

* 读取优先级：`DEEPSEEK_API_KEY` env → `agi/data/secrets.json`（`SOULAGENT_SECRETS`
  可换路径）→ `web_config.json`（旧行为，命中会告警建议迁移）。
* `db_keys` / `image_aes_key` 这类**没法放环境变量**的密钥，靠
  `agi/data/secrets.json`；`wx_core/config.py`、`webui/llm.py`、`wx_core/web_search.py`
  都已接入。
* 换机时**只拷 `agi/data/secrets.json`**（用加密介质/U 盘），不要拷 `config.json`
  里的明文密钥块，更不要提交。

### 8.4 微信数据目录（wx_core 自动发现）

`wx_core/media_cache.py` 会按顺序找这些位置（没有环境变量开关）：

1. `%USERPROFILE%\Documents\xwechat_files`
2. `%USERPROFILE%\xwechat_files`
3. `D:\xwechat_files`
4. `E:\xwechat_files`

换机后微信数据不在上面位置时：把它移动/软链到其中一个候选路径（例如
`mklink /D "%USERPROFILE%\Documents\xwechat_files" "D:\wx_files\xwechat_files"`），
否则媒体/图片解密会「找不到缓存」（文字消息的 WCDB 直读走的是另一套定位，但同样要能
看到 `xwechat_files`）。

### 8.5 迁移后的自检

```bat
python tools/check_portability.py                    :: 代码里没有旧路径硬编码
python tools/check_secrets.py                        :: git 跟踪文件里没有密钥
python tools/harden_acl.py --verify                  :: 敏感目录 ACL 已收紧（非零 = 未收紧）
python tools/rotate_secrets.py --dry-run             :: 明文密钥迁移计划（默认只预演）
python tools/prune_chat_logs.py --dry-run            :: 聊天/媒体留存 TTL 计划（默认只预演）
python -m pytest tests -q                            :: 根测试全绿
python -m pytest agi/tests -q -k "config or llm or secret or paths"   :: 关键子集
```

### 8.6 换机时**不要**带走的东西

- `config.json` / `web_config.json`（本机配置，含历史明文密钥；在新机器重新生成/迁移）；
- `log/`（聊天记录）、`media/`（解密图片）—— 要保留请走加密备份，并设 TTL
  （`python tools/prune_chat_logs.py --apply`，默认保留 30 天）；
- `agi/data/desktop_profile/`（画像/训练派生数据，重建成本高但含隐私）。
