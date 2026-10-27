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
