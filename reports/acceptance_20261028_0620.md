# 验收测试报告 · acceptance_20261028_0620

> 总体结论：**PASS**  ·  PASS 20 / FAIL 0 / SKIP 3  ·  总耗时 19s  ·  生成时间 2026-10-28 06:21:16

## 环境

| 项 | 值 |
|---|---|
| 时间 | 2026-10-28 06:21:16 |
| 模式 | --fast（跳过全量 pytest） |
| 主机/Python | WIN-20251030KZR / 3.14.7 |
| idea venv | C:\Users\Administrator\Desktop\idea\.venv\Scripts\python.exe |
| SoulAgent | C:\Users\Administrator\Desktop\SoulAgent |
| SoulAgent Python | C:\Python314\python.exe |
| 服务端口 | 801=UP，802=UP，803=UP，8123=UP，8125=UP |
| 管理令牌 | ***（默认 8888） |
| 工作台/网关/控制台 | http://127.0.0.1:803 / http://127.0.0.1:801 / http://127.0.0.1:802 |

## 汇总表

| 组 | 检查项 | 状态 | 耗时(s) | 详情 |
|---|---|---|---|---|
| 自动化测试 | idea pytest | **SKIP** | 0.00 | --fast：跳过全量 pytest |
| 自动化测试 | SoulAgent pytest (agi/tests) | **SKIP** | 0.00 | --fast：跳过全量 pytest |
| 自动化测试 | SoulAgent pytest (tests) | **SKIP** | 0.00 | --fast：跳过全量 pytest |
| 全服务冒烟 | smoke_all.py | **PASS** | 1.34 | 16 PASS / 0 FAIL |
| 功能 E2E | 工作台 /api/wb/train/datasets | **PASS** | 0.03 | HTTP 200 {"ok": true, "items": [{"path": "C:\\Users\\Administrator\\Desktop\\SoulAgent\\a |
| 功能 E2E | 工作台 /api/wb/models/running | **PASS** | 0.05 | HTTP 200 {"ok": true, "items": [{"port": 8123, "pid": 26724, "running": true, "model_path |
| 功能 E2E | 工作台 /api/wb/models/disk | **PASS** | 0.02 | HTTP 200 {"ok": true, "items": [{"kind": "models", "dir": "E:\\agent\\models", "size": 13 |
| 功能 E2E | 工作台 /api/wb/chat/persona | **PASS** | 0.01 | HTTP 200 {"ok": true, "items": [{"name": "xiaoyu", "path": "C:\\Users\\Administrator\\Des |
| 功能 E2E | 工作台 /api/wb/chat/traces | **PASS** | 0.13 | HTTP 200 {"ok": true, "db": "C:\\Users\\Administrator\\Desktop\\SoulAgent\\agi\\data\\sel |
| 功能 E2E | 工作台 /api/wb/moments | **PASS** | 0.00 | HTTP 200 {"ok": true, "count": 1, "total": 1, "items": [{"id": "15009948642808435271", "t |
| 功能 E2E | 工作台 /api/wb/models/running（只读） | **PASS** | 0.05 | HTTP 200 {"ok": true, "items": [{"port": 8123, "pid": 26724, "running": true, "model_path |
| 功能 E2E | 网关 /health | **PASS** | 0.00 | HTTP 200 {"ok": true, "service": "wechat_gateway", "admin_token_weak": false, "admin_toke |
| 功能 E2E | 网关 /status（X-Admin-Token） | **PASS** | 0.00 | HTTP 200 {"ok": true, "service": "wechat_gateway", "port": 801, "started_at": 1793139624. |
| 功能 E2E | 对话试跑 POST /api/wb/chat/test | **PASS** | 2.63 | HTTP 200 dry_run=True latency=2628.7ms reply=（试跑）听到了，今天想聊点什么？ |
| 功能 E2E | 朋友圈刷新 POST /api/wb/moments/refresh | **PASS** | 1.01 | HTTP 200 |
| 功能 E2E | 控制台 /api/health | **PASS** | 0.00 | HTTP 200 {"ok": true, "service": "soul-console", "ts": 1793139662.8677993, "contact_db":  |
| UI 冒烟 | 截图 view=welcome | **PASS** | 2.18 | 93KB |
| UI 冒烟 | 截图 view=train | **PASS** | 2.10 | 100KB |
| UI 冒烟 | 截图 view=models | **PASS** | 2.05 | 84KB |
| UI 冒烟 | 截图 view=chat | **PASS** | 2.82 | 60KB |
| UI 冒烟 | 截图 view=moments | **PASS** | 1.84 | 45KB |
| UI 冒烟 | 截图 view=drafts | **PASS** | 2.24 | 93KB |
| UI 冒烟 | index.html 可访问 | **PASS** | 0.00 | 6/6 张截图 |

## 分组耗时

| 组 | PASS | FAIL | SKIP | 耗时(s) |
|---|---|---|---|---|
| 自动化测试 | 0 | 0 | 3 | 0.0 |
| 全服务冒烟 | 1 | 0 | 0 | 1.3 |
| 功能 E2E | 12 | 0 | 0 | 3.9 |
| UI 冒烟 | 7 | 0 | 0 | 13.2 |

## 失败详情

无 FAIL 项。✅

## 截图清单

| view | 文件 | 大小 | 状态 |
|---|---|---|---|
| welcome | `reports\screens\acceptance_20261028_0620\welcome.png` | 93KB | OK |
| train | `reports\screens\acceptance_20261028_0620\train.png` | 100KB | OK |
| models | `reports\screens\acceptance_20261028_0620\models.png` | 84KB | OK |
| chat | `reports\screens\acceptance_20261028_0620\chat.png` | 60KB | OK |
| moments | `reports\screens\acceptance_20261028_0620\moments.png` | 45KB | OK |
| drafts | `reports\screens\acceptance_20261028_0620\drafts.png` | 93KB | OK |

## 已知限制（由 Lead 在最终验收时填写）

> 本小节留白，供 Lead 补充已知限制、豁免说明与上线决策；验收脚本不自动填写。

- （待填）……

## 复现

```bash
C:\Users\Administrator\Desktop\idea\.venv\Scripts\python.exe scripts/acceptance.py --fast
```

> 报告由 `scripts/acceptance.py` 自动生成；完整 data 见同目录同名 md，截图在 `reports/screens/acceptance_20261028_0620/`。
