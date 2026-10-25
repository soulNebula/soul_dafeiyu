# 801 微信网关（接收 → 生成 → wxauto 发送）

## 启动

```bash
# 接收/生成用项目 Python 3.14
python -m agi.wechat_gateway.server
```

首次启动会**自动生成管理 token** 并写回 `agi/data/wechat/config.json` 的 `admin_token`，
同时打印一次。之后所有 `/api/*` 与 `/status` 都要带 `X-Admin-Token`（或 `?token=`）：

```bash
TOKEN=$(python -c "import json;print(json.load(open('agi/data/wechat/config.json',encoding='utf-8'))['admin_token'])")
curl -H "X-Admin-Token: $TOKEN" http://127.0.0.1:801/status   # /health 是公开的
```

发送使用独立 Python 3.13 venv：

```text
agi/data/wechat/.venv/Scripts/python.exe
```

## 链路

```text
wx_core MonitorBackend 持续监听
  → WeChatReceiver 过滤：好友 + 文本 + 白名单 + 新消息
  → Gateway 队列
  → SelfLearnAgent（每个联系人独立 DB）
  → WxAutoSender（3.13 venv 子进程）
  → wxauto4 SendMsg 发回微信
```

## 配置

`agi/data/wechat/config.json`

```json
{
  "enabled": true,
  "port": 801,
  "auto_reply": true,
  "dry_run": true,
  "monitored_contacts": ["文件传输助手"],
  "allowed_contacts": ["文件传输助手"],
  "max_chars": 500,
  "reply_delay": 0.3,
  "send_mode": "confirm",
  "contact_modes": {"文件传输助手": "auto"},
  "admin_token": ""
}
```

安全默认：

- `dry_run=true`：只生成，不真实发送；
- `send_mode=confirm`（默认）：先落草稿，人工批准才发；某个联系人可在 `contact_modes` 单独升为 `auto`；
- `admin_token` 为空时启动自动生成，**不再"空即放行"**；`Host` 必须是回环地址 + 实际端口；
- 只处理白名单；只处理监听开始之后的新好友文本；自己的消息会被过滤，不会回环；
- 真实发送前必须用户显式改 `dry_run=false`。

## 鉴权与草稿接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 公开：只返回服务存活 |
| GET | `/status` | 需 token：运行态、策略、待发草稿数 |
| GET | `/api/drafts` | 需 token：列出待发草稿 |
| POST | `/api/draft/approve` | 需 token：`{"id": "D-..."}` 批准并真实发送 |
| POST | `/api/draft/reject` | 需 token：`{"id": "D-..."}` 丢弃 |
| POST | `/api/approve` | 需 token：直接发送 `{"contact","text"}`（绕过 confirm） |

发送失败不丢内容：失败的那条会落成 `reason=send_failed` 的草稿，等你决定重发还是丢弃。
草稿存在 `agi/data/wechat/drafts.jsonl`（一行一条 JSON，重启不丢）。
请求体上限 1 MB；鉴权用 `hmac.compare_digest` 定长比较。

## 测试

```bash
python -m pytest agi/tests/test_wechat_gateway.py agi/tests/test_wechat_control.py \
                 agi/tests/test_wechat_drafts.py agi/tests/test_wechat_draft_flow.py -q
```

已验证：

- sender dry-run / 成功 / 会话不匹配 / ChatInfo 空兜底；
- receiver 好友/白名单/去重/自己消息过滤；
- 真实 wxauto4 发到文件传输助手成功；
- 801 dry-run 模拟来信 → 生成 → planned；
- confirm 只落草稿、批准才发、失败自动归档、token 缺失/错 token/伪造 Host 全部被拒。

## 微信内显式反馈

在微信里直接发：

```text
反馈 好
评价：一般
feedback bad
```

801 会把这条记到该联系人上一条 AI 回复的 `feedback`，不再当普通聊天生成回复。
仅认显式前缀，普通“好/一般”不会误判。

## 发送硬闸门（M1）

`config.json`：

```text
stop: true                 一键急停（改后重启生效）
quiet_hours: [0,7]         夜间静默
min_interval_s: 1.5        最小间隔（只约束主动发送；被动回复/多气泡不受限）
rate_per_minute: 3         每分钟上限
hourly_limit: 40           每小时上限
daily_limit: 200           每日上限
max_chars: 500             单条长度上限（超长直接拒发）
```

内容闸门（`policy.check_content`）会在发送前拦掉：空文本、超长、本机绝对路径、
疑似凭据赋值（`API key: …` / `password=…`）、`wxid_` 内部 ID、`sk-…`、私钥头。
所有拦截都记 `send_blocked` + 原因，不会调用 wxauto。

## STOP 开关与测试隔离（P0-22b）

### 用法

STOP 是"最高优先级"的发送总闸：**文件存在 = 立即停止一切外发（含 reactive/force）**，
删除即恢复，不需要重启进程。

```bash
# 手动急停 / 恢复（生产路径 = 与 config.json 同目录）
touch agi/data/wechat/STOP      # 停
rm -f agi/data/wechat/STOP      # 恢复
```

也可走管理接口（需 token，幂等）：

```bash
curl -X POST -H "X-Admin-Token: $TOKEN" http://127.0.0.1:801/api/proactive/stop
curl -X POST -H "X-Admin-Token: $TOKEN" http://127.0.0.1:801/api/proactive/resume
```

- `/status` 的 `proactive.stopped` 反映当前状态；
- 命中后 `_send` 返回 `{ok:false, status:"blocked", error:"stop_file"}`，并记
  `send_blocked(reason=stop_file)`；`run_cycle` 直接返回
  `{ran:false, reason:"stopped", stopped:true}`（不调 ticker、不发消息）；
- **blocked 不算失败**：不计 `failed`、不参与熔断计数（不会因为 STOP 期误开熔断），
  但会在 `/status` 与日志里可见；日志 `proactive_stopped` 10 分钟节流一次。

### 为什么测试必须隔离

事故（2026-10-25）：运维为保险在 `agi/data/wechat/STOP` 建了停止文件，结果
`test_wechat_draft_flow.py` 4 条全部失败——测试里的 `Gateway` 读的是**生产 STOP 路径**，
被真实文件拦成 `blocked`。**生产文件的存在不应该改变任何测试结果。**

`agi/tests/conftest.py` 的 autouse fixture `_isolate_stop_path` 把 STOP 路径重定向到
`tmp_path/"STOP"`：

- 常规测试：`config.stop_path()` → `tmp_path/"STOP"`，生产 STOP 永远读不到；
- 测试自己把 `CONFIG_PATH` 指到 tmp 时（如 `test_config_hot_reload`），STOP 仍跟随
  `config.json` 同目录，保持"开关与配置同目录"的语义；
- 解析结果落在生产 `config.DATA_DIR` 下一律改写为 tmp 路径；
- 每个测试结束恢复原 `STOP_PATH`，不会串味。

### 代码里的注入点（测试/多实例）

```python
from agi.wechat_gateway import config as gw_config

gw_config.set_stop_path("/tmp/STOP")   # 模块级覆盖（None = 恢复默认）
gw_config.stop_path()                  # 已应用覆盖的路径
gw_config.stopped()                    # 该路径是否存在

Gateway(cfg, stop_path="/tmp/STOP")    # 单实例显式注入（优先级最高）
```

`STOP_PATH` 支持 `Path`/`str`/`callable`（callable 每次调用求值，conftest 用它做动态重定向）。

**验收**：在 `agi/data/wechat/STOP` 真实存在的情况下跑

```bash
python -m pytest agi/tests/test_wechat_draft_flow.py agi/tests/test_interval_random.py \
                 agi/tests/test_config_hot_reload.py agi/tests/test_stop_isolation.py -q
```

应全绿（实测 31 passed）；删掉生产 STOP 后全量 `pytest agi/tests -q` 同样全绿。

## 窗口级兜底（默认关闭）

wxauto 失败时可以用窗口级兜底，但**默认只做"人工协助"**——因为窗口输入无法可靠校验
"当前打开的是谁"，盲发可能发错人：

```json
{
  "fallback_enabled": false,
  "fallback_mode": "manual",
  "fallback_input_box": null
}
```

- `fallback_enabled=false`（默认）：失败只用草稿兜底；
- `fallback_mode="manual"`：把文本放进剪贴板 + 记 `fallback` 审计 + 落草稿，
  由你切到目标聊天手动粘贴；
- `fallback_mode="auto"`：**必须**同时配置 `fallback_input_box: [x, y]`（截图坐标的点），
  且要求微信窗口能切到前台；结果是 `verified: false`，请人工抽查。

## 发送控制面

```text
send_mode: auto | confirm | off    # 全局默认（缺省 confirm）
contact_modes: {"好友名": "auto"}   # 按联系人覆盖
admin_token: ""                    # 为空则启动自动生成；所有管理接口都要它
complaint_stop: true               # 收到投诉/举报/机器人识别 → 立即 stop
```

## 回复合批（「等他把话说完」）

```json
{"reply_debounce_ms": 2000}
```

对方连着发几条文字时，先攒 `reply_debounce_ms` 再一次性回（默认 2s，0=关闭）。
图片/表情/系统消息不攒（直接回）；攒的字数超过 `max_chars` 会立刻回。

## 主动链（M5，默认关闭）

之前主动消息只写进 selflearn 的 messages 表，从不发送；调度还挂在网页的
`setInterval` 上（关页面就停）。现在由网关的守护线程驱动：

```json
{
  "proactive_enabled": false,
  "proactive_interval_s": 900,
  "proactive_contacts": []
}
```

- 默认 **false**：不主动开口；
- 打开后每 15 分钟对 `proactive_contacts`（默认 = `allowed_contacts`）逐个跑一次
  `ticker.run_once`（先过 `autonomy.gate`：静默时段/日预算/冷却）；
- 生成的消息走 `_send(proactive=True)`：白名单、confirm/auto、静默时段、频率窗口、
  内容闸门全部照常生效；
- 主动消息会补一条 turn，发送成功后登记到 `last_turns`，之后发「反馈 好/差」能落到该轮；
- **空闲闸**：对方 `proactive_idle_s`（默认 600s）内说过话就不主动；
- **负反馈冷却**：对方对上一条主动消息回"别发了/烦/没意思"→ 24h 不再主动；7 天内两次 → 7 天不再主动；
- ⚠️ 建议：开启主动时把该联系人设为 `"contact_modes": {"联系人": "confirm"}`，
  先看草稿再批准，避免对真人"突然开口"。

## 尚未做

- 主动消息若落在草稿（confirm）里，批准后不会补登记 last_turns（只对直接发送的登记）；
- 草稿的列表界面（目前只有 JSON 接口）；
- 发送频率的按小时窗口（现在只有每分钟/每日两个窗口）。
