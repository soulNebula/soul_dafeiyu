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
| 记忆 | `agi/selflearn/memory.metrics()` + `user_memories` 最近 50 条 + `/api/tone`（AI 语气 vs `agi/eval_data/human_tone/profile.md` 真人基线） |
| 提示词 | `agi/data/prompts.json`（内置 `chat_system` 预览；编辑后保存，下一条消息生效；P4-b 的 prompt_store 接入后由后端接管） |
| 模型 | `agi/selflearn/model_registry`（list/health/start/stop；`soul-05b` 可启停，`qwen15` reserved 拒绝；含 inner mode 开关，写回网关 config 热加载） |
| 设置 | `/api/config`（主动开关/概率/间隔/静默时段）+ STOP/恢复 |

## 接口

只读（GET，Host 回环校验）：
`/api/health` `/api/status` `/api/feed?limit=&kind=` `/api/inner?limit=` `/api/messages?limit=`
`/api/memory` `/api/tone?limit=` `/api/models` `/api/config` `/api/prompts`

写（POST，`X-Admin-Token`，全部写 `agi/data/console_audit.log`）：

| 接口 | body | 说明 |
| --- | --- | --- |
| `/api/proactive/stop` | `{}` | 创建 `agi/data/wechat/STOP`（立即停发） |
| `/api/proactive/resume` | `{}` | 删除 STOP（恢复） |
| `/api/config` | `{"updates": {...}}` | 白名单：`proactive_enabled`、`quiet_hours`、`proactive_interval_min_s/max_s`、`proactive_probability_min/max`、`inner_mode`、`inner_shadow_only`、`inner_mode_scope`、`inner_schema`；保存即返回新配置（网关热加载，无需重启） |
| `/api/models/start` `/api/models/stop` | `{"name":"soul-05b"}` | 只允许 `soul-05b`；qwen15 reserved / 未知模型拒绝 |
| `/api/prompts` | `{"prompts":{key:text}}` | 合并写入 `prompts.json`；key 限 `[a-zA-Z0-9_.-]{1,64}`、单值 ≤20000 字 |

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

## 说明与限制

- 对话流只读 `gateway.log` 尾部，不会触发任何生成/发送；日志轮转/超大文件按块 tail。
- 记忆指标读取会走 `memory.metrics()`（与 P1-g 同口径）；该函数内部用 `db.connect()`，首次可能在旧库做幂等 schema 迁移，之后只读。
- 提示词编辑当前直接写 JSON 文件（P4-b 后端 `prompt_store` 落地后应改为调后端）。
- STOP/配置改动是**立即生效**的（网关热加载）；前端只是改文件/配置，不重启网关。
- 没有健康检查之外的自动操作；不会自动发消息、不会调用 LLM。
