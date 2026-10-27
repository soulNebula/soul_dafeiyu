# wx_box 使用说明（小白版）

> **一句话介绍**：wx_box 是一个运行在你自己电脑上的**微信好友消息查看器**。在**你本人明确授权**下，它默认采用**数据库直读**：只读解密你自己电脑上的微信消息库与本地缓存，把勾选好友的消息精确整理到独立窗口——**文字是数据库原文**（含 `[微笑]`，不再靠 OCR）、图片/表情包/文件/语音时长与消息一一对应、原始媒体离线解密（实测与微信"另存为"导出的原图 sha1 完全一致、贴纸 `wxgf` 转动画 GIF），并输出纯文本聊天记录 `log/聊天记录.txt`。数据库不可用时自动回退到"窗口截图 + 本地 OCR"模式。按你的要求，**默认不再截图保存图片/表情**。
>
> **它只读，永远不会发送消息**。不写微信内存、不注入、不 Hook、不修改微信数据库、不向微信目录写入。唯一接触内存的地方是"**一次性只读**扫描密钥"（图片/数据库密钥，默认开启、可关闭，取到即缓存、之后不再读内存）。技术边界详见 [风控与合规说明](风控与合规说明.md)，遇到问题先看 [常见问题](常见问题.md)。

---

## 一、环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（64 位） |
| Python | 3.10 及以上，本机为 **Python 3.14**，路径 `C:\Python314\python.exe` |
| 微信 | 微信电脑版 **4.1.15.56**（进程 `Weixin.exe`），已登录并停在主界面 |
| 屏幕 | 建议 1280×720 以上；多显示器/高 DPI 也能用（程序启动时会自动适配） |
| 网络 | 安装依赖时需要联网；**运行时不联网、不上传任何数据**。OCR 的 ONNX 模型随 pip 包安装，首次运行**不需要**再下载模型 |

> ⚠️ **窗口最小化说明**：默认的**数据库直读**不需要窗口画面，**微信最小化也能继续监听**；但"同步好友""自动切换会话（观感）"和回退后的 OCR 模式仍需要微信窗口可用。建议同步好友时保持窗口在桌面，监听阶段可以最小化。

## 二、安装（只需一次）

### 1. 确认 Python

打开"命令提示符"（开始菜单搜索 `cmd`），输入：

```bat
C:\Python314\python.exe --version
```

应输出 `Python 3.14.x`。如果没有这个路径，请先安装 Python（安装时勾选 tcl/tk）。

### 2. 安装依赖

在**项目目录**下打开命令提示符（方法：文件资源管理器地址栏输入 `cmd` 回车），执行：

```bat
C:\Python314\python.exe -m pip install -r requirements.txt
```

国内网络慢可加清华镜像：

```bat
C:\Python314\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 首次安装会下载 RapidOCR 的 ONNX 依赖（onnxruntime、opencv 等，合计数百 MB），请耐心等待；**安装完成后运行不需要联网**。
> `requirements.txt` 里是本机实测版本：Pillow 12.3.0 / psutil 7.2.2 / numpy 2.5.2 / rapidocr-onnxruntime 1.2.3（自动带入 onnxruntime 1.31.0、opencv-python 5.0.0.93 等）/ pycryptodome 4.1.0（图片与贴纸解密）/ imageio-ffmpeg 0.6.0（内置 ffmpeg 7.1，`wxgf` 转动画 GIF，无需另装 ffmpeg）。若在其他 Python 版本上 pip 解析失败，可把 `==` 改成 `>=` 或直接使用 Python 3.14。
>
> ⚠️ **首次运行会尝试一次性只读提取图片密钥**（默认开启，见第七节说明）：需要与微信**相同的运行权限**（如果微信以管理员身份运行，本程序也要用管理员启动），否则可能提取失败。失败不影响文字监听；按默认"不截图"策略，图片/表情匹配不到原文件时**不保存截图**，只在日志区警告。

### 3. 合规自检（建议执行）

```bat
C:\Python314\python.exe tools\audit_compliance.py
```

看到 `结果：PASS` 和退出码 `0` 即可放心使用（实际输出见本文最后的"合规自检"一节）。

## 三、启动

**方法一（推荐，双击）**：双击项目根目录的 **`run.bat`**。
它会自动进入项目目录、找到 Python、检查依赖（缺了会自动装）、然后启动程序。

**方法二（命令行）**：

```bat
cd /d C:\Users\Administrator\Desktop\SoulAgent
C:\Python314\python.exe app.py
```

> 若系统已把 Python 加入 PATH，直接 `python app.py` 也可以。
> 想先不连微信、只看界面长什么样？执行 `C:\Python314\python.exe app.py --fake`（或先 `set WXBOX_FAKE=1` 再 `python app.py`），会进入假数据演示模式：自动生成文字/图片/GIF/文件/语音五类示例消息。

## 四、首次使用（三步）

> **开始前的前提（重要）**：
> 1. 微信电脑版已**登录**；**同步好友**时窗口需要开着（不要最小化到托盘）。**监听阶段默认数据库直读，窗口最小化也能继续监听**；若数据库直读不可用回退 OCR，则需要窗口保持非最小化。
> 2. 监听期间，窗口可用时程序会自动把微信**切换到被监听好友的会话**（只影响观感，不影响读库）；它**只读、不会发送任何消息**。你在微信里手动操作的节奏可能被打断，建议先看完本节再开始。

1. **同步好友**：先确保微信电脑版已登录、窗口没有最小化。启动 wx_box 后，点左侧好友区顶部的"**重新同步好友**"按钮。程序会临时打开微信"通讯录"，用真实滚轮逐屏翻页读取好友名单（默认上限 120 次滚动 / 90 秒），完成后自动切回原来的聊天。
   - 同步时微信会被**短暂切到前台、鼠标会自己动**（结束后自动恢复原窗口和光标位置），期间**不要抢鼠标、不要操作微信**；
   - 好友很多时同步需要十几秒到一分多钟，属正常；日志会显示进度（如"已同步 120/380"）和结束原因。
2. **勾选好友**：在左侧好友列表里用搜索框筛选，勾选你想监听的好友（可多选）。可以点"全选/清空"快速调整。
3. **开始监听**：点底部"**开始监听**"。之后被勾选好友发来的消息就会出现在右侧消息区。
   - **默认数据库直读**（`db_direct=true`）：首次监听会**一次性只读**扫描微信数据库密钥（图片密钥 + 每个库自己的 WCDB 密钥，默认开启、可关闭；成功日志显示"数据库直读已启用"，之后不再读内存、二次启动秒开）；
   - 文字是数据库原文（含 `[微笑]` 等），图片/贴纸/文件/语音时长与消息一一对应；图片/贴纸解密成原始格式，文件复制原文件，语音显示如 `[语音] 2秒`；
   - 按默认"不截图"策略（`allow_screenshot_media=false`），原文件匹配不到时**不保存截图**，只在日志警告；
   - 同时写纯文本聊天记录 `log/聊天记录.txt`（严格每行一条：`好友名: 内容` / `我: 内容`，媒体记为 `[图片]/[表情包]/[文件]/[语音]`，emoji 已转中文名）；
   - 数据库不可用（密钥失败/库被锁/微信未登录/群聊找不到 wxid）时**自动回退 OCR 模式**；也可在 `config.json` 设 `db_direct:false` 强制 OCR。

> 程序一次只"看"一个会话：如果收到监听好友的新消息，开启"自动切换"时它会自动把微信切到该好友会话（配置项 `auto_switch`，默认开启）。切换失败会提示你手动打开。

## 五、界面说明

```
┌──────────────────────────────────────────────────────────────┐
│ 顶部状态栏：微信连接(已连接/未连接) · 当前会话标题 · 监听状态 · 错误提示 │
├─────────────────────┬────────────────────────────────────────┤
│ 左侧：好友列表        │ 右侧：消息流                            │
│  · 好友总数徽标       │  · 发送者（对方/我）+ 时间 + 类型标签     │
│  · 搜索框            │  · 文字气泡                             │
│  · 复选框多选         │  · 图片 / 表情包内联预览（GIF 自动播放）  │
│  · 全选 / 清空        │  · 文件卡片（文件名 + 大小 + 图标）       │
│  · 重新同步好友按钮   │  · 语音消息显示"语音消息（未接收）"       │
├─────────────────────┴────────────────────────────────────────┤
│ 底部控制：开始监听 / 停止监听 · 清空消息 · 打开媒体目录            │
├──────────────────────────────────────────────────────────────┤
│ 日志区：按级别着色（info 普通 / warn 警告 / error 错误），自动滚动    │
└──────────────────────────────────────────────────────────────┘
```

| 区域 | 说明 |
|---|---|
| 顶部状态栏 | 左侧"微信已连接/微信未连接"表示是否找到微信窗口（未连接时检查微信是否登录、是否最小化）；中间显示"当前会话"（你正在查看的本地视图）和监听状态；右侧红字是错误提示。演示模式会显示"演示模式（假后端）"徽标。 |
| 左侧好友列表 | 显示"好友总数: N"；搜索框按名字即时过滤；点好友名字=在右侧查看该好友的消息，勾选=加入监听名单；"全选"只作用于当前搜索结果；"清空"取消全部勾选；"重新同步好友"重新扫描通讯录。 |
| 右侧消息流 | 默认显示"全部好友"，点"查看全部"可回到总视图；右上角显示当前条数。文字与小黄脸 emoji 优先显示微信"复制"得到的精确原文；图片优先预览**解密出的原始 PNG/JPG/GIF**（`media/originals/`），表情包贴纸预览**解密出的原始 GIF/PNG**（`media/stickers/`）；按默认不截图策略，匹配不到原文件时不保存截图，只在日志警告。文件显示文件名+大小+图标；语音显示"语音消息（未接收）"。 |
| 底部控制 | "开始监听"（监听中变为"停止监听"，停止后会一起停掉后台采集线程，再点开始即重新启动）；"清空消息"只清界面与本次运行的内存记录，**不删除** `media/` 文件和 `log/messages.jsonl`；"打开媒体目录"打开 `media/` 文件夹。 |
| 日志区 | "运行日志"：程序状态、警告和错误按级别着色，遇到问题先看这里；右上角可"清空日志"。 |

## 六、数据库直读（默认）

> 用户已明确授权（"可以忽略之前的限制"、"可以的，你先跑"）：本模式**只读**解密你自己账号的微信数据库，不写回、不发送消息。默认开启（`db_direct=true`）。

### 6.1 原理：一次性只读密钥 + 本机临时目录解密

1. **一次性只读扫描内存**：定位社区公开的 `com.Tencent.WCDB.Config.Cipher` 对象链，用 27 字节 XOR 掩码还原 `x'<64hex key><32hex salt>'`，再对**每个库**用页 1 HMAC-SHA512 强校验匹配，得到每个库自己的密钥（本机实测 7 个库）。扫描只做一次，成功即缓存、之后不再读内存。
2. **SQLCipher4 逐页解密**：AES-256-CBC、page=4096、reserve=80；再叠加 **WAL 帧**拿到最新未落盘消息；遇到微信正在写 WAL 的竞态会自动退化为"只解主库"（数据稍旧但一定可用）。
3. **只写本机临时目录**：解密结果写到 `tempfile` 创建的 `wxdb_*` 临时目录，用 `sqlite3` 的 **只读连接 `mode=ro`** 读取；**从不写回/修改微信数据库，也不写微信任何目录**。
4. **密钥缓存**：缓存到 `config.json` 的 `db_keys`（含 key/salt/layout），二次启动秒开；缓存会自动用页 1 HMAC 校验，失效则自动重扫。

### 6.2 消息一一对应（不再靠 OCR/猜测）

| 类型 | 数据来源 | 效果 |
|---|---|---|
| 文字 | 数据库原文 | 精确原文（含 `[微笑]` 等），不再 OCR / 剪贴板 |
| 图片 | 消息 XML 的 md5/长度 + 本地 `msg/attach/.../Img/*.dat` 尺寸指纹 | 精确匹配后本地解密成原图（PNG/JPG/GIF）→ `media/originals/` |
| 表情包 | 消息 XML 的 md5 → `cache/<月>/Emoticon/<2位>/<md5>` | 精确对应后解密成动画 GIF / 原 PNG → `media/stickers/` |
| 语音 | 消息 XML 的 `voicelength`（毫秒） | `[语音] 2秒`（不用显示/猜测） |
| 文件 | appmsg 的 title | 从 `msg/file/` 复制原文件 → `media/files/` |
| 发送者 | message 库的 `Name2Id` 映射 | 准确区分「我 / 对方」 |

### 6.3 实测证据（白云会话 16 条消息）

- **文字精确**：`何工，参数这边客户没问题，剩下你cp过来就行`（我）/ `你去看看`（对方）
- **图片**：`media/originals/1e0df822...png`、`0c5b3ded...png`、`1463973401...png`（均为本地解密原图）
- **表情包**：`media/stickers/d8a0d235d854ab3fbe7694add4631b25.gif`（消息 XML 的 md5 与缓存文件 md5 完全一致）
- **语音**：`[语音] 2秒`（来自 `voicelength=1645ms`）
- **文件**：`media/files/BOM_Board1_Schematic1_2026-10-19.xlsx`
- **聊天记录**：`log/聊天记录.txt` 严格逐行，如 `我: [微笑]`、`白云一片去悠悠: [表情包] d8a0....gif`

### 6.4 配置项

| 键 | 默认值 | 含义 |
|---|---|---|
| `db_direct` | `true` | 数据库直读（推荐）。设为 `false` 强制回退 OCR 模式 |
| `db_history_count` | `20` | 首次直读时补发的历史条数（需 `emit_history_on_start=true`） |
| `db_poll_limit` | `50` | 每次轮询最多读取多少条新消息 |
| `db_keys` | `{}` | 数据库密钥缓存（自动提取后写入；**敏感信息，不要分享**；删除后自动重扫） |

### 6.5 回退机制与窗口要求

- **自动回退**：数据库不可用（密钥提取失败/数据库被锁/持续 WAL 竞态/微信未登录/好友是群聊找不到 wxid）时，自动回退 OCR 模式并在日志 warn；
- **强制 OCR**：`config.json` 设 `db_direct:false` 后重启即可；
- **窗口要求**：数据库直读**不需要窗口画面，微信最小化也能监听**；窗口可用时仍会自动切到被监听好友会话（只影响观感）。同步好友、以及 OCR 回退模式仍需要微信窗口非最小化。

### 6.6 隐私与风险

- `db_keys` 含数据库密钥，能解密你的聊天库，**不要分享 `config.json` 或项目目录**；删除 `db_keys`（或整个 config.json）后下次启动会自动重扫；
- 解密在本机临时目录（`wxdb_*`）进行，建议退出后清理系统临时目录里的 `wxdb_*` 残留；
- 聊天内容（`log/聊天记录.txt`、`log/messages.jsonl`）只在本机处理、不上传；
- 微信升级可能改变数据库结构/密钥链，届时可能回退 OCR；
- **仍然禁止**：写回/修改微信数据库、SQL 写操作、向微信目录写文件、发送消息——这些在合规审计里都会 FAIL。

## 七、文件与目录

所有数据都在**项目自己的目录**里，不会写入微信目录：

| 路径 | 内容 | 说明 |
|---|---|---|
| `media/` | 截图/合成 GIF（仅调试时 `allow_screenshot_media=true` 才会生成） | 默认不截图，通常为空或只有调试产物 |
| `media/originals/` | **解密出的原始图片/GIF**（原始扩展名 `.png/.jpg/.gif/.webp` 等） | 图片解密成功时保存在这里，而不是截图 |
| `media/stickers/` | **解密出的表情包贴纸**：`<md5>.gif/.png/.jpg` + `<md5>.wxgf`（微信原始 HEVC 容器） | 动画贴纸的 GIF 由内置 ffmpeg 从 `wxgf` 转码 |
| `media/files/` | 文档原文件（从微信缓存 `msg/file` 复制） | 保留原文件名 |
| `log/聊天记录.txt` | **纯文本聊天记录**（UTF-8，严格每行一条：`好友名: 内容` / `我: 内容`） | 媒体记 `[图片]/[表情包]/[文件]/[语音]`；emoji 转中文名如 `[微笑]`；无表头/分隔线 |
| `log/messages.jsonl` | 结构化消息记录，每行一条 JSON（UTF-8） | 用于排错/回溯，可随时删除 |
| `config.json` | 配置（监听名单、密钥缓存、直读开关等） | 首次启动自动生成；**含图片/数据库密钥（`image_*`、`db_keys`），不要分享/上传**；删除后恢复默认（密钥会重扫） |
| `docs/` | 本说明、合规说明、常见问题 | 文档 |
| `wx_core/`、`ui/`、`app.py` | 程序源码 | 开发者说明见下节 |

常用配置（`config.json`，修改后需重启程序）：

| 键 | 默认值 | 含义 |
|---|---|---|
| `poll_interval` | `1.2` | 轮询间隔（秒），**不要小于 1** |
| `auto_switch` | `true` | 有新消息时是否自动切到被监听好友会话 |
| `auto_restore_window` | `true` | 检测到微信最小化时，是否尝试静默恢复窗口（不抢焦点） |
| `allow_foreground_switch` | `true` | 是否允许"短暂切前台 + 搜索框输入"的兜底切换方式 |
| `rotate_interval` | `6.0` | 监听多个好友时的轮换间隔（秒） |
| `capture_gif` | `true` | 仅当 `allow_screenshot_media=true`（调试用）时，截图合成动图的兜底开关 |
| `allow_screenshot_media` | `false` | 默认**不截图**（用户要求）。设为 `true` 才允许把图片/表情保存为截图或合成 GIF（仅调试） |
| `emit_history_on_start` | `false` | 首次抓帧是否把已有历史消息也推出来（默认关闭，避免刷屏） |
| `save_media` | `true` | 是否把媒体保存到 `media/` |
| `max_messages` | `3000` | 界面最多保留多少条消息 |
| `friends_cache` | `[]` | 上次同步的好友名缓存（启动时先显示，避免列表空白） |
| `db_direct` | `true` | 默认**数据库直读**：文字精确、媒体与消息一一对应、窗口最小化也能监听。设为 `false` 强制回退 OCR |
| `db_history_count` | `20` | 首次直读时补发的历史条数（配合 `emit_history_on_start=true`） |
| `db_poll_limit` | `50` | 每次轮询最多读取多少条新消息 |
| `db_keys` | `{}` | 数据库密钥缓存（自动提取后写入；**敏感信息，不要分享**；删除后自动重扫） |
| `auto_extract_image_key` | `true` | **一次性只读**从微信内存提取图片密钥（用户已授权）。设为 `false` 可完全关闭内存读取，改用你手填的密钥 |
| `image_xor_key` | `""` | 图片 `.dat` 的单字节异或密钥（自动提取后写入；也可手填十进制/`0x` 十六进制） |
| `image_aes_key` | `""` | 图片 `.dat` 的 16 字节 AES 密钥（自动提取后写入；也可手填 ASCII） |
| `image_wxid` | `""` | 密钥所属账号（自动提取时记录，仅作参考） |
| `image_cfg_dword` | `""` | 表情包密钥派生用的 seed（= 图片的 `cfgDword`，自动提取时写入） |
| `decrypt_media_max_age` | `86400.0` | 解密时回看多少秒内的新 `.dat` 缓存（秒） |
| `decrypt_recent_within` | `60.0` | 新图片优先匹配"最近 N 秒内写入的 `.dat`" |
| `export_original_media` | `false` | 可选：用微信自带"另存为"导出原图/原 GIF（默认关闭；慢，且会短暂操作微信界面） |
| `export_timeout` | `12.0` | 单条"另存为"导出超时（秒） |
| `copy_text_exact` | `true` | 文本/小黄脸消息用微信"右键→复制"取精确原文（含 `[微笑]`），失败回退 OCR |
| `copy_text_timeout` | `8.0` | 单条精确复制超时（秒） |

> **关于本地缓存、数据库、原文件与密钥（用户授权）**：
> - **数据库直读（默认）**：只读解密你自己账号的 `db_storage` 微信数据库到本机临时目录、`sqlite3 mode=ro` 读取，详见第六节；`db_keys` 存密钥、**不要分享**。
> - **文档原文件**：只读微信缓存 `Documents\xwechat_files\<你的wxid>\msg\file\<yyyy-mm>\*`（明文），复制到 `media/files/`，保留原文件名。
> - **图片/GIF 原文件**：本机实测已能解密。首次需要密钥时，程序从 `Weixin.exe` 内存**一次性只读**提取 `cfgDword + wxId`，派生 `imageAesKey = md5(str(cfgDword)+wxId)[:16]`、`imageXorKey = cfgDword & 0xFF`，然后解密最近的 `.dat` 缓存（V1/V2/V3）为原始 `.png/.jpg/.gif`；实测解出的 PNG 与微信自带"另存为"导出的原图 **sha1 完全一致**（1727×1246，311021 字节）。密钥成功后缓存到 `config.json` 的 `image_aes_key`/`image_xor_key`，之后解密完全是本地运算、**不再读内存**。
> - **表情包（贴纸）原文件**：本机实测已能解密。密钥 `key = md5(f"{seed}{wxid}EMOTICON").digest()[:16]`（`seed` = `image_cfg_dword`），AES-128-CBC（key 同时作 IV，PKCS7）；解出的 PNG/GIF/JPG 原样保存，`wxgf`（微信 HEVC 动画容器）用内置 ffmpeg 转成**动画 GIF**，同时保留原始 `.wxgf`。实测 `cache/2026-10/Emoticon/d8/d8a0...` 解出 `wxgf`（131332B）→ GIF 300×318、11 帧，与聊天里显示的表情逐帧对应。文件在 `media/stickers/<md5>.gif / .png / .jpg` + `<md5>.wxgf`；索引是增量的，只解密新增/变更的缓存文件。
> - **贴纸匹配策略（三层，宁缺勿错）**：① **时间戳绑定**——实时收到新贴纸时微信会在几秒内写入新缓存文件；若"最近 180 秒内新增、且未被使用过"的贴纸**唯一**，直接认定（实时监听下≈1:1 精确）；② **视觉匹配**——候选不唯一时按抽样帧差打分，且**优先只在时间候选里选**；同一贴纸文件用 `used` 集合防重复绑定；③ **歧义保护**——第一、第二候选分数太接近时**不猜、返回空**（不保存截图、绝不错配），日志 warn。因此**历史回放/多个相似贴纸可能匹配不上**；要做到 100% 精确需读取消息库里的 md5 绑定（列为后续计划）。
> - **内置小黄脸 Emoji**：小 emoji 消息直接"右键→复制"读剪贴板，微信返回 `[微笑]`，写入聊天记录；`wx_core/builtin_emoji.py` + `wx_core/emoji_assets/`（109 个微信表情 PNG，素材来自 npm `wechat-emojis`）作为可选的图像识别兜底。
> - **默认开启、可关闭**：`auto_extract_image_key=true` 是默认值（用户已授权）；设为 `false` 后程序不会再读微信内存，可手填密钥。读取只发生在首次、只读、取到即关闭句柄，**不写内存、不注入、不 Hook、不碰 SQLite 数据库、不写微信目录**。
> - **默认不截图**：`allow_screenshot_media=false`（用户要求）。图片/表情原文件解密或匹配不到时，不保存截图、不合成 GIF，只在日志警告；仅在调试时才把它设为 `true`。
> - **密钥属于敏感信息**：`image_aes_key` / `image_xor_key` / `image_cfg_dword` 能解密你自己账号的图片与贴纸缓存，**不要分享 `config.json` 或项目目录**；删除后会重新提取。

## 八、退出

- 直接点 wx_box 窗口右上角的 **×** 关闭即可，后台监听线程会一起退出。
- 若从命令行启动，关闭窗口后命令提示符会回到提示符；按 `Ctrl+C` 也可中断。
- 极端情况下窗口无响应：任务管理器结束 `python.exe` 进程即可。退出不会影响微信本身，也不会留下后台服务。

## 九、合规自检

项目自带一个只读的静态审计脚本，扫描所有产品源码，确认没有**写内存、注入、Hook、数据库写操作、向微信目录写文件**等越界行为；所有用户授权项（只读缓存、图片/贴纸解密、**一次性只读密钥扫描**、**数据库只读直读 `mode=ro`**、剪贴板/窗口消息/内置 emoji 素材辅助）都会在报告里如实列出（含文件:行号），不冒充"零内存访问"：

```bat
C:\Python314\python.exe tools\audit_compliance.py
```

（若 `python` 已在 PATH 中，等价命令：`python tools\audit_compliance.py`。在项目目录下执行。）

当前实测输出（2026-10-22，项目源码定型后执行，命令即上方第一条）：

```text
==================================================================
wx_box 合规静态审计（只读 / 不注入 / 不写数据库 / 授权只读直读）
==================================================================
项目根目录 : C:\Users\Administrator\Desktop\SoulAgent
扫描文件数 : 31 个 .py
已排除目录 : .dsh-ops、.git、.idea、.mypy_cache、.pytest_cache、.venv、.vscode、WechatMoments、__pycache__、_refs、build、dist、env、log、logs、media、node_modules、recon、reference_weflow、venv
已排除文件 : tools/audit_compliance.py（自身 pattern 表含禁用字样）
素材目录   : wx_core/emoji_assets/（109 个 PNG 随包素材，非 .py，不在扫描范围）
授权只读 : wx_core/media_cache.py、wx_core/dat_decrypt.py、wx_core/emoticon.py（只读本地缓存/解密输入；不写微信目录）
授权辅助 : wx_core/clipboard.py、wx_core/textlog.py、wx_core/media_export.py、wx_core/builtin_emoji.py（窗口消息/剪贴板/纯文本导出/内置 emoji 素材匹配；无禁用 API）
授权密钥 : wx_core/wechat_keys.py、wx_core/wcdb.py（用户显式授权、一次性只读：OpenProcess / VirtualQueryEx / ReadProcessMemory；成功即缓存、之后不再读内存）
授权数据库: wx_core/wcdb.py、wx_core/db_source.py（用户授权：只读 db_storage 微信库、解密到本机临时目录、sqlite3 mode=ro；写 SQL/非只读连接仍 FAIL）

[1/2] 禁用 API / 库扫描（写内存、注入、Hook、DB 写操作；授权只读项豁免）
  [PASS] 未发现越界的 API / 库引用
  [授权] 一次性只读密钥提取（wechat_keys/wcdb，可 auto_extract_image_key=false 关闭）：
         wx_core/wcdb.py:38  打开微信进程句柄（OpenProcess）
         wx_core/wcdb.py:39  打开微信进程句柄（OpenProcess）
         wx_core/wcdb.py:40  读取其他进程内存（ReadProcessMemory）
         wx_core/wcdb.py:70  打开微信进程句柄（OpenProcess）
         wx_core/wcdb.py:92  打开微信进程句柄（OpenProcess）
         wx_core/wcdb.py:99  读取其他进程内存（ReadProcessMemory）
         wx_core/wechat_keys.py:18  读取其他进程内存（ReadProcessMemory）
         wx_core/wechat_keys.py:18  打开微信进程句柄（OpenProcess）
         wx_core/wechat_keys.py:40  打开微信进程句柄（OpenProcess）
         wx_core/wechat_keys.py:41  打开微信进程句柄（OpenProcess）
         wx_core/wechat_keys.py:42  读取其他进程内存（ReadProcessMemory）
         wx_core/wechat_keys.py:156  打开微信进程句柄（OpenProcess）
         wx_core/wechat_keys.py:158  打开微信进程句柄（OpenProcess）
         wx_core/wechat_keys.py:163  读取其他进程内存（ReadProcessMemory）
  [授权] 数据库只读直读（db_storage 读取 / sqlite3 mode=ro；写回微信库仍 FAIL）：
         wx_core/wcdb.py:245  只读引用微信数据库路径（db_storage 等，不写回）
         wx_core/wcdb.py:330  只读连接 sqlite3 mode=ro
         wx_core/wcdb.py:398  只读连接 sqlite3 mode=ro
         wx_core/wcdb.py:409  只读引用微信数据库路径（db_storage 等，不写回）
         wx_core/wcdb.py:414  只读连接 sqlite3 mode=ro
         wx_core/wcdb.py:427  只读引用微信数据库路径（db_storage 等，不写回）
         wx_core/wcdb.py:431  只读连接 sqlite3 mode=ro
         wx_core/wcdb.py:443  只读引用微信数据库路径（db_storage 等，不写回）
         wx_core/wcdb.py:458  只读连接 sqlite3 mode=ro
         wx_core/wcdb.py:468  只读引用微信数据库路径（db_storage 等，不写回）
         wx_core/wcdb.py:515  只读引用微信数据库路径（db_storage 等，不写回）
         wx_core/wcdb.py:516  只读引用微信数据库路径（db_storage 等，不写回）

[2/2] 微信数据/安装目录字符串检查（写入=FAIL；授权只读模块豁免 WARN）
  [PASS] 未发现越界的微信目录引用或写入

------------------------------------------------------------------
结果：PASS —— 严重问题 0 项，警告 0 项
结论：未发现写内存、Hook、注入、数据库写操作或向微信目录写文件的代码。
      授权项：wx_core/media_cache.py、wx_core/dat_decrypt.py、wx_core/emoticon.py
              （缓存只读 / 图片与贴纸离线解密）；
              wx_core/wechat_keys.py、wx_core/wcdb.py（一次性只读密钥提取）；
              wx_core/wcdb.py、wx_core/db_source.py（只读直读微信库、mode=ro、临时目录解密）；
              wx_core/clipboard.py、wx_core/textlog.py、wx_core/media_export.py、
              wx_core/builtin_emoji.py（窗口消息/剪贴板/纯文本/内置 emoji 素材辅助）。
==================================================================
```

退出码：`0` = PASS，`1` = FAIL。审计脚本不 import 项目代码、不启动微信，可随时重复运行。

## 十、开发者说明

### 10.1 技术路线（为什么这样设计）

微信 4.1.15.56 采用 Qt5 自绘界面：UIAutomation 树只有空 Pane，MSAA 也拿不到子元素，因此**元素级自动化不可用**。本项目有两条路线，默认优先数据库直读：

0. **数据库直读（默认，`db_direct=true`）**：一次性只读扫描 `Weixin.exe` 内存拿 WCDB/图片密钥 → SQLCipher 逐页解密到本机临时目录（`mode=ro` 读取）+ 叠加 WAL → 消息与媒体精确对应（见第六节）；数据库不可用时自动回退下面 1~10 的 OCR 路线；
1. `EnumWindows` 找到 `Weixin.exe` 主窗口 → `PrintWindow(PW_RENDERFULLCONTENT)` 抓完整窗口画面（被遮挡也能抓，最小化不行）；
2. OCR：**本地 RapidOCR（ONNX 模型，优先）** 识别画面文字；不可用时自动降级到系统 OCR（`Windows.Media.Ocr`，`zh-Hans-CN`）；文本消息再通过微信"右键→复制"（`PostMessage` + 剪贴板）取**精确原文**，失败回退 OCR；
3. 小黄脸 emoji 消息同样"右键→复制"，微信直接返回 `[微笑]` 等中文名（零截图）；`builtin_emoji.py` + `emoji_assets/`（109 个素材 PNG）作为可选图像识别兜底；
4. 图片/GIF：首次从 `Weixin.exe` 内存**一次性只读**提取密钥（默认开启、可关闭），派生 `imageAesKey`/`imageXorKey` 并缓存；之后用 `dat_decrypt.py`（V1/V2/V3）本地解密最近 `.dat` 为原始 `.png/.jpg/.gif`；
5. 表情包（贴纸）：`emoticon.py` 用 `md5(f"{seed}{wxid}EMOTICON")[:16]` 派生 AES-128-CBC 密钥（key=IV），增量解密 `cache/*/Emoticon`、`business/emoticon` 缓存；`wxgf`（HEVC 动画）用 imageio-ffmpeg 转动画 GIF，并保留原始 `.wxgf`；消息↔贴纸绑定采用"时间戳唯一→视觉帧差→歧义不猜"三层策略（`used` 防重复），历史回放/相似贴纸匹配不上时宁缺勿错；
6. **默认不截图**：`allow_screenshot_media=false`。原图/贴纸匹配不到时只记日志警告，不保存截图、不合成 GIF；仅调试时打开该开关才走旧截图逻辑；
7. 文件：只读微信缓存 `msg/file` 明文文档，复制原文件到 `media/files/`；
8. 用颜色/几何分析定位面板、切分消息气泡、区分图片/表情/文件；OCR 路线额外做"3 分钟内相似度 ≥0.9"的文本模糊去重；
9. 需要切会话/翻通讯录时，用 `PostMessage` 投递窗口级鼠标消息（不抢焦点），失败才用 `SetForegroundWindow + SendInput` 兜底；
10. 产出落盘：`media/originals/`（原图）、`media/stickers/`（贴纸 GIF/PNG + `.wxgf`）、`media/files/`（文档）、`log/聊天记录.txt`、`log/messages.jsonl`。

**红线**：不**写**微信进程内存、不注入 DLL、不 Hook API、不**写回/修改**微信数据库（授权只读解密到临时目录）、不修改微信安装目录与文件、不向微信目录写入、不发送消息。例外是用户显式授权、默认开启且可关闭的"一次性只读密钥/数据库扫描"（只读、取到即缓存并关闭句柄）。

### 10.2 项目文件清单

```
wx_box/
├─ app.py                      # 程序入口
├─ run.bat                     # 双击启动（自动找 Python、检查依赖）
├─ requirements.txt            # 运行时依赖（含实测版本）
├─ config.json                 # 运行时生成：配置
├─ SPEC.md                     # 架构与接口约定
├─ wx_core/
│  ├─ __init__.py  api.py  models.py  config.py
│  ├─ wcdb.py                  # WCDB/SQLCipher 只读直读（密钥/解密/WAL）
│  ├─ db_source.py             # 数据库消息 → Message（精确媒体对应）
│  ├─ win.py                   # 找窗口 / 截图 / 窗口级输入
│  ├─ ocr.py                   # RapidOCR 主引擎 + Windows OCR 降级
│  ├─ layout.py  extract.py    # 布局识别 / 消息抽取
│  ├─ friends.py               # 通讯录翻页枚举好友
│  ├─ wechat_keys.py           # 一次性只读提取图片密钥（授权、可关闭）
│  ├─ media_cache.py           # 只读缓存：文档复制 / 定位图片 .dat
│  ├─ dat_decrypt.py           # .dat 解密（V1/V2/V3，AES+异或，纯本地）
│  ├─ emoticon.py              # 表情包（贴纸）AES-CBC 解密 + wxgf→GIF
│  ├─ builtin_emoji.py         # 内置小黄脸素材匹配（可选兜底）
│  ├─ emoji_assets/            # 109 个微信表情 PNG 素材（非代码）
│  ├─ media_export.py          # 精确复制文本 / 可选"另存为"导出
│  ├─ clipboard.py             # 剪贴板读写（取精确原文后还原）
│  ├─ textlog.py               # 纯文本聊天记录（严格逐行）+ emoji 中文名
│  ├─ monitor.py               # 轮询 / 去重 / 自动切换 / 事件队列
│  └─ store.py                 # media/ 与 log/ 落盘
├─ ui/
│  ├─ __init__.py  main_window.py  widgets.py  dev_backend.py
├─ tools/
│  ├─ audit_compliance.py      # 合规静态审计
│  └─ reference_weflow/        # WeFlow 参考实现（Apache-2.0，仅参考、不运行）
├─ tests/
│  ├─ test_core.py  test_ocr.py  test_friends.py  test_gui_smoke.py
│  └─ fixtures/                # 离线截图样本与测试素材
├─ docs/
│  ├─ README.md                # 本文件
│  ├─ 风控与合规说明.md
│  └─ 常见问题.md
├─ WechatMoments/              # 用户放置的参考项目（不参与运行/审计，交付时可剔除）
├─ _refs/                      # 第三方参考代码归档（不参与运行/审计）
├─ media/                      # 运行时生成：originals/ 原图  stickers/ 贴纸  files/ 文档
└─ log/                        # 运行时生成：聊天记录.txt  messages.jsonl
```

### 10.3 模块职责

| 文件 | 职责 |
|---|---|
| `app.py` | 程序入口：创建 Tkinter 主窗口，注入真实后端（导入失败或 `WXBOX_FAKE=1` 时回退假后端） |
| `wx_core/models.py` | 数据模型：`Friend` / `Message` / `OcrLine`、消息类型常量、事件构造器 |
| `wx_core/api.py` | 后端抽象协议 `Backend`，GUI 只依赖它（事件队列 + 状态快照） |
| `wx_core/config.py` | `config.json` 读写与默认配置（含 `db_direct`/`db_keys`/图片密钥） |
| `wx_core/wcdb.py` | 数据库直读：一次性只读扫描 `com.Tencent.WCDB.Config.Cipher` 取每库密钥（页1 HMAC 强校验）→ SQLCipher4 AES-256-CBC 逐页解密 → 叠加 WAL → 写本机临时目录、`sqlite3 mode=ro` 读取；只读，不写回微信库 |
| `wx_core/db_source.py` | 数据库消息源：会话解析、`Name2Id` 发送者映射、消息 XML → Message；按 md5/尺寸精确匹配并本地解密图片/贴纸、复制文件、解析语音时长 |
| `wx_core/win.py` | Win32 外部观察层：找窗口、DPI、`PrintWindow` 截图、窗口级点击/滚动/按键 |
| `wx_core/ocr.py` | OCR 统一封装：RapidOCR（ONNX）主引擎 + Windows 自带 OCR 降级（PowerShell 5.1 WinRT 常驻 worker），线程安全，返回统一 `OcrLine` |
| `wx_core/layout.py` | 面板定位（聊天区/标题/列表/输入框）、消息气泡切分、颜色分析 |
| `wx_core/extract.py` | 从一帧画面抽取消息块并分类为文本/图片/表情包/文件 |
| `wx_core/monitor.py` | 轮询线程：默认数据库直读（`db_direct`），不可用自动回退 OCR；去重、自动切会话、事件队列 |
| `wx_core/friends.py` | 打开"通讯录"→逐屏 OCR→真实滚轮翻页→去重收集好友名→自动切回"聊天"页；数量与表头校验、超时/最大滚动兜底、结束还原前台窗口与光标 |
| `wx_core/media_cache.py` | 用户授权的只读缓存访问：定位本机微信缓存目录，复制 `msg/file` 明文文档到 `media/files/`；枚举最近图片/表情 `.dat` 提供解密输入（只读，不写微信目录、不 stat 数据库） |
| `wx_core/wechat_keys.py` | 用户显式授权的一次性只读密钥提取：`OpenProcess`/`VirtualQueryEx`/`ReadProcessMemory` 定位 Config.Cipher → `cfgDword+wxId` → `md5` 派生 AES/XOR 密钥，用"能否解密 `.dat`"自校验；成功即缓存到 config、关闭句柄（默认开启，`auto_extract_image_key=false` 可关闭；不写内存/不注入/不 Hook） |
| `wx_core/dat_decrypt.py` | 微信 4.x `.dat` 解密的**纯算法**模块（V1/V2/V3，AES-128-ECB + 单字节异或），不读微信路径/不接触进程，密钥由调用方注入 |
| `wx_core/emoticon.py` | 表情包（贴纸）解密：`md5(f"{seed}{wxid}EMOTICON")[:16]` → AES-128-CBC(key=IV, PKCS7)；**增量**扫描 `cache/*/Emoticon`、`business/emoticon`，`wxgf` 用 imageio-ffmpeg 转动画 GIF 并保留原始 `.wxgf`；**三层匹配**：时间戳唯一绑定（最近 180s）→ 视觉帧差（优先时间候选、`used` 防重复）→ 歧义不猜 |
| `wx_core/builtin_emoji.py` | 内置小黄脸识别（可选兜底）：把聊天里的 emoji 块与 `emoji_assets/` 的 109 个微信表情 PNG 逐像素比较，返回中文名 |
| `wx_core/emoji_assets/` | 随包分发的 109 个微信表情 PNG 素材（来自 npm `wechat-emojis`，Apache/MIT，见其仓库说明；非代码） |
| `wx_core/media_export.py` | 用 `PostMessage` 模拟右键菜单：`copy_message_text` 拿精确文本（+剪贴板还原）；`export_block` 用微信"另存为"导出原图（可选、默认关闭） |
| `wx_core/clipboard.py` | Win32 剪贴板读写（`get_text` / `set_text`），供精确复制文本使用 |
| `wx_core/textlog.py` | 纯文本聊天记录 `log/聊天记录.txt`（**严格逐行** `好友名: 内容` / `我: 内容`，无表头/分隔线）+ emoji 中文名映射 |
| `wx_core/store.py` | 媒体落盘 `media/`、消息日志 `log/messages.jsonl` |
| `ui/main_window.py` | 主界面：状态栏、好友列表、消息流、控制区、日志区 |
| `ui/widgets.py` | 图片/表情包预览、动画 GIF 循环播放控件 |
| `ui/dev_backend.py` | 假数据后端（演示/无微信环境自测） |
| `tools/audit_compliance.py` | 合规静态审计（禁用 API + 微信目录写入检查） |
| `tests/` | 离线测试（用 `tests/fixtures/` 样本，不依赖微信在线；当前实测 **78 passed**，含 WCDB/db_source 单测） |

### 10.4 数据流与线程模型

```
数据库直读（默认）：Weixin 数据库 --wcdb 解密到临时目录--> sqlite3(mode=ro) --WAL--> db_source --> Message
        --monitor 轮询/去重--> queue.Queue --> GUI after(100) 渲染 --> media/ + log/
OCR 回退：微信窗口 --PrintWindow--> win.capture() --OCR/颜色分析--> layout + extract --> Message
```

- 采集全部在后台线程；GUI 线程只做 `queue.get_nowait()` + `root.after(100)`，禁止在 GUI 线程里截图/OCR/点击。
- 事件协议（GUI 只认这四种）：`status` / `message` / `friends` / `log`，形状见 `wx_core/models.py`。

### 10.5 运行测试

```bat
C:\Python314\python.exe -m pytest tests -q
```

测试使用 `tests/fixtures/` 下的离线截图样本（来自 `recon/` 实拍），不需要微信在线；无图形界面的环境会自动跳过 GUI 冒烟用例。`pytest` 属于开发依赖，未列入运行时 `requirements.txt`，需要时单独安装：

```bat
C:\Python314\python.exe -m pip install pytest
```

开发调试用的命令行参数（`app.py`）：

| 参数 | 作用 |
|---|---|
| `--fake` | 使用假数据后端（不连微信），自动生成五类示例消息 |
| `--interval 2.0` | 假后端生成消息的间隔秒数 |
| `--duration 10` | 运行 N 秒后自动退出（便于自动化验证） |
| `--screenshot 路径.png` | 启动后把主窗口截图保存到指定路径（配合 `--screenshot-delay`） |

> 真实后端不可用（例如缺少 OCR 依赖、找不到微信）时，界面会**自动回退假后端**并在日志区提示，方便先看界面；但这不代表真实监听可用，请按日志提示修复。

### 10.6 已知限制速查

- **数据库直读（默认）不需要窗口画面，微信最小化也能监听**；同步好友、自动切会话（观感）、OCR 回退模式仍需要窗口非最小化；
- 数据库密钥扫描是一次性只读；失败（权限不足/微信升级/库被锁/无 WAL 校验样本）时自动回退 OCR 模式（日志 warn），也可 `db_direct:false` 强制 OCR；
- 数据库被微信占用或写 WAL 竞态时，程序会退化为只解主库（数据稍旧但可用）；持续失败则回退 OCR；
- `db_keys` 含数据库密钥、`image_*` 含图片/贴纸密钥：**敏感**，不要分享 `config.json`/项目目录；删除后下次自动重扫；
- 解密使用系统临时目录中的 `wxdb_*` 目录，建议退出后清理残留；
- 贴纸匹配"宁缺勿错"：历史回放/相似贴纸可能不绑定，默认不截图、只记日志；
- `wxgf` 转 GIF 依赖 imageio-ffmpeg（内置 ffmpeg 7.1）；转换失败仍保留原始 `.wxgf`；
- 文件复制原文件、语音时长来自数据库；`log/聊天记录.txt` 是明文聊天内容，注意隐私；
- 微信升级可能改变窗口结构/缓存格式/数据库结构，需要适配。

---

## 网页端（新增）

> 在浏览器里实时查看监听消息，并用 DeepSeek 大模型做摘要 / 待办 / 情绪 / 风险分析；还带「朋友圈」与「同步历史聊天」页面（复用同目录 `WechatMoments/`）。详细步骤见 **[网页端使用说明](网页端使用说明.md)**。

```bat
C:\Python314\python.exe web_app.py          :: 真实微信后端，默认 http://127.0.0.1:8081
C:\Python314\python.exe web_app.py --fake   :: 演示模式（不需要微信，推荐先试）
```

也可以直接**双击 `run_web.bat`**。真实后端不可用时会自动回退演示后端并在页面提示；8080 让给其他项目。

特点：

* **好友 / 群聊**：左栏可切换「好友 / 群聊」，默认**只显示有历史聊天记录的联系人**（无记录的不显示，可点刷新重新读取）；头像统一为拼音/英文首字母、不同人不同颜色；群名同样可勾选监听、筛选消息、同步历史；群消息显示成员名（备注优先），AI 分析会按「群名/成员名」区分发言人；
* **实时同步**：文字 / 图片 / GIF 表情包 / 文件 / 语音 / 日志通过 SSE 推到 `http://127.0.0.1:8081`，刷新页面历史还在（内存最多 3000 条）；
* **AI 分析**：配置 DeepSeek Key 后，选「当前会话 / 全部消息 / 监听好友」范围，用快捷指令或自定义问题分析，支持深度思考、附带图片、流式输出、停止与复制；
* **朋友圈**：手动同步最近 **5/10/20** 条，页面最多显示 30 条；文字/图片/位置/评论/点赞均可查看，也能按「全部朋友圈 / 当前动态」交给 DeepSeek 分析；数据只存内存，关闭网页端即清空；
* **同步历史聊天**：选中好友后可拉取最近 5/10/20/50/100 条或本机全部缓存记录，条数不足会提示；聊天与朋友圈都支持一键清空后重新同步；
* **只读安全 / 风控**：服务只绑定 `127.0.0.1`、带随机令牌与 Host 校验；Key 只存本机 `web_config.json`（页面只显示后 4 位，不写日志）；不发送任何微信消息，不自动刷新微信；朋友圈图片在线补图仅拉缩略图、限并发/次数/超时，失败显示占位；
* **零新增依赖**：仅用 Python 标准库（`http.server`/`urllib`），不需要安装额外的包；
* ⚠️ **不要同时开桌面版和网页端**，二者会各跑一份采集线程，重复写 `media/`、`log/`。
