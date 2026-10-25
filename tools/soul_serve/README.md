# tools/soul_serve —— llama.cpp 部署链路（adapter → GGUF → llama-server）

目标：绕开 HF/Python 每 token ~40ms 的固定开销，把 0.5B 灵魂模型用 **llama.cpp + GGUF Q4**
跑起来（GPU 实测生成 ~125 tok/s，128 token ≈1s），并让输出 **100% 是合法 InnerState JSON**。

链路：

```
peft adapter ──merge_adapter.py──> 合并后 HF 目录
      ──convert.py──> f16 GGUF ──llama-quantize──> Q4_K_M GGUF
      ──llama-server.exe（--jinja）──> HTTP /v1/chat/completions
      ──agi/soul/llama_client.py（json_mode=True 自动带 response_format json_schema）
```

---

## 0. 一次性准备

```powershell
# 1) 拉取与 llama-server 同 commit 的转换源码（b11857 => 701fd25b3）
E:\agent\venv312\Scripts\python.exe tools\soul_serve\fetch_convert_src.py
#   -> E:\agent\llama\convert\{convert_hf_to_gguf.py, conversion\, gguf-py\}

# 2) gguf 包（转换脚本需要；装了同版本会优先用本地 gguf-py）
E:\agent\venv312\Scripts\python.exe -m pip install gguf
```

> 为什么不用 x 随便下载的 convert 脚本：llama.cpp 的 `convert_hf_to_gguf.py` 与 `gguf-py`
> 强耦合，master 脚本 + PyPI 旧 gguf 会报
> `AttributeError: type object 'MODEL_ARCH' has no attribute 'DFLASH'`。
> 本仓库 `fetch_convert_src.py` 按 **llama-server 的 commit** 一起拉 `conversion/` 和 `gguf-py/`，
> 脚本会自动把本地 gguf-py 插到 sys.path，规避版本错位。

---

## 1. 合并 adapter

```powershell
E:\agent\venv312\Scripts\python.exe tools\soul_serve\merge_adapter.py `
  --base    E:\agent\models\Qwen2.5-0.5B-Instruct `
  --adapter E:\agent\output\soul_qwen25_05b_v0 `
  --out     E:\agent\output\merged_soul_qwen25_05b_v0
```

- 默认 `--device cpu`：merge 是纯权重加法，CPU 即可，**不占 GPU**（训练跑着也能用）；
- 实测（0.5B）：**14s**，输出 `model.safetensors` ≈ **0.92GB**。

## 2. 转 GGUF + 量化

```powershell
E:\agent\venv312\Scripts\python.exe tools\soul_serve\convert.py `
  --hf-dir E:\agent\output\merged_soul_qwen25_05b_v0 `
  --name soul_qwen25_05b_v0 `
  --out-dir E:\agent\output\gguf
```

- 产物：`<name>-f16.gguf` + `<name>-q4_k_m.gguf`；
- 实测（0.5B）：convert **~5s**，f16 **994MB**；quantize **4.2s**，Q4_K_M **379MB**。

## 3. 启动 llama-server

```powershell
# 脚本方式（自动把 torch CUDA DLL 目录加进 PATH，等价于手动 $env:PATH）
pwsh -File tools\soul_serve\start_server.ps1 `
  -Model E:\agent\output\gguf\soul_qwen25_05b_v0-q4_k_m.gguf -Port 8123
# 不占 GPU 调试：
pwsh -File tools\soul_serve\start_server.ps1 -Model <gguf> -Port 8124 -Cpu

# 或 Python 方式（评测脚本里可编程起停）
E:\agent\venv312\Scripts\python.exe tools\soul_serve\server.py `
  --model E:\agent\output\gguf\soul_qwen25_05b_v0-q4_k_m.gguf --port 8123
```

**必须注意**：

- `PATH` 里必须有 `E:\agent\venv312\Lib\site-packages\torch\lib`，否则 `ggml-cuda.dll`
  加载失败（`start_server.ps1` / `server.py` 已内置）；
- 必须 `--jinja`：由 GGUF 自带 chat template 渲染 messages（client 不本地套模板）；
- **不要加 `--json-schema-file/-jf`**（见下节）。

## 4. 客户端

```python
from agi.soul.llama_client import make_client

client = make_client("http://127.0.0.1:8123")          # timeout_s 默认 30
assert client.available()                               # GET /health
result = client.chat(messages, purpose="soul_sft", max_tokens=300,
                     temperature=0.0, json_mode=True)
# result: {ok, text, usage:{prompt_tokens,completion_tokens,total_tokens},
#          timings:{prompt_per_second,predicted_per_second,...}, elapsed, model, error?}
```

接口与 `agi.selflearn.llm.Client.chat` 同签名（鸭子类型），`messages` 直接用训练数据里的
`[{"role","content"}]`（context 里的 system/history + 最后一条 user）。

---

## 5. ⚠ JSON 约束方式实测结论（重要）

llama.cpp **build b11857（0.7.0-dev, commit 701fd25b3）** 的 `llama-server.exe` 同时提供：

| 方式 | 参数 | 实测结果 |
| --- | --- | --- |
| 服务端常驻 schema | `-j / --json-schema`、`-jf / --json-schema-file` | **不可用（本版本 + Qwen2.5 jinja template 有 bug）** |
| 请求级 schema | `POST /v1/chat/completions` body 里 `response_format={"type":"json_schema","json_schema":{...}}` | **可用**，HTTP 200，输出被 grammar 约束 |
| GBNF | `--grammar` / body `grammar` | 未采用（请求级 json_schema 已满足） |

服务端 `-jf` 的原始报错：

```
Failed to initialize samplers: Unexpected empty grammar stack after accepting piece: <|im_start|> (151644)
```

原因：带 `--jinja` 时 chat template 会生成 `<|im_start|>assistant` 前缀，而 `-jf` 的 grammar
sampler 从第一枚 token 就要求 JSON（`{`），和特殊 token 冲突 → 400。

**结论：server 只带 `--jinja`；JSON 约束走请求级 `response_format`。**
`agi/soul/llama_client.py` 在 `json_mode=True` 时默认附加该字段（`use_response_format=True`），
所以上层（evaluate / inject.model）照常 `json_mode=True` 即可，**G1 由解码器保证**。

如果以后 llama.cpp 修了 `-jf`：`make_client(..., use_response_format=False)` +
`start_server(..., server_json_schema=True)` 即可切回服务端常驻约束。

InnerState schema 在 **`agi/soul/inner_state_schema.json`**：`additionalProperties: false`，
`stage` / `behavior.length` / `behavior.emoji` 用中文/英文枚举（emoji = `偶尔|不用`），
数值只约束 0–1（软值，推理侧会覆盖为真实状态）。

---

## 6. 实测数据

### CPU 冒烟（`-ngl 0`，base / v0 adapter，仅验证链路）

| 场景 | 结果 |
| --- | --- |
| server 启动 → `/health` | 1.6s 内 healthy |
| 无约束 plain 请求（base） | 200，0.73s / 31 tok |
| 请求级 json_schema（base） | 200，175 tok / 5.8s（CPU，prompt 144 tok） |
| v0 adapter Q4_K_M，v1_val 前 3 行 | **JSON 合法率 3/3**，2.79–3.17s/条；输出结构正确（stance/intent/monologue 中文、behavior 枚举合法） |
| 连续 8 条请求（同一 server，v0 adapter） | **8/8 成功**，2.37–3.53s/条，CPU 生成 ~40–49 tok/s，无 grammar/连接错误 |
| merge / convert / quantize | 14s / ~5s / 4.2s |

> CPU 数字只证明链路通；上线看 GPU 数字（见下）。

### GPU 实测（RTX 4050 Laptop 6GB，v1 adapter，Q4_K_M，`-ngl 99 -c 2048 -np 1`）

20 条 v1_val（报告：`E:\agent\logs\llama_v1_gpu_smoke.json`）：

| 项 | 数值 |
| --- | --- |
| server start → healthy | **1.56s** |
| 端到端（含 HTTP）延迟 | **mean 0.587s / p50 0.576s / p95 0.634s / max 0.971s** |
| 生成速度 | **mean 227.6 tok/s**（min 132.1） |
| token | prompt 147.3 / completion 128.1（均值） |
| **G1 JSON 合法率（json.loads + schema.validate）** | **20/20 = 100%** |
| llama-server 显存 | 运行期 **~667 MiB**（min 667 / max 1920，max 是同时加载 HF 对比模型时的合计） |
| 与 HF 同 adapter 一致性（5 条，bf16 贪心 vs Q4_K_M 贪心） | behavior 四字段 **19/20 = 95%**（length 5/5、advice 5/5、emoji 5/5、ask 4/5）；stance 精确 1/5（字符 Jaccard 0.72）、intent 精确 0/5（Jaccard 0.40）——自由文本措辞不同但语义同向 |

> 对比：HF/Python 0.5B 生成 128 token 约 **12.6s**；llama.cpp Q4_K_M **0.59s**，约 **21x**。
> 单条 128 token 的 ~1s 预算已达标。

验收命令（20 条 + HF 对比）：

```powershell
E:\agent\venv312\Scripts\python.exe tools\soul_serve\smoke_eval.py `
  --gguf E:\agent\output\gguf\soul_qwen25_05b_v1-q4_k_m.gguf `
  --limit 20 --ngl 99 `
  --hf-dir E:\agent\output\merged_soul_qwen25_05b_v1 `
  --out E:\agent\logs\llama_v1_gpu_smoke.json
```

---

## 7. 踩坑清单

1. **ggml-cuda.dll 加载失败** → PATH 加 `E:\agent\venv312\Lib\site-packages\torch\lib`；
2. **convert 报 `MODEL_ARCH has no attribute DFLASH`** → convert 脚本/gguf-py 版本与 binary 不一致，
   用 `fetch_convert_src.py --ref 701fd25b3` 拉同 commit 源码；
3. **GitHub raw 被 reset** → 走 `https://gh-proxy.com/`（fetch 脚本已内置）；
4. **server 带 `-jf` 出 `Unexpected empty grammar stack`** → 去掉 `-jf`，用请求级 `response_format`；
5. **中文乱码**：`llama-server` 输出是 UTF-8，Windows 控制台是 cp936 会显示乱码，落盘/解析不受影响；
6. **`-c 2048` vs 训练 seq512**：推理上下文留 2048 足够（最长 prompt 实测 <200 tok）；
7. 量化 warning `N of 290 tensor(s) required fallback quantization` 属正常（embedding/小张量不量化）；
8. 偶发 `ConnectionResetError: [WinError 10054]`（本仓库只见过 1/11 次，其它 8 连发全成功）：
   client 会返回 `{"ok": False, "error": ...}`，上层按失败重试一次即可，不要当致命错误。

## 8. 文件清单

| 文件 | 作用 |
| --- | --- |
| `tools/soul_serve/fetch_convert_src.py` | 按 commit 拉 llama.cpp 转换源码（conversion/ + gguf-py/） |
| `tools/soul_serve/merge_adapter.py` | peft adapter + base → 合并 HF 目录（CPU） |
| `tools/soul_serve/convert.py` | HF → f16 GGUF → Q4_K_M |
| `tools/soul_serve/start_server.ps1` | PowerShell 启动 llama-server（PATH + --jinja） |
| `tools/soul_serve/server.py` | Python 启动封装（编程起停 + /health 等待） |
| `tools/soul_serve/smoke_eval.py` | 20 条端到端验收（G1/延迟/tok/s，可选 HF 对比） |
| `agi/soul/llama_client.py` | HTTP 客户端（与 llm.Client 同接口，json_mode 带 json_schema） |
| `agi/soul/inner_state_schema.json` | InnerState JSON schema（服务端/请求级约束共用） |


---

## 9. 模型资产与预留接口（P3-c，2026-10-26）

> **所有模型/适配器/GGUF 一律禁止删除**（用户要求保留；清单见 `E:\agent\output\MANIFEST.md`）。
> 1.5B 模型先不删、先不合并：`qwen15` 以 `status=reserved / port=8124` 登记在
> `agi/data/local_models.json`（gitignore），默认不启动；以后需要再改 status/role。

### 9.1 注册表（`agi/selflearn/model_registry.py`）

```powershell
E:\agent\venv312\Scripts\python.exe -m agi.selflearn.model_registry --list
E:\agent\venv312\Scripts\python.exe -m agi.selflearn.model_registry --get qwen15
E:\agent\venv312\Scripts\python.exe -m agi.selflearn.model_registry --resolve reserved
E:\agent\venv312\Scripts\python.exe -m agi.selflearn.model_registry --health qwen15
E:\agent\venv312\Scripts\python.exe -m agi.selflearn.model_registry --missing
E:\agent\venv312\Scripts\python.exe -m agi.selflearn.model_registry --start soul-05b
E:\agent\venv312\Scripts\python.exe -m agi.selflearn.model_registry --stop soul-05b
```

- `start/stop` 只操作注册表登记的端口，并且只 kill 自己启动的 PID（state 在
  `agi/data/local_models_state.json`）；`reserved` 模型默认拒绝启动；
- `health` 探测 `http://127.0.0.1:<port>/health`，带 5s TTL 缓存，端口未开返回 `False` 不抛异常；
- `missing_files` 校验 base/adapter/gguf 路径是否存在。

### 9.2 1.5B 预留位怎么用

1. 现状：`qwen15`（`E:\agent\models\Qwen2.5-1.5B-Instruct` + 适配器 `soul_qwen25_15b_v1` +
   GGUF `soul_qwen25_15b_v1-q4_k_m.gguf`），端口 8124，`status=reserved`；
2. 需要启用时：编辑 `agi/data/local_models.json`，把 `status/role` 改成 `ready/...`，
   然后 `--start qwen15`；**本仓库默认不启动它**；
3. 也可以直接 `python -m agi.selflearn.model_registry --health qwen15` 确认当前没占用端口。

### 9.3 任意适配器 → 可服务模型

```powershell
# 只预览（本任务只验证 dry-run，不执行合并/转换）
E:\agent\venv312\Scripts\python.exe tools\soul_serve\prepare_model.py `
  --adapter E:\agent\output\<adapter> --base E:\agent\models\<base> `
  --name <new_name> --dry-run

# 真跑（以后新任务用；默认 CPU merge + llama.cpp convert/quantize，不占训练 GPU）
E:\agent\venv312\Scripts\python.exe tools\soul_serve\prepare_model.py `
  --adapter ... --base ... --name <new_name> --out-dir E:\agent\output\gguf --register --port 8125
```

### 9.4 资产清单

所有基座/适配器/GGUF 的路径、大小、用途、评测数字与“禁止删除”标记见
`E:\agent\output\MANIFEST.md`（在 `E:\agent` 下，不进仓库）。
