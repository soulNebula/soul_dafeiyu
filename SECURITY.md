# 安全策略（Security Policy）

SoulAgent 是一个**在本机运行、连接你自己微信账号**的 AI 伴侣项目。它的安全边界和
普通 Web 服务不一样：最大的风险不是「服务器被打」，而是**账号凭证/聊天数据泄漏**
和**无人值守时被用来误发消息**。本文先给出威胁模型与当前实现的安全事实，再写怎么报漏洞。

> 口径说明：本文只描述**代码里真实存在**的机制与默认值。凡是「本工具保证/绝不…」
> 这类无法被测试证明的绝对断言，都不属于本文的承诺范围（见 §7「漏洞边界」）。

## 1. 威胁模型（Threat Model）

| # | 威胁 | 现实场景 | 代码层的缓解 | 仍需用户做的事 |
| --- | --- | --- | --- | --- |
| T1 | **本机多用户/共享电脑** | 家里的共用机、公司电脑、远程桌面会话；同机其他登录账户能读到项目目录 | `tools/harden_acl.py` 切断继承，只留 当前用户/SYSTEM/Administrators；`--verify` 非零退出 | 管理员终端跑一次 `--apply`；不要在共享账户下长期登录微信 |
| T2 | **明文密钥落盘** | `web_config.json`/`config.json` 被复制、打包、截图、误提交；`db_keys` 可解密整台机器的微信库 | 三级来源：`DEEPSEEK_API_KEY` env → `agi/data/secrets.json` → 旧明文文件（命中告警）；`tools/rotate_secrets.py` 迁移 + ACL；`tools/check_secrets.py` 扫 git 跟踪文件（`sk-`、`db_keys`、`aes_key` 等模式） | 迁移并轮换历史 key；不要把 `config.json`/`web_config.json`/`log/`/`media/`/`agi/data/` 提交 |
| T3 | **聊天记录/媒体长期堆积** | `log/messages.jsonl`、`log/聊天记录.txt`、`media/` 无限增长；设备丢失/网盘同步即全量泄漏 | `tools/prune_chat_logs.py`（默认 dry-run，`--days` 默认 30）按 `ts`/mtime 清理；草稿另有 TTL（`drafts.prune`，见 `agi/wechat_gateway/drafts.py`） | 定期跑 `--apply`；备份用加密盘；不用网盘同步项目目录 |
| T4 | **无人值守自动发送** | 模型幻觉/重复发送/发错人；夜间打扰；账号风控 | 发送只走 `Gateway._send`：STOP 急停、engine 开关、白名单 **fail-closed**、`send_mode`（auto/confirm/off）、静默时段、频率/日限额、内容正则 | 生产建议 `send_mode=confirm` 或先 `dry_run=true`；只把必要联系人放进白名单 |
| T5 | **远程访问** | 网关/控制台端口暴露到局域网或公网 | 默认只监听 `127.0.0.1`；`/api/*` 需要 `admin_token`；弱 token 不被接受（启动自动轮换） | 不要改成 `0.0.0.0`；不要把 token 放进 URL/截图 |
| T6 | **供应链/工具脚本** | 依赖投毒、`tools/` 下脚本被诱导执行 | 依赖版本在 PyPI 逐条核对；工具默认 dry-run、白名单路径、失败不静默 | 只从本仓库/官方源安装；跑 `--apply` 前先看 `--dry-run` 输出 |

## 2. 本机敏感数据清单（迁移/备份/清理都按这张表）

| 路径 | 内容 | 敏感度 | 默认留存 | 清理/迁移 |
| --- | --- | --- | --- | --- |
| `agi/data/secrets.json` | API key、WCDB `db_keys`、图片 AES/xor 密钥 | **极高**（等于账号数据钥匙） | 长期（迁移后） | 只加密备份；`harden_acl.py` 收紧 |
| `config.json` | 采集配置；迁移前含 `db_keys`/`image_aes_key` | 高（迁移前） | 长期 | `rotate_secrets.py --apply --purge-plaintext` |
| `web_config.json` | 网页端 LLM 配置；迁移前含 `api_key` | 高（迁移前） | 长期 | 同上（`llm.api_key`） |
| `log/messages.jsonl` | 全量消息（JSONL，带 `ts`） | 高（聊天内容） | TTL 30 天 | `prune_chat_logs.py --apply` |
| `log/聊天记录.txt` | 同上纯文本版（**逐行无时间戳**） | 高 | 按整文件 mtime | 整个文件超期才删（见工具说明） |
| `media/` | 解密出的图片/贴纸/GIF | 高 | TTL 30 天（`store.cleanup` 另有 7 天口径） | `prune_chat_logs.py --apply` |
| `agi/data/`（自学习库/草稿/画像） | 记忆、草稿、`desktop_profile`、`gateway.log` | 中高 | 草稿 TTL（drafts） | 不自动删画像；按需手动 |
| `agi/data/wechat/STOP` | 急停开关（有即停） | — | — | 手工创建/删除 |

> `agi/data/secrets.json`、`config.json`、`web_config.json`、`log/`、`media/`、`agi/data/`
> 全部在 `.gitignore` 里；`tools/check_secrets.py` 只扫 **git 已跟踪** 文件，
> 因此「本地有密钥」不算失败，「密钥进了仓库」才算失败（`exit 1`）。

## 3. 密钥来源与轮换

### 3.1 来源优先级（DeepSeek API key）

1. 环境变量 `DEEPSEEK_API_KEY`（推荐：不落盘、可随时换）；
2. `agi/data/secrets.json` 的 `llm.api_key`（路径可用 `SOULAGENT_SECRETS` 覆盖）；
3. `web_config.json` 的 `llm.api_key`（旧部署兼容；**命中时启动会打一条告警**，日志里不含 key）。

`db_keys` / `image_aes_key` 这类没法放环境变量的密钥：迁移后只在
`agi/data/secrets.json`；`wx_core/config.py`（读）、`webui/llm.py`、`wx_core/web_search.py`
已接入，**默认行为不变**（没有密钥文件时仍读旧文件）。

### 3.2 轮换步骤（怀疑泄漏时按顺序做）

```bat
:: 0) 先看计划，不动文件
python tools/rotate_secrets.py --dry-run

:: 1) 复制到密钥库（默认只复制 + 收紧 ACL，旧文件不动）
python tools/rotate_secrets.py --apply

:: 2) 到 DeepSeek 控制台作废旧 key、生成新 key，设成环境变量
setx DEEPSEEK_API_KEY "<新 key>"
:: （新开终端生效；确认聊天/搜索可用）

:: 3) 校验密钥库已有副本后，清空旧文件里的明文
python tools/rotate_secrets.py --apply --purge-plaintext

:: 4) 收紧 ACL；复核
python tools/harden_acl.py --apply
python tools/harden_acl.py --verify

:: 5) 确认仓库里没有密钥
python tools/check_secrets.py
```

微信侧密钥（`db_keys`/`image_*`）如果怀疑泄漏：删除 `config.json` 里对应字段或
`agi/data/secrets.json` 的 `config` 段，重启程序让它重新从微信内存**只读**提取一次
（`auto_extract_image_key=true`，见 `wx_core/wechat_keys.py`）。**重要**：换微信账号后
旧 `db_keys` 会失效，属于正常现象，不是 bug。

### 3.3 回滚

`rotate_secrets.py --apply` 只复制、不清空，本身无破坏性；只有 `--purge-plaintext`
会改旧文件。回滚方式：把 `agi/data/secrets.json` 里的值手工填回对应文件的同名字段，
或直接重启程序让密钥重新提取。工具在每次 `--apply` 后都会打印回滚步骤。

## 4. ACL 收紧（Windows）

Windows 上 `chmod 600` 是空操作，必须用 ACL：

```bat
python tools/harden_acl.py --check     :: 只读盘点（默认）
python tools/harden_acl.py --apply     :: 切断继承：当前用户 / SYSTEM / Administrators
python tools/harden_acl.py --verify    :: 只读复核；仍有 Authenticated Users 等不可信主体 → exit 1
```

范围（存在才处理）：`config.json`、`web_config.json`、`log/`、`media/`、`agi/data/`、
`agi/data/desktop_profile/`、`.data/`、`star/`。目录授 `(OI)(CI)F`，文件授 `F`。
拿不到当前用户 SID（`whoami` 失败）时不改 ACL 并返回非零（`--verify`/`--strict`）。

手动等价命令（管理员终端，把 `<路径>` 换成实际路径）：

```bat
icacls "<路径>" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" /grant:r "*S-1-5-18:(OI)(CI)F" /grant:r "*S-1-5-32-544:(OI)(CI)F"
```

## 5. 留痕与停止线

### 5.1 留痕（能查、能复盘）

- 网关事件：`agi/data/wechat/gateway.log`（`send_blocked`/`send_failed`/`proactive_send`/
  `fallback_unverified` 等 kind）；草稿：`agi/data/wechat/drafts.jsonl`；
- 消息原始记录：`log/messages.jsonl`（带 `ts`，便于按时间清理）；
- 页面操作：`agi/data/console_audit.log`；
- 合规静态审计：`python tools/audit_compliance.py`（未受控发送路径/DB 写/微信目录写 → FAIL）。

### 5.2 停止线（满足任一立即停）

1. 微信出现「登录异常/账号受限/操作频繁」等提示，或收到平台警告；
2. 白名单为空时**任何**发送都不该发生（fail-closed：`error=no_allowlist`，审计事件
   `send_blocked/no_allowlist`）——如果你看到「白名单空但发出去了」，按漏洞上报；
3. 出现误发/重复发送/发错人，或模型输出明显不当；
4. 设备/账号不是本人，或目录被同步到网盘/共享盘；
5. 怀疑密钥泄漏（仓库里出现 `sk-`/`db_keys`、`check_secrets.py` 报红）。

停法（从硬到软）：

```bat
:: 最硬：STOP 文件（存在即停一切外发）
echo. > agi\data\wechat\STOP
:: 次之：配置里 stop=true / send_mode=off（改完等热加载或重启）
:: 再次：engine_enabled=false / auto_reply=false
:: 排障：dry_run=true（只记录不发送）
```

`dry_run=true` 时网关只记录「本应发送」的内容，不做真实发送；它**不能**验证
真实发送链路的可用性（那是两件事）。

## 6. 无人值守发送的默认闸门（现状）

| 闸门 | 现状 |
| --- | --- |
| 白名单 | **fail-closed**：`allowed_contacts` 为空 → 拒绝发送（`no_allowlist`） |
| `send_mode` | `off` / `confirm`（落确认草稿，需人工 `confirm=true` 批准）/ `auto` |
| 频率 | 主动 `rate_per_minute=3`、响应 `reactive_rate_per_minute=8`、`hourly_limit=40`、`daily_limit=200`（默认值，可按需下调） |
| 静默时段 | `quiet_hours`，到点不自动发 |
| 内容 | 敏感内容/本机路径/私钥/凭证样式正则拦截 |
| 管理 API | 只监听 `127.0.0.1`；需要 `admin_token`；**弱 token（空/`8888`/过短）不被接受**，启动时自动轮换为长随机 token |
| 审批接口 | `/api/approve`、`/api/draft/approve` 要求显式 `confirm=true` + 白名单，不接受 URL 里的 `?token=` 传参 |

## 7. 漏洞边界（这些不是「已被修复」的承诺）

安全文档最怕自欺。以下都是**已知限制**，不是缺陷隐瞒：

1. **`fallback_unverified` 不能证明送达**。窗口/UIA 兜底发送成功只代表「本机把动作发出去了」，
   结果标记为 `unverified`、`verified=False`、不计入 `sent`。看到 `ok=true` 不等于对方收到。
2. **数据库直读/OCR 是尽力而为**。微信升级、窗口改版、OCR 识别错误都会让内容失真或缺失；
   这些不是安全承诺。
3. **进程内存只读提取密钥依赖微信内部结构**。取不到时功能降级（图片解密不可用），工具不会
   写内存/注入/Hook/minidump；但「不写」不等于「平台允许」。
4. **`check_secrets.py` 是模式匹配，不是熵分析**。它会漏（格式新颖的凭证）也会误报（长 hex 串）；
   它是「提交前闸门」，不是「仓库绝对干净」的证明。
5. **`harden_acl.py --verify` 只检查本工具的白名单路径**，且按 icacls 显示的名字识别主体；
   不在列表里的目录、或名称被本地化/重命名的主体，不在检查范围内。
6. **明文密钥迁移是「可选加固」**。不迁移时程序照旧从 `web_config.json`/`config.json` 读，
   安全收益取决于你是否真的执行并轮换了旧 key。
7. **本工具不防「已经拿到你 Windows 账户的人」**：同一账户下的进程可以读你的文件、环境变量和内存。

## 8. 怎么报漏洞

**请不要在公开 issue 里贴漏洞细节、聊天记录截图、凭证或可复现的攻击脚本。**

1. 仓库页面 → **Security** → **Report a vulnerability**（Private vulnerability reporting）；
2. 若该功能不可用，在仓库 Issue 里发一条**不含细节**的「安全联系请求」，维护者会回复私密联系方式。

报告请带上：受影响版本/commit、操作系统；复现步骤（用假数据，别贴真实聊天内容或 key）；
影响判断（能读到什么？能发出什么？能否持久化？）；希望的修复方向（可选）。

## 9. 响应时间（尽力而为）

- **7 天内**确认收到并给出初步判断；
- 确认有效后尽量在 **30 天内**给出修复或缓解（复杂问题同步进度）；
- 修复发布后可在 `CHANGELOG` 致谢（不公开你的个人信息）。

## 10. 适用范围

**在责任范围内的：** 代码导致的本地数据泄漏；凭证处理问题（API key/`admin_token`/Cookie
出现在日志、仓库、发送内容里）；发送闸门绕过（白名单/STOP/静默/频率/内容拦截）；
远程访问绕过；工具脚本注入；依赖供应链。

**不在范围内的：** 微信客户端/服务器自身漏洞（报腾讯）；你自己把 `config.json`/`agi/data/`/key
提交到公开仓库造成的泄漏（请立刻轮换并清理历史）；用本项目操作**非本人账号**、群发营销、
骚扰他人等滥用；社会工程/物理接触/已被 root 的机器。

## 11. 我们不会做的事

- 不会要求你提供微信密码、支付信息或他人的聊天记录；
- 不会提供「绕过微信风控/多开/群控」的能力；
- 不会为「用它替我操作别人账号」的请求提供帮助。

## 12. 披露政策

- 修复发布前请不要公开细节（给维护者留时间）；
- 修复发布后欢迎公开讨论（可引用本文件与 `CHANGELOG`）。