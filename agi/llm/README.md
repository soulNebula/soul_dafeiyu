# agi/llm —— 统一 LLM 路由层（P13）

所有需要「模型」的功能都从这里进：按 **用途（purpose）** 决定走**本地**还是 **API**，
统一兜底、记账、日预算。设计原则（用户拍板）：

> **耗钱的（高量、长文本）放本地；需要判断力/人话的低频调用走 API；「视频真理解」必须走 API。**

本层只做路由，不改任何现有业务调用（接线由后续任务做）。

## 1. 目录

| 文件 | 职责 |
| --- | --- |
| `router.py` | `Router` / `chat()` / `usage_summary()` / `purposes()` / `available_backends()`；配置加载与校验 |
| `openai_local.py` | 本地 OpenAI 兼容后端（llama-server `8125/v1/chat/completions`，纯标准库） |
| `vision.py` | 识图 client 适配器：`image_understanding` → 本地 Qwen2.5-VL-3B（失败回落 DeepSeek） |
| `deepseek.py` | API 后端，**复用 `agi.selflearn.llm.Client`**（不重复造轮子） |
| `rule.py` | 纯规则后端（`relevance_score` 等简单用途，零成本） |
| `usage.py` | 用量 JSONL 记账、`usage_summary` 聚合、每日 API token 预算计数 |

## 2. 配置 `agi/data/llm_router.json`

> ⚠️ `agi/data/` 在 `.gitignore` 里（与 gateway `config.json` 同样的运行时数据策略）。
> 新机器/部署时需要把该文件放到位；路径可用 env `LLM_ROUTER_CONFIG` 覆盖。

```json
{
  "backends": {
    "local": {"kind": "openai",  "base_url": "http://127.0.0.1:8125/v1",
              "model": "local-3b", "timeout_s": 60},
    "api":   {"kind": "deepseek", "model": "deepseek-flash", "timeout_s": 30},
    "rule":  {"kind": "rule"}
  },
  "purposes": {
    "video_understanding": {"backend": "api",   "must_api": true},
    "ingest_summary":      {"backend": "local", "fallback": ["api"], "max_tokens": 400},
    "comment_cluster":     {"backend": "local", "fallback": ["api"]},
    "keyword_extract":     {"backend": "local", "fallback": ["api"], "max_tokens": 200},
    "relevance_score":     {"backend": "local", "fallback": ["rule"]},
    "share_copy":          {"backend": "api",   "fallback": ["local"]},
    "chat_reply":          {"backend": "api"},
    "mood_judge":          {"backend": "api"},
    "persona_reflect":     {"backend": "api",   "fallback": ["local"]},
    "transcribe":          {"backend": "local_whisper"}
  },
  "budget": {"daily_api_tokens": 300000, "on_exceed": "local"},
  "log_path": "agi/data/llm_usage.jsonl"
}
```

## 3. 用途路由表（默认配置）

| purpose | 后端 | 兜底 | 为什么 |
| --- | --- | --- | --- |
| `video_understanding` | **api（must_api）** | 无（必须 API） | 视频真理解需要多模态判断力，本地做不了 |
| `ingest_summary` | local | api | 高量、长文本摘要，本地 3B 足够 |
| `comment_cluster` | local | api | 高量聚类，本地 |
| `keyword_extract` | local | api | 高量抽取，本地 |
| `relevance_score` | local | rule | 简单打分，规则兜底零成本 |
| `share_copy` | api | local | 需要「人话」的文案 |
| `chat_reply` | api | — | 主对话质量优先 |
| `mood_judge` | api | — | 需要判断力（0.5B 实测不合格，见 plans/34） |
| `persona_reflect` | api | local | 低频、需要判断力 |
| `transcribe` | `local_whisper`（占位） | — | P14 提供本地 whisper 后接入；现在调用会明确报 `unknown_backend` |

## 4. 接口

```python
from agi.llm import chat, usage_summary, purposes, available_backends

result = chat("chat_reply", messages, max_tokens=500, temperature=0.7)
# -> {
#   ok, text, backend, backend_kind, purpose,
#   usage: {tokens_in, tokens_out}, latency_ms,
#   fallback_used, budget_exceeded, error, model,
#   warnings: [...], attempts: [{backend, ok, error}, ...]
# }

summary = usage_summary(days=7)   # 控制台统计页用
# -> {window, total, by_purpose, by_backend, by_day}
```

单例/测试：`default_router()`、`reset_default_router()`；测试可 `Router(config,
backend_overrides={...}, usage_path=tmp, now_fn=...)` 完全离线。

## 5. 兜底语义

1. 按 purpose 的 `backend` 选主后端；
2. `must_api=true`：**强制 api**（即使配置写成 local；记 `must_api_forced_api` warning）；
3. 主后端不可用/调用失败/空正文 → 按 `fallback` 链依次尝试（不重复主后端）；
4. 全部失败 → `{ok: False, error: "all_backends_failed: backend=reason; ..."}`，
   **不会静默返回空串**；未知 purpose / 未定义 backend 也明确报错；
5. `rule` 后端只支持 `relevance_score`（其它用途返回 `rule_unsupported_purpose`）。

## 6. 记账与日预算

- 每次调用（成功/失败都记）追加 `agi/data/llm_usage.jsonl`：
  `ts / purpose / backend / backend_kind / tokens_in / tokens_out / latency_ms /
  ok / fallback / budget_exceeded / must_api / error / warnings`；
- `usage_summary(days)` 按 **天 / 用途 / 后端** 聚合（calls / ok / tokens / 平均延迟 /
  回退次数 / 超预算次数）；
- `budget.daily_api_tokens`：只统计 **API 后端**（`kind=deepseek`）当天 tokens_in+out；
  超过后按 `on_exceed`：
  - `local`：自动改走本地（`budget_exceeded=true`，`warnings` 记
    `budget_exceeded:degrade_to_local`）；
  - `none`：直接失败（`error="budget_exceeded"`）；
  - `must_api` 用途：**不静默降级**，返回 `error="budget_exceeded:must_api"`
    （明确让人知道视频理解被预算挡了，而不是偷偷换本地）。
- 记账写盘失败静默（不能因为记账挂掉业务）；读日志坏行跳过。

## 7. 成本估算（按默认配置）

- 本地 8125（Qwen2.5-VL-3B）≈ 0 元/次；高量提取（摘要/聚类/关键词）全部本地，**识图也走本地**（失败回落 API）。
- API（deepseek-flash）只用于：视频理解、主对话、分享文案、心情判定、人格反思。
  按每条 ~1–3k prompt + ~200 completion tokens 估：300k tokens/天预算 ≈
  每天百次级 API 调用（具体取决于对话量）；超了自动降级本地。
- 换后端/模型：改 `agi/data/llm_router.json` 即可；跑 `available_backends()` 看探活。

## 8. 怎么加一个新用途

1. 在 `purposes` 里加一项：`{"backend": "...", "fallback": [...], "max_tokens": ...}`；
2. 需要新后端就在 `backends` 里加（kind 支持 `openai` / `deepseek` / `rule`）；
3. 业务代码调用 `agi.llm.chat("<purpose>", messages)`；
4. 跑 `agi/tests/test_llm_router.py`；新后端建议在 `backend_overrides` 下加单测。

## 9. 测试

```bash
python -m pytest agi/tests/test_llm_router.py -q
```

全部 tmp_path + mock 后端（`transport` / `backend_overrides`），**不联网**；
覆盖配置解析/非法配置、purpose 路由、must_api 强制、fallback 链、全失败、
记账字段、usage_summary 聚合、日预算降级、rule 后端、两个真实后端适配。
