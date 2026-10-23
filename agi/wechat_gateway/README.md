# 801 微信网关（接收 → 生成 → wxauto 发送）

## 启动

```bash
# 接收/生成用项目 Python 3.14
python -m agi.wechat_gateway.server

# 健康检查
curl http://127.0.0.1:801/health
curl http://127.0.0.1:801/status
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
  "reply_delay": 0.3
}
```

安全默认：

- `dry_run=true`：只生成，不真实发送；
- 只处理白名单；
- 只处理监听开始之后的新好友文本；
- 自己的消息会被过滤，不会回环；
- 真实发送前必须用户显式改 `dry_run=false`。

## 测试

```bash
python -m pytest agi/tests/test_wechat_gateway.py -q
```

已验证：

- sender dry-run / 成功 / 会话不匹配 / ChatInfo 空兜底；
- receiver 好友/白名单/去重/自己消息过滤；
- 真实 wxauto4 发到文件传输助手成功；
- 801 dry-run 模拟来信 → 生成 → planned。

## 尚未做

- 真实好友来信的端到端实机测试（需要你选一个白名单联系人并让 TA 发消息）；
- 自动回复真实联系人前的二次确认/免打扰/频率策略；
- 801 的鉴权（当前只绑定 127.0.0.1）。

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

`config.json` 现在支持：

```text
stop: true                 一键急停
quiet_hours: [0,7]         夜间静默
rate_per_minute: 3         每分钟上限
daily_limit: 200           每日上限
```

超限记录 `send_blocked`，不会调用 wxauto。默认 quiet_hours 覆盖凌晨；需要夜间测试时显式改配置。
