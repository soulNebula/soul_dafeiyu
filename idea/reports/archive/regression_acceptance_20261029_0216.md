# 精简后回归验收报告（2026-10-29 02:10–02:16）

> 范围：IDEA 803（7 保留页 + 新增语料清洗/用户标注）、802 控制台（发送模式/朋友圈/模型）、
> 全服务冒烟、网关回归、死引用与依赖检查。**只验证与修一行级小问题；不 commit。**
>
> 结论：**PASS（附 1 个 FAIL、2 个待复测/小问题）** —— 核心功能可用；发送模式在网关侧的
> `/status` 未自动同步（需显式 reload），以及静默时段导致真实发送未能实测，需白天复测。

## 一、环境

| 项 | 值 |
|---|---|
| 时间 | 2026-10-29 02:10–02:16 |
| 仓库 | `C:\Users\Administrator\Desktop\idea`、`C:\Users\Administrator\Desktop\SoulAgent` |
| 服务端口 | 801=UP，802=UP，803=UP，8123=UP，8125=UP（netstat 实测） |
| IDEA Python | `idea/.venv/Scripts/python.exe`（3.14.7） |
| SoulAgent Python | `C:\Python314\python.exe`（3.14.7） |
| 工作台令牌 | `***`（默认 8888，已验证可访问全部 `/api/wb/*`） |

## 二、结果总表

| 区域 | 检查项 | 结果 | 证据/数字 |
|---|---|---|---|
| IDEA | `pytest tests -q` | **PASS** | **94 passed**（2.37s） |
| IDEA | 7 保留页截图（welcome/overview/services/config/data/train/models） | **PASS** | `.data/screens/verify_*.png`，38–83KB；DOM 校验 `view-<name>` 为 active，无 Uncaught |
| IDEA | 新增 2 页截图（corpus/annotate） | **PASS** | `verify_corpus.png` 78KB / `verify_annotate.png` 104KB（1440×2200 全页） |
| IDEA | 已删功能死引用 grep | **PASS** | `chatstudio|memstudio|gitstudio|momentsstudio|monaco|/api/wb/git|/api/wb/files` → **0 命中**；`drafts` → 0 命中 |
| IDEA | 核心 API 200 | **PASS** | overview/config/services/db/train/models(running/inventory/disk)/corpus/jobs 均 200 |
| IDEA | 语料清洗 E2E | **PASS** | 新建任务 `c1177b30378` → 上传 txt(198B) → 异步 parse：done，5 条（3 me / 2 you）→ 保存 `verify_corpus.jsonl`（1 train + 1 val，messages 末条 assistant） |
| IDEA | 用户标注 E2E | **PASS** | `annot/source` 返回 3 条 me + 建议；保存 → `verify_annot.jsonl`（1 train + 1 val，target 10 字段齐全、score 过滤生效） |
| IDEA | 标注页中文 | **PASS（修 1 处小 bug）** | 长度下拉=短句/正常/长句，表情=偶尔/不用，理由字段名=心情/精力/分享欲/电量/信任/关系阶段/态度/意图/长度；修复前理由里 mood/stage/short 未翻译（见问题 2） |
| 802 | 控制台相关 pytest（console_server + moments_models + model_control + bili + daily_corpus） | **PASS** | **91 passed**（15.64s）注：Lead 预期 88，实际收集 91（新增测试导致，非失败） |
| 802 | 发送模式 auto→confirm→auto | **FAIL（同步）** | 控制台写配置成功：config=confirm→auto；但网关 `/status` 在 +0/1/2/4s 仍为 `auto`；手动 `POST 801/api/config/reload` 后变 `confirm`（reloaded=true）——见问题 1 |
| 802 | 朋友圈刷新 | **PASS** | `POST /api/moments/refresh {limit:5}` → 200 `{fetched:5, added:0}` |
| 802 | 模型导入/删除 | **PASS** | 假 GGUF(64B, 头 GGUF) → `E:\agent\models\imported\verify_regression_model\verify_regression.gguf`；删除 → `E:\agent\models\.trash\20261029_021044\...`，源已不存在 |
| 802 | 发送模式/朋友圈/模型页截图 | **PASS** | `verify_console_sendmode.png`(91KB，显示 当前:草稿)、`verify_console_moments.png`(83KB)、`verify_console_models.png`(115KB) |
| 冒烟 | `idea/scripts/smoke_all.py` | **PASS** | **13/13 PASS**（803/801/802/8123/8125） |
| 网关 | `/health` | **PASS** | 200 `{ok:true, admin_token_weak:false}` |
| 网关 | `/api/approve` 发「文件传输助手」测试文字 | **SKIP（未实测发送）** | HTTP 200 但 `{"ok":false,"status":"blocked","error":"quiet_hours"}`：当前 02:10 在 22–9 静默时段，保护正常；真实发送需白天复测 |
| 依赖 | IDEA `compileall app scripts` | **PASS** | exit 0 |
| 依赖 | SoulAgent `compileall agi/selflearn agi/wechat_gateway agi/soul tools/console` | **PASS** | exit 0 |
| 安全 | `SoulAgent/tools/check_secrets.py` | **PASS** | `[PASS] secrets scan ... 没有发现密钥/凭证`（exit 0） |
| 死引用 | SoulAgent `WechatMoments|agi.core|agi/core`（*.py） | **PASS（历史说明）** | 仅 `tools/audit_compliance.py` 历史注释与 `webui/moments*.py` 的可选朋友圈 legacy 路径探测；无 `agi.core` import/运行引用 |

## 三、发现的问题（按严重度）

1. **[中] 发送模式写回配置后，网关 `/status` 不同步（验收判据 FAIL）**
   - 现象：802 控制台 `POST /api/control/send_mode {mode:confirm}` → 配置文件 `send_mode=confirm`
     （控制台截图显示"当前：草稿"），但 801 `/status` 的 `send_mode` 仍为 `auto`
     （+0/1/2/4s 均未变）。
   - 手动调用 `POST http://127.0.0.1:801/api/config/reload` 后：`reloaded=true`,
     `changes.send_mode: auto→confirm`，`/status` 立即变 `confirm`。
   - 原因：控制台只写 `config.json`；网关 `send_mode` 只在 `run_cycle`/显式 reload 时热加载，
     `/status` 不重读文件。控制台文案也写着"网关热加载，下一条消息生效"。
   - 影响：验收要求"网关 /status 同步"不满足；实际下一条消息会生效，属状态可见性问题。
   - 建议（一行级）：`tools/console/server.py::set_send_mode` 成功后调用网关
     `POST /api/config/reload`（带 admin token），或 `/status` 展示时按 mtime 触发 reload。
2. **[低，已修] 标注页"建议理由"字段名未中文化（0x08 转义 bug）**
   - 现象：修复前 DOM 里理由显示 `mood=0.55` / `stage=陌生` / `长度=short`
     （`annotZh()` 中两处 `\b` 被写成了退格字符 0x08，正则不生效）。
   - 修复：`idea/web/index.html` 中 4 个 0x08 字符 → `\b`（一行级，共 4 字符）。
   - 修复后：理由全中文（`心情=0.55 精力=… 分享欲=… 电量=… 信任=…`、
     `关系阶段=陌生`、`长度=短句`、`会反问=True`、`给建议=False`、`表情=不用`），DOM 中英文 token 0 残留。
3. **[低/待复测] 真实发送未验证**：`/api/approve` 在静默时段返回 `blocked/quiet_hours`（保护正常），
   白天需再发一条「文件传输助手」测试文字确认端到端发送。
4. **[提示] 数字与 Lead 预期略有差异**：冒烟 **13/13**（不是 16）、console 相关 pytest **91**
   （不是 88）、IDEA 视图共 **9** 个（7 保留页 + corpus + annotate）。均为当前代码实际值。
5. **[提示] SoulAgent `webui/moments*.py`** 仍引用可选外部目录 `WechatMoments/`（历史朋友圈
   legacy 功能探测，不是 import 死链）；主链未使用，若彻底下线朋友圈可另开清理任务。

## 四、截图清单（`.data/screens/`）

| 文件 | 用途 | 大小 |
|---|---|---|
| `verify_welcome.png` | IDEA 欢迎页 | 47KB |
| `verify_overview.png` | IDEA 概览 | 50KB |
| `verify_services.png` | IDEA 服务管理 | 42KB |
| `verify_config.png` | IDEA 配置中心 | 74KB |
| `verify_data.png` | IDEA 数据工作室 | 39KB |
| `verify_train.png` | IDEA 训练工厂 | 62KB |
| `verify_models.png` | IDEA 模型中心 | 83KB |
| `verify_corpus.png` | IDEA 语料清洗（含任务/上传区） | 78KB |
| `verify_annotate.png` | IDEA 用户标注（数据源/多项设置） | 104KB |
| `verify_console_sendmode.png` | 802 控制台概览 + 发送模式（当前:草稿） | 91KB |
| `verify_console_moments.png` | 802 朋友圈页 | 83KB |
| `verify_console_models.png` | 802 模型页 | 115KB |

## 五、复现命令

```bash
# IDEA 测试
cd C:/Users/Administrator/Desktop/idea && .venv/Scripts/python.exe -m pytest tests -q
# 冒烟
cd C:/Users/Administrator/Desktop/idea && .venv/Scripts/python.exe scripts/smoke_all.py
# 控制台测试
cd C:/Users/Administrator/Desktop/SoulAgent && C:/Python314/python.exe -m pytest \
  agi/tests/test_console_server.py agi/tests/test_console_moments_models.py \
  agi/tests/test_console_model_control.py agi/tests/test_bili_console.py \
  agi/tests/test_daily_corpus_console.py -q
```

> 备注：验收前把 `send_mode` 恢复成 `auto`（已在验收结束后确认 config=auto、gateway status=auto）；
> 假模型导入产物已删除到 `E:\agent\models\.trash\20261029_021044\`（可从回收目录清理）。
