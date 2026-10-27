# W4-G3：下游盲评（本地内状态块 vs teacher 内状态块）

> **这是 W4 唯一真正决定「能不能上线」的门。**
> G1（格式合法率）和 G2（与 teacher 逐字段一致）都只回答「像不像 teacher」；
> W2 消融已经证明「prompt 里随便塞一段状态描述，对回复质量没有可测提升」。
> G3 固定其余一切，**只换 prompt 里的内状态块**，看最终回复质量有没有差别。

- 实现：`agi/eval_data/downstream.py`
- 测试：`agi/tests/test_downstream.py`（不联网、不加载模型）
- 复用：`agi/eval_data/judge.py`（配对盲评协议）、`agi/soul/prompt.py`（训练/推理同源 messages）、
  `agi/inner/schema.py` + `agi/inner/render.py`（InnerState 校验与渲染）、`agi/selflearn/chat._prompt`（注入点）

本工具**不改** `judge.py` / `ablation.py` / `agi/soul/` / `agi/inner/` / `tools/`。

## 两臂设计

```
golden 一条 (context + user_msg + 当前状态)
   │
   ├─ arm local   : LlamaClient(local_soul=True) → llama-server
   │                prompt = soul.prompt.build_messages(state, recent, text)（训练/评测同源）
   ├─ arm teacher : DeepSeek Client
   │                prompt = soul.dataset.teacher_messages（打标签同源，带 JSON schema 约束）
   │                temperature=0.2, max_tokens=300, json_mode
   │
   ├─ 两臂输出都: parse_json → inner.schema.validate → 数值字段以会话状态为准
   │             → sanitize_monologue → inner.render.render_block
   │             = <inner_state>…</inner_state> 块
   │
   ├─ 分别注入 chat.SelfLearnAgent._prompt(inner_ctx=块)
   │   （除该块外 prompt 逐字节相同 —— 命门，单测逐字节锁住）
   │
   ├─ DeepSeek 生成回复 temperature=0.3, max_tokens=500（两臂同一个 client）
   │
   └─ judge.judge_one（A/B 左右随机）→ winner ∈ {a, b, TIE}
```

- 两臂**只差**「由谁生成 InnerState」：local 用 llama-server，teacher 用 DeepSeek；
  两者拿到的是**同一份信息**（当前状态 + 最近对话 + 用户这句话），prompt 格式各按
  自己的训练口径（原因见下节）——这正是两种方案在各自上线形态下会看到的输入。
- 生成回复与裁判都用 DeepSeek（同族自评偏差，见「已知限制」）。
- `--arms local|teacher` 只跑单臂（单臂不盲评），用于在 llama-server 就绪前先打通 teacher 链路。

### 命门不变量（单测锁住）

`test_two_arms_prompt_differs_only_in_inner_block`：两个不同块生成的**最终回复 prompt**，
各自把块替换成占位符后逐字节相同，且：
- 标题「你的内在状态（只影响语气与选择，不是事实，不要说出口）：」恰好出现 1 次；
- 不含 `context.fit` 的「上下文过长，已裁剪」标记（当前 prompt 远小于 5200 token 预算）。

注意：锁的是**注入 chat._prompt 的最终 prompt**；两臂生成 InnerState 的输入格式不同（下节）。

### 两臂 prompt 为什么不对称（重要）

local 模型的训练/推理同源 prompt（`soul.prompt.build_messages`）**不含 “json” 一词**，
格式由 llama-server 的 `json_schema` grammar 保证；而 DeepSeek 的
`response_format={"type":"json_object"}` 硬性要求 prompt 里出现 “json”，否则直接
HTTP 400（实测：`Prompt must contain the word 'json' ...`）。

因此：
- **local 臂保持训练同源 prompt**（改了就不再是训练分布，G3 对 local 不公平）；
- **teacher 臂复用生成 `soul_sft` 标签时的 `dataset.teacher_messages`**：同一份
  当前状态 + 最近对话 + 用户这句话，外加 schema/枚举/JSON 约束——这正是产出 1890
  条训练 target 的那份 prompt，也是 teacher 方案“上线”时该用的 prompt；
- 试过“相同字符串 + 一句简化 JSON 后缀”：teacher 仍把 `behavior` 写成字符串，
  `schema.validate` 失败 → rule 回退，等于没测到 teacher 的块，故放弃。

两臂各自输出都走**同一套** `parse_json → schema.validate → 数值字段以会话状态为准
→ sanitize_monologue → render_block`，所以最终比较的是“两条内状态块”，不是两种后处理。
`meta.local_prompt` / `meta.teacher_prompt` 会记录这一口径。

## 数据与状态口径

默认输入 `agi/eval_data/golden_v1.jsonl`（50 条，带 `ideal_points`）。也支持
`agi/soul/data/soul_sft_v1_val.jsonl`（带 `state_profile` + `target`，但没有 ideal_points）。

| 输入 | 当前状态怎么来 |
| --- | --- |
| soul_sft_* 行 | 解析 `context[0]`（system）里的「当前状态：mood=… energy=… crisis=…」状态行，**与训练同口径**（单测验证 `build_messages` 重建出的 system 与原始 system 逐字节相同） |
| golden_v1 行 | 没有 state_profile：按 bucket 映射到最接近的训练档位（下表），再 `soul_prompt.profile_state(profile, bucket)` |

| bucket | state_profile | | bucket | state_profile |
| --- | --- | --- | --- | --- |
| work | work | | boundary | guarded |
| low_mood | low_mood | | joke | joke |
| conflict | guarded | | privacy | guarded |
| help | default | | crisis | default（强制 crisis=true） |
| smalltalk | default | | silence | tired |

`chat._prompt` 里的「当前情绪/关系」同样由这份状态派生（`chat_state_from`），两臂共用。
**状态只取自输入 context，不取自任何一臂的输出**，避免泄漏。

## 用法

```bash
cd C:\Users\Administrator\Desktop\SoulAgent

# 1) mock 自测：不联网、不需要 key、不需要 llama-server（CI 也能跑）
python -m agi.eval_data.downstream --mock --limit 3 --bias-check 2 \
    --out "$TEMP\g3_mock.json" --report "$TEMP\g3_mock.md"

# 2) 只跑 teacher 臂（不需要 llama-server；用于正式跑前打通 DeepSeek 链路）
python -m agi.eval_data.downstream --arms teacher --limit 2 \
    --out "$TEMP\g3_teacher.json" --report "$TEMP\g3_teacher.md"

# 3) 正式两臂 50 条（等 Lead 通知 GPU/llama-server 空闲后再跑）
python -m agi.eval_data.downstream --base-url http://127.0.0.1:8123 --limit 50 \
    --out E:\agent\logs\g3_downstream.json --report E:\agent\logs\g3_report.md

# 4) 中断后断点续跑（按 golden_id 跳过已有条目，不重复写）
python -m agi.eval_data.downstream --base-url http://127.0.0.1:8123 --limit 50 --resume \
    --out E:\agent\logs\g3_downstream.json --report E:\agent\logs\g3_report.md

# 5) 只重跑上轮 winner=null 的条目（瞬态 API 错误/裁判不可解析；不按结果筛选）
python -m agi.eval_data.downstream --base-url http://127.0.0.1:8123 --limit 50 --retry-failed \
    --out E:\agent\logs\g3_downstream.json --report E:\agent\logs\g3_report.md
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--data` | `agi/eval_data/golden_v1.jsonl` | 输入 JSONL（golden_v1 或 soul_sft_*） |
| `--out` | `agi/eval_data/g3_downstream.json` | 逐条结果 JSON（逐条落盘，可续跑） |
| `--report` | `agi/eval_data/g3_report.md` | Markdown 报告 |
| `--arms` | `both` | `both` / `local` / `teacher`；单臂不做盲评 |
| `--limit` | `0`（全部） | 只跑前 N 条 |
| `--seed` | `42` | 盲评左右随机种子（可复现） |
| `--base-url` | `http://127.0.0.1:8123` | llama-server 地址 |
| `--timeout-s` | `30` | llama-server 单次请求超时 |
| `--bias-check` | `0` | 对前 N 条用**相反左右顺序**再判一次，检查裁判位置偏差 |
| `--resume` | 关 | 断点续跑；不加则覆盖 `--out` 重跑 |
| `--retry-failed` | 关 | 隐含 resume：只重跑上轮 `winner=null` 的条目（为空的那一臂重生成回复、再重判），修复瞬态错误；不按胜负筛选 |
| `--mock` | 关 | 确定性假 client（不联网、不需要 key/server） |

真实模式的前置检查（缺了会**明确报错退出码 2**，不会静默回退）：
- DeepSeek key：`web_config.json` 的 `llm.api_key` 或 `DEEPSEEK_API_KEY`（显式空值可复现缺 key 路径）；
- `--arms both/local` 需要 llama-server 的 `/health` 可达（先按 `tools/soul_serve/README.md` 起 server，`--jinja`）。

## 产物

`--out` JSON：

```json
{
  "meta": {"data": "...", "mode": "real", "arms": "both", "seed": 42,
           "inner_temperature": 0.2, "reply_temperature": 0.3,
           "teacher_model": "deepseek-flash", "local_model": "local-soul",
           "base_url": "http://127.0.0.1:8123", "elapsed_s": 123.4,
           "state_policy": {...}},
  "summary": {"overall": {...}, "by_bucket": {...}, "block_sources": {...},
              "judge_flip": {...}, "position_bias": {...}, "errors": {...}},
  "items": [
    {"golden_id": "G-001", "bucket": "work", "state_profile": "work",
     "state": {"mood": 0.6, "energy": 0.75, "...","crisis": false},
     "arm_a_block": "<inner_state>…</inner_state>",
     "arm_a_source": "model", "arm_a_error": null, "arm_a_latency_ms": 512.3,
     "arm_a_raw": "{...模型原始输出（截断 1000 字）...}",
     "arm_b_block": "<inner_state>…</inner_state>", "arm_b_source": "model",
     "arm_b_error": null, "arm_b_latency_ms": 1320.0, "arm_b_raw": "{...}",
     "reply_a": "…", "reply_b": "…", "reply_error": null,
     "winner": "a", "judge_reason": "…", "judge_flip": false,
     "judge_error": null,
     "bias_check": {"opposite_winner": "a", "opposite_flip": true,
                    "consistent": true, "order_dependent": false},
     "latency_ms": 1830.0, "ts": 1792872486.6}
  ]
}
```

- `arm_a_*` = local 臂，`arm_b_*` = teacher 臂；`--arms` 单臂时另一侧为 `null`。
- `arm_*_source`：`model`＝模型输出通过校验；`rule`＝输出非法/失败后回退 rule 渲染；`none`＝回退也失败。
- `winner`：`a`（local 胜）/ `b`（teacher 胜）/ `TIE` / `null`（未判或跳过）。
- `--report` Markdown 自动生成，正式跑完**不用手工改**：环境（含 `server 实际加载`）、
  口径（local/teacher 各自 prompt 来源 + 状态来源 + 注入点不变量）、总体胜率 + Wilson 95% CI、
  分 bucket、块来源、位置偏差、**预注册判读标准**、复现命令、已知限制。

## 统计口径与判读

- **胜率不含平**：`local 胜率 = a_wins / (a_wins + b_wins)`；`tie_rate = ties / judged`。
- **Wilson 95% CI**（复用 `judge.wilson`，z=1.96）算在「可判定」条数上，不是总条数。
- 判读（50 条、不含平时）：
  - local 胜率明显 >50% ⇒ 本地小模型路线成立，可以继续投入；
  - 区间跨 50% ⇒ **没有测出**明显差异（不能说「一样好」）；
  - 区间上界 <50% ⇒ G2 的分数没有传导到回复质量，应回头改训练数据 / 渲染方式。
- `--bias-check N`：同一条配对用相反左右顺序再判一次；「顺序相关」比例高说明裁判有位置偏差，胜率结论要打折扣。

## 实测

### mock（离线，不联网、不加载模型）

| 指标 | 数值 |
| --- | --- |
| mock 全量 50 条（arms=both，bias-check=10） | 50 行、judged=50、0 失败，**0.12 s**（含 260 次假 LLM 调用） |
| 单测 `python -m pytest agi/tests/test_downstream.py -q` | **14 passed in 0.37 s** |
| mock 统计 | local 胜 50 / teacher 胜 0 / 平 0，Wilson=[92.9%, 100%]（**构造结果，不是 G3 结论**） |
| 位置偏差检查 | 双向重评 10 条，顺序一致 10/10（mock 裁判是内容型，必然一致） |
| 左右随机分布 | flip=True 17 / flip=False 33（共 50） |

mock 的 `MockJudgeClient` 固定偏好带 `local` 标记的回复，所以 local 全胜；它只证明
「盲评→标签映射→统计→续跑」管线正确，**不能用来判断 G3**，真实结论必须跑 `--real`。

### 真实 teacher 单臂 pilot（2026-10-25，GPU 训练期间跑，不占 GPU）

- **发现并修复**：DeepSeek `response_format=json_object` 要求 prompt 里含 “json”，
  否则 HTTP 400（`Prompt must contain the word 'json'`）——训练同源 prompt 不含该词。
  teacher 臂改为复用 `dataset.teacher_messages`（打标签同源）后恢复正常，见上文「两臂
  prompt 为什么不对称」。
- **5 条 teacher 单臂**：5/5 InnerState 走 `model`（0 条 rule 回退）、5/5 回复非空，
  总耗时 **11.35 s / 10 次 DeepSeek 调用**。
- 实测每次调用（DeepSeek 返回的 usage，5 条均值）：

| 调用 | 输入 tokens | 输出 tokens | 单次耗时 |
| --- | --- | --- | --- |
| teacher InnerState（max_tokens=300） | 801 | 185 | ~1.58 s |
| 回复生成（temperature=0.3, max_tokens=500） | 578 | 28 | ~0.69 s |

样本很小（5 条），只用于校准上方成本与耗时量级。
最新代码又复跑一次 `--arms teacher --limit 2`：2/2 `model`、0 错误、回复非空，
4.71 s / 4 次调用；产物留档在 `E:\agent\logs\g3_teacher_pilot_20261025.json|.md|.log`。

### 正式跑：0.5B B（平衡采样）vs teacher（2026-10-25，C 臂训练同时进行）

- server GGUF：`E:\agent\output\gguf\soul_qwen25_05b_v1b-q4_k_m.gguf`（`/props` 实测，
  非 v0/v1）；50 条 golden，`--bias-check 10`，总墙钟 **479 s**（训练并发，local 单条被拖慢到 ~1s）。
- **总体（预注册主指标）**：n=50、judged=50；**local 26 胜 / teacher 20 胜 / 平 4**；
  local 胜率（不含平）**56.5%**，Wilson 95% **[42.2%, 69.8%]**，tie 率 8.0%。
  → 区间跨 50%，按预注册标准：**未测出显著差异**（不是「一样好」）。
- 分 bucket（n=5/桶，CI 都很宽，仅看方向）：边界/危机 4–0、隐私/工作 4–1；
  闲聊 3–2；求助/玩笑 2–3；冲突/沉默 1–3；情绪低落 1–4。
- `block_sources` rule 回退：**local 0/50，teacher 2/50**（G-003、G-010：
  teacher 输出不是 JSON 对象，回退 rule；两条胜负各一）。
- 数据完整性：首轮 9 条 `winner=null`（5 条裁判不可解析 + 4 条一臂回复瞬态失败），
  `--retry-failed` 重跑 **11 次**全部补齐，最终缺 0 条（只重跑失败项，不按胜负筛选）。
- 裁判位置偏差（10 条双向重评）：一致 7、顺序相关 3（30%）；平局/极近回复的裁决不稳
  （G-049 重判出现 TIE/A/TIE），结论需人工复核。
- 观察（供解读，不作为胜负）：两臂回复长度均值 local 40 字 vs teacher 84 字（内状态块
  的 length 指令会系统性影响长度）；去掉 crisis（线上不注入块）后 local 22/42=52.4%
  [37.7%, 66.6%]，更接近 50%。
- 产物：`E:\agent\logs\g3_downstream.json`、`g3_report.md`；首轮原始数据备份在
  `g3_downstream_firstpass.json` / `g3_report_firstpass.md`。

### A 臂对照：0.5B v1 原版 vs teacher（2026-10-25，D 训练同时进行）

- server GGUF：`E:\agent\output\gguf\soul_qwen25_05b_v1-q4_k_m.gguf`（`/props` 实测）；
  与 B 完全同口径（同 50 条、同 teacher、同 seed=42、同 `--bias-check 10`），墙钟 425.5 s。
- **总体**：n=50、judged=50；**local 19 胜 / teacher 28 胜 / 平 3**；local 胜率
  **40.4%**，Wilson 95% **[27.6%, 54.7%]**，tie 6.0% → 与 teacher **无显著差异**（点估计偏低）。
  数据完整性好：0 次重试、0 裁判错误、teacher rule 回退 1。
- **A vs B**：B − A = **+16.1pt**（56.5% vs 40.4%，方向与 G2 排序 A 0.620 < B 0.686 一致），
  但两比例检验 p=0.116、同 item McNemar p=0.265，**不显著**；按当前效应量约需
  **148 条/臂**才能判定。⇒ 目前既不能证实「平衡采样传导到下游」，也不能证实「脱钩」。
- 长度混杂：A 的 local 回复平均 **20 字** vs B **40 字**（内状态块 length 指令的系统性差异），
  B 的优势可能部分来自更长的回复；长度配平臂待做。
- 完整对照报告：`E:\agent\logs\g3_A_vs_B.md`。

## 真实跑一次的成本 / 耗时估算（50 条，两臂）

基于真实 teacher pilot 的 usage 实测（上表）+ mock 全量 50 条的 prompt 规模：

| 调用 | 次数 | 平均输入 | 平均输出 |
| --- | --- | --- | --- |
| teacher InnerState（DeepSeek） | 50 | 801 tok（实测） | 185 tok（实测） |
| 回复生成（DeepSeek，两臂各一条） | 100 | 578 tok（实测） | 28 tok（实测） |
| 盲评裁判（DeepSeek） | 50 + bias 10 | ~250 tok（judge prompt 474 字符估） | ~20 tok（估） |
| local InnerState（llama-server） | 50 | 0 API 费用 | 0 API 费用 |

- DeepSeek 调用 **≈210 次**（含 `--bias-check 10`）；输入 **≈113k tokens**、输出 **≈13k tokens**。
- 成本：按 ¥1/M 输入、¥4/M 输出 ≈ **¥0.17**；按 ¥0.5/M、¥2/M ≈ **¥0.08**（未计重试/缓存优惠）。
- 墙钟：按实测单次（teacher 1.58 s、回复 0.69 s、裁判 ~0.7 s、local 0.45 s），
  每条两臂 ≈ 4–5 s，50 条 **≈ 4–6 分钟**（串行；重试/排队会拉长）。
  实测 2026-10-25 正式跑（C 臂训练并发、local 被拖慢）**479 s / 约 8 分钟**，
  含 9 条失败重试共 11 次重跑；因此可按 **5–10 分钟**做时间预算。
- 建议先 `--limit 10 --bias-check 5`（≈45 次调用、1–2 分钟、<¥0.05）确认方向，再跑全量 50。
- 本工具不 import torch/transformers，仓库 python 3.14 与 `E:\agent\venv312` 都能跑；正式跑建议后台执行。

## 与 judge.py 的关系

- 直接复用 `judge.judge_one`（JUDGE_PROMPT、`build_context`、左右随机、`winner` 映射）与 `judge.wilson`；
- 新增「双向重评」位置偏差检查在 `downstream.py` 内实现，**不修改** `judge.py`。

## 踩坑记录（以后调 teacher 的人一定会遇到）

1. **DeepSeek `json_object` 必须 prompt 含 “json”**（2026-10-25 实测）：
   `llm.Client.chat(json_mode=True)` 会给 DeepSeek 带
   `response_format={"type":"json_object"}`，而 DeepSeek 要求 messages 里出现 “json”
   一词，否则 **HTTP 400**：
   `Prompt must contain the word 'json' in some form to use 'response_format' of type 'json_object'`。
   - 训练同源 prompt（`soul.prompt.build_messages`）里没有 “json” → 直接 400；
   - 只加一句简化的 JSON 后缀也不够：teacher 会把 `behavior` 写成一段字符串，
     `schema.validate` 失败（本工具会回退 rule 并记 `arm_b_source=rule`）；
   - 解决：teacher 臂用打标签同源的 `soul.dataset.teacher_messages`（自带完整字段说明、
     枚举与「只输出一个 JSON 对象」），见上文「两臂 prompt 为什么不对称」。
2. **llama-server 的 `/props`** 用来确认实际加载的 GGUF；本工具在真实 local 模式下会读取
   并写入 `meta.local_server_props`，模型路径含 “v0” 时会打印警告（防误用旧底座）。

## 已知限制

1. **裁判与回复生成同族（DeepSeek）**：存在自评/同族偏好，结果只作初筛；建议对平局与关键 bucket 人工复核。
2. **golden_v1 没有 state_profile**：按 bucket 映射档位（上表）；这改变的是「喂给两臂的当前状态」，两臂完全一致，不影响对照公平性，但换映射会改变绝对结论，需在报告里注明。
3. **隔离 prompt**：只用 `chat._prompt`（无记忆/画像/检索/教训/两段式生成），目的是把差异收敛到内状态块；线上完整 prompt 下的绝对效果需另测（此工具回答的是「块本身有没有用」）。
4. **crisis 条目**：线上 `chat.send` 的危机分支不注入内状态块；G3 为了让两臂可比，仍按常规路径注入（两臂同样处理），crisis 在分 bucket 里单列，判读时不要把 crisis 的胜负外推。
5. **一位裁判、一条 prompt**：每对只判一次（bias-check 是抽样复核）；judge 输出不可解析时该条 `winner=null` 且不计入统计（`summary.errors.judge` 有计数）。
6. **单线程串行**：真实 50 条约 4–6 分钟；中断用 `--resume` 续跑，已完成的 `golden_id` 不会重复调用。
7. **teacher 臂 max_tokens=300**（任务书口径）：pilot 里出现过 completion=296 接近顶格的情况，
   正式跑若 `block_sources.teacher.rule` 回退条数偏多，可考虑调大该上限后重跑——但那样与
   打标签口径不再一致，需在报告里注明。
8. mock 结果不代表 G3；`--arms teacher` 单臂不产生 winner，只用于链路自检。

## 正式跑检查清单（等 Lead 通知后）

1. `git branch` 应在 `w4-soul`；不切换分支、不 commit；
2. llama-server 已起在 `--base-url`（`curl http://127.0.0.1:8123/health` → `{"status":"ok"}`），GPU 不再被训练占用；
   server 由 Lead 起时**不要抢端口/重复起**，只读 `/health` 与 `/props`；
3. **确认底座**：`curl http://127.0.0.1:8123/props` 看 `model_path`，必须是用 Lead 指定的最佳
   GGUF（0.5B 用 `E:\agent\output\gguf\soul_qwen25_05b_v1-q4_k_m.gguf`；1.5B 待 Lead 转出后
   按指定路径），**不要用 v0**；本工具会把它记进 `meta.local_server_props` 并在模型路径含
   “v0” 时告警；
4. DeepSeek key 可用（`web_config.json` 的 `llm.api_key`）；
5. 先 `--limit 2 --arms teacher` 验证 teacher 链路 → 再 `--limit 10 --bias-check 5` 两臂试跑 → 最后全量 50；
6. 产物写 `E:\agent\logs\`，不写 `agi/data/`；
7. 报告里重点看：总体 local 胜率 + Wilson CI、10 个 bucket 是否有系统性掉分、`position_bias.order_dependent`、`block_sources` 里 rule 回退条数（回退多说明本地模型输出不合格，G3 要打折）。
