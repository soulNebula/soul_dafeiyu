# P2-b 语气适配：数据缺口 + smoke 训练 + 分布/pairwise 评测

> 目标：让模型学会「用户本人真实发言」的语气（target = 用户 self 真实发言，**不用 teacher 合成 target**）。
> 本报告照实记录：数据量不够 → 只做管道 + smoke；adapter 目前**不可用于上线**，原因见结论。

## 1. 数据量结论：**不够，先不正式训练**

| 项 | 数值 |
| --- | --- |
| 合法 SFT 对（前 2–4 条真实上下文 → self 真实发言） | **65 对** |
| 其中 train / val（90/10 按 session 切） | **58 / 7（10.8%）** |
| session 数 / val sessions | 10 / 4（同一截图、同一联系人会话不跨集；已断言） |
| 来源 | OCR 截图 26 对 + 联系人库 39 对 |
| target 长度中位数 | 6 字 |
| **距正式训练（≥200 对）还差** | **135 对** |
| 隐私断言 / 会话泄漏断言 | 通过（`tools/build_human_tone.privacy_violations` 逐条 + session 交集检查） |

数据文件：`agi/eval_data/human_tone/sft.jsonl`（字段 `{id, source, session, messages, target_len, split}`，
messages 最后一条 = assistant = 用户真实发言，之前 2–4 条真实上下文；无 system 文档，避免“照文档模仿”）。
构建/训练脚本：`agi/eval_data/tone_train.py`（`--dry-run` 只造数据；数据 ≥200 时自动走正式训练）。

> 用户之后补截图 → 重跑 `tone_train.py --dry-run` 即可扩充；到 200 对后直接：
> `python agi/eval_data/tone_train.py --epochs 2 --lr 1e-4 --out E:\agent\output\tone_qwen05_v1`
> 不需要改代码。

## 2. smoke 训练（GPU，已验证独占：跑前 nvidia-smi 729–752MiB/6141MiB、1%）

| 项 | 数值 |
| --- | --- |
| 模型 / adapter | Qwen2.5-0.5B-Instruct + LoRA r16/α32/dropout0.05 |
| 数据/步数 | 58 对 train，**100 步（2 epochs）**，bs1、grad ckpt、bf16、seq384、lr 1e-4 |
| 耗时 | **49.98s（0.50s/step）** |
| loss | first **13.53** → last **3.19**，mean 3.81（bs=1 抖动大，仅证明可降） |
| 显存 | peak alloc **1.294GB** / peak reserved **1.49GB**（6GB 卡余量很大） |
| 可训练参数 | 8.8M |
| adapter 落盘 | `E:\agent\output\tone_qwen05_v1_smoke`（含 `train_metrics.json`） |

结论：**训练管道通了**，6GB 显存完全够（就算 1.5B 也能跑），可复现。

## 3. 分布对比（val 7 条，生成用 adapter，贪心 max_new_tokens=64）

人类参考 = `human_tone/profile.md` 的 self 语料（59 条）。

| 指标 | 真人 self | smoke adapter 生成 | 差距 |
| --- | --- | --- | --- |
| 长度中位数 | **7 字** | **2 字** | 模型过短（-5） |
| 句尾无标点率 | 86.4% | 100% | +13.6pp（方向对，但原因不同：几乎只输出 1 个词） |
| 问号率 | 13.6% | **0%** | -13.6pp（模型不会提问） |
| 句号率 | 0% | 0% | — |
| 口语词覆盖率 | 30.5% | 85.7% | 看似更高，实为只吐“嗯/可以/去不去”这类高频短词 |
| 单条主题数（均值） | 1.36 | 1.0 | 模型没有“多说一句”的能力 |

**生成样例（val 全部 7 条，全部与上下文无关）**：

| context（节选） | 真人原话 | 模型生成 |
| --- | --- | --- |
| 好像是这个 / 今晚请你喝水 / 8到9 | 定了吗 | 可以 |
| …8到9 / 定了吗 | 是不是这个号码 | 嗯 |
| 不过找到了就好 / 在家里太无聊了… | 真的服了，3D电影没带3D眼镜 | 去不去 |
| …3D电影没带3D眼镜 | 播到一半，电影出问题了 | 可以 |
| 真的挺好看的，嗯 / 你可以提醒她 / 骂就别了 / 待会别人脾气一上来就难搞 | 我不会轻易骂别人的，后来我换了个位置就好点了 | 可以 |
| 你可以提醒她 / … | 有星星看吗？ | 可以 |
| 好好好，吃上了是吧 / 我还要再烧一串炮竹 | 跟下邻居的风 | 嗯 |

→ 模型学到的只是「高频短词 + 不加标点」的表面分布，**完全没有接住上下文**。

## 4. pairwise（DeepSeek 当裁判，20 条）

同一 context 下 A=真人原话、B=模型生成（左右随机），问「哪条更像真人在微信里跟朋友说话」：

| 集合 | n | 模型胜 | 真人胜 | 平局 | 模型胜率 |
| --- | --- | --- | --- | --- | --- |
| 全部 | 20 | 12 | 4 | 4 | **60%** |
| held-out val | 7 | 4 | 1 | 2 | 57.1% |
| in-sample train | 13 | 8 | 3 | 2 | 61.5% |

**⚠ 这个 60% 不能当作“语气达标”**：
- 裁判只问“更像真人”，模型输出「嗯/可以/可以」这种 1–2 字片段，天然更像人随手回一句；
- 但这些回复**与上下文无关**（7/7 val 全是牛头不对马嘴），真人原话反而因为更长、信息多被判“太完整”；
- 20 条里只有 7 条是 held-out，且裁判没有做“是否接得上上下文”的判断；
- 说明当前评测口径需要补「相关性/是否接话」这一维，否则会被“短即真人”的偏差骗过。

## 5. 结论与下一步

**能上线吗？不能。** 管道验证通过，但这个 adapter 不可用：数据 65 对太少（差 135 对），模型退化成高频短词生成器。

建议：
1. **等用户补截图**：目标 ≥200 对（最好 300–500；本轮数据每张截图约产 4–6 对），到时重跑 `tone_train.py`（无需改代码）。
2. **数据侧加工**：同一 user 的高频短句（可以/嗯/行/好）会主导分布 → 训练前做「同一 target 上限/降权」，或按 target 长度分桶采样；同时保留长一点的真实回复作为少数类（P2-a 里“气死我了/电影看了多久…”这种）。
3. **训练参数**：200 对时建议 2–3 epochs、lr 5e-5~1e-4、bs1+grad_accum 4；继续观察 loss 与生成长度。
4. **评测补维**：pairwise 加一条「回复是否接得上上文」的问题（或加 relevance 判断），只比“像不像人”会得出误导性胜率；同时 val 至少 20–30 条（按 session 切分，不够时可用 2 折交叉验证替代 90/10）。
5. **上线形态**：语气 adapter 最好和「状态/内容生成」分开，避免 0.5B 小模型把内容也学丢；或把 adapter 输出限制在“改写语气”而非“整句生成”。

## 6. 产物与复现

| 文件 | 说明 |
| --- | --- |
| `agi/eval_data/human_tone/sft.jsonl` | 65 对（train 58 / val 7，session 级切分） |
| `agi/eval_data/tone_train.py` | 造数据 + LoRA 训练（`--dry-run` / `--smoke` / `--epochs` / `--limit`） |
| `agi/eval_data/tone_eval.py` | 分布对比 + DeepSeek pairwise（`--adapter` / `--pairwise-n` / `--no-judge`） |
| `E:\agent\output\tone_qwen05_v1_smoke` | smoke adapter + train_metrics.json |
| `E:\agent\logs\tone_eval_smoke.json` | 7 条生成 + 20 条 pairwise 明细 |

复现命令：

```powershell
E:\agent\venv312\Scripts\python.exe agi/eval_data/tone_train.py --dry-run
E:\agent\venv312\Scripts\python.exe agi/eval_data/tone_train.py --smoke --out E:\agent\output\tone_qwen05_v1_smoke
E:\agent\venv312\Scripts\python.exe agi/eval_data/tone_eval.py --adapter E:\agent\output\tone_qwen05_v1_smoke --pairwise-n 20 --out E:\agent\logs\tone_eval_smoke.json
```
