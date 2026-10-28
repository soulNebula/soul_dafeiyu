# 验收测试报告 · acceptance_20261029_0041

> 总体结论：**PASS**  ·  PASS 19 / FAIL 0 / SKIP 3  ·  总耗时 15s  ·  生成时间 2026-10-29 00:42:07

## 环境

| 项 | 值 |
|---|---|
| 时间 | 2026-10-29 00:42:07 |
| 模式 | --fast（跳过全量 pytest） |
| 主机/Python | WIN-20251030KZR / 3.14.7 |
| idea venv | C:\Python314\python.exe |
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
| 全服务冒烟 | smoke_all.py | **PASS** | 1.24 | 13 PASS / 0 FAIL |
| 功能 E2E | 工作台 /api/wb/overview | **PASS** | 0.28 | HTTP 200 {"ok": true, "soulagent": "C:\\Users\\Administrator\\Desktop\\SoulAgent", "agent |
| 功能 E2E | 工作台 /api/wb/config | **PASS** | 0.00 | HTTP 200 {"ok": true, "groups": ["基础", "模型", "训练"], "items": [{"key": "enabled", "group": |
| 功能 E2E | 工作台 /api/wb/services | **PASS** | 0.12 | HTTP 200 {"ok": true, "items": [{"name": "gateway", "label": "微信网关", "port": 801, "runnin |
| 功能 E2E | 工作台 /api/wb/db/databases | **PASS** | 0.32 | HTTP 200 {"ok": true, "items": [{"id": "C:\\Users\\Administrator\\Desktop\\idea\\.data\\a |
| 功能 E2E | 工作台 /api/wb/train/datasets | **PASS** | 0.02 | HTTP 200 {"ok": true, "items": [{"path": "C:\\Users\\Administrator\\Desktop\\SoulAgent\\a |
| 功能 E2E | 工作台 /api/wb/models/running | **PASS** | 0.06 | HTTP 200 {"ok": true, "items": [{"port": 8123, "pid": 5392, "running": true, "model_path" |
| 功能 E2E | 工作台 /api/wb/models/disk | **PASS** | 0.03 | HTTP 200 {"ok": true, "items": [{"kind": "models", "dir": "E:\\agent\\models", "size": 13 |
| 功能 E2E | 网关 /health | **PASS** | 0.00 | HTTP 200 {"ok": true, "service": "wechat_gateway", "admin_token_weak": false, "admin_toke |
| 功能 E2E | 网关 /status（X-Admin-Token） | **PASS** | 0.00 | HTTP 200 {"ok": true, "service": "wechat_gateway", "port": 801, "started_at": 1793195099. |
| 功能 E2E | 控制台 /api/health | **PASS** | 0.02 | HTTP 200 {"ok": true, "service": "soul-console", "ts": 1793205714.8452787, "contact_db":  |
| UI 冒烟 | 截图 view=welcome | **PASS** | 1.29 | 43KB |
| UI 冒烟 | 截图 view=overview | **PASS** | 1.77 | 45KB |
| UI 冒烟 | 截图 view=services | **PASS** | 1.71 | 39KB |
| UI 冒烟 | 截图 view=config | **PASS** | 1.48 | 69KB |
| UI 冒烟 | 截图 view=data | **PASS** | 1.91 | 36KB |
| UI 冒烟 | 截图 view=train | **PASS** | 1.34 | 61KB |
| UI 冒烟 | 截图 view=models | **PASS** | 2.08 | 70KB |
| UI 冒烟 | index.html 可访问 | **PASS** | 0.00 | 7/7 张截图 |

## 分组耗时

| 组 | PASS | FAIL | SKIP | 耗时(s) |
|---|---|---|---|---|
| 自动化测试 | 0 | 0 | 3 | 0.0 |
| 全服务冒烟 | 1 | 0 | 0 | 1.2 |
| 功能 E2E | 10 | 0 | 0 | 0.8 |
| UI 冒烟 | 8 | 0 | 0 | 11.6 |

## 失败详情

无 FAIL 项。✅

## 截图清单

| view | 文件 | 大小 | 状态 |
|---|---|---|---|
| welcome | `reports\screens\acceptance_20261029_0041\welcome.png` | 43KB | OK |
| overview | `reports\screens\acceptance_20261029_0041\overview.png` | 45KB | OK |
| services | `reports\screens\acceptance_20261029_0041\services.png` | 39KB | OK |
| config | `reports\screens\acceptance_20261029_0041\config.png` | 69KB | OK |
| data | `reports\screens\acceptance_20261029_0041\data.png` | 36KB | OK |
| train | `reports\screens\acceptance_20261029_0041\train.png` | 61KB | OK |
| models | `reports\screens\acceptance_20261029_0041\models.png` | 70KB | OK |

## 已知限制（由 Lead 在最终验收时填写）

> 本小节留白，供 Lead 补充已知限制、豁免说明与上线决策；验收脚本不自动填写。

- （待填）……

## 复现

```bash
C:\Python314\python.exe scripts/acceptance.py --fast
```

> 报告由 `scripts/acceptance.py` 自动生成；完整 data 见同目录同名 md，截图在 `reports/screens/acceptance_20261029_0041/`。
