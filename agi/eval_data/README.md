# eval_data/ · 评测数据说明

> 本目录是**评测资产**，不是训练语料。所有内容已匿名化，但仍属于个人数据，**不要公开发布本仓库**。

## 文件

| 文件 | 内容 | 生成方式 |
|---|---|---|
| `golden_v1.jsonl` | 50 条黄金集，10 类场景 × 5 | `python tools/build_golden.py` |
| `baseline.md` | 数据基线：5 个库的计数、反馈对账、延迟结论、9 条数据质量问题 | `python tools/build_golden.py --stats` |

## 黄金集格式

```json
{"id":"G-001","bucket":"work","context":[{"role":"user","text":"..."}],
 "user_msg":"...","ideal_points":["...","..."],"source_ref":"messages.jsonl:94"}
```

- `bucket`：work / low_mood / conflict / help / smalltalk / boundary / joke / privacy / crisis / silence
- `synthetic: true`：合成样本（目前 low_mood 5 条 + crisis 5 条，因为真实数据里没有这两类）。
- `source_ref`：只给审计用，**不要喂给模型**。

## 使用方式（W1 验收）

1. 每次改 `chat.py` / `proactive.py` / 注入层后，用同一批 context 生成回复；
2. 用 `ideal_points` 逐条 0/1 打分（或人工盲评），按 `bucket` 看退化，而不是只看总分；
3. 与 `baseline.md` 的现状对比，掉分 >2% 回滚。

## 匿名化规则

- 真实昵称 → A/B/C/D/E/F（脚本从数据动态发现，不硬编码）；
- 「X工」→ A工/B工；`wxid_*` → `[微信号]`；
- 手机号/身份证/邮箱/本地路径/医院/品牌 → 占位符；
- 生成后用 `python tools/build_golden.py --check` 校验，宽口径扫描命中应为 0。

## 注意

- 10 条 synthetic 只能验证「格式与安全」，不能证明真实场景效果；有真实样本后替换。
- 黄金集冻结：定版后 3 个月内不改（换版另起 `golden_v2.jsonl`）。
- `agi/data/` 下的原始库不进 git；本目录的匿名产物才进 git。
