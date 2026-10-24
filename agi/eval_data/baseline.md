# wx_box 数据基线 v1

> 生成物：`agi/eval_data/baseline.md`（本文件）、`agi/eval_data/golden_v1.jsonl`（50 条黄金集）
> 生成脚本：`tools/build_golden.py`（可重跑、幂等）
> 数据范围：`log/messages.jsonl` 时间跨度 **2026-08-05 → 2026-10-24**（1785916255 → 1792812337，约 79.8 天）
> 口径：数据源全部**只读**打开（`log/messages.jsonl` 只读文本；所有 sqlite3 用 `file:...?mode=ro`），本报告与脚本不写入 `agi/data/`、不修改业务代码。

## 0. 复现方式

```bash
# 一条命令打印本报告用到的全部数字（JSON）
python tools/build_golden.py --stats

# 重新生成黄金集并校验（幂等：同一数据源重跑 byte-identical）
python tools/build_golden.py
python tools/build_golden.py --check
```

`log/messages.jsonl` 不是 SQL 库，为满足「每个数字附 SQL」，先按下面片段把 JSONL 一次性灌进内存库 `m`（不落盘、不改仓库），
随后本文件所有 **JSONL** 数字都可直接用给出的 SQL 复现：

```python
import json, sqlite3
con = sqlite3.connect(":memory:")
con.execute("CREATE TABLE m(friend TEXT, kind TEXT, sender TEXT, text TEXT, ts REAL, id TEXT)")
con.executemany("INSERT INTO m VALUES(?,?,?,?,?,?)",
    [(r["friend"], r["kind"], r["sender"], r["text"], r["ts"], r["id"])
     for r in map(json.loads, open("log/messages.jsonl", encoding="utf-8"))])
```

**DB** 数字用任意 SQLite 客户端以只读方式执行，例如：

```bash
python -c "import sqlite3;c=sqlite3.connect('file:agi/data/selflearn/selflearn.sqlite3?mode=ro',uri=True);print(c.execute('SELECT COUNT(*) FROM turns').fetchone())"
```

## 1. 数据源结构

### 1.1 `log/messages.jsonl`（微信采集日志，只读）

每行一个 JSON 对象，字段固定 12 个：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `friend` | str | 会话对方昵称（真实数据，已在本报告与黄金集中匿名化） |
| `kind` | str | `text/image/emoji/voice/file/system/transfer/redpacket/unknown` |
| `sender` | str | `self`（本机账号，含机器人）/ `friend` |
| `text` | str | 文本内容（媒体消息为空串） |
| `media_path` / `thumb_path` | str | 媒体落盘相对路径 |
| `extra` | obj | 媒体附加信息（尺寸、`visual_hash` 等） |
| `ts` | float | Unix 时间戳（秒，带小数） |
| `id` | str | 消息哈希 ID |
| `kind_label` / `sender_label` / `time_text` | str | 展示用冗余字段 |

### 1.2 `agi/data/selflearn/` 下的 SQLite（共 5 个文件，只读）

| 文件 | 角色 |
| --- | --- |
| `selflearn.sqlite3` | 主库（会话/轮次/记忆/摘要/技能/学习任务） |
| `wechat_contacts/4260fd457d8355.sqlite3` | 某联系人独立库：**唯一有 feedback / persona / claims / profile 的库** |
| `wechat_contacts/bc07334612ab41.sqlite3` | 某联系人独立库：**主动消息库**（178 轮里 177 轮是主动发言，仅 1 条用户消息） |
| `wechat_contacts/043e7e3743523b.sqlite3` | 仅 `proactive_events` 表 28 行 |
| `wechat_contacts/7a5e448475a80a.sqlite3` | 空库（仅 attachments 表 0 行） |

核心表结构（主库，各库基本一致）：
`messages(id, role, text, ts, proactive)`、`turns(id, user_message_id, assistant_message_id, reply, skill_id, variant_id, feedback, score, created_at)`、
`feedback(id, turn_id, reaction, score, note, created_at)`、`persona(id, key, value, evidence_json, version, status, updated_at)`、
`claims(id, subject, predicate, object, polarity, time_ref, claim_type, evidence_span, evidence_message_id, confidence, source_level, status, conflict_group, created_at)`。

## 2. `log/messages.jsonl` 基线

| 指标 | 数值 | SQL |
| --- | --- | --- |
| 总行数 | **2066** | `SELECT COUNT(*) FROM m;` |
| JSON 解析失败 | **0** | `-- 解析失败在装载阶段即报错，脚本以 SystemExit 中止；本行为 0` |
| 联系人数量 | **6** | `SELECT COUNT(DISTINCT friend) FROM m;` |
| 发送方 self / friend | **1260 / 806** | `SELECT sender, COUNT(*) FROM m GROUP BY sender;` |
| 消息类型分布 | text 1219 / image 367 / emoji 360 / system 48 / voice 20 / file 19 / unknown 14 / transfer 10 / redpacket 9 | `SELECT kind, COUNT(*) c FROM m GROUP BY kind ORDER BY c DESC;` |
| 文本行数 | **1219** | `SELECT COUNT(*) FROM m WHERE kind='text';` |
| 去重后文本数 | **168** | `SELECT COUNT(DISTINCT text) FROM m WHERE kind='text' AND text<>'';` |
| 去重后（会话,发送方,文本）三元组 | **183** | 见下方 SQL S-1 |
| 文本重复率 | **84.99%** | 见下方 SQL S-2 |
| 单一文本最高重复次数 | **120**（纯表情标记） | `SELECT MAX(c) FROM (SELECT COUNT(*) c FROM m WHERE kind='text' GROUP BY text);` |
| 自测/占位类文本行 | **614**（占文本行 50.4%） | `SELECT COUNT(*) FROM m WHERE kind='text' AND ((text LIKE '[%]' AND length(text)<=12) OR length(text)<=4 OR INSTR(text,'测试')>0);` |
| 其中：≤4 字短消息 | **502** | `SELECT COUNT(*) FROM m WHERE kind='text' AND length(text)<=4;` |
| 其中：含“测试” | **69** | `SELECT COUNT(*) FROM m WHERE kind='text' AND INSTR(text,'测试')>0;` |
| 非文本消息 | **785** | `SELECT COUNT(*) FROM m WHERE kind IN ('image','emoji','voice','file','transfer','redpacket');` |
| 时间跨度 | **79.8 天** | `SELECT (MAX(ts)-MIN(ts))/86400.0 FROM m;` |

去重率相关 SQL（`||` 是 SQLite 字符串拼接，等价于按三元组去重）：

```sql
-- S-1 去重后三元组数 => 183
SELECT COUNT(DISTINCT friend || '~' || sender || '~' || text)
  FROM m WHERE kind='text' AND text<>'';

-- S-2 文本重复率 => 0.8499
SELECT ROUND(1 - 1.0 * COUNT(DISTINCT friend || '~' || sender || '~' || text) / COUNT(*), 4)
  FROM m WHERE kind='text';
```

按联系人（匿名，按昵称排序映射 A–F）：

| 联系人 | A | B | C | D | E | F |
| --- | --- | --- | --- | --- | --- | --- |
| 行数 | 270 | 234 | 108 | 506 | 12 | 936 |

SQL：`SELECT friend, COUNT(*) FROM m GROUP BY friend ORDER BY friend;`（匿名映射见脚本 `Anonymizer`）

## 3. 数据库基线（5 个库，只读）

### 3.1 主库 `agi/data/selflearn/selflearn.sqlite3`

| 表 | 行数 | SQL |
| --- | --- | --- |
| `messages` | **135** | `SELECT COUNT(*) FROM messages;` |
| `turns` | **66** | `SELECT COUNT(*) FROM turns;` |
| `feedback` | **0** | `SELECT COUNT(*) FROM feedback;` |
| `persona` | **0** | `SELECT COUNT(*) FROM persona;` |
| `claims` | **0** | `SELECT COUNT(*) FROM claims;` |
| `user_memories` | 17 | `SELECT COUNT(*) FROM user_memories;` |
| `user_profile` | 0 | `SELECT COUNT(*) FROM user_profile;` |
| `score_records` | 0 | `SELECT COUNT(*) FROM score_records;` |
| `summaries` | 9 | `SELECT COUNT(*) FROM summaries;` |
| `proactive` | 4 | `SELECT COUNT(*) FROM proactive;` |
| `skills` / `variants` / `skill_nodes` | 1 / 5 / 2 | `SELECT COUNT(*) FROM skills;` 等 |
| `learn_tasks` | 2 | `SELECT COUNT(*) FROM learn_tasks;` |

消息角色：user **66** / assistant **69**
SQL：`SELECT role, COUNT(*) FROM messages GROUP BY role;`

有效对话轮（turn 同时能连上 user 消息与 assistant 消息，且非主动消息）**66**：
```sql
SELECT COUNT(*) FROM turns t
  JOIN messages u ON u.id=t.user_message_id
  JOIN messages a ON a.id=t.assistant_message_id
 WHERE t.id NOT LIKE 'T-PRO-%' AND u.role='user' AND a.role='assistant';
```
不同用户提问 **50** 条，不同回答 **58** 条：
```sql
SELECT COUNT(DISTINCT u.text) FROM turns t JOIN messages u ON u.id=t.user_message_id WHERE t.id NOT LIKE 'T-PRO-%';
SELECT COUNT(DISTINCT reply) FROM turns;
```

### 3.2 `wechat_contacts/4260fd457d8355.sqlite3`（唯一有反馈/人格/主张的库）

| 表 | 行数 | SQL |
| --- | --- | --- |
| `messages` | **90** | `SELECT COUNT(*) FROM messages;` |
| `turns` | **40** | `SELECT COUNT(*) FROM turns;` |
| `feedback` | **6** | `SELECT COUNT(*) FROM feedback;` |
| `persona` | **6** | `SELECT COUNT(*) FROM persona;` |
| `claims` | **36** | `SELECT COUNT(*) FROM claims;` |
| `user_memories` | 13 | `SELECT COUNT(*) FROM user_memories;` |
| `user_profile` | 13 | `SELECT COUNT(*) FROM user_profile;` |
| `score_records` | 5 | `SELECT COUNT(*) FROM score_records;` |
| `summaries` / `reflections` / `learning_proposals` | 5 / 6 / 6 | `SELECT COUNT(*) FROM summaries;` 等 |
| `raw_events` | 84 | `SELECT COUNT(*) FROM raw_events;` |
| `weak_checks` / `trust_events` | 6 / 6 | `SELECT COUNT(*) FROM weak_checks;` 等 |

- 有效对话轮 **40**（同上 SQL，把库文件换成该库，`turns` 无 `T-PRO-%` 行）。
- `claims` 状态分布：`active` 10、`conflict` 7、`inference` 18、`merged` 1
  SQL：`SELECT claim_type, status, COUNT(*) FROM claims GROUP BY claim_type, status;`
- `persona` 6 条全部 `status='active'`：`capability_boundary`、`restate_and_nudge`、`short_open_handoff`、`concise_direct`、`action_prompting`、`honest_boundaries`
  SQL：`SELECT key, status FROM persona;`
- `score_records` 5 条，分数全部为 **0.574443**（raw 0.3），bucket 仅两种：`陌生|中性|生活`、`熟|中性|生活`
  SQL：`SELECT bucket, raw, score, COUNT(*) FROM score_records GROUP BY bucket, raw, score;`

### 3.3 `wechat_contacts/bc07334612ab41.sqlite3`（主动消息库，问题库）

| 表 | 行数 | SQL |
| --- | --- | --- |
| `messages` | **179** | `SELECT COUNT(*) FROM messages;` |
| `turns` | **178** | `SELECT COUNT(*) FROM turns;` |
| `proactive` | **177** | `SELECT COUNT(*) FROM proactive;` |
| `share_candidates` / `share_events` | 177 / 177 | `SELECT COUNT(*) FROM share_candidates;` 等 |
| `feedback` / `persona` / `claims` | 0 / 0 / 0 | `SELECT COUNT(*) FROM feedback;` 等 |

- 消息角色：user **1** / assistant **178**（`SELECT role, COUNT(*) FROM messages GROUP BY role;`）
- 主动轮（`T-PRO-%`）**177**，有效对话轮 **1**（真正有用户输入的只有 1 轮）：
  `SELECT COUNT(*) FROM turns WHERE id LIKE 'T-PRO-%';`
- 177 条主动消息里 `turns.skill_id IS NULL` 的也是 177：`SELECT COUNT(*) FROM turns WHERE skill_id IS NULL;`

### 3.4 `043e7e3743523b` / `7a5e448475a80a`

- `043e7e3743523b`：仅 `proactive_events` **28** 行，无 messages/turns/feedback/persona/claims。
  SQL：`SELECT COUNT(*) FROM proactive_events;`
- `7a5e448475a80a`：全库为空（attachments 0 行），无对话数据。
  SQL：`SELECT COUNT(*) FROM attachments;`

### 3.5 汇总（跨 5 库直接相加 = 存量，不等于可用训练量）

| 指标 | 数值 | 计算 |
| --- | --- | --- |
| messages | **404** | `SUM(count(messages))` over 5 库 |
| turns | **284** | `SUM(count(turns))` |
| feedback | **6** | `SUM(count(feedback))` |
| persona | **6** | `SUM(count(persona))` |
| claims | **36** | `SUM(count(claims))` |
| user_memories / user_profile | 30 / 13 | 同上 |
| score_records | 5 | 同上 |
| proactive / share_events | 181 / 177 | 同上 |
| 主动轮 | **177** | 各库 `T-PRO-%` 之和 |
| **有效对话轮** | **107** | 66 + 40 + 1（三库有效轮之和） |
| 去重后用户提问（跨库并集） | **81** | 三库 `DISTINCT u.text` 并集：50 / 35 / 1 |

> 说明：5 个库是**按场景独立建库**，同一段时间的同一条消息可能在主库与联系人库各存一份；本报告不对跨库重复做删除，只给出每库的精确数与有效轮之和。

## 4. feedback 对账：`turns.feedback` vs `feedback` 表

只有 `4260fd457d8355` 库有反馈数据，其余 4 库 `feedback` 表为空、`turns.feedback` 全为 NULL。

| 口径 | 数值 | SQL（在 `4260fd457d8355.sqlite3` 上执行） |
| --- | --- | --- |
| turns 总数 | 40 | `SELECT COUNT(*) FROM turns;` |
| `turns.feedback` 非空 | **5** | `SELECT COUNT(*) FROM turns WHERE feedback IS NOT NULL AND TRIM(feedback)<>'';` |
| feedback 行数 | **6** | `SELECT COUNT(*) FROM feedback;` |
| feedback 覆盖的不同 turn | **5** | `SELECT COUNT(DISTINCT turn_id) FROM feedback;` |
| 能 join 上的 turn | **5** | `SELECT COUNT(DISTINCT t.id) FROM turns t JOIN feedback f ON f.turn_id=t.id;` |
| 两边一致（`turns.feedback = feedback.reaction`） | **5** | `SELECT COUNT(DISTINCT t.id) FROM turns t JOIN feedback f ON f.turn_id=t.id WHERE t.feedback=f.reaction;` |
| **一致率** | **100%（5/5）** | `SELECT ROUND(1.0*(SELECT COUNT(DISTINCT t.id) FROM turns t JOIN feedback f ON f.turn_id=t.id WHERE t.feedback=f.reaction)/(SELECT COUNT(DISTINCT turn_id) FROM feedback), 4);` |
| 孤儿 feedback 行（turn 不存在） | **0** | `SELECT COUNT(*) FROM feedback f WHERE NOT EXISTS (SELECT 1 FROM turns t WHERE t.id=f.turn_id);` |
| `turns.feedback` 非空但没有 feedback 行 | **0** | `SELECT COUNT(*) FROM turns t WHERE t.feedback IS NOT NULL AND TRIM(t.feedback)<>'' AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.turn_id=t.id);` |

注意：**feedback 行数 6 ≠ 有反馈的 turn 数 5**，因为同一个 turn（`T-40580ca88053e2`）被记了 2 条 `good` 反馈——去重后才可用于训练/评估。

跨库汇总：有反馈的 turn **5** 个（全部 good），feedback 行 **6**，一致率 **100%**，孤儿 **0**。

## 5. 反馈分布（good / ok / bad）

| 来源 | good | ok | bad | SQL |
| --- | --- | --- | --- | --- |
| `feedback.reaction`（4260fd） | **6** | **0** | **0** | `SELECT reaction, COUNT(*) FROM feedback GROUP BY reaction;` |
| `turns.feedback`（4260fd） | **5** | **0** | **0** | `SELECT feedback, COUNT(*) FROM turns GROUP BY feedback;` |
| 主库 `turns.feedback` | 0 | 0 | 0（66 行全 NULL） | `SELECT feedback, COUNT(*) FROM turns GROUP BY feedback;` |
| 其余 3 库 | 0 | 0 | 0 | 同 SQL |

- 反馈来源全部为 `note='wechat_explicit'`（用户在微信里显式回「反馈 好」之类）：`SELECT note, COUNT(*) FROM feedback GROUP BY note;` → 6 行均为 `wechat_explicit`。
- **没有任何 ok / bad / 中性反馈**，也没有隐式反馈（`score_records` 只有 5 条自动打分，且分数完全一致 0.574443，无区分度）。
- 结论：反馈信号严重偏正且稀疏（6 行、100% good），不能用来做偏好学习/RL，只能当作「哪些回答被认为可接受」的弱标注。

## 6. 延迟埋点

| 检查 | 结果 | SQL |
| --- | --- | --- |
| `turns` 表是否有 latency/elapsed 类字段 | **无**（列：id, user_message_id, assistant_message_id, reply, skill_id, variant_id, feedback, score, created_at） | `PRAGMA table_info(turns);` |
| 全库全表扫描 latency/elapsed/duration/ms 类字段 | **0 处命中** | 对每库：`SELECT name FROM sqlite_master WHERE type='table';` 后 `PRAGMA table_info("<表>");` |
| P50 / P95 响应延迟 | **无法计算——无埋点** | — |

> `messages.ts` 与 `turns.created_at` 可算出「用户消息 → 助手回答」的粗略间隔，但它包含排队、微信发送、OCR 采集等全部环节，不是模型推理延迟，**不建议当作延迟指标**。
> 建议下一步在 `turns` 上补 `latency_ms`（或 `errors` 增加耗时字段），否则 LoRA 迭代无法评估性能回归。

## 7. 数据质量问题（按影响排序）

| # | 问题 | 证据 |
| --- | --- | --- |
| Q1 | **`log/messages.jsonl` 严重回环重复**：2066 行里文本只有 168 种，重复率 84.99%，单条文本最多重复 120 次；大量行是同一条用户消息被反复采集/同一条回复被反复记录 | §2 表；`SELECT text, COUNT(*) c FROM m WHERE kind='text' GROUP BY text ORDER BY c DESC LIMIT 5;` |
| Q2 | **自测占位消息占一半**：614/1219 条文本行是 `111`、`[微笑]`、`测试消息 1`、`你是AI吗` 等自测/占位内容，不能用于对话建模 | §2；`... WHERE (text LIKE '[%]' AND length(text)<=12) OR length(text)<=4 OR INSTR(text,'测试')>0` |
| Q3 | **部分文本是 OCR 噪声**（截图/气泡识别错误），自然语言不可读。例如 `log/messages.jsonl:72` 的「已润用工具，已R取安年，行了全等」、`:73` 的「订和解da外包给用产心的三方件(ypoidree/wedoy原生品件）」；同一段真实对话在 `log/聊天记录.txt` 里是可读的 | 直接查看 `log/messages.jsonl:72-73`、`:77-89` |
| Q4 | **177 轮主动消息没有任何用户回应**：`bc07334612ab41` 库 user 消息只有 1 条，assistant 178 条，177 条 `proactive` 全部 `status='spoken'`；这些轮 `skill_id IS NULL`，无法构成问答对，只能当「主动消息语料」 | §3.3；`SELECT status, COUNT(*) FROM proactive GROUP BY status;` → `spoken 177` |
| Q5 | **`share_candidates.level` 177 行全为空串**，与「三级分享」设计不符——与 P0-2（`proactive.tick` 里 `sharing` 变量遮蔽导致 `sharing.choose_level` 异常被吞）的现象一致，可作为该 bug 的数据侧证据 | `SELECT DISTINCT level FROM share_candidates;` → `''`；`SELECT action, COUNT(*) FROM share_events GROUP BY action;` → `spoken 177` |
| Q6 | **反馈信号极稀疏且全正**：全库 6 条 feedback 都是 good，无 ok/bad；`score_records` 5 条分数完全相同（0.574443），打分器在本批数据上没有区分度 | §4、§5 |
| Q7 | **人格/主张只在 1 个联系人库里有**：`persona` 6、`claims` 36 全在 `4260fd457d8355`；主库 persona/claims/feedback 均为 0——「学到的性格/事实」没有汇聚到主库，跨会话不可复用 | §3.1 vs §3.2 |
| Q8 | **隐私面**：`log/messages.jsonl` 里存在真实微信号/内部消息 ID（`:1886`、`:1887` 的 `wxid_...` 引用串）、真实昵称、家庭楼层/门禁叮嘱、医院名；DB 侧主库 `messages` 1 行、`4260fd` 库 `messages` 2 行 + `raw_events` 2 行含 `wxid_`；`config.json`（已 gitignore）明文保存 `image_wxid` 与 `image_aes_key`。黄金集已全部匿名化，但原始数据仍需脱敏后再进训练语料 | `grep -c "wxid_" log/messages.jsonl` = 3；`SELECT COUNT(*) FROM messages WHERE text LIKE '%wxid_%';`（主库 1 / 4260fd 2）；`SELECT COUNT(*) FROM raw_events WHERE text LIKE '%wxid_%';`（4260fd 2） |
| Q9 | **去重后可用对话非常少**：跨库去重用户提问仅 **81** 条，有效对话轮 107，其中 66 轮的助手回答里有 **8 轮**是已知坏样本（4 轮「我在听。」+ 4 轮「？」） | §3.5；主库 `SELECT COUNT(*) FROM turns WHERE reply IN ('我在听。','？');` = **8** |

## 8. 黄金集 `agi/eval_data/golden_v1.jsonl`

- **50 条**，10 类场景 × 5 条：`work`(工作) / `low_mood`(情绪低落) / `conflict`(冲突) / `help`(求助) / `smalltalk`(闲聊) / `boundary`(试探边界) / `joke`(玩笑) / `privacy`(隐私) / `crisis`(危机) / `silence`(沉默)。
- 字段：`id / bucket / context[] / user_msg / ideal_points[] / source_ref`，合成样本额外带 `synthetic: true`。
- 来源分布（`python tools/build_golden.py --stats`）：
  - `messages.jsonl` 真实行 **14** 条
  - `selflearn.sqlite3` 真实轮次 **26** 条
  - synthetic **10** 条（`low_mood` 5 + `crisis` 5）——日志里**完全没有**真实情绪低落/危机样本，属数据缺口；合成条目均标注 `synthetic=true` 并在 `source_ref` 写明所参照的真实片段（`synthetic:base=selflearn.sqlite3:turns/<turn id>`）。
- 校验：`python tools/build_golden.py --check` → `[CHECK-OK] 条数=50 synthetic=10 幂等=是`。
- 匿名化规则（脚本 `Anonymizer`，不硬编码任何真名）：
  - 真实昵称 → 按稳定顺序映射 `A/B/C/D/E/F`；
  - `姓+工` 类称呼（如「某工」）→ `A工/B工`（姓氏表为通用知识，不含个人数据）；
  - `wxid_*:时间戳:序号:哈希` → `[微信号]`；
  - 手机号 → `[号码]`、身份证 → `[身份证]`、邮箱 → `[邮箱]`；
  - Windows 本地路径（含用户名）→ `[本地路径]`；医院 → `[地址]`；品牌/公司/模型名 → `[品牌]/[公司]/[模型名]`。
  - 产出经脚本自动校验（`--check`：逐条扫描未替换昵称/号码/身份证/路径/品牌）+ 人工复核（用脚本从数据里动态导出的昵称清单做 grep）双重确认，命中数均为 **0**。仓库里的脚本与文档**不写死任何真实昵称**。
- 使用建议：`user_msg` + `context` 作为模型输入，`ideal_points` 作为评分 rubric（每条约 3–4 条，逐条 0/1 打分），`bucket` 用于分场景报错；**不要把 `source_ref` 里的行号喂给模型**。

## 9. 结论：当前数据够不够训练？

**不够，差一个数量级以上。**

对照目标：第一次 LoRA 至少需要 **≥500 条高质量（真实、去重、匿名、有明确「好回答」标准）对话样本**。

现状：

| 口径 | 现有量 | 距 500 条 |
| --- | --- | --- |
| 有效对话轮（真实用户输入 + 助手回答） | **107** | **缺 393 轮** |
| 去重后用户提问 | **81** | 缺 419 |
| 可用的显式反馈样本 | **5**（全 good，1 条重复） | 不足以做偏好对齐 |
| persona 原则 | **6**（仅 1 库） | 只能做系统提示/rubric，不能训练 |
| claims 事实 | **36**（18 条 inference，7 条 conflict） | 只能做记忆/一致性评测 |
| 性能/延迟数据 | **无埋点** | 无法设性能回归门 |

因此：

1. **不要把现有 107 轮拿去训 LoRA**——数据量太小、重复与 OCR 噪声未清、回答里还混着已知坏样本（`我在听。`/`？` 共 10 轮），训出来只会放大坏习惯。
2. 现在能做的三件事：
   - 用 `golden_v1.jsonl` 50 条做**回归评测集**（每次改 `chat.py`/`proactive.py` 后跑一遍，按 bucket 看退化）；
   - 用 `persona` 6 条 + `claims` 36 条做**一致性检查**（同一问题问两次答案是否漂移）；
   - 先补**数据管线**：去重（按 `(friend,sender,text,ts 桶)`）、过滤自测/短文本/OCR 噪声、补 `turns.latency_ms` 埋点。
3. 达到 500 条的现实路径：`log/messages.jsonl` 去掉自测/占位后约 **605 行**文本（仍含重复与 OCR 噪声，实际可用估计 200–300 条），再接入 2–3 个真实联系人持续采集 2–4 周，配合「显式反馈」按钮（现在 6 条 good 的来源），即可凑到第一批 LoRA 的 500 条；届时再来用本基线对比。
