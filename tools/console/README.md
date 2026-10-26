# Soul Console（P4-a 本地调试前端）

给真机调试用的**本地单页控制台**：看网关在发生什么、内状态注入效果、记忆指标、切模型/主动消息。
不改旧 `agi/selflearn/web.py`，前端纯 HTML/CSS/JS（无 npm / 无构建 / 无 CDN，离线可用）。

## 启动

```bash
# 仓库根目录
python tools/console/server.py
# → Soul Console: http://127.0.0.1:802/
```

可选参数：

```bash
python tools/console/server.py --port 802 --contact-db agi/data/selflearn/wechat_contacts/4260fd457d8355.sqlite3
```

- 服务只绑 `127.0.0.1`，并校验 `Host` 必须回环（防 DNS rebinding）。
- 默认自动选「最近有消息的联系人库」；`--contact-db` 可显式指定。
- 需要网关在 `127.0.0.1:801` 上跑着，总览/设置里的实时状态才有数据（网关没起时页面显示错误原因，本机文件部分照常显示）。

## admin token

写操作（STOP/恢复、改配置、模型启停、保存提示词）需要 `X-Admin-Token`，与网关
`agi/data/wechat/config.json` 的 `admin_token` 一致。在页面右上角 token 输入框填一次即可
（只存 `sessionStorage`，不写盘、不下发别的页面）。

```bash
TOKEN=$(python -c "import json;print(json.load(open('agi/data/wechat/config.json',encoding='utf-8'))['admin_token'])")
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"updates":{"proactive_enabled":false}}' http://127.0.0.1:802/api/config
```

## 页面与数据来源

| 页面 | 数据来源 |
| --- | --- |
| 总览 | `/api/status`（网关 801 代理）+ `/api/feed` + `/api/config` + `/api/models` |
| 对话流 | `agi/data/wechat/gateway.log`（JSONL 事件，只读 tail，3s 自动刷新） |
| 内状态 | 联系人库 `inner_states`（只读 URI 连接；mode/injected/state_json/block/延迟/fallback） |
| 对话测试 | 隔离库 `agi/data/selflearn/console_test.sqlite3` → 进程内 `SelfLearnAgent.send()`，可试提示词/模型效果，不影响真实联系人 |
| 媒体 | `agi/wechat_gateway/media.capabilities()`（import 失败降级静态矩阵）+ `emoji_archive`/目录扫描 + `agi/data/media_wishes.jsonl`；测试发送只允许文件传输助手 |
| 内状态（心情卡片） | `/api/mood`（`mood_judge.snapshot`）：心情值/7 情绪/注入预览/时间线；`/api/mood/inject`、`/api/mood/enabled` 只写 mood.json 白名单键 |
| 记忆 | `agi/selflearn/memory.metrics()` + `user_memories` 最近 50 条 + `/api/tone`（AI 语气 vs `agi/eval_data/human_tone/profile.md` 真人基线） |
| 提示词 | `agi/selflearn/prompt_store`（11 个内置键的 defaults/overrides/effective；降级时直读 `agi/data/prompts.json`）。左列选键 → 右侧大文本框编辑 → 保存/恢复默认/全部恢复默认 |
| 模型配置 | 网关 `config.json` 的白名单键（llm_model/llm_temperature/llm_max_tokens/soul_server_url/soul_timeout_s/inner_schema）+ DeepSeek key 仅显示已配置/未配置 |
| 统计 | `/api/stats?days=&contact=` `/api/mood?contact=&limit=`：发送/成功率/丢弃/重生成/内状态/延迟/媒体；CSS 条形图、最近被拦表 |
| 模型 | `agi/selflearn/model_registry`（list/health/start/stop；`soul-05b` 可启停，`qwen15` reserved 拒绝；含 inner mode 开关，写回网关 config 热加载） |
| 数据管理 | `/api/data/*`：体量/备份/导出/导入（写操作 token+审计）；按记忆库/人格库分栏，含 legacy 旧库 |
| 记忆编辑 | `/api/memory/*`：列表/搜索/claims/行内改重要性/标记/软删除/合并（token+审计） |
| 日志 | `gateway.log`(JSONL)/`inner.log`/`prompt_override.log`/regen_failed；搜索+时间倒序+错误标红 |
| 设置 | `/api/config`（主动开关/概率/间隔/静默时段）+ STOP/恢复 |

## 接口

只读（GET，Host 回环校验）：
`/api/health` `/api/status` `/api/feed?limit=&kind=` `/api/inner?limit=` `/api/messages?limit=`
`/api/memory` `/api/tone?limit=` `/api/models` `/api/config` `/api/prompts`
`/api/model-config` `/api/chat-test/history?limit=` `/api/logs?source=&limit=&q=&kind=` `/api/stats?days=&contact=`
`/api/data/info` `/api/data/export` `/api/data/export-memories` `/api/data/backup/download`
`/api/memory/list` `/api/memory/claims`
`/api/media/capabilities` `/api/media/library?limit=` `/api/media/assets?limit=&kind=` `/api/media/stats` `/api/media/wishes` `/api/media/acquisition` `/api/media/file?path=`

写（POST，`X-Admin-Token`，全部写 `agi/data/console_audit.log`）：

| 接口 | body | 说明 |
| --- | --- | --- |
| `/api/proactive/stop` | `{}` | 创建 `agi/data/wechat/STOP`（立即停发） |
| `/api/proactive/resume` | `{}` | 删除 STOP（恢复） |
| `/api/config` | `{"updates": {...}}` | 白名单：`proactive_enabled`、`quiet_hours`、`proactive_interval_min_s/max_s`、`proactive_probability_min/max`、`inner_mode`、`inner_shadow_only`、`inner_mode_scope`、`inner_schema`；保存即返回新配置（网关热加载，无需重启） |
| `/api/models/start` `/api/models/stop` | `{"name":"soul-05b"}` | 只允许 `soul-05b`；qwen15 reserved / 未知模型拒绝 |
| `/api/prompts` | `{"prompts":{key:text}}` | 优先 `prompt_store.set_many`（11 键白名单、单条 ≤4000 字、原子写）；import 失败降级直写 `prompts.json` |
| `/api/prompts/reset` | `{"keys":[...]}` 或 `{"all":true}` | 恢复默认（`prompt_store.delete/delete_all`，降级时删 JSON 键/文件） |
| `/api/chat-test` | `{"text":"..."}` 或 `{"reset":true}` | 隔离库对话测试（60s 超时；reset 删除 console_test.sqlite3）；不会发微信 |
| `/api/model-config` | `{"updates":{...}}` | 保存模型/内状态白名单键（DeepSeek key 不回传明文） |
| `/api/mood/inject` | `{inject: bool}` | 写 mood.json 的 `inject`（原子写/保留其它键/审计） |
| `/api/mood/enabled` | `{enabled: bool}` | 写 mood.json 的 `judge_enabled`（审计；注意等 P9-b 后端切换后再开） |
| `/api/data/backup` | `{}` | 打包运行数据到 `E:gentackups\manual_<ts>.zip`（不改数据） |
| `/api/data/import` | `{zip_path, confirm:"导入备份", mode, overwrite_confirm?}` | 只允许备份目录 zip；merge 不覆盖，overwrite 需双短语 |
| `/api/data/backup/delete` | `{name, confirm:"删除备份"}` | 删除备份 zip |
| `/api/memory/update` `/delete` `/mark` `/merge` | `{id,...}` | 记忆编辑；delete=软删除（status=invalid），需 `确认:"删除记忆"` |
| `/api/media/send`（别名 `/api/media/test-send`） | `{"type":"text|emoji_text|image|gif|sticker|native_emoji","text?":"...","path?":"...","name?":"..."}` | 测试发送；**联系人强制覆盖为「文件传输助手」**；网关暂无媒体接口时返回 501（等 P5-a） |

## 界面结构（AstrBot 风格深色）

```
┌──────────────┬──────────────────────────────────────────────────────────┐
│ Soul Console │ 总览   [gateway 在线] [主动消息开] [inner: model/shadow]  │ ← 顶部 56px
│ ◧ 总览       ├──────────────────────────────────────────────────────────┤
│ ☰ 对话流     │  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐        │
│ ◈ 内状态     │  │ 主动消息 │ │ 今日发送 │ │ 后台监听 │ │ 内状态  │        │ ← 卡片主区
│ ▤ 记忆       │  └─────────┘ └─────────┘ └─────────┘ └─────────┘        │
│ ✎ 提示词     │  ┌───────────────────────┐ ┌───────────────────────┐    │
│ ▣ 模型       │  │ 最近事件（可点开 JSON）│ │ 最近对话气泡           │    │
│ ⚙ 设置       │  └───────────────────────┘ └───────────────────────┘    │
│              │                                                          │
│ (240px 侧栏) │  对话流：inbound 灰 / outbound 绿 / proactive 蓝 / 拦截红  │
└──────────────┴──────────────────────────────────────────────────────────┘
```

- palette：bg `#1a1a1a`、surface `#242424`、primary `#5ba4d4`、success `#52c41a`、
  warning `#faad14`、error `#ff4d4f`、text `#e8eaed`、border `#333`、气泡 `#2d2e30`。
- 侧边栏 240px + 顶部 header 56px + 圆角 10px 卡片；JSON 用等宽字体。
- 所有页面右上角显示「刷新于 HH:MM:SS」；接口失败显示红色原因而不是空白。

## 心情卡片（P9-c）

内状态页顶部：大字心情值 + 中文 label、7 情绪条形（happy/calm/sweet 绿、heartache/worry/sad/jealous 红）、
判定开关/注入开关（点击即写 `agi/data/selflearn/mood.json` 对应键）、下一轮注入预览（`injected_preview`）、
最近 10 条时间线（时间/mood/主要变化/reason/confidence/mode）。
接口：`GET /api/mood?contact=&limit=`（token；调 `mood_judge.snapshot()`，只读）、
`POST /api/mood/inject` / `/api/mood/enabled`（token；只写白名单两键，原子写并审计）。
`mood.json` 当前 `judge_enabled=false`（等 P9-b 后端切换），页面会有确认提示，不会自动打开。

## 统计（P8-a）

- `GET /api/stats?days=7&contact=<alias|stem>`（admin token）：扫描 gateway.log 尾部最近 ≤200k 行（懒加载，
  不整文件读入内存）+ 只读记忆库（`*.memory.sqlite3`，兼容未迁移单库/legacy）：
  发送量按天/按小时、成功率（sent/(sent+failed+blocked)）、拦截/丢弃/重生成原因分组、主动消息
  schedule/skip/平均间隔、内状态 count/stance/mode/延迟 p50·p95·max、对话消息统计、媒体按类型成功/失败、
  事件延迟 p50/p95、最近 20 条被拦/丢弃、`window` 与 `errors`（文件缺失时 ok:true + 空结构）
- 前端「统计」页：1/7/30 天切换、联系人下拉；卡片（今日发送/成功率/丢弃率/平均间隔/内状态 P95）；
  CSS 横向条形图（按天、24h、丢弃/被拦/重生成 Top5、内状态 stance）；最近被拦/丢弃表；空数据友好提示。
  `contact` 默认跟随数据页的联系人配置（全部联系人 = 空）。

## 数据管理 / 记忆编辑（P7-c）

- **数据管理**：`/api/data/info` 展示 selflearn 体积、联系人库、消息/记忆/claim 总数、日志行数、媒体文件数、
  备份列表；按 **记忆库 `*.memory.sqlite3` / 人格库 `*.persona.sqlite3`** 分两栏（含各联系人路径、大小、
  消息/记忆行数 vs persona 行数），并标出迁移前的 `*.legacy.sqlite3`（可删除/可回滚，不计入统计）；
  未迁移的单库 `.sqlite3` 也兼容显示。
  - 备份：`POST /api/data/backup` → `E:gentackups\manual_<ts>.zip`，整目录打包（自然包含两种库），不改数据
  - 导出：`GET /api/data/export?contact=&format=jsonl|md&limit=` 聊天记录下载流；`/api/data/export-memories` 记忆 JSON
  - 导入：`POST /api/data/import`，仅备份目录 zip，解压临时目录校验结构后复制联系人库；merge 同名跳过，
    overwrite 需 `confirm:"导入备份"` + `overwrite_confirm:"覆盖现有库"`；旧版单库 zip 也接受
  - 备份列表可下载/删除（删除需输入「删除备份」）；审计入口跳「日志→audit」
- **记忆编辑**：搜索/状态筛选/分页表格；行内改 content（≤500）/importance（0–1）/status、标重要、软删除
  （status=invalid，不物理删，需输入「删除记忆」）、选两条合并（保留较早、另一条 invalid、evidence 合并）；
  claims 只读；顶部显示 memory.metrics 摘要。

## 对话测试 / 模型配置 / 日志（P7-b）

- **对话测试**：气泡对话，`POST /api/chat-test` 在**隔离库** `agi/data/selflearn/console_test.sqlite3` 里进程内调
  `SelfLearnAgent.send()`，返回 reply/bubbles/inner/media/latency；右上角 token 必填。
  支持「清空会话」（删隔离库）、历史回读（`GET /api/chat-test/history`）、每条 AI 回复折叠查看内状态/媒体/延迟。
  **不会发微信，也不读写真实联系人库**。
- **模型配置**：读写网关 config 白名单 `llm_model / llm_temperature / llm_max_tokens / soul_server_url /
  soul_timeout_s / inner_schema`，保存即热加载；DeepSeek key 只显示"已配置/未配置"与来源
  （`web_config.json:llm.api_key` 或环境变量 `DEEPSEEK_API_KEY`），**不回传明文**。
- **日志**：`GET /api/logs?source=gateway|inner|prompt|regen&limit=200&q=&kind=`，读 gateway.log(JSONL) /
  inner.log / prompt_override.log / regen_failed 计数；关键词+kind 过滤、时间倒序、错误行红色、点行看整条 JSON。
- 顶部导航新增「对话测试 / 模型配置 / 日志」三项。

## 媒体页（P5-d + P5-e-B 本地导入）

本地导入面板：多选图片（png/jpg/jpeg/gif/webp、≤2MB）→ 预览/名称/标签 → 逐条显示结果
（已导入/重复/被拒原因）；也支持白名单路径导入（Desktop / 素材库 / 仓库 media）。
图片会复制进 `agi/data/selflearn/media/`、自动去重/剥 EXIF 并索引，可作为贴纸（图片形式）发送。
愿望清单显示 pending/fulfilled，pending 可点「导入一张满足它」预填标签，导入成功后自动标记 fulfilled。
接口：`POST /api/media/import`（base64 多文件）、`/api/media/import-path`、`POST /api/media/wish/fulfill`；
联网获取/下载仍为预留（不实现）。


- **能力矩阵**：text / unicode_emoji / image / gif / sticker_as_image / sticker_native / sticker_collect / mixed，
  每项 ✅支持 / ⚠️降级 / 🔒预留 / ❌不可用 + 说明。优先读 `agi/wechat_gateway/media.capabilities()`，
  import 失败自动降级静态矩阵（页面不崩）。
- **素材库**：优先读联系人库 `emoji_archive`（使用次数/标签）；表为空时回退扫描
  `wx_core/emoji_assets` + `media/stickers` + `selflearn/media`（当前 120+ 个文件），网格缩略图
  （经 `/api/media/file` 白名单读取，路径穿越 404）。
- **统计卡**：总数 / 已使用 / 未使用 / 最常用 top5（emoji_usage 计数；扫描回退时无使用记录）。
- **测试发送**：选类型（文本/Unicode emoji/图片/GIF/贴纸）→ 素材或文本 → 发送；
  **联系人强制为「文件传输助手」**（body 里传别人也覆盖，服务端强制），显示 sent/blocked/error。
  P5-a 的 gateway `/api/media/send` 未落地时返回 **501 + 说明**，不会真的发消息。
- **愿望清单**：读 `agi/data/media_wishes.jsonl`（模型想发但库里没有的表情），标注
  「获取管线预留、未实现下载」（P5-e task-63）。

## 提示词页 v2（P4-d）

- 左列 **11 个内置键**（中文标签 + key + `默认`/`已覆盖` 徽标），右列大文本框编辑当前 `effective` 值；
  顶部提示：单条 ≤4000 字、删 `agi/data/prompts.json` 即全部回滚、改完先去「对话流」观察效果。
- 底部按钮：`保存`（`POST /api/prompts`）、`恢复默认`（回滚当前键）、`全部恢复默认`。
- 有覆盖时显示「覆盖前默认值对比」折叠区；保存/回滚后徽标自动刷新，toast 提示
  「已保存，下一条消息 / 下一次 tick 生效」。
- 后端优先 `from agi.selflearn import prompt_store`（P4-b：校验/原子写/mtime 热加载）；
  import 失败自动降级为直接读写 JSON（页面显示降级提示，不崩）。

| 键 | 含义 |
| --- | --- |
| `proactive.persona_constraints` | 人格化边界（数字生活/禁物理起居） |
| `proactive.kind.care` | care 关心 |
| `proactive.kind.share` | share 分享 |
| `proactive.kind.light` | light 轻松 |
| `proactive.kind.followup` | followup 接话 |
| `proactive.kind.silence_ack` | silence_ack 久未联系 |
| `proactive.anti_fabrication` | 不编造共同经历 |
| `proactive.punctuation` | 标点/分句约束 |
| `proactive.continuity` | 连续性约束 |
| `proactive.voice_fewshot` | 真实语气 few-shot |
| `chat.system` | 被动对话 SYSTEM |

恢复方式：单个键点「恢复默认」；全部点「全部恢复默认」（等价删 `agi/data/prompts.json`）。改完建议在「对话流」看下一条输出。

## 恢复出厂设置（P4-c）

设置页最下方有红色「清空状态 / 恢复出厂设置」按钮，点击后是 **4 步挽留**：

1. **真实统计**：会聊过多少条消息 / 多少条记忆 / 多少条 claim / 多少个联系人（`/api/factory/preview` 实读），
   "真的要走吗？"
2. **后果**：人格、记忆、信任度清零，TA 会完全不认识你，像第一次见面。
3. **不可撤销 + 先备份**：会先把要清的内容打包到 `E:\agent\backups\factory_reset_<时间>.zip`，
   备份失败就中止（绝不先删）。
4. **最终确认**：必须手动输入 `清空我和TA的回忆` 才解锁红色按钮；输入不一致按钮保持禁用。

任意一步都可以取消（取消按钮 / ESC / 点遮罩），默认焦点在「取消」；取消后提示"已取消，什么都没删"。
执行完页面显示备份路径与删除统计，并提示"建议重启网关让内存状态一起归零"（控制台不会替你杀进程）。

**清空范围**（删除前会校验路径白名单）：

- `agi/data/selflearn/` 整个目录内容（所有联系人库、默认库、inner.log、media 等）
- `agi/data/wechat/gateway.log`、`drafts.jsonl`
- `agi/data/prompts.json`
- `log/messages.jsonl`、`log/聊天记录.txt`

**保留**：模型权重/GGUF（E:\agent）、`agi/eval_data/`、`agi/data/wechat/config.json`、
`agi/data/local_models.json`、代码本身。

接口（reset 需 `X-Admin-Token`；preview 只读不需要）：

```bash
# 只读预览（不会删任何东西）
curl -s -X POST http://127.0.0.1:802/api/factory/preview | python -m json.tool

# 真正执行：先 zip 备份，备份成功才清理
curl -s -X POST -H "X-Admin-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"confirm":"清空我和TA的回忆","backup":true}' \
     http://127.0.0.1:802/api/factory/reset
```

## 说明与限制

- 对话流只读 `gateway.log` 尾部，不会触发任何生成/发送；日志轮转/超大文件按块 tail。
- 记忆指标读取会走 `memory.metrics()`（与 P1-g 同口径）；该函数内部用 `db.connect()`，首次可能在旧库做幂等 schema 迁移，之后只读。
- 提示词编辑优先走 `prompt_store`；仅在 import 失败时降级直接写 JSON（无 11 键校验/默认值对比）。
- STOP/配置改动是**立即生效**的（网关热加载）；前端只是改文件/配置，不重启网关。
- 没有健康检查之外的自动操作；不会自动发消息、不会调用 LLM。
