# agi/inner —— 内状态层（InnerState）

把「当前会话状态 + 最近对话」翻译成一段**注入 prompt 的行为指令**
（`<inner_state>…</inner_state>`），让主模型（DeepSeek）的语气/长度/追问/建议/表情
跟着状态走。内状态只影响**语气与选择**，不是事实，不进回复正文。

## 1. 文件分工

| 文件 | 职责 |
| --- | --- |
| `schema.py` | `InnerState` 数据结构、`from_state()` / `validate()`（越界 clamp、非法 ValueError） |
| `render.py` | 状态 → 行为（`render_rule`）→ prompt 片段（`render_block` / `render_static`） |
| `inject.py` | 按 `INNER_MODE` 组装注入片段；本地 llama-server client 管理 / 探活 / 回退 / 观测 |

## 2. 模式（`INNER_MODE`，默认 `off`）

| mode | 行为 | 注入 prompt |
| --- | --- | --- |
| `off`（默认） | 不算内状态 | 否（prompt 与改动前逐字节一致） |
| `shadow` | 用 rule 渲染并落 `inner_states(mode='shadow', injected=0)` | 否 |
| `rule` | 按阈值用规则渲染行为指令 | 是 |
| `static` | 固定人格段落（B 臂） | 是 |
| `model` | **本地 0.5B 灵魂模型**产出 InnerState（训练同源 messages）；失败自动回退 rule | 是（灰度时否） |

`model` 模式失败（探活失败 / 超时 / HTTP 错误 / 非 JSON / 字段非法）→ **自动回退 rule**，
并写 `inner_fallback` 日志（`agi/data/selflearn/inner.log`，每 10 分钟最多 1 条）。
主链永不因内状态失败而失败。

## 3. 生产接入（P3-a）

### 3.1 前置资产（本机已就绪）

- 灵魂 adapter：`E:\agent\output\soul_qwen25_05b_v1b`（GGUF：`soul_qwen25_05b_v1b-q4_k_m.gguf`）
- llama.cpp：`E:\agent\llama\cuda\llama-server.exe`（b11857，实测本机可用）
- JSON 约束：**请求级** `response_format={"type":"json_schema",...}`；
  `-jf/--json-schema-file` 在本版本 + Qwen jinja template 下会 400，**不要加**。
- 详见 `tools/soul_serve/README.md`（merge → GGUF → 启动链路）。

### 3.2 启动 llama-server

```powershell
# 仓库根目录（脚本会自动把 torch 的 CUDA DLL 目录加进 PATH）
pwsh -File tools\soul_serve\start_server.ps1 `
  -Model E:\agent\output\gguf\soul_qwen25_05b_v1b-q4_k_m.gguf -Port 8123
```

等价的手动命令（**PATH 必须包含** `E:\agent\venv312\Lib\site-packages\torch\lib`，
否则 `ggml-cuda.dll` 加载失败；必须 `--jinja`）：

```powershell
$env:PATH = "E:\agent\venv312\Lib\site-packages\torch\lib;" + $env:PATH
E:\agent\llama\cuda\llama-server.exe `
  -m E:\agent\output\gguf\soul_qwen25_05b_v1b-q4_k_m.gguf `
  -ngl 99 --host 127.0.0.1 --port 8123 -c 2048 -np 1 --jinja
```

探活：`curl http://127.0.0.1:8123/health` → `{"status":"ok"}`。
（`-fa on` 实测无收益，不必加。）

### 3.3 配置（`agi/data/wechat/config.json`）

```json
{
  "inner_mode": "off",
  "soul_server_url": "http://127.0.0.1:8123",
  "soul_timeout_s": 2.0,
  "inner_shadow_only": false
}
```

环境变量优先（上线推荐用 env，避免改配置文件后测试读到线上灰度值）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `INNER_MODE` | `off` | off/static/rule/model/shadow |
| `SOUL_SERVER_URL` | `http://127.0.0.1:8123` | llama-server 地址 |
| `SOUL_TIMEOUT_S` | `2.0` | 内状态调用超时（秒）；超时回退 rule |
| `INNER_SHADOW_ONLY` | `false` | 灰度：只记录不注入 |
| `INNER_HEALTH_CACHE_S` | `15` | `/health` 探活缓存秒数（0=每次探） |

> 说明：显式注入 client 的调用方（单元测试 / 离线消融）视为「非生产上下文」，
> 只认环境变量、不读 `config.json`，保证线上灰度配置不会污染测试套件。

### 3.4 灰度上线 SOP

1. 起 llama-server，确认 `/health` ok；
2. **只看不注入 24h**：
   - env：`INNER_MODE=model INNER_SHADOW_ONLY=1`（或 config 同步）
   - 观察 `inner_states`（`mode='model', injected=0`）与 `last_latency_ms`；
   - 关注失败率：`/status` 的 `inner.fallbacks_24h`，以及
     `agi/data/selflearn/inner.log` 里 `inner_fallback` 的 reason；
3. 确认失败率/延迟可接受后切注入：`INNER_SHADOW_ONLY=0`（config `inner_shadow_only=false`）；
4. 随时回退：
   - 软回退：`INNER_MODE=off`（下一条消息生效，无需重启进程）；
   - 硬回退：停掉 llama-server（探活失败自动降级 rule，主链不受影响）。

### 3.5 可观测

- `GET /status` → `inner: {mode, shadow_only, backend, server_url, timeout_s,
  last_latency_ms, fallbacks_24h}`
- `inner_states` 表（每次 model/shadow 一轮一行）：
  - `mode` = `model` | `shadow`
  - `injected` = 1 注入 / 0 只记录
  - `source` = `local`（本地小模型成功） | `rule`（回退规则） | `remote`
  - `fallback` = `""` | `timeout` | `client_unavailable` | `http_error` |
    `connection_error` | `bad_json` | `invalid_state` | `client_error` | `internal_error`
  - `latency_ms` = 本次内状态调用耗时（含失败尝试）
  - `state_json` / `block` = 本轮内状态与渲染块（失败时 `block=""`）
- 回退日志：`agi/data/selflearn/inner.log`（JSONL，`kind=inner_fallback`，10 分钟节流）
- 主链回复耗时仍看 `turns.latency_ms`（W3-A 埋点）。

### 3.6 延迟实测（2026-10-26，RTX 4050 Laptop 6GB）

条件：Qwen2.5-0.5B-Instruct + v1b LoRA → Q4_K_M；llama-server b11857，8123；
输入取冻结验证集 `agi/soul/data/soul_sft_v1_val.jsonl` 的真实 context/state/user_msg；
走生产同一条 `inject.build_inner_ctx_ex(mode="model")` 链路，n=30：

| 指标 | 实测 |
| --- | --- |
| 成功率 | 30/30（G1 由 decoder grammar 保证，全为合法 InnerState JSON） |
| P50 | **554 ms** |
| mean | 574 ms |
| **P95** | **696 ms**（max 700 ms，min 521 ms） |
| 生成量 | 123–128 completion tokens，约 234–241 tok/s |
| 主链增量（chat.send，热库） | off ≈ 0.22s → model ≈ 0.83s（+≈0.6s/条） |

**目标 P95 ≤ 400ms 当前未达标**：InnerState 完整 JSON（13 个字段）约 123 tokens，
0.5B 在该机器约 240 tok/s → 单次生成约 510ms，是硬下限。可行优化方向（后续任务）：
精简输出 schema（如只让模型产 `stance/intent/monologue/behavior`，数值字段本来就会被
真实会话状态覆盖，可省约 1/3 tokens）、或换更快 GPU/量化。当前护栏：
`SOUL_TIMEOUT_S=2.0` 超时即回退 rule，回退路径不阻塞主链，最坏情况主链多 ≤2s 且不失败。

## 4. 测试

- `agi/tests/test_inner.py`：schema/阈值渲染/四模式与回退
- `agi/tests/test_chat_inner.py`：chat 接入、off 字节锁（sha256）
- `agi/tests/test_shadow.py`：shadow 只记不注入
- `agi/tests/test_inner_production.py`：P3-a 生产接入（本地 client / 超时降级 / 日志节流 /
  off 字节锁 / 灰度 injected=0 / `/status` / 旧库迁移 / 探活缓存 / 配置优先级 /
  对话回复口语化与 20 条句号率对比）

```bash
python -m pytest agi/tests/test_inner_production.py agi/tests/test_inner.py \
  agi/tests/test_chat_inner.py agi/tests/test_shadow.py agi/tests/test_latency.py -q
```
