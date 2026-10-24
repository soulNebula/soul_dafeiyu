# 四臂消融生成器（W2-B）

给定黄金集 `agi/eval_data/golden_v1.jsonl`，对每条 context 用 4 种 `INNER_MODE`
各跑一次 `SelfLearnAgent.send()`，产出可对比的 JSONL。**本工具只负责“可复现地生成”，
评审（人工/裁判）在之后单独做。**

- 实现：`agi/eval_data/ablation.py`
- 测试：`agi/tests/test_ablation.py`
- 默认输出：`agi/eval_data/ablation_raw.jsonl`（可用 `--out` 覆盖）
- 不写 `agi/data/`：每条 (golden_id, mode) 都用独立的临时 SQLite，跑完即删

## 四臂含义

| mode | 注入内容 | 说明 |
| --- | --- | --- |
| `off` | 不注入 | 默认值，prompt 与改动前逐字节一致（A 任务保证） |
| `static` | 固定人格段落 | 不读用户/自身状态，衡量“固定人设”本身的增益 |
| `rule` | 按当前状态的规则渲染 | mood/energy/sharing/battery/trust/stage → 行为指令 |
| `model` | LLM 先生成 InnerState 再渲染 | 失败/超时/非法 JSON 自动回退 `rule` |

模式由环境变量 `INNER_MODE` 控制，脚本在每次 `send()` 前设置、结束后恢复原值。
四臂的实现在 A 任务（`agi/inner/` + `chat.py` 注入）里；**A 未合入时 `INNER_MODE`
不生效，四臂回复会完全相同**（此时结果只能验证管线，`mode` 字段仍如实记录）。
启动时会检测 `agi/inner` 并在缺失时打印警告。

## 用法

```bash
cd C:\Users\Administrator\Desktop\wx_box

# 1) mock 自测：确定性 FakeClient，不联网、不需要 key。不要写默认输出路径，
#    避免 mock 行占位导致之后的 --real 被断点续跑跳过。
python -m agi.eval_data.ablation --modes off,static,rule,model --limit 5 --mock \
    --out "$TEMP\ablation_mock.jsonl"

# 2) 全量真实跑（需要 DeepSeek API key；缺 key 会明确报错退出码 2，不会静默回退 mock）
python -m agi.eval_data.ablation --modes off,static,rule,model --real \
    --out agi/eval_data/ablation_raw.jsonl

# 3) 断点续跑：同一条命令再跑一遍，已存在的 (golden_id, mode) 会跳过，不重复写
python -m agi.eval_data.ablation --modes off,static,rule,model --real \
    --out agi/eval_data/ablation_raw.jsonl
```

参数：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--golden` | `agi/eval_data/golden_v1.jsonl` | 黄金集 JSONL（字段 id/bucket/context/user_msg/ideal_points） |
| `--out` | `agi/eval_data/ablation_raw.jsonl` | 输出 JSONL，追加写；父目录自动创建 |
| `--modes` | `off,static,rule,model` | 逗号分隔，可只跑部分臂；未知模式直接报错 |
| `--limit` | `0`（全部） | 只跑黄金集前 N 条（文件顺序） |
| `--mock` | 默认 | 确定性 FakeClient；显式写上也行 |
| `--real` | 关 | 真实调用 DeepSeek；与 `--mock` 互斥 |

### 无 key 时怎么 mock

`--mock` 的 `MockClient` 是确定性的：

- 对记忆/主张抽取返回空结果（`{"items":[]}` / `{"claims":[]}`），不污染临时库；
- 对 `model` 臂的内状态请求返回合法 JSON；
- 对普通回复返回固定文案，**带 `（mock）` 前缀**；prompt 里出现注入标题
  「你的内在状态」时文案换成另一句，方便确认 static/rule/model 臂确实注入了。

因此 mock 行只能验证“管线 + 四臂是否真的分开跑通”，**不能用于评价回复质量**。
产物里的 `client` 字段会标明这一行是 `mock` 还是 `real`，下游统计请过滤。

`--real` 的 key 解析顺序：`DEEPSEEK_API_KEY` 环境变量（含显式空值）>
`web_config.json` 的 `llm.api_key`。两者都没有可用 key 时打印原因并以 **退出码 2** 结束，
且不会创建输出文件（不会静默回退到 mock）。⚠️ 用 `DEEPSEEK_API_KEY=` 显式置空可以
在“配置里有 key”的机器上复现缺 key 路径。

## 产物字段

每行一个 `(golden_id, mode)`，UTF-8 JSON：

```json
{"golden_id": "G-001", "bucket": "work", "mode": "off",
 "reply": "（mock）嗯，我在听，你慢慢说", "ok": true, "latency_ms": 633.2,
 "error": null, "client": "mock", "ts": 1792849622.965}
```

| 字段 | 说明 |
| --- | --- |
| `golden_id` | 黄金集条目 id |
| `bucket` | 场景桶（work/low_mood/conflict/help/smalltalk/boundary/joke/privacy/crisis/silence） |
| `mode` | 本次运行的 `INNER_MODE` |
| `reply` | `send()` 返回的首条气泡；`ok=true` 但为空时见 `error=empty_reply` |
| `ok` | 该条是否成功返回（异常/空消息为 false） |
| `latency_ms` | 单条耗时：建临时库 + 回放 context + `send()` 返回的墙钟毫秒 |
| `error` | 失败/跳过原因；成功为 `null`。`empty_reply` 表示 agent 主动不回（如心情差） |
| `client` | `mock` / `real`；mock 与 real 必须用不同 `--out` |
| `ts` | 该行写入时间（Unix 秒） |

行顺序：黄金集顺序 × `--modes` 顺序；每行写完立即 flush，进程中断也能续跑。

## 断点续跑

- 键是 `(golden_id, mode)`：输出文件里已存在的组合直接跳过。
- **不区分** client、黄金集内容或 prompt 版本：改了 golden 条目 / 从 mock 换 real /
  A 任务改了注入实现后，应换新的 `--out`（或删掉旧产物），否则会误跳过。
- 中途崩溃若留下半行 JSON，读断点时会在 stderr 告警并忽略该行，重跑会补写。

## 实测（mock，离线）

环境：Windows，Python 3.14.7，本仓库；50 条 golden × 4 模式：

| 指标 | 数值 |
| --- | --- |
| 条数 | 200 行（50×4），`ok=200`，`failed=0` |
| 总耗时 | **126.7 s**（约 0.63 s/条；另一次与其它任务并发时 196 s） |
| 单条 `latency_ms` | mean 633 / median 630 / p95 1012 / max 1186 |
| 断点续跑 | 第二次运行 0.26 s，新写 0 行、跳过 200 行、文件仍 200 行 |
| 注入确认 | `static/rule/model` 各 41 条命中 mock 的“已注入”文案；`off` 0 条 |

mock 无网络开销，耗时几乎全部来自 selflearn 自身：每次 `send()` 会反复打开 SQLite
连接、每个连接都执行一遍建表脚本（≈50 次连接/条），加上逐条重建临时库。
真实跑由模型延迟主导。

## 真实跑一次的成本估算（50 × 4 = 200 条）

单条 `send()` 的 LLM 调用次数（用 mock 对全部 50 条实测，`purpose` 计数）：

| 臂 | 每条调用数（均值） | 组成 |
| --- | --- | --- |
| `off` / `static` / `rule` | ≈ 2.6 | 记忆抽取 1 + claim 抽取 1 + 主回复 1（41/50 条走到主回复；危机/元问题/内部查询/身份/技能闸门等 9 条早退，0 调用） |
| `model` | ≈ 3.4 | 上面 + 内状态生成 1（同一批 41 条） |

真实 key 下还会有：真实记忆抽取产出条目后多出的 evidence stage（0–1 次/条）、
guard 命中 L1/L2 时偶发的 NLI 校验（1 次，概率低）；`research` 工具循环未被触发
（50 条 `user_msg` 都不满足 `should_lookup` 的启发式）。因此全量约 **550–750 次调用**。

Token（按 mock 实测 prompt 字符数估算，中文按 ≈0.6 token/字）：

| 项 | 每次调用字符数 | ≈tokens |
| --- | --- | --- |
| 记忆抽取 prompt | ~330 | ~200 |
| claim 抽取 prompt | ~470 | ~280 |
| 主回复 prompt（off / 注入臂） | ~800 / ~900 | ~480 / ~540 |
| 内状态 prompt（model 臂） | ~500 | ~300 |

全量估算：**输入 ≈ 200k–260k tokens（取 216k），输出 ≈ 40k–70k tokens（取 55k）**。
成本按供应商单价折算：

```
成本(元) ≈ 输入tokens/1e6 × 输入单价 + 输出tokens/1e6 × 输出单价
例：¥1/M 输入、¥4/M 输出  → 0.216 + 0.218 ≈ ¥0.43
     ¥0.5/M 输入、¥2/M 输出 → ≈ ¥0.22
```

未计重试、缓存命中优惠与失败重跑；实际以供应商当期价格为准。
墙钟方面，每条约 3–4 次串行调用，按单次 3–8 s 估算，全量 **约 25–60 分钟**，
适合放后台跑（`--out` 每次落盘，中断可续）。

## 已知限制

1. **依赖 A 任务**：`INNER_MODE` 的四种模式由 `agi/inner/` + `chat.py` 注入实现；
   A 未合入时四臂行为相同，只能验证生成管线。
2. **mock 不代表质量**：`MockClient` 回复固定且带 `（mock）` 前缀，只用于离线和 CI；
   四臂质量必须用 `--real` 产物做评审。
3. **断点键不含版本**：见上文，换 golden / 换 client / 换注入实现要换 `--out`。
4. **每条独立全新库**：只回放该条 `context` 的 user/assistant 消息，不模拟跨会话的
   长期记忆、人格与信任累积；同一 golden 的四臂互不影响（这是消融需要的隔离，
   但与线上连续多轮状态不同）。
5. **crisis 条目**走 `chat.py` 固定安全回复，不受 `INNER_MODE` 影响（安全设计）。
6. **串行单进程**：不并发，Windows 上无需额外配置；本身不含评分/评审逻辑，
   `ideal_points` 仍留在 golden 里，产物不回写。
