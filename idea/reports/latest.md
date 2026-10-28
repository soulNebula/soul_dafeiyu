# 验收测试报告 · acceptance_20261029_0602

> 总体结论：**PASS**  ·  PASS 19 / FAIL 0 / SKIP 3  ·  总耗时 19s  ·  生成时间 2026-10-29 06:02:34

## 环境

| 项 | 值 |
|---|---|
| 时间 | 2026-10-29 06:02:34 |
| 模式 | --fast（跳过全量 pytest） |
| 主机/Python | WIN-20251030KZR / 3.14.7 |
| idea venv | C:\Users\Administrator\Desktop\SoulAgent\idea\.venv\Scripts\python.exe |
| SoulAgent | C:\Users\Administrator\Desktop\SoulAgent\SoulAgent |
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
| 全服务冒烟 | smoke_all.py | **PASS** | 1.30 | 13 PASS / 0 FAIL |
| 功能 E2E | 工作台 /api/wb/overview | **PASS** | 0.35 | HTTP 200 {"ok": true, "soulagent": "C:\\Users\\Administrator\\Desktop\\SoulAgent", "agent |
| 功能 E2E | 工作台 /api/wb/config | **PASS** | 0.00 | HTTP 200 {"ok": true, "groups": ["基础", "模型", "训练"], "items": [{"key": "enabled", "group": |
| 功能 E2E | 工作台 /api/wb/services | **PASS** | 0.26 | HTTP 200 {"ok": true, "items": [{"name": "gateway", "label": "微信网关", "port": 801, "runnin |
| 功能 E2E | 工作台 /api/wb/db/databases | **PASS** | 0.36 | HTTP 200 {"ok": true, "items": [{"id": "C:\\Users\\Administrator\\Desktop\\SoulAgent\\ide |
| 功能 E2E | 工作台 /api/wb/train/datasets | **PASS** | 0.04 | HTTP 200 {"ok": true, "items": [{"path": "E:\\agent\\datasets\\verify_annot_val.jsonl", " |
| 功能 E2E | 工作台 /api/wb/models/running | **PASS** | 0.07 | HTTP 200 {"ok": true, "items": [{"port": 8123, "pid": 5392, "running": true, "model_path" |
| 功能 E2E | 工作台 /api/wb/models/disk | **PASS** | 0.03 | HTTP 200 {"ok": true, "items": [{"kind": "models", "dir": "E:\\agent\\models", "size": 19 |
| 功能 E2E | 网关 /health | **PASS** | 0.00 | HTTP 200 {"ok": true, "service": "wechat_gateway", "admin_token_weak": false, "admin_toke |
| 功能 E2E | 网关 /status（X-Admin-Token） | **PASS** | 0.00 | HTTP 200 {"ok": true, "service": "wechat_gateway", "port": 801, "started_at": 1793222818. |
| 功能 E2E | 控制台 /api/health | **PASS** | 0.01 | HTTP 200 {"ok": true, "service": "soul-console", "ts": 1793224937.8893716, "contact_db":  |
| UI 冒烟 | 截图 view=welcome | **PASS** | 1.59 | 47KB |
| UI 冒烟 | 截图 view=overview | **PASS** | 2.77 | 42KB |
| UI 冒烟 | 截图 view=services | **PASS** | 2.28 | 42KB |
| UI 冒烟 | 截图 view=config | **PASS** | 1.88 | 74KB |
| UI 冒烟 | 截图 view=data | **PASS** | 2.59 | 98KB |
| UI 冒烟 | 截图 view=train | **PASS** | 1.84 | 65KB |
| UI 冒烟 | 截图 view=models | **PASS** | 2.33 | 84KB |
| UI 冒烟 | index.html 可访问 | **PASS** | 0.00 | 7/7 张截图 |

## 分组耗时

| 组 | PASS | FAIL | SKIP | 耗时(s) |
|---|---|---|---|---|
| 自动化测试 | 0 | 0 | 3 | 0.0 |
| 全服务冒烟 | 1 | 0 | 0 | 1.3 |
| 功能 E2E | 10 | 0 | 0 | 1.1 |
| UI 冒烟 | 8 | 0 | 0 | 15.3 |

## 失败详情

无 FAIL 项。✅

## 截图清单

| view | 文件 | 大小 | 状态 |
|---|---|---|---|
| welcome | `reports\screens\acceptance_20261029_0602\welcome.png` | 47KB | OK |
| overview | `reports\screens\acceptance_20261029_0602\overview.png` | 42KB | OK |
| services | `reports\screens\acceptance_20261029_0602\services.png` | 42KB | OK |
| config | `reports\screens\acceptance_20261029_0602\config.png` | 74KB | OK |
| data | `reports\screens\acceptance_20261029_0602\data.png` | 98KB | OK |
| train | `reports\screens\acceptance_20261029_0602\train.png` | 65KB | OK |
| models | `reports\screens\acceptance_20261029_0602\models.png` | 84KB | OK |

## 已知限制（由 Lead 在最终验收时填写）

> 本小节留白，供 Lead 补充已知限制、豁免说明与上线决策；验收脚本不自动填写。

- （待填）……

## 复现

```bash
C:\Users\Administrator\Desktop\SoulAgent\idea\.venv\Scripts\python.exe scripts/acceptance.py --fast
```

> 报告由 `scripts/acceptance.py` 自动生成；完整 data 见同目录同名 md，截图在 `reports/screens/acceptance_20261029_0602/`。
