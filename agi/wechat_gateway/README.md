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

## 发送硬闸门

`config.json` 支持：

```text
stop: true                 一键急停（改后重启生效）
quiet_hours: [0,7]         夜间静默
rate_per_minute: 3         每分钟上限
daily_limit: 200           每日上限
```

超限记录 `send_blocked`，不会调用 wxauto。默认 quiet_hours 覆盖凌晨；需要夜间测试时显式改配置。

## 发送控制面

```text
send_mode: auto | confirm | off    # 全局默认（缺省 confirm）
contact_modes: {"好友名": "auto"}   # 按联系人覆盖
admin_token: ""                    # 为空则启动自动生成；所有管理接口都要它
complaint_stop: true               # 收到投诉/举报/机器人识别 → 立即 stop
```

## 尚未做

- 在线急停（改 `stop` 需要重启；目标 <1s 的 HTTP 开关还没做）；
- 草稿的列表界面（目前只有 JSON 接口）；
- 发送频率的按小时窗口（现在只有每分钟/每日两个窗口）。
