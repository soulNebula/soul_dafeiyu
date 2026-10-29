# 开源上手 + Bot 化 · 验收清单

> 对应路线：[`../plans/63-开源上手与Bot化路线.md`](../plans/63-开源上手与Bot化路线.md)
> 环境：Windows 10/11 + 微信 PC + Python 3.12+；仓库根目录为 `SoulAgent`。
> 验收日期：2026-10-30。状态：✅ 已验证 / 🟡 部分可用 / ❌ 未实现 / ⏸ 按决策暂不做。

## 0. 验收前检查

```bat
:: 1) 仓库根目录，确认服务在跑
curl http://127.0.0.1:801/health
curl http://127.0.0.1:802/api/health
curl http://127.0.0.1:803/api/wb/health

:: 2) 环境脚本只检查（不安装）
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -CheckOnly
```

预期：801/802/803 都是 200；`setup.ps1 -CheckOnly` 输出 Python / AGENT_HOME / venv 状态，不改文件。

---

## A. 第 1 步：环境脚本 → 803

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| A1 | 检查 Windows / Python≥3.12 / 微信进程 | ✅ | `setup.ps1 -CheckOnly` |
| A2 | 缺 Python 自动装（winget → 国内镜像安装包） | 🟡 | 代码路径已实现；**未在“干净无 Python”机器实测** |
| A3 | 国内源 pip（默认清华，可 `-Mirror ""` 关） | ✅ | `setup.ps1`，失败会报 pip 错误 |
| A4 | 建主 venv / 803 venv / 发送器 venv | ✅ | 检查 `.venv`、`idea\.venv`、`E:\agent\venv_wx312` |
| A5 | 自动装 `wechatauto`（失败回退源码/提示） | 🟡 | pip 装包名可能因网络失败；失败不影响 803 |
| A6 | 创建 AGENT_HOME 目录 | ✅ | `E:\agent\{models,datasets,output,logs}` |
| A7 | 启动 803 并打开浏览器 | ✅ | <http://127.0.0.1:803/> |

**验收命令**：

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -CheckOnly
```

---

## B. 第 2 步：803 新建 Bot（云端方案）

入口：803 左侧 **🤖 Bot**。

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| B1 | 目录用系统文件夹选择 | ✅ | 点「选择目录」弹 Windows 文件夹框（803 同机） |
| B2 | Bot 名称/802 登录密码（≥6 位） | ✅ | 密码不足 6 位会拒绝 |
| B3 | 聊天服务商/base_url/模型下拉 | ✅ | DeepSeek/OpenAI/阿里百炼/硅基流动/Ollama/自定义 |
| B4 | 聊天 API key | ✅ | 只写 `<Bot>/secrets.json`，不回显 |
| B5 | 灵魂 base_url/模型/API key 下拉 | ✅ | 「同聊天」或单独配置 |
| B6 | 用户自选目录保存 Bot 包 | ✅ | `<Bot>/{bot.json,secrets.json,db,data,datasets,logs,models}` |
| B7 | 添加已有目录 / 校验 / 移除登记（不删文件） | ✅ | 右侧已登记列表 |
| B8 | 打开 802 入口 | ✅ | 右上「打开 802」 |
| B9 | 内状态/识图/主动消息在 Bot 创建时配置 | 🟡 | 目前可在 802 设置页配置；Bot 表单未全部暴露 |
| B10 | 本地模型方案（GPU/下载） | ❌ | 按决策“无 GPU 只云端”暂不做；见 Phase 2 |

**验收命令**：

```bat
python tools\bot_profile.py create --path E:\bots\accept-demo --name 验收Bot ^
  --password accept123 --base-url https://api.deepseek.com --model deepseek-flash ^
  --api-key sk-你的key
python tools\bot_profile.py validate --path E:\bots\accept-demo
python tools\bot_profile.py show --path E:\bots\accept-demo
```

预期：validate `ok=true`；show 里 `providers.*.configured=true`（key 打码）。

---

## C. 第 3 步：803 → 802 登录

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| C1 | 803 底部/右上打开 802 | ✅ | 803 Bot 页「打开 802」 |
| C2 | 802 新用户看到「加载 Bot」弹层 | ✅ | 无 token 打开 <http://127.0.0.1:802/> |
| C3 | 选 Bot 目录（不用手填） | ✅ | 弹层「选择目录」 |
| C4 | 输入 Bot 密码 → 自动重启 801/802 | ✅ | 约 5–10 秒，页面自动刷新 |
| C5 | 老用户可「跳过」用当前数据 | ✅ | 弹层「跳过」 |
| C6 | 登录后数据根 = `<Bot>/data`；手工重启 801/802 也会自动对齐活跃 Bot | ✅ | `/api/bot/info` active=true；`/api/health` contact_db 在 Bot 目录 |

**验收命令**（也可用页面）：

```bat
curl http://127.0.0.1:802/api/bot/info
curl -X POST http://127.0.0.1:802/api/bot/login ^
  -H "Content-Type: application/json" -d "{\"password\":\"accept123\"}"
```

预期：`active=true`；login 返回 `token`。

> 实测记录：2026-10-29 用临时 Bot 加载成功（active=true、联系人 0），`/api/bot/clear` 切回默认数据后原联系人/消息恢复。切换 helper 已改为 Windows 计划任务，避免被父进程 Job 杀掉。

---

## D. 第 4 步：802 刷新微信联系人 + 单/多选

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| D1 | 总览「刷新联系人」按钮 | ✅ | 右侧顶部按钮 |
| D2 | 先提示打开微信/版本 | 🟡 | 弹层有提示文字；未做微信版本范围强校验 |
| D3 | 显示当前微信昵称/wxid | ✅ | 弹层标题「当前微信：小鱼」 |
| D4 | 显示好友列表 | ✅ | 读 `contact.db`；服务号过滤 |
| D5 | 单/多选 | ✅ | 复选框 + 全选/清空 |
| D6 | 应用选择写入白名单/监听 | ✅ | 保存 `allowed_contacts` / `monitored_contacts` |
| D7 | 群聊入口保留但置灰 | ✅ | 群列表 disabled + 说明 |
| D8 | 刷新前/后联系人库同步 | 🟡 | 选中后需下一轮监听生效；未做数据库自动创建 |
| D9 | 首次读取 DB 密钥耗时提示 | ✅ | 弹层提示 10–20 秒 |

**验收步骤**：

1. 打开 802 总览 → 点「刷新联系人」；
2. 确认「当前微信：<你的昵称>」；
3. 勾选 1–2 个好友 → 「应用选择」；
4. 看 802 联系人栏/网关状态是否更新。

---

## E. 第 5 步：提示词 / 模型 / 模型配置

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| E1 | 提示词页可编辑，保存到 Bot 数据根 | 🟡 | 页面已有；Bot 激活后写 `<Bot>/data/prompts.json`，未做导入向导 |
| E2 | 模型页（启停本地模型） | 🟡 | 页面已有；云端方案可跳过 |
| E3 | 模型配置页（key/参数） | ✅ | 802「模型配置」；key 已从 Bot secrets 生效 |
| E4 | Bot 的聊天模型/base_url 自动带入 | ✅ | 激活时写 `<Bot>/data/web_config.json` |
| E5 | Bot 的灵魂 key 真正用于内状态 | ❌ | 目前内状态走主 LLM client（DeepSeek）；soul key 仅保存，未单独路由 |
| E6 | 参数保存热加载 | ✅ | `/api/model-config` 保存后下一条消息生效 |

---

## F. 第 6 步：B站配置

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| F1 | B站扫码登录 | 🟡 | 802 B站页依赖仓库外 `~/.dsh/bili-duty` 插件；无插件不可用 |
| F2 | 关键词默认让 LLM 找兴趣 | 🟡 | 现有 B站/兴趣画像逻辑；未做 Bot 级开关 |
| F3 | 人设提示词默认用 Bot 人设 | 🟡 | 复用 802 提示词；未做 Bot 表单 |
| F4 | Bot 级「是否刷 B站/数据入库」 | ❌ | 未接；现有全局配置/插件 |

> 这一块不是 Phase 1 核心；开源用户没有 DSH 插件时，B站页只显示空/不可用。

---

## G. 第 7 步：设置栏

入口：802 左侧「设置」。

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| G1 | 监听/引擎/自动回复/演练/发送模式 | ✅ | 设置页第一组 |
| G2 | 发送限制（字数/日上限/时上限/间隔/延迟/防抖） | ✅ | 设置页第二组 |
| G3 | 模型/内状态/识图/灵魂地址 | ✅ | 第三组 |
| G4 | 主动消息（概率/间隔/静默） | ✅ | 第四组 |
| G5 | pacing | ✅ | 第五组 |
| G6 | 记忆/叙事 | ✅ | 第六组 |
| G7 | 媒体 | ✅ | 第七组 |
| G8 | trace/内驱力/发送探测 | ✅ | 第八组 |
| G9 | 保存全部设置（43 项，校验+热加载） | ✅ | 底部「保存全部设置」 |
| G10 | 设置按 Bot 隔离 | ✅ | 配置文件在 `<Bot>/data/wechat/config.json` |
| G11 | B站/朋友圈/上网的 Bot 级开关 | ❌ | 未做；目前只在旧页面/插件侧 |

**验收命令**：

```bat
curl -H "X-Admin-Token: <token>" http://127.0.0.1:802/api/config
```

预期：`config` 里有 43 个可编辑键；保存后值保留。

---

## H. 第 8 步：测试 → 开始运行

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| H1 | 模型连通测试（chat/soul/vision） | ✅ | 802「对话测试」→「开始测试」 |
| H2 | 聊天模型测试 | ✅ | 对话测试发一句 |
| H3 | 灵魂模型测试 | 🟡 | 测的是本地 8123 `/health`；云端内状态未单独测 |
| H4 | 识图模型测试 | ✅ | 测本地 8125 `/health` 或云端 key |
| H5 | 开始运行/停止 | ✅ | 总览「开始监听/停止监听」 |

实测（2026-10-30）：chat `HTTP 200`、soul `HTTP 200`、vision `HTTP 200`。

---

## I. 第 9 步：本地模型 / 训练 / 数据积累

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| I1 | GPU/显存检测 + 方案推荐 | ✅ | 803 Bot 页「本机硬件/方案推荐」→ 检测 |
| I2 | 无 GPU 只允许云端 | ✅ | 无 GPU 时推荐 mode=cloud、local_training=false |
| I3 | 一键装 CUDA/llama.cpp | ❌ | 未做（Phase 2） |
| I4 | 模型下载/推荐/校验 | 🟡 | 803「模型中心」已有下载功能，但未接 Bot/GPU 推荐自动下载 |
| I5 | 语料清洗/蒸馏/训练 | 🟡 | 803 已有页面；尚未从 Bot 包/打包数据自动带入 |
| I6 | 802 数据积累进度条 | ✅ | 总览右侧「训练数据积累」 |
| I7 | 满 300 自动打包 | ✅ | 802 后台线程每 180s 检查，满阈值自动生成包 |
| I8 | 打包数据在 803 可见 | ✅ | 803 Bot 页「训练数据包（当前 Bot）」 |
| I9 | 训练后 802 加载新模型 | ❌ | 未做（Phase 3） |
| I10 | 标签/删除前二次确认 | 🟡 | 删除确认未做；当前打包只增不删 |

**验收命令**：

```bat
curl -H "X-Admin-Token: <token>" http://127.0.0.1:802/api/train/packages
curl -H "X-Admin-Token: <token>" http://127.0.0.1:803/api/wb/hardware
```

预期：packages 显示 threshold/pending/已打包列表；hardware 显示 GPU 与推荐。

---

## J. Bot 数据隔离（贯穿）

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| J1 | 不同 Bot 数据库不连通 | ✅ | 加载 Bot 后 `/api/contacts` 为空；切回默认恢复 |
| J2 | 记忆/日记/日志隔离 | ✅ | `<Bot>/data/selflearn`、`<Bot>/logs` |
| J3 | 密钥隔离 | ✅ | `<Bot>/secrets.json`，`SOULAGENT_SECRETS` 指向它 |
| J4 | 训练数据隔离 | ✅ | `TRAIN_ROOT = DATA_DIR/train` |
| J5 | 切 Bot 自动重启 801/802 | ✅ | 计划任务 helper，约 5–10 秒 |
| J6 | 退出 Bot 回默认数据 | ✅ | `POST /api/bot/clear` 或 `python tools\bot_activate.py --clear --restart` |

---

## K. 自动化测试 / 守卫

| # | 项目 | 状态 | 命令 |
| --- | --- | --- | --- |
| K1 | Bot 包测试 5 | ✅ | `python -m pytest agi/tests/test_bot_profile.py -q` |
| K2 | Bot 激活测试 3 | ✅ | `python -m pytest agi/tests/test_bot_activate.py -q` |
| K3 | 打包测试 3 | ✅ | `python -m pytest agi/tests/test_train_packages.py -q` |
| K4 | 803 Bot API 测试 3 | ✅ | `idea\.venv\Scripts\python.exe -m pytest idea/tests/test_botstudio.py -q` |
| K5 | 803 硬件测试 2 | ✅ | `idea\.venv\Scripts\python.exe -m pytest idea/tests/test_hardware.py -q` |
| K6 | 803 全套 | ✅ 106 passed | `cd idea && .venv\Scripts\python.exe -m pytest tests -q` |
| K7 | 802 console 测试 | ✅ 87 passed | `python -m pytest agi/tests/test_console_server.py -q` |
| K8 | 架构守卫 | ✅ | `python tools/check_architecture.py` |
| K9 | 文档链接 | ✅ 181/0 | `python tools/check_docs_links.py` |
| K10 | 全量 agi 回归 | 🟡 | 未在最终改动上重跑（需先停止 801，避免生产文件隔离守卫） |

---

## L. 本轮涉及提交

| 提交 | 内容 |
| --- | --- |
| `6db3813` | Phase 1A：setup.ps1 + Bot 包 v1 |
| `d1c054a` | 803 Bot 管理页/API |
| `d466234` | 802 Bot 登录/加载 + 数据根重启 |
| `1aaf996` | 803 文件夹选择 + 服务商/模型下拉 |
| `61e9bde` | 引用消息 `<title>` 修复 + 历史回填 |
| `c297354` | 计划任务修复 Bot 切换 |
| `3f74218` | 802 刷新微信联系人 |
| `8376455` | 802 设置 43 项 + 模型连通测试 |
| `27cf70d` | Bot 设置映射到运行时配置 |
| `a37bfdf` | 训练数据进度 + 训练目录 Bot 化 |
| `fe32fd2` | 自动打包 + 803 包列表 + 硬件检测 |

---


## N. 把当前 802 配置保存为 Bot

| # | 验收项 | 状态 | 怎么验 |
| --- | --- | --- | --- |
| N1 | 803「保存当前 802 配置」按钮 | ✅ | 803 Bot 页填目录/名称/密码 → 点按钮 |
| N2 | CLI 导出 | ✅ | `python tools\export_bot.py --path E:ots\my802 --name 我的802 --password 你的密码` |
| N3 | 配置/白名单/提示词/联系人/人格/日记一起导出 | ✅ | 导出后 `validate` 通过；日志看 copied_files |
| N4 | 不导出明文 secrets | ✅ | Bot 里只有 `secrets.json`（LLM key），不复制原 `agi/data/secrets.json` |
| N5 | 导出后 803 自动登记 | ✅ | 803 已登记 Bot 列表出现 |
| N6 | 导出时决定是否带 media_chat | 🟡 | CLI 有 `--with-media`；803 按钮默认不带 |

**命令**：

```bat
python tools\export_bot.py --path E:gentots\802-config-20261030 ^
  --name "802 当前配置" --password 你的密码
python toolsot_profile.py validate --path E:gentots\802-config-20261030
```

> 实测：2026-10-30 已导出当前 802（27 个文件，配置/白名单/联系人/人格/日记/提示词），
> Bot 目录 `E:gentots\802-config-20261030`，已在 803 登记。


## O. 2026-10-30 反馈修复记录

| 反馈 | 处理 | 状态 |
| --- | --- | --- |
| 点「开始监听」只闪一下 | 网关 `set_engine(start)` 现在会把总开关 `enabled` 一起打开并真正 `start()` receiver；实测 stop→start 后 `ready=true`、backend running | ✅ |
| 点「刷新联系人」没反应 / 应用选择报 `not_allowed:allowed_contacts` | 按钮事件已补绑；新增 `/api/contacts/apply`（801 即时更新 receiver/backend，不走 /api/config 白名单），静态 no-store；首次读 DB 密钥 10–20 秒 | ✅ |
| D1~D8 没实现 | 已实现并在 Bot 模式实测：当前微信/好友列表/复选/应用；用 `?contacts=1` 可直接打开弹层 | ✅ |
| 802 提示词页 | 新增「＋ 新增提示词」；内置 11 键不可删除/只能覆盖/恢复默认；自定义 `custom.*` 可添加、编辑、删除 | ✅ |
| 回复“这么晚还醒着”不像“还不睡？” | 用户自行调整默认提示词；代码不硬编码回复 | ✅ 用户处理 |
| 上下文溢出 4.10K/4.10K | 8125 3B llama-server 上下文 4096→8192 并已重启（`SOULAGENT_LOCAL3B_CTX` 可覆盖） | ✅ |
| 新 Bot 没数据库 | 803 Bot 列表新增「添加数据库」；已有数据库显示「已有数据库」置灰；数据页也有当前 Bot 一键添加入口 | ✅ |
| 802 没有切换 Bot | 总览右上新增「切换 Bot」，复用加载弹层（选目录/密码→重启加载） | ✅ |
| 联系人列表出现不在聊天列表里的联系人 | `/api/contacts` 过滤 0 消息的无名残留库；实测只剩 2 个真实会话 | ✅ |


## P. 2026-10-30 第二阶段（备份 + 四项功能）

| 项 | 状态 | 说明/怎么验 |
| --- | --- | --- |
| 备份 Bot + 原 agi/data | ✅ | `E:gentackupsackup-20261030-020719\{bot,agi_data,root_files}`；live copy（含 wal/shm），约 707MB |
| 云端灵魂路由 | ✅ | Bot 配置 `soul_provider_mode=cloud` + base_url/model；`agi.inner.inject` 用 OpenAI 兼容 Client，key 取 Bot `secrets.providers.soul`（回退 chat key）；802「模型连通测试」soul 显示 mode=cloud / HTTP 200 |
| 802 设置：灵魂来源 | ✅ | 「模型与内状态」组：local / cloud + 云端 base_url / 模型 |
| B站/朋友圈 Bot 开关 | 🟡 | `surf_enabled`（B站/上网冲浪，真实生效）、`moments_enabled`（关闭后朋友圈 API 503）、`bili_ingest_enabled`（仅配置项，尚未在所有入库路径生效） |
| 训练闭环（数据→803→802） | 🟡 | 803 数据集发现包含 Bot 的 packages/daily；导出成功后**自动注册**到当前 Bot 的 `local_models.json`（role=soul, source=train_export），802 模型页可启动新模型；**自动启动/自动切换模型仍未做** |
| 改 Bot 密码 | ✅ | 803 Bot 列表「改密码」；`tools/bot_profile.change_password` 校验旧密码；API `/api/wb/bots/password`；实测错误旧密码拒绝、正确后可登录 |
| 3B 上下文 | ✅ | 8125 上下文 8192 已重启，缓解 4.10K 溢出 |

## M. 明确未完成（不要按“已验收”）的项

1. **云端灵魂模型单独路由**：Bot 里有 soul key，但内状态仍用主聊天 client；要做「soul provider → 独立 OpenAI 兼容 client」。
2. **B站/朋友圈/上网的 Bot 级开关与入库开关**。
3. **CUDA/llama.cpp 一键安装**、GPU 驱动安装、模型自动下载（Phase 2）。
4. **打包数据 → 803 训练 → 802 加载新模型**的完整闭环（Phase 3）。
5. **群聊**（按决策置灰，不做）。
6. **干净机器 setup.ps1 全流程**未实测（只验证了 CheckOnly 与本机各分支）。
7. **微信版本兼容矩阵**未整理；只在本机微信 4.x 验证。

> 验收建议：A→B→C→D→G→H→I6–I8→J 逐项走；F/B9/I1–I5 可按“部分”记录，M 中项目单独排期。
