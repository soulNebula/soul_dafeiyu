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
  "inner_mode_scope": "proactive_only",
  "inner_schema": "slim",
  "soul_server_url": "http://127.0.0.1:8123",
  "soul_timeout_s": 2.0,
  "inner_shadow_only": false
}
```

环境变量优先（上线推荐用 env，避免改配置文件后测试读到线上灰度值）：

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `INNER_MODE` | `off` | off/static/rule/model/shadow |
| `INNER_MODE_SCOPE` | `proactive_only`（生产） | all / proactive_only / off：哪些消息允许调 model（见 3.7） |
| `INNER_SCHEMA` | `slim` | slim（4 字段，省 token）/ full（13 字段，对照用） |
| `SOUL_SERVER_URL` | `http://127.0.0.1:8123` | llama-server 地址 |
| `SOUL_TIMEOUT_S` | `2.0` | 内状态调用超时（秒）；超时回退 rule |
| `INNER_SHADOW_ONLY` | `false` | 灰度：只记录不注入 |
| `INNER_HEALTH_CACHE_S` | `15` | `/health` 探活缓存秒数（0=每次探） |

> 说明：显式注入 client 的调用方（单元测试 / 离线消融）视为「非生产上下文」，
> 只认环境变量、不读 `config.json`；且 scope 默认 `all`（保持 W2/W4 的 model 全量语义），
> 生产（网关/web/cli）scope 默认 `proactive_only`。这样线上灰度开关不会污染测试套件。

### 3.4 延迟策略：slim 输出 + scope（P3-b）

**slim schema（默认）**：模型只产 4 个字段 `stance / intent / monologue / behavior`。
`mood/energy/sharing/battery/trust/stage` 不再让模型生成——推理侧本来就会用真实会话
状态覆盖（`inject._generate_model`），多生成只是白花 ~60 token。
定义在 `agi/soul/prompt.py::INNER_SCHEMA_SLIM`；训练 system 输入（完整状态行）不变，
**不需要重训 adapter**（实测行为字段一致率反而略升，见 3.7）。
`INNER_SCHEMA=full` 可切回 P3-a 的 13 字段口径做对照。

**scope（延迟兜底）**：`inner_mode_scope` 控制「哪些消息允许额外调一次小模型」。

| scope | 被动对话（reactive） | 主动消息（proactive） |
| --- | --- | --- |
| `all` | model（≈+0.34s） | model |
| `proactive_only`（**生产默认**） | **rule（0 额外延迟）** | model（异步可等 ~0.4s） |
| `off` | rule | rule |

- chat 侧调用点：`SelfLearnAgent.send(..., proactive=bool)`；默认 `proactive=False`
  （网关被动回复不传即 rule）。`build_inner_ctx_ex(..., proactive, scope)` 负责判定，
  scope 拦下时直接 rule 渲染：**不调模型、不写 inner_states、gen_meta.inner.latency_ms=None**。
- 注意：当前仓库的主动消息文本由 `agi/selflearn/proactive.py::tick` 自己生成（不经过
  `chat.send`，见 `agi/wechat_gateway/proactive_worker.py`）。因此按现状
  `scope=proactive_only` 意味着**被动回复全部走 rule（0 延迟）**；等 proactive 生成侧
  接入内状态（把 `proactive=True` 传给 `send`，或直接把 slim InnerState 注入主动 prompt）
  之后，主动消息才会真正用上 model。这条属于后续任务，不在本层硬接。

### 3.5 灰度上线 SOP（建议组合）

推荐：`INNER_MODE=model` + `INNER_MODE_SCOPE=proactive_only` + `INNER_SCHEMA=slim`
+ 先 `INNER_SHADOW_ONLY=1` 跑 24h。

1. 起 llama-server，确认 `/health` ok；
2. **只看不注入 24h**：
   - env：`INNER_MODE=model INNER_SHADOW_ONLY=1 INNER_SCHEMA=slim INNER_MODE_SCOPE=all`
     （仅灰度记录时用 all，才能看到主动/被动所有内状态；正式注入再切 proactive_only）
   - 观察 `inner_states`（`mode='model', injected=0`）与 `last_latency_ms`；
   - 关注失败率：`/status` 的 `inner.fallbacks_24h`，以及
     `agi/data/selflearn/inner.log` 里 `inner_fallback` 的 reason；
3. 确认失败率/延迟可接受后切注入：
   `INNER_SHADOW_ONLY=0`、`INNER_MODE_SCOPE=proactive_only`（被动回复不增加延迟）；
4. 随时回退：
   - 软回退：`INNER_MODE=off`（下一条消息生效，无需重启进程）；
   - 只停模型不关内状态：`INNER_MODE_SCOPE=off`（内状态全部 rule，仍注入行为指令）；
   - 硬回退：停掉 llama-server（探活失败自动降级 rule，主链不受影响）。

### 3.6 可观测

- `GET /status` → `inner: {mode, shadow_only, scope, schema, backend, server_url,
  timeout_s, last_latency_ms, fallbacks_24h}`
- `inner_states` 表（每次真正调 model 的一轮一行；scope 拦下 / rule 模式不写）：
  - `mode` = `model` | `shadow`
  - `injected` = 1 注入 / 0 只记录
  - `source` = `local`（本地小模型成功） | `rule`（回退规则） | `remote`
  - `fallback` = `""` | `timeout` | `client_unavailable` | `http_error` |
    `connection_error` | `bad_json` | `missing_fields` | `invalid_state` | `client_error` |
    `internal_error`
  - `latency_ms` = 本次内状态调用耗时（含失败尝试）
  - `state_json` / `block` = 本轮内状态与渲染块（失败时 `block=""`）
- 回退日志：`agi/data/selflearn/inner.log`（JSONL，`kind=inner_fallback`，10 分钟节流）
- 主链回复耗时仍看 `turns.latency_ms`（W3-A 埋点）。

### 3.7 延迟实测（2026-10-26，RTX 4050 Laptop 6GB）

条件：Qwen2.5-0.5B-Instruct + v1b LoRA → Q4_K_M；llama-server b11857，8123；
输入取冻结验证集 `agi/soul/data/soul_sft_v1_val.jsonl` 的前 30 条真实
context/state/user_msg；走生产同一条 `inject.build_inner_ctx_ex`
（`proactive=True, scope=proactive_only`），**同一轮实验只切换 response_format schema**：

| 指标 | full（13 字段，P3-a） | slim（4 字段，P3-b） | 变化 |
| --- | --- | --- | --- |
| 成功率 | 30/30 | 30/30 | — |
| completion tokens（均值/max） | 125.3 / 133 | **70.8 / 79** | **−43.5%** |
| P50 | 531.0 ms | **339.8 ms** | **−36.0%** |
| mean | 533.9 ms | 342.1 ms | −35.9% |
| **P95** | 595.7 ms | **399.9 ms** | **−32.9%（达标 ≤400ms）** |
| max | 607.5 ms | 409.9 ms | −32.5% |
| behavior 四字段全对率 | 0.367 | 0.433 | 略升 |
| 单字段一致率 length/ask/advice/emoji | .967/.767/.933/.533 | .967/.700/.933/.667 | 基本持平 |

**结论：不需要重训 adapter。** slim 的 4 字段输出 30/30 合法、行为字段一致率与 full 持平
（ask 略降、emoji 略升，四字段全对率反而 +6.6pt），说明 adapter 靠 in-context 状态 + grammar
即可产出可用行为指令。

**scope=proactive_only 实测（真实 server 在跑）**：

| 场景 | model 调用次数 | chat.send wall | gen_meta.inner |
| --- | --- | --- | --- |
| 被动回复（`proactive=False`） | 0 | ≈221 ms（与 off 同量级） | mode=rule / latency_ms=None |
| 主动消息（`proactive=True`） | 1 | ≈632 ms（+内状态 389ms） | mode=model / source=local / latency≈389ms |

当前护栏：`SOUL_TIMEOUT_S=2.0` 超时即回退 rule，回退路径不阻塞主链，最坏情况主链多 ≤2s 且不失败；
scope=proactive_only 时被动回复不调用模型，完全没有额外延迟。

## 4. 测试

- `agi/tests/test_inner.py`：schema/阈值渲染/四模式与回退
- `agi/tests/test_chat_inner.py`：chat 接入、off 字节锁（sha256）
- `agi/tests/test_shadow.py`：shadow 只记不注入
- `agi/tests/test_inner_production.py`：P3-a 生产接入（本地 client / 超时降级 / 日志节流 /
  off 字节锁 / 灰度 injected=0 / `/status` / 旧库迁移 / 探活缓存 / 配置优先级 /
  对话回复口语化与 20 条句号率对比）
- `agi/tests/test_inner_latency_slim.py`：P3-b slim schema / 缺字段回退 / scope 矩阵 /
  生产默认 proactive_only / off 字节锁

```bash
python -m pytest agi/tests/test_inner_latency_slim.py agi/tests/test_inner_production.py \
  agi/tests/test_inner.py agi/tests/test_chat_inner.py agi/tests/test_shadow.py \
  agi/tests/test_latency.py -q
```

## 5. 主动消息链路（P3-d）

主动消息文本由 `agi/selflearn/proactive.py::tick` 自己生成，不经过 `chat.send`；P3-d 在 tick
生成前显式调用内状态，把「灵魂」带进主动消息。

- **调用点**：`tick()` 在计算 prompt 前调用一次
  `inject.build_inner_ctx_ex(path, mode, client, "", recent_messages, None,
  proactive=True, scope="all", shadow_only=...)`；每 tick 只调一次。
  渲染块拼进 `_kind_prompt` 的人物设定之后、具体 kind 指令之前。
- **延迟预算**：主动消息是异步发送，可接受 ~400ms；`SOUL_TIMEOUT_S` 在主动链路被压到
  `min(1.5, 配置值)`，模型不可用/超时/非法 JSON → rule 渲染兜底，不阻塞 tick。
- **开关**：
  - `proactive_inner`：`off | auto | force`，默认 `auto`。
    `off` 完全不调用；`auto` 仅在 `/health`（15s TTL）可用时试模型，不可用直接用 rule；
    `force` 始终尝试模型（失败回退 rule）。
  - `inner_mode`：总开关；为 `off` 时主动链路完全不注入，prompt 与改动前逐字节一致。
  - `inner_mode_scope`：主动链路固定用 `scope="all"`（主动消息本来就允许延迟）；
    被动回复仍按 `scope` 策略，默认 `proactive_only` 时 0 模型延迟。
- **behavior 落地**（slim `behavior{length,ask,advice,emoji}`）：
  - `ask=false` → 主动消息禁问（prompt 不提问、输出不出现问号）；
  - `length=short` → 输出长度上限收到 12 字；
  - `advice=false` → 去掉「建议/最好/记得/应该」类建议小句；
  - `emoji=不用` → 不追加 emoji；`偶尔` → 按 5% 概率加 1 个。
- **观测**：`tick()` 返回 `inner{mode, source, latency_ms, injected, behavior, fallback}`；
  模型回退额外写 `proactive_inner_fallback` JSONL（10 分钟节流）。
- **测试**：`agi/tests/test_proactive_inner.py`（注入/behavior/超时回退/off 字节锁/
  每 tick 一次/20 tick 注入率 ≥90%/fallback 节流）。
