# SelfLearn · 自学习对话体（v0.1）

不是客服，也不是“陪伴型”，更不是用户设定好的人格。

它从空白开始，通过对话、行动结果和反馈，自己决定记住什么、自己选择聊天策略，
并逐步形成稳定的自画像/人格。用户信息只是记忆的一层，不是人格定义。

## 核心边界

- **空白出生**：初始没有已学会策略、没有记忆、没有人格；只有一个不显示的“自然回应”基线；
- **记忆自己定义**：每轮由 LLM 自行决定记什么，scope 可为 user/self/world；
- **人格自己涌现**：只从它自己的回复 + 反馈中反思，不从用户偏好复制；
- **人格稳定**：已激活的 persona key 不允许改值，避免今天一个明天一个；
- **识图**：网页可上传图片，交给 DeepSeek V4.1 Flash 多模态理解；
- **不接微信**：当前只做网页聊天，wxauto 不参与。

## 网页

```bash
python -m agi.selflearn.web --port 8082
```

打开：`http://127.0.0.1:8082/`

- 聊天 + 上传图片；
- 每条 AI 回复下可点 `good / ok / bad`；
- 反馈会更新策略变体的 α/β；
- 每 3 条反馈触发一次自反思，只从**自己的行动结果**形成自画像；
- 右侧显示：它自己的记忆、自画像、聊天策略。

## CLI

```bash
python -m agi.selflearn.cli chat "你好"
python -m agi.selflearn.cli status
python -m agi.selflearn.cli memories
python -m agi.selflearn.cli persona
python -m agi.selflearn.cli skills
python -m agi.selflearn.cli feedback <turn_id> good
python -m agi.selflearn.cli reflect
```

## 数据表

- `messages`：原始对话
- `user_memories`：由 LLM 自己决定留存的记忆（scope=user/self/world）
- `turns`：每轮回复 + 技能/变体 + 反馈
- `feedback`：用户反馈
- `skills` / `variants`：聊天技能与策略带 α/β
- `persona`：已稳定的自画像，激活后不可轻易改值
- `persona_candidates`：候选自画像（审计用）
- `reflections`：自反思记录

## 模型

- 默认 `deepseek-flash`（DeepSeek V4.1 Flash API）
- 文本 + 图片多模态：`image_url` data URL
- 图片输入在网页端自动读取为 base64

## 测试

```bash
python -m pytest agi/tests/test_selflearn.py -q
```

## 旧方向

旧的业务/IDENTITY/客户聊天/`agi/core`/wxauto 客户发送已移出工作区，
归档在 `_archive_wrong_direction_*`，新代码不 import。

## 09–14 已接模块

- `state.py`：情绪 valance/arousal、边界危机、关系阶段
- `trust.py`：信任账本、正向/负向事件、未解决事件
- `proactive.py`：分享欲候选（check_in/share_memory/share_thought/care），**只生成候选，不自动发送**
- `scheduler.py`：intent 到期查询，用于按需唤醒
- `errors.py`：bad 反馈 → 错误账本 → 可执行教训
- `acceptance.py`：本地 MVP 验收（聊天、记忆证据、反馈、人格唯一、识图、主动、调度）

## 验收命令

```bash
python -m agi.selflearn.cli acceptance
python -m agi.selflearn.cli proactive
python -m agi.selflearn.cli state
python -m agi.selflearn.cli trust
python -m agi.selflearn.cli errors
python -m agi.selflearn.cli intent due
```

## 弱验证与信任修复

- `skills.record_weak_check` / `evaluate_skill`：样本 <5、Wilson 95% 下限 <=0.5 时 **技能不升级**；
- 技能等级只升不降，显示为 `Lv0–Lv5`；
- `trust.repair`：显式修复一条未解决负面事件；`good` 反馈也会顺带修复一条旧账；
- 图片理解会写成 `scope=world` 的记忆，作为下一轮上下文。

## 主动候选审核

网页点「它想主动说」只生成候选，必须点「放进输入框」或「跳过」，没有自动发送。

## 记忆冲突与防幻觉

- 记忆写入先过证据/问答过滤；同一偏好正负相反、同一住址/姓名不同时：
  - **不覆盖、不二选一**；
  - 两条都置为 `conflict`，写入 `conflicts`；
  - 下次对话自然确认一句，用户确认后用 `resolve-conflict` 定稿。
- `guard.py` 对生成结果做轻量规则校验：
  - 绝对化词软化；
  - 无工具却声称“我查了” → L2 安全回复；
  - 超长截断。
- `open_conflicts()` / CLI `conflicts` / `resolve-conflict`。

## 成长报告

```bash
python -m agi.selflearn.cli report
python -m agi.selflearn.cli report --markdown
```

网页右上角「成长报告」显示：消息/轮次/反馈、信任、记忆 scope、自人格、冲突、技能等级、错误教训、主动候选、调度状态。

示例（临时 E2E 库，不是活动库）：
`recon/selflearn_growth_demo.md`

## 媒体持久化

上传图片会保存到 `agi/data/selflearn/media/`，并在 `attachments` 表关联消息、sha256、字节数、路径。
`python -m agi.selflearn.cli media` 可列出附件；图片理解同时进入 `scope=world` 记忆。

## 动态技能与两段式生成

- `skills.add_dynamic_variant`：反馈/反思可以长出一条新的策略变体，最多 8 条，重复 hint 不新增；
- `generation.py`：先让模型只列有记忆 id 支持的 claims，再只根据 claims 写回复；
  - 没有证据或证据阶段失败时自动回退单段生成；
  - 可用环境变量 `SELFLEARN_TWO_STAGE=0` 关闭。

## 灰度策略与错误预算

- `add_dynamic_variant` 新建策略先进入 `gray`，只有 10% 流量；
- 灰度样本 ≥5 且 Wilson 下限 >0.5 → 转 `active`；否则 `retired`；
- `errors.budget`：最近 20 轮同类错误 ≥3 条 → 进入保守模式（只共情/提问，不陈述事实）。

## 记忆控制与导出

- 网页记忆面板每条有「删除」：标记 `invalid`，不物理删除，保留审计；
- 「导出脑」下载 `selflearn-brain.json`，含 messages/memories/turns/feedback/persona/skills 等；
- CLI：`python -m agi.selflearn.cli forget <memory_id>`。

## 阶段摘要

- 每 6 条用户消息自动尝试压缩最近对话为 `summaries`；
- 摘要只保留用户明确事实、关系进展、未解决话题/承诺；
- 聊天 prompt 会带上最近阶段摘要，让长期经历连续；
- CLI：`python -m agi.selflearn.cli summary` / `summarize --force`。

## 真实样本风格档案

- 来源：用户授权的 5 张真实聊天截图；
- 只提取节奏：短句、口语、不列清单、对方低投入时收住；
- 不保存原始聊天内容、不做事实记忆；
- `style_profile.py` 注入 prompt，并在普通聊天回复后做短句收敛；
- 10 条盲评样本：`recon/style_blind_10_v2.csv`（natural 列待用户填）。

## 主动开口

- 网页每 30 秒轮询 `/api/proactive/tick`；
- 空闲 >= 60 秒、分享欲分数够、且不在冷却期时，它会主动生成一句并写入聊天；
- 主动消息写入 `messages.proactive=1`，不会发微信；
- 冷却 15 分钟，避免刷屏；
- 前提：网页开着（浏览器轮询），关掉页面就不会主动。

## 情绪与主动节奏

- `state.self` 保存它自己的心情/精力/分享欲/电量，不是用户情绪；
- 心情差、短消息且无明确要求时，它可以选择不回；
- 心情好、分享欲高时，一次回复可拆成 2–3 条短消息；
- 主动 tick 由它自己的心情/分享欲决定，不是只按用户空闲；
- 网页每 30 秒轮询；仍不发微信。

## Batch 2：Learn Loop

```bash
python -m agi.selflearn.cli learn-status --skill SK-CODE-HTML
python -m agi.selflearn.cli learn-gap --skill SK-CODE-HTML --goal "学会写 HTML 页面"
python -m agi.selflearn.cli learn-source --skill SK-CODE-HTML --url "https://..." --keyword "..."
python -m agi.selflearn.cli learn-card --skill SK-CODE-HTML --content "..." --example "..."
python -m agi.selflearn.cli learn-practice --task LT-... --passed
python -m agi.selflearn.cli learn-verify --task LT-... --passed
```

- 来源必须带 quote，卡必须有 provenance；
- 至少 3 次练习且 verifier 通过，才走 `skilltree.unlock` 到 L3；
- 这不是全自动全网自学：URL 和 verifier 结果目前需人工提供。

## Batch 3：入库/切分/图片结构化/画像

```bash
python -m agi.selflearn.cli ingest-jsonl --file messages.jsonl
python -m agi.selflearn.cli sessions
python -m agi.selflearn.cli segment --session <id>
python -m agi.selflearn.cli image-describe --path <本地图片>
python -m agi.selflearn.cli profile
python -m agi.selflearn.cli profile-distill
```

- ingest：JSONL → raw_events，按 friend/时间切 session，按字符切 segment；
- image-describe：本地图片 → caption/tags/emotion 写 media_understanding；
- profile：从 active user_memories + active claims 蒸 10 维画像，每维必须有证据；
- 这些仍不接旧客户数据，也不做视频。

## Batch 4：表情包 + 04 评分/提案

```bash
python -m agi.selflearn.cli emoji-index --dir <本地表情目录>
python -m agi.selflearn.cli emoji-stats
python -m agi.selflearn.cli proposals
python -m agi.selflearn.cli proposal-transition <P-id> gray
python -m agi.selflearn.cli score-bucket --stage 熟 --mood positive --topic 生活
```

- 表情包只索引本地资产，不联网；
- 反馈现在会写 `score_records`（三维桶 + 加权 sigmoid）；
- proposal 只走 draft→gray→merged/rejected，不自动改人格/技能。

## Batch 5：09–14 细化

```bash
python -m agi.selflearn.cli affect
python -m agi.selflearn.cli initiative
python -m agi.selflearn.cli weak-status
python -m agi.selflearn.cli weak-schedule --skill SK-CODE-HTML
python -m agi.selflearn.cli weak-recheck --skill SK-CODE-HTML --passed
python -m agi.selflearn.cli wake-add --text "..." --after 3600
python -m agi.selflearn.cli wake-run
```

- affect：情绪/能量/分享/电量/信任 + 时间衰减 + grudge 上下文；聊天已接 grudge；
- initiative：主动欲望评分，只生成候选，不发送；
- weak_validation：跨时间复测，失败只标记回退，不直接降级；
- wake：优先级/预算/去重/审计，只标记 due，不自动发微信。

## 生产级验收

```bash
python -m agi.selflearn.cli --db <db> production
python -m agi.selflearn.cli --db <db> production --markdown
```

门槛：消息≥30、轮次≥10、反馈≥3、记忆≥5、证据覆盖≥80%、人格≥3、claims≥5、未解决冲突≤5、错误率≤25%。

注意：这是 pilot 门槛，不能替代 7 天真实运行；当前活动库只有 `needs_work`。

### 历史回填

```bash
python -m agi.selflearn.cli --db <db> backfill-claims --limit 12
python -m agi.selflearn.cli --db <db> backfill-persona
```

- claims 从真实历史 user 消息重新抽取，evidence 必须回原文；
- persona 走现有反思，证据不足会自动不写；
- feedback 不能回填，不能伪造。

### 隐式反馈

```bash
python -m agi.selflearn.cli --db <db> implicit-feedback
```

- 从下一句真实用户消息推断 good/ok/bad，写 `note=implicit:...`；
- 只是行为代理，不是用户主动评价；
- production 门槛有独立 `explicit_feedback` 项，隐式反馈不计入。

### 实时入库

selflearn 每条消息会自动镜像到 `raw_events`（source=live）；session/segment 仍需 `ingest.build_sessions` 重建。

### 跨天门槛

production 现在要求 `active_days >= 3`、`feedback_days >= 2`，防止一天猛聊就宣称生产通过。

### M1 质量审计

```bash
python -m agi.selflearn.cli --db <db> quality
```

- claim evidence 必须能回到原消息；
- pending 比例目标 ≤25%；
- 10 维画像每维至少 2 条证据。

### LLM reasoning 预算

`deepseek-flash` 的 reasoning tokens 可能吃满 max_tokens，导致正文为空；`llm.Client` 现在检测 `reasoning_tokens >= 80%` 或 `finish_reason=length` 时自动扩容重试一次（4096/8192 上限）。

### 上下文注入与预算

- 画像、阶段摘要、检索片段、冲突提醒会真正进入 prompt；
- `context.fit` 以 5200 token 预算裁剪，保留开头设定与结尾用户原话。

### 目标树（M4）

```bash
python -m agi.selflearn.cli goals
python -m agi.selflearn.cli goal-add "把 AGI 跑通" --priority 0.9
python -m agi.selflearn.cli goal-update <id> --status done --progress 1
```

只记录目标与进度，不自动执行/发送。

### 夜间整理（M4）

```bash
python -m agi.selflearn.cli nightly --force
```

顺序：摘要 → 画像 → 自反思 → 弱验证状态；只写本地，不发送、不改技能等级。

### 风格漂移审计（M3）

```bash
python -m agi.selflearn.cli --db <db> drift
```

比较最近/之前窗口的回复长度、提问比例、软化词比例；只审计，不自动改策略。
