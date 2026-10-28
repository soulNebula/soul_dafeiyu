# sources/wx_box/ · 代码与文档说明

> 来源：`C:\Users\Administrator\Desktop\SoulAgent`（2026-10-24）
> 定位：**这是本项目真正的上游实现**——`agi/selflearn/` 已经实现了 00–14 的 MVP（605 tests 全绿），微信主链真实跑过。
> 本项目（AGI 目录）里的 00–14 文档，正是从 `wx_box/agi/` 复制出来的规格。

---

## 一、代码地图（按 00–14 分组）

> 行数为导入副本的实测值。**这些代码不能直接跑**（缺 data/config/包路径），用途是阅读、对照、移植。

### 1. 对话主链（03 为主）

| 模块 | 行 | 作用 | 我们怎么用 |
|---|---:|---|---|
| `chat.py` | 497 | `SelfLearnAgent`：收消息 → 记忆 → prompt → 生成 → 校验 → 落库；**`_prompt()` 在 411 行** | **内状态注入点**（18 §六） |
| `generation.py` | 75 | 两段式生成：先列有证据的 claims，再写回复 | 保留原样 |
| `context.py` | 28 | 5200 token 预算裁剪 | 保留 |
| `guard.py` | 97 | 绝对化/工具声称/长度校验，L0–L2 降级 | 保留 |
| `citation.py` | 120 | `[M-]` 引用解析与校验 | 保留 |
| `nli.py` | 59 | 蕴含校验（高风险时） | 保留 |
| `llm.py` | 245 | DeepSeek 客户端、reasoning token 处理 | 接小模型时做路由 |
| `db.py` | 242 | SQLite schema/连接 | 新表加这里或独立库 |
| `web.py` | 241 | 网页 8082：聊天 + good/ok/bad 反馈 + 成长报告 | 反馈 UI 的起点 |
| `cli.py` | 364 | 命令行全功能入口 | 运维/调试 |
| `agent.py` / `tools.py` / `tool_reply.py` / `grounding.py` | 314/410/242/84 | 多步工具循环、工具结果回填、落地校验 | 暂不动 |

### 2. 记忆（01/02/03）

| 模块 | 行 | 作用 |
|---|---:|---|
| `claims.py` | 429 | S6 claim 抽取（六类 + evidence_span 硬校验） |
| `gates.py` | 598 | S7 七道闸门（原文匹配/来源分级/敏感/去重/冲突/pending） |
| `search.py` | 402 | S8 FTS5 + 中文 bigram + 混合检索 + MMR |
| `retrieval.py` | 67 | 检索结果组装、时间/来源权重 |
| `memory.py` | 426 | 记忆写入/读取/scope/冲突 |
| `ingest.py` | 481 | S0/S1/S2 接入/清洗/切段 |
| `claim_ingest.py` | 97 | 聊天 → claim 的接入 |
| `backfill.py` | 48 | 历史回填 claim/persona |
| `profile.py` | 712 | S9 十维画像 + 证据 + 渲染 900 字 |
| `image_understanding.py` | 323 | S3 图片结构化 |
| `emoji.py` / `emoji_profile.py` | 357/75 | S5 表情包档案 |
| `quality.py` | 84 | claim 证据率 / pending / 画像证据数 |
| `summary.py` | 83 | 阶段摘要 |
| `pii.py` | 67 | PII 脱敏 |
| `media.py` | 57 | 附件落盘 |

### 3. 情绪 / 信任 / 人格（09/10）

| 模块 | 行 | 作用 |
|---|---:|---|
| **`state.py`** | 121 | 09 状态：`emotion(valence/arousal)`、`boundary(level/crisis)`、`relationship(stage/closeness)`、**`self(mood/energy/sharing/battery)`**；30min 半衰期、7 天回基线 |
| `affect.py` | 496 | 情绪更新、行为影响（低 mood 少回/少气泡） |
| `trust.py` | 133 | 10 信任账本：正负事件、五阶段、repair、G1–G4 |
| `personality.py` / `persona_layers.py` | 166/113 | 自画像/人格三层 |
| `style_profile.py` | 75 | 真实样本风格（短句/口语），只提节奏不存原文 |

### 4. 学习（04/06/07/13）

| 模块 | 行 | 作用 |
|---|---:|---|
| `scoring.py` | 585 | 04：60 情境桶 + 七维加权 + sigmoid + 同桶 baseline |
| `proposals.py` / `_bridge` / `_auto` | 326/48/46 | 提案状态机 draft→gray→merged/rejected |
| `weak_validation.py` / `blind.py` / `drift.py` / `evaluation.py` | 418/91/53/61 | 13：Wilson 下限、盲评、漂移、跨时间复测 |
| `errors.py` / `error_three.py` | 119/237 | 07：错误账本、三次法则 case→rule |
| `skilltree.py` / `skills.py` | 223/233 | 06：技能树 L0–L5、闸门、变体 |
| `learn_loop.py` / `research.py` / `verifier.py` | 766/393/413 | 06：八步学习环、抓正文、verifier |
| `learn_flow/plan/practice.py` | 61/52/60 | Learn Loop 的编排/计划/练习 |

### 5. 主动与调度（11/12/14）

| 模块 | 行 | 作用 |
|---|---:|---|
| `sharing.py` / `share_store.py` | 398/68 | 11 分享欲驱动、候选池 |
| `initiative.py` | 386 | 12 主动欲望评分 |
| `proactive.py` | 312 | 主动候选（**有变量遮蔽 P0**） |
| `wake.py` / `scheduler.py` / `ticker.py` / `autonomy.py` | 376/59/39/82 | 14 唤醒预算/到期/心跳/自主开关 |
| `goals.py` / `decision.py` | 90/44 | 目标树、决策 |

### 6. 网关 `wechat_gateway/`（真实发送，801）

`receiver.py`（收）· `sender.py`（发）· `policy.py`（静默/限频/敏感拦截）· `drafts.py`（草稿队列）· `proactive_worker.py` · `control.py` · `server.py` · `win_send.py`（wxauto 适配）· `send_cli.py`

> 真实运行过：日志 `inbound 49 / outbound 50（sent 38、failed 6、blocked 5、draft 30）`。**改主链时不要动它。**

### 7. 采集 `wx_core/`

`db_source.py`（数据库只读直读）· `wcdb.py` / `wechat_keys.py`（密钥）· `dat_decrypt.py`（图片解密）· `ocr.py`（RapidOCR/Windows OCR）· `extract.py`（OCR 切分）· `emoticon.py`（表情包/wxgf）· `models.py`（Message）· `store.py` / `textlog.py`（归档）· `monitor.py` / `friends.py` / `layout.py` / `win.py` / `clipboard.py`（窗口/通讯录/剪贴板）· `media_cache.py` / `media_export.py` · `safe_fetch.py` / `web_search.py` · `api.py`（Backend 协议）

### 8. `tools/`

`search_messages.py`（全库检索）· `export_emoticons.py`（表情导出）· `audit_compliance.py`（合规静态扫描）· `harden_acl.py`

---

## 二、文档地图（`docs/`，20 份）

| 文件 | 一句话 | 优先读 |
|---|---|---|
| `README_wx_box.md` | 采集层总说明：数据库直读/OCR/媒体/网页/API | ★★ |
| `SPEC_wx_box.md` | 采集层规格 | ★ |
| `混合路线重构_方案与可行性评估_20261024.md` | **对本路线的实测评估 + 6 周排期 + 13 风险** | ★★★ |
| `00-14_完成度审计_20261024.md` | 357 项逐条审计（✅45/⚠️181/❌99/📊30） | ★★★ |
| `实现缺口清单.md` | 按 00–14 逐章的缺口与批次进展 | ★★★ |
| `最终状态_20261024.md` | 605 tests / 801 验收 9/11 / 未完成 10 项 | ★★ |
| `真实数据接入与学习流程.md` | 真实数据怎么进管线 | ★★ |
| `selflearn_README.md` | selflearn 模块/CLI/验收/成长报告 | ★★★ |
| `wechat_gateway_README.md` | 801 网关的配置、发送策略、切回 | ★★ |
| `webui_CONTRACT.md` | 网页端 API 契约 | ★ |
| `执行覆盖审计.md` / `_v2.md` | 执行覆盖度 | ★ |
| `BATCH1–5_CONTRACT.md` | 五个批次的施工契约（claims/gates、learn_loop、ingest/profile、scoring/emoji、affect/initiative） | ★★ |
| `docs_风控与合规说明.md` | 合规底线 | ★★ |
| `docs_微信接入与自主能力_路线图.md` | 发送能力路线 | ★ |
| `docs_主动试跑复盘_20261024.md` | 主动行为试跑复盘 | ★ |

## 三、评测证据（`recon/`，11 份）

`memory_eval_report.md` / `memory_eval_round2_report.md` / `memory_eval_notes.md`（记忆评测）· `blind_round2–5_report.md`（盲评）· `acceptance_after_memory.md` · `selflearn_growth_demo.md` / `selflearn_growth_round10.md`（成长报告样例）· `wxauto_review_after_fix.md`（发送复核）

---

## 四、必须先知道的四个事实

1. **两个 P0**：危机分支 `UnboundLocalError`（危机用户收不到回复）；`proactive.py` 变量遮蔽（三级分享恒失效）。
2. **数据极少**：联系人库 90 消息 / 40 轮 / 6 条反馈（**全部 good**）；网页库 135 / 66 / 0 反馈；`log/messages.jsonl` 2066 行**从未进管线**。
3. **`agi/hybrid/` 不存在**：混合路线只写了方案，没开工。
4. **全仓没有 `PLAN.md`**：00–14 开头那句「实施以 PLAN.md 为准」一直悬空。

---

## 五、建议先读的 10 个模块

`chat.py` → `state.py` → `scoring.py` → `gates.py` → `claims.py` → `search.py` → `profile.py` → `wake.py` → `weak_validation.py` → `web.py`

（顺序 = 从主链到外围，读完就能判断「内状态该接在哪、用什么数据、怎么验」。）

---

## 六、使用注意

- 代码用相对导入（`from . import db`），**离开 `agi.selflearn` 包就跑不起来**；这里只是参考副本。
- 原项目 Python 3.14（应用）+ 3.12（训练），Windows；torch/Ollama **未安装**。
- 上游有 `.venv` 和 `data/`，**没有导入**；要跑测试得回 `wx_box` 原目录。
- 与本项目的关系：**AGI = 规格 + 决策 + 导入参考；wx_box = 可运行实体**（见 19 §九）。
