# wx_box · 微信好友消息监听器

> 在**你自己的 Windows 电脑**上，只读地查看、归档、分析指定微信好友与群聊的消息。
>
> 默认走**数据库直读**：文字是原文，图片/表情包是解密出的原文件，文件是原件，语音带时长，红包/转账/通话可识别；数据库不可用时自动回退「窗口截图 + 本地 OCR」。
> 提供 **Tk 桌面版** 与 **本地网页版**（SSE 实时同步、好友/群聊、同步历史、DeepSeek 分析、朋友圈时间线）。

**只读、不发送任何消息。** 数据默认不出本机；例外只有两处：① 你主动点「开始分析」时把选中的文本/图片发给 DeepSeek；② 朋友圈没有本地原图时，网页端向微信 CDN 拉取小缩略图（150 宽，限次/限并发/超时即放弃）。

`Windows 10/11` · `Python 3.10+`（本机实测 3.14）· `微信 4.x`（实测 4.1.15.56）· `本地运行` · `纯 Python，无云端后端`

> ⚠️ 本项目会读取**你本人账号**的微信本地缓存/数据库，并默认执行**一次性只读**密钥提取。请先阅读 [安全与合规](#安全与合规) 与 [风控与合规说明](docs/风控与合规说明.md)，确认理解并接受后再使用。

---

## 扩展：`agi/` 记忆与表达层（v1.0）

在主监听器之上，`agi/` 是一个独立的**记忆 + 表达**子项目：

> 从真实聊天里捞出「该跟进的事」，写成一句像你会说的话。
> **记得住，且从不编**——每条记忆都能点回原文某一句。

```bash
python web_app.py            # 启动后访问 http://127.0.0.1:8081/agi
```

产出 10 条草稿 → 你读 → 挑一条 → 点「复制」→ **手工发到微信**。
**本项目仍然零发送能力**：没有任何 send 代码，`--dry-run` 写死在服务端。

入口文档：[`agi/INTRO.md`](agi/INTRO.md)（项目介绍）· [`agi/README.md`](agi/README.md) · 日常使用 [`agi/USE.md`](agi/USE.md) · 施工图 [`agi/PLAN.md`](agi/PLAN.md)

---

**目录**：[界面预览](#界面预览) · [功能特性](#功能特性) · [工作原理](#工作原理) · [快速开始](#快速开始) · [网页版](#网页版) · [命令行工具](#命令行工具) · [项目结构](#项目结构) · [配置](#配置configjson) · [运行产物](#运行产物) · [安全与合规](#安全与合规) · [测试](#测试) · [已知限制](#已知限制) · [常见问题](#常见问题速查) · [文档](#文档) · [致谢](#致谢与第三方) · [许可与免责](#许可与免责声明)

---

## 界面预览

| 桌面版（含「同步历史」） | 网页版（好友/群聊 + 同步历史 + 红包 + AI） |
| :---: | :---: |
| ![桌面版](recon/gui_history.png) | ![网页版](recon/webui_listen_hongbao_final2.png) |

网页版「朋友圈」页签（手动同步、只存内存）：

![朋友圈](recon/webui_v2_moments_8081.png)

---

## 功能特性

| 能力 | 说明 |
| --- | --- |
| **数据库直读（默认）** | 一次性只读扫描微信内存取密钥 → 只读解密 `db_storage` 到系统临时目录（`sqlite3 mode=ro`）→ 叠加 WAL 拿最新消息。文字无 OCR 误差、媒体与消息一一对应，**好友和群聊都能直接读**，微信窗口最小化也能监听。 |
| **OCR 回退** | 数据库不可用时自动回退：`PrintWindow` 截图（被遮挡也能截）→ 本地 RapidOCR（缺省降级 Windows OCR）→ 颜色/几何布局 → 消息切分。文本再用微信「右键→复制」取精确原文并还原你的剪贴板。 |
| **原始媒体** | 图片 `.dat` 解密为原 PNG/JPG/GIF（`media/originals/`）；表情包/贴纸解密为原 GIF/PNG，`wxgf`（HEVC 动画）用内置 ffmpeg 转动画 GIF 并保留原始容器（`media/stickers/`）；文档从 `msg/file` 复制原件（`media/files/`）。 |
| **完整消息类型** | 文字、图片、表情包（GIF 可动）、文件、语音时长（`[语音] 2秒`，只提示不接收）、**红包 / 转账 / 语音视频通话记录**、系统消息；微信原始 XML/HTML 会清洗成人话，不会把标签露出来。 |
| **聊天记录归档** | `log/聊天记录.txt` 严格每行 `好友名: 内容` / `我: 内容`，媒体记 `[图片]/[表情包]/[文件]/[语音]`，emoji 自动转中文名；`log/messages.jsonl` 每行一条结构化 JSON。 |
| **桌面 GUI** | 好友/群聊列表（数据库秒读、搜索、多选、未读徽标）、**同步历史**、消息卡片、GIF 播放、图片预览、分级日志、开始/停止监听、清空、打开媒体目录。 |
| **好友/群列表** | 默认从数据库直读（`friends_source=recent`：有聊天记录的会话按最近排序；`all`：全部有记录的好友+群），实测 0.1–0.2 秒；数据库不可用时回退「滚动通讯录 + OCR」。启动时自动重新同步一次。 |
| **多账号切换** | 自动识别当前登录账号，图片/数据库密钥按账号缓存；切换账号后自动重连，旧账号的监听名单需重新同步后勾选。 |
| **本地网页版** | `http://127.0.0.1:8081`，SSE 实时推送、消息 3000 条、**好友 / 群聊**分栏、拼音首字母头像、**同步历史**、清空聊天、原生 JS 无 CDN、随机令牌 + Host 校验，纯 Python 标准库。 |
| **DeepSeek 分析** | 选「当前会话 / 全部消息 / 监听好友 / 朋友圈」范围，摘要、待办、情绪、风险、关键结论等快捷指令；群聊转写稿带「群名/成员名」，流式输出、深度思考、附带图片、可停止。 |
| **朋友圈** | 复用同目录 `WechatMoments/`（微信 4.x 适配），手动同步最近 5/10/20 条、页面最多 30 条，展示文字/图片/位置/评论/点赞/头像；缺少本地原图时按微信 ISAAC-64 算法解密 CDN 小缩略图。**只存内存，关闭网页即清空。** |
| **命令行工具** | `tools/export_emoticons.py` 导出商店/收藏表情包（wxgf→GIF + manifest）；`tools/search_messages.py` 按关键词/时间/联系人检索全库历史消息，可导出 txt/json。 |
| **合规审计** | `tools/audit_compliance.py` 静态扫描源码，检查「写内存 / 注入 / Hook / 数据库写 / 向微信目录写文件」，授权项如实列出文件:行号。 |
| **开箱演示** | `--fake` 假后端，无需微信即可看完整界面与各类示例消息。 |

**它明确不做**：不发送/自动回复消息、不写回或修改微信数据库、不注入 DLL、不 Hook API、不向微信目录写入、不自动刷新或自动浏览微信朋友圈、不在未经你操作的情况下把聊天数据发给任何服务器（AI 分析是你主动触发，朋友圈缩略图只是向微信 CDN 下载小图）。

---

## 工作原理

```
                  ┌──────────────────────────────────────────────┐
                  │              微信电脑版 4.x（Weixin.exe）       │
                  └───────────────┬──────────────────────────────┘
          ┌───────────────────────┴────────────────────────┐
          ▼ 路线 A：数据库直读（默认，db_direct=true）        ▼ 路线 B：OCR 回退
   db_storage/*.db                                  PrintWindow 截图（整窗）
   └ wcdb 一次性只读取密钥 → 解密到 TEMP/wxdb_*      └ layout 面板/气泡定位
     → sqlite3 mode=ro 只读 + WAL                     └ RapidOCR / Windows OCR
     └ db_source：XML/Name2Id → Message               └ extract：切分/分类/标题
          └──────────────┬─────────────────────────────────┘
                         ▼
            去重 / 自动切会话 → media/ + log/ → queue.Queue（4 类事件）
                         ▼
        ┌────────────────┴──────────────────┐
        ▼                                   ▼
  Tk 桌面界面（100ms 事件泵）        本地网页（WebBridge → SSE → 浏览器）
                                           ├ 好友/群/联系人（只读 DB，内存缓存）
                                           ├ 同步历史（只读 DB，内存回放）
                                           └ DeepSeek 分析 / 朋友圈
```

两条路线共用同一套 `Message` 模型与事件协议（`status` / `message` / `friends` / `log`），界面只依赖 `wx_core/api.py` 的 `Backend` 协议。网页端的好友/群聊列表、历史聊天、朋友圈同样只走只读逻辑。

### 两条采集路线对比

| 维度 | 数据库直读（默认） | OCR 回退 |
| --- | --- | --- |
| 触发条件 | `db_direct=true` 且监听名单非空 | `db_direct=false`、密钥/库不可用，或连续轮询异常 |
| 窗口要求 | 不需要画面，**最小化也能监听** | 微信窗口不能最小化（被遮挡没关系） |
| 文字 | 数据库原文（含 `[微笑]`） | OCR +「右键→复制」精确复制 |
| 图片 / 表情包 | 按消息 md5 / 尺寸精确匹配，本地解密原文件 | 按时间与画面宽高比匹配近期 `.dat` / 贴纸库，宁缺勿错 |
| 文件 | 按 appmsg 标题复制 `msg/file` 原文件 | 按文件名匹配缓存后复制 |
| 语音 | `voicelength` → `[语音] N秒` | 识别为「语音消息（未接收）」 |
| 红包 / 转账 / 通话 | 从 appmsg XML / 系统消息识别并人话化 | 按文字启发式识别 |
| 好友 / 群聊 | 好友和群聊都可直读、监听、同步历史 | 按画面（当前打开的会话） |
| 发送者 | `Name2Id` 映射，精确区分「我/对方」 | 头像左右位置 + 气泡颜色 |
| 典型开销 | 缓存后增量轮询毫秒级；首次密钥扫描约 10–20 秒 | 每帧截图 + OCR 约 0.5 秒；精确复制单条约几秒 |

切换方式：`config.json` 中 `db_direct: false` 可强制 OCR；运行中数据库路线失败后本进程不再重试，重启可再次尝试。

---

## 快速开始

### 1. 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11（64 位） |
| Python | **3.10+**，本机实测 **3.14**（`C:\Python314\python.exe`）；需要自带 Tkinter |
| 微信 | 电脑版 **4.x**（实测 **4.1.15.56**，进程 `Weixin.exe`），已登录并停在主界面 |
| 依赖 | Pillow、numpy、psutil、rapidocr-onnxruntime、pycryptodome、imageio-ffmpeg（见 `requirements.txt`） |
| 权限 | 与微信**相同权限**运行（微信以管理员运行则本程序也需管理员），否则密钥提取/窗口操作可能失败 |
| 网络 | 仅安装依赖、DeepSeek 分析、朋友圈缩略图补图时需要；其余全程离线 |

### 2. 安装依赖（只需一次）

在项目目录打开命令提示符：

```bat
python --version
python -m pip install -r requirements.txt
```

国内网络慢可加清华镜像：

```bat
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 本机 Python 未加入 PATH 时，把 `python` 换成完整路径（如 `C:\Python314\python.exe`）。
> 首次安装会下载 RapidOCR 的 ONNX 依赖（数百 MB），装完运行不再需要联网，模型随包提供。

### 3. 启动桌面版

```bat
python app.py
```

或直接双击 **`run.bat`**。

### 4. 启动网页版（可选，默认端口 8081）

```bat
python web_app.py
```

或直接双击 **`run_web.bat`**。默认打开 <http://127.0.0.1:8081>，启动后自动唤起浏览器。

> 8080 已让给其他项目，本项目网页端默认 **8081**；被占用时用 `--port 8082` 换端口。
> ⚠️ **桌面版与网页版不要同时以真实模式运行**（会各跑一份采集线程、重复写 `media/` 与 `log/`）。
> 若桌面版已在监听、只想用网页查看，请用只读跟随模式：`python web_app.py --follow-log`。

### 5. 先看演示模式（不需要微信）

```bat
python app.py --fake
python web_app.py --fake
```

演示后端每 2 秒生成一条消息，轮流覆盖文字 / 图片 / GIF 表情包 / PDF 文件 / 语音 / 自己发送，适合先熟悉界面。

### 三步上手

1. **同步好友**：点左侧「重新同步好友」。默认直接从数据库秒读（`friends_source=recent`，好友+群聊，只列有聊天记录的会话）；数据库不可用时才回退「滚动通讯录 + OCR」（此时微信会短暂切前台、鼠标会自己动，期间不要抢鼠标）。
2. **勾选好友 / 群聊**：搜索或直接勾选要监听的对象（可多选）。群聊在数据库直读模式下同样能监听。
3. **开始监听**：点「开始监听」。新消息进入右侧消息区，同时写入 `media/` 与 `log/`。

> 数据库直读模式下，开始监听后微信可以最小化；首次会一次性只读扫描密钥（成功日志显示「数据库直读已启用」），之后秒开。
> 想看某个好友/群以前的聊天：勾选后点底部 **「同步历史」**，选 5/10/20/50/100/全部（临时数据：只进界面内存/临时目录，不写 `log/`、关程序即清）。

---

## 网页版

网页版与桌面版共用 `wx_core` 采集后端、`config.json`、`media/` 与 `log/`，是独立进程，**同一时间只开一个真实采集实例**。

**V2 起的数据生命周期**：网页端的朋友圈、图片/头像字节、同步进来的历史聊天**只放在内存**，关闭网页端（Ctrl+C / 关窗口）即清空，不写 `webui/cache/`、不写 `media/moments/`。

### 启动参数

| 参数 | 作用 |
| --- | --- |
| `--fake` | 演示后端（无需微信） |
| `--interval 0.5` | 演示模式生成消息的间隔秒数（默认 2） |
| `--port 8082` | 换端口（默认 **8081**） |
| `--host` | 监听地址（保持默认 `127.0.0.1`；Host 校验只放行回环地址，本工具不是局域网服务） |
| `--no-browser` | 不自动打开浏览器 |
| `--no-moments` | 关闭朋友圈模块（排障用） |
| `--follow-log` | **只读跟随**：不启动采集，只读取并 tail 桌面版写入的 `log/messages.jsonl`（`run_web.bat` 不支持该参数，需命令行） |
| `--duration 30` | 运行 N 秒后自动退出（自动化验证） |

### 界面与功能

- **顶栏**：连接状态、当前会话、监听名单、**开始监听 / 结束监听**（结束只停采集，已加载消息保留）、模型设置、模式徽标（真实 / 演示 / 只读跟随）、日志抽屉。
- **左栏「好友 / 群聊」**：两个页签带数量徽标；顶部有搜索、全选/清空/刷新；默认**只显示有历史聊天记录的联系人**（刷新时重新读库，60 秒内存缓存）；头像统一用**拼音/英文首字母**、不同人不同颜色；勾选=监听，点名字=只看该会话，行尾显示条数与最近时间。
- **中栏消息流**：类型筛选（全部 / 文字 / 图片 / 表情包 / 文件 / 语音）、关键词搜索、自动滚动、图片/GIF 灯箱、文件下载、语音时长；**红包显示红色气泡、转账显示金色气泡**（只读展示）；群消息气泡上方显示成员名（有 `extra.sender_name` 时）。
- **同步历史**：选中好友或群聊后点「同步历史」，可选 **5/10/20/50/100/全部** 条；只读本机缓存、只进内存，条数不足会提示。
- **清空聊天**：清空网页内存里的消息、计数和日志；不影响桌面版 `log/`、`media/` 或微信本身。
- **只读跟随模式**：不采集、不写 `config.json`/`media/`/`log/`，勾选只影响本页筛选；同步历史/朋友圈仍可用（只读本机）。

### 朋友圈

- **必须手动**点「同步朋友圈」，可选最近 **5 / 10 / 20** 条；页面最多渲染 **30** 条；不会自动同步。
- 数据来自本机微信已缓存的朋友圈，**只能看到你在这台电脑的微信里浏览过的内容**；图片需在微信里点开过才会缓存。
- 本地没有原图时：只向微信 CDN 拉**小缩略图**（150 宽，最多 60 张/次、并发 ≤3、超时 10 秒、不重试），按微信 ISAAC-64 流加密算法在本地解密；失败显示占位。
- 「清空朋友圈」可清空内存后重新同步；所有图片/头像/历史字节不进磁盘。

### AI 分析

- 范围：当前会话（好友或群聊）/ 全部消息 / 监听好友 / 朋友圈（全部朋友圈或当前动态，最多 30 条）。
- 当前模型：**DeepSeek V4.1 Flash 1017**（模型 id `deepseek-flash`，默认 Base URL `https://api.deepseek.com`）。
- Key 只保存在本机 `web_config.json`，页面仅回显后 4 位，不写日志；也可用环境变量 `DEEPSEEK_API_KEY` 兜底。
- 支持深度思考、最多附带 6 张图片（单张 ≤ 8MB，朋友圈图片从内存取）、流式输出、随时停止；401/402/429/5xx 会转成中文提示。
- 群聊转写稿会写成「群名/成员名（对方）」，能分清群里谁说了什么。

### HTTP API（开发者速查）

服务只监听 `127.0.0.1`；除 `/api/health` 外所有 `/api/*` 需要令牌（`X-WXBOX-Token` 或 `?token=`），`/media/*` 用 `?token=`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（免令牌） |
| GET | `/api/bootstrap` | 首屏快照：状态 / 好友 / 消息 / 日志 / LLM / 朋友圈 / 各服务可用性 |
| GET | `/api/events` | SSE 实时事件流 |
| GET | `/api/contacts` | 好友+群聊（默认只含历史记录；`?refresh=1&all=0`） |
| GET | `/api/groups` | 群聊列表（`/api/contacts` 的兼容接口） |
| POST | `/api/monitored` | 设置监听名单（好友/群名） |
| POST | `/api/friends/sync` | 重新同步好友/群列表 |
| POST | `/api/backend/start` · `/api/backend/stop` | 启停采集后端 |
| POST | `/api/history/load` | 同步所选好友/群的历史（`{"friend":...,"limit":5\|10\|20\|50\|100\|0}`） |
| POST | `/api/messages/clear` | 清空网页内存消息 |
| GET / POST | `/api/llm/config` | 读取 / 保存 DeepSeek 配置 |
| POST | `/api/llm/test` · `/api/llm/analyze` · `/api/llm/cancel` | 测试连接 / 发起流式分析 / 取消 |
| GET | `/api/moments` | 朋友圈快照（最多 30 条） |
| POST | `/api/moments/sync` | 后台同步（`{"limit":5\|10\|20}`） |
| POST | `/api/moments/clear` | 清空朋友圈内存数据 |
| GET | `/api/moments/media/<key>?token=` | 朋友圈内存图片/头像字节 |
| GET | `/media/<相对路径>?token=` | 桌面版采集的媒体文件 |

完整契约见 [webui/CONTRACT.md](webui/CONTRACT.md)（含 v2/v3/v4 追加需求），中文使用说明见 [docs/网页端使用说明.md](docs/网页端使用说明.md)。

---

## 命令行工具

两个独立小工具，都是**只读**的，实现在 `Demo/tools_impl/`（按项目 `Demo/` 归档约定），启动脚本在 `tools/` 下。

### 导出微信表情包

```bat
python tools\export_emoticons.py                 :: 导出到 media/emoticons_export
python tools\export_emoticons.py --limit 20      :: 只处理前 20 个源文件（调试）
python tools\export_emoticons.py --out D:\emo    :: 指定输出目录
```

输出结构：`<out>/store/<包名>/<标题>.gif|png`、`<out>/favorite/...`、`<out>/unknown/...`、`<out>/manifest.json`；`wxgf` 动画会转成 GIF。数据源是本机表情包缓存与数据库，不写微信目录。

### 全库历史消息检索

```bat
python tools\search_messages.py --keyword 会议
python tools\search_messages.py --keyword 发票 --since 2026-10-01 --until 2026-10-22
python tools\search_messages.py --contact 刘工 --limit 50
python tools\search_messages.py --keyword 合同 --out log\合同检索.txt --json log\合同检索.json
```

数据源是微信全文索引库 `message_fts.db`（只读解密到临时目录后查询），支持关键词/日期/联系人过滤与 txt/json 导出。

> 两个工具都依赖微信已登录、数据库密钥可用；失败会给中文原因。`Demo/tools_impl/` 是两个工具的实际实现，桌面版/网页端主程序不依赖它。

---

## 项目结构

```
wx_box/
├─ app.py                      # 桌面版入口（Tk，事件泵，后端探测/自动回退）
├─ web_app.py                  # 网页版入口（本地 HTTP + SSE + AI + 朋友圈，默认 8081）
├─ run.bat / run_web.bat       # Windows 双击启动脚本
├─ requirements.txt            # 运行时依赖（pytest 不在此列）
├─ config.json                 # 运行配置 + 密钥缓存（首次启动自动生成，敏感）
├─ web_config.json             # DeepSeek 配置（首次保存时生成，含明文 Key，敏感）
├─ SPEC.md                     # 架构约定 + 各版本交付记录（历史部分已过时，现状以 README/代码为准）
├─ wx_core/                    # ★ 采集核心（21 个模块）
│  ├─ wcdb.py / db_source.py   # 数据库只读直读 / 消息解析、好友+群枚举、红包转账通话识别
│  ├─ monitor.py               # 轮询、去重、自动切会话、同步历史、事件队列
│  ├─ win.py / ocr.py          # Win32 截图与窗口输入 / OCR 主引擎 + 降级
│  ├─ layout.py / extract.py   # 面板定位与气泡切分 / 一帧 → 消息块
│  ├─ friends.py               # 通讯录滚动枚举（数据库不可用时的回退）
│  ├─ wechat_keys.py / media_cache.py / dat_decrypt.py / emoticon.py
│  │                           # 一次性只读密钥 / 缓存定位 / .dat 解密 / 贴纸解密
│  ├─ textlog.py / store.py    # 聊天记录 / 媒体与 JSONL 落盘
│  └─ models.py / api.py / config.py / clipboard.py / media_export.py / builtin_emoji.py
├─ ui/                         # Tk 界面：main_window / widgets / dev_backend
├─ webui/                      # 网页端（纯标准库）
│  ├─ server / bridge / llm    # HTTP+SSE+API / 事件桥与内存快照 / DeepSeek
│  ├─ contacts / groups        # 好友+群聊列表（只读 contact.db/session.db）
│  ├─ history                  # 同步历史聊天（只读 message_*.db，内存回放）
│  ├─ moments / moments_worker / moments_crypto
│  │                           # 朋友圈子进程桥 / 内存媒体 / ISAAC-64 在线缩略图解密
│  ├─ pinyin / wechat_text     # 拼音首字母头像 / 微信 XML/HTML 人话化
│  └─ replay / static/         # 只读跟随后端 / 原生 JS 页面
├─ tools/
│  ├─ audit_compliance.py      # 合规静态审计（PASS/FAIL + 文件:行号）
│  ├─ export_emoticons.py      # 表情包导出（实现在 Demo/tools_impl）
│  ├─ search_messages.py       # 全库消息检索（实现在 Demo/tools_impl）
│  └─ reference_weflow/        # WeFlow 参考实现（Apache-2.0，仅参考、不运行）
├─ Demo/                       # 本轮新增/修改代码归档：tools_impl/ 实现、dev/ 原型、snapshot/ 副本
├─ tests/                      # 离线测试 + fixtures（截图样本与生成式素材）
├─ docs/                       # 使用说明 / 风控合规 / 常见问题 / 网页端说明
├─ media/                      # 运行时产出：originals/ stickers/ files/ emoticons_export/
├─ log/                        # 运行时产出：聊天记录.txt、messages.jsonl
├─ recon/                      # 开发期探测脚本与截图（不参与运行）
├─ _refs/                      # 第三方参考归档（不参与运行，可删除）
└─ WechatMoments/              # 可选：网页端「朋友圈」依赖（独立项目 + wx4 适配 + 其 venv）
```

- `wx_core/` 与 `webui/` 的模块职责可参考 [docs/README.md](docs/README.md) 第 10.3 节与 [webui/CONTRACT.md](webui/CONTRACT.md)。
- `recon/`、`_refs/`、`tools/reference_weflow/`、`Demo/` 都是**开发/参考/归档产物**，不影响主程序运行，也不在合规审计扫描范围内（`Demo/tools_impl/` 仅被两个命令行工具引用）。
- `WechatMoments/` 是随目录提供的独立开源项目（Apache-2.0），仅网页端「朋友圈」页签通过其 `.venv` 子进程复用；缺失时只降级该页签。

---

## 配置（`config.json`）

首次启动自动生成，修改后**需重启程序**；删除该文件可恢复默认（密钥会重新扫描）。完整键表（32 个）见 `wx_core/config.py::DEFAULTS` 与 [docs/README.md](docs/README.md) 第七节。

| 键 | 默认 | 含义 |
| --- | --- | --- |
| `monitored` | `[]` | 监听好友/群聊显示名列表（界面勾选会写回） |
| `friends_cache` | `[]` | 上次同步的好友/群名（启动先显示，随后数据库自动刷新） |
| `friends_source` | `"recent"` | 好友列表来源：`recent` 有聊天记录的会话（默认）/ `all` 全部有记录的好友+群 / `ui` 界面滚动 |
| `poll_interval` | `1.2` | 主循环轮询间隔（秒），不建议小于 1 |
| `auto_switch` | `true` | 有新消息时自动切到被监听会话（只影响观感） |
| `rotate_interval` | `6.0` | 监听多个会话时的轮换间隔（秒） |
| `emit_history_on_start` | `false` | 首次是否补发历史消息（开启后每次重启会再补发一次） |
| `db_direct` | `true` | 数据库直读（推荐）；`false` 强制 OCR |
| `db_history_count` | `20` | 首次直读补发的历史条数 |
| `db_poll_limit` | `50` | 每次轮询最多读取的新消息数 |
| `auto_extract_image_key` | `true` | 首次运行一次性只读提取图片/贴纸密钥（设为 `false` 则完全不读内存） |
| `copy_text_exact` | `true` | 文本/小黄脸用「右键→复制」取精确原文 |
| `allow_screenshot_media` | `false` | 是否允许截图/合成 GIF 作媒体（仅调试；默认不截图） |
| `export_original_media` | `false` | 用微信「另存为」导出原图（默认关闭，慢且会操作微信界面） |
| `save_media` | `true` | 是否保存媒体（OCR 路径生效） |
| `db_keys` ★ | `{}` | 各库 WCDB 密钥缓存（key/salt/layout） |
| `db_keys_by_account` ★ / `image_keys_by_account` ★ | `{}` | 按账号缓存密钥，切换账号免重扫 |
| `image_xor_key` ★ / `image_aes_key` ★ / `image_cfg_dword` ★ / `image_wxid` ★ | `""` | 图片/贴纸密钥（可自动提取，也可手填） |

★ = **敏感信息**：能解密你的聊天数据，**不要分享 `config.json` 或项目目录**。删除对应键后程序会自动重扫。

> 想完全避免读微信进程内存：`auto_extract_image_key=false` + `db_direct=false`（回退 OCR，文字精度会下降）。
> 首次密钥提取失败（权限不一致 / 微信升级 / 库被锁）时，程序会自动回退 OCR，不影响启动。

---

## 运行产物

桌面版写在项目自己的目录里，**不写微信目录**；网页端的朋友圈/历史只存内存：

| 路径 | 内容 |
| --- | --- |
| `media/originals/` | 解密出的原始图片（`.png/.jpg/.gif/.webp`） |
| `media/stickers/` | 解密出的表情包/贴纸（`.gif/.png/.jpg`）+ 原始 `.wxgf` |
| `media/files/` | 从微信缓存复制的文档原文件（保留原名） |
| `media/emoticons_export/` | `tools/export_emoticons.py` 导出目录（可选，含 manifest.json） |
| `log/聊天记录.txt` | 纯文本聊天记录（UTF-8，严格每行一条，无表头/分隔线） |
| `log/messages.jsonl` | 结构化消息（每行一条 JSON，用于回溯/排错） |
| `%TEMP%\wxdb_*` | 数据库解密副本（`mode=ro` 读取，正常退出删除，残留由 24h 清理） |
| `%TEMP%\wxbox_history_*` | 桌面版「同步历史」的临时媒体（停止监听/退出即删除） |
| `webui/cache/`、`media/moments/` | **旧版本残留**：V2 起网页端不再写入，可直接删除 |
| `__pycache__/`、`.pytest_cache/` | Python / pytest 缓存，可删 |

> 网页端的朋友圈、图片/头像字节、同步历史全部只放内存（最多 3000 条消息 + 30 条朋友圈），关闭网页端即清空。
> `log/` 与 `media/` 含**明文聊天内容与图片**，`config.json`/`web_config.json` 含密钥，分享截图或打包前请先清理。纳入版本控制前建议忽略这些路径。

---

## 安全与合规

### 红线

1. 不**写**微信进程内存，不注入 DLL，不 Hook API，不修改微信安装目录/源码；
2. 不**写回/修改**微信数据库，不执行 SQL 写操作，不向微信目录写入/删除/改名任何文件；
3. **不发送任何消息**（没有发送入口，也不会代替你回复）；不自动刷新/自动浏览微信朋友圈；
4. 只写项目自己的 `media/`、`log/`、`config.json`、`web_config.json`；网页端临时数据只放内存。

### 用户授权的「只读」项（默认开启，可关闭）

- **一次性只读密钥扫描**：定位微信 `Config.Cipher` 结构取图片密钥与每库 WCDB 密钥；取到即关闭句柄、成功后缓存，之后**不再读内存**。可用 `auto_extract_image_key=false` 关闭图片密钥提取（`db_direct=false` 则连数据库路线一起关闭）。
- **数据库只读直读**：解密到本机临时目录 + `sqlite3 mode=ro` 读取 + WAL；不回写、不改库。网页端的好友/群/联系人列表、同步历史走同一套只读逻辑，结果只进内存（60 秒缓存）。
- **只读本地缓存**：文档 `msg/file`、图片 `.dat`、贴纸 `Emoticon` 缓存、朋友圈本地缓存。
- **窗口/剪贴板**：`PrintWindow` 截图、`PostMessage` 点击/滚轮、复制文本后还原剪贴板。
- 以上授权**仅限使用者本人账号、本人设备**；不得扩展到他人账号/设备，不得传播密钥或聊天记录。

### 网络与数据边界

- 聊天内容、图片、密钥全部留在本机；网页服务只监听 `127.0.0.1`，带每次启动随机的会话令牌与 Host 校验，无 CORS。
- **DeepSeek 分析**：只在你主动点「开始分析」时，把选中的聊天/朋友圈文本（开启附带图片时最多 6 张）发送到 `api.deepseek.com`。
- **朋友圈缩略图**：只有你手动点「同步朋友圈」且本地没有原图时，才会向微信 CDN 请求小缩略图（150 宽，最多 60 张/次、并发 ≤3、超时 10 秒、不重试），并在本地解密；除此之外不联网抓取任何内容。
- 网页端朋友圈/历史数据只存内存，不写磁盘；旧版遗留的 `webui/cache/`、`media/moments/` 可直接删除。

### 合规自检

静态审计脚本不 import 项目代码、不启动微信，可随时运行：

```bat
python tools\audit_compliance.py
```

期望输出末行：

```text
结果：PASS —— 严重问题 0 项，警告 0 项
```

退出码 `0` = PASS（允许警告），`1` = FAIL。脚本扫描项目内全部 `*.py`（排除 `recon/`、`_refs/`、`WechatMoments/`、`Demo/`、缓存目录等参考/归档目录），并把授权项（只读内存、只读数据库、只读缓存、剪贴板/窗口辅助）**如实列出文件:行号**，不冒充「零内存访问」。

---

## 测试

依赖 pytest（开发依赖，未列入 `requirements.txt`）：

```bat
python -m pip install pytest
python -m pytest tests -q
```

- `tests/` 下 5 个测试文件、**160+ 个用例**（最新记录 `166 passed`），**全部离线**：截图 fixture、假后端、假 DeepSeek 服务、`127.0.0.1:0` 随机端口，不需要微信在线、不需要联网。
- 覆盖：布局/OCR/消息切分/去重、好友翻页枚举、`.dat` 与贴纸解密、WCDB 页解密与消息解析、红包/转账/通话识别、历史记录与群聊解析、文本聊天记录格式、GUI 冒烟、网页端 contacts/groups/history/bridge/server/llm/moments/replay。
- 无桌面环境时 GUI 用例自动跳过；缺 RapidOCR/素材时相关用例自动跳过。
- 合规审计：`python tools\audit_compliance.py`（见上一节）。

---

## 已知限制

- **强依赖微信版本**：微信 4.x 的窗口结构、密钥链、WCDB 页格式、`.dat`/表情包/朋友圈缓存格式任一变化都可能失效。已验证版本为 **4.1.15.56**。
- **群聊实时成员名可能缺失**：数据库直读的实时群消息目前可能只能显示群名；「同步历史」读到的群消息会带成员显示名，AI 分析按「群名/成员名」区分。
- **网页端数据是临时的**：朋友圈、历史聊天、网页消息最多 3000 条/30 条朋友圈，关闭网页端即清空；需要长期留存请用桌面版的 `log/` 与 `media/`（或先同步历史后自行复制）。
- **朋友圈能力边界**：只能看到本机微信浏览/缓存过的内容；图片需在微信里点开过；缺原图时只有 CDN 小缩略图（150 宽），视频通常只有封面/占位。
- **默认只显示有聊天记录的联系人**（桌面版与网页端一致）；`friends_source=all` 也只是扩大到「全部有记录的好友+群」，不会显示从未聊过的联系人。
- **语音只提示不接收**；视频及部分 appmsg 只显示摘要；贴纸在历史回放/相似贴纸场景可能匹配不上（「宁缺勿错」，默认不截图，只记日志）。
- **OCR 模式**需要微信窗口非最小化；同步好友的界面回退方式会短暂切换前台并移动鼠标。数据库直读模式不受窗口最小化影响。
- **多账号**：切换账号后旧账号的监听名单需要重新同步并重新勾选；密钥按账号缓存，首次进新账号需要重扫。
- **单实例**：事件队列只允许一个消费者。桌面版与网页版不要同时真实运行；需要共存时网页端用 `--follow-log`。
- **敏感文件**：`config.json`（图片/数据库密钥）、`web_config.json`（明文 DeepSeek Key）、`log/`、`media/` 均含隐私，注意清理。
- **微信升级/风控**：升级后可能需适配；建议低频、仅用于自己账号，避免高频自动操作。

---

## 常见问题速查

| 现象 | 处理 |
| --- | --- |
| 启动报 `ModuleNotFoundError` / 依赖缺失 | 在项目目录执行 `python -m pip install -r requirements.txt`；确认 Python 带 tcl/tk |
| 状态栏「微信未连接」 | 微信已登录且停在主界面；OCR 回退/界面同步需窗口非最小化；程序与微信同权限运行 |
| 好友列表为空 / 只显示一部分 | 默认只显示有历史聊天记录的会话；点「重新同步好友」，确认数据库直读正常（看日志） |
| 没有图片/贴纸 | 看日志是否有密钥提取失败；确认 `auto_extract_image_key=true`；默认不截图，匹配不到只记日志 |
| 日志提示「数据库直读不可用」 | 权限不一致 / 微信升级 / 库被锁；自动回退 OCR，重启可重试 |
| 网页打不开或接口 401 | 默认端口 8081，占用时 `--port 8082`；令牌每次启动随机，旧标签页刷新即可；仅本机可访问 |
| 网页「同步历史」提示不足 | 本机微信只缓存了这么多条，属正常；提示会说明实际条数与可用总数 |
| 朋友圈没图 / 只有缩略图 | 先在微信里点开图片再同步；缺原图时只补 150 宽缩略图，失败显示占位 |
| AI 报 401/402/429 | Key 无效 / 余额不足 / 触发限流；在模型设置里「测试连接」看中文提示 |
| 桌面版和网页版同时开 | 只保留一个真实采集；另一侧用 `python web_app.py --follow-log` |

更多按现象排查见 [docs/常见问题.md](docs/常见问题.md) 与 [docs/网页端使用说明.md](docs/网页端使用说明.md) 的 FAQ。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/README.md](docs/README.md) | **桌面版完整说明（小白版）**：安装、界面、数据库直读原理与实测、全部配置项、开发者说明、技术路线 |
| [docs/网页端使用说明.md](docs/网页端使用说明.md) | 网页端逐个界面说明、好友/群聊、同步历史、AI 分析、朋友圈、实时同步与安全 |
| [docs/风控与合规说明.md](docs/风控与合规说明.md) | 授权范围、三不红线、合规论证、风险与免责声明 |
| [docs/常见问题.md](docs/常见问题.md) | 按现象排障的 FAQ |
| [docs/微信接入与自主能力_路线图.md](docs/微信接入与自主能力_路线图.md) | **未来计划（尚未实施）**：从只读观察升级为「能聊、能查、能学」的微信 agent——现状盘点、发送通道选型、agent loop、联网/学习/主动能力分阶段路线与风险 |
| [webui/CONTRACT.md](webui/CONTRACT.md) | 网页端接口契约（Python 接口 / HTTP / SSE / 错误码 + v2/v3/v4 追加需求） |
| [SPEC.md](SPEC.md) | 架构约定与 v2–v6 交付记录（早期红线部分已过时，现状以本 README、docs 与代码为准） |

> 文档会随版本迭代，个别早期段落可能滞后；若与 `wx_core/`、`webui/` 代码冲突，以代码和本 README 为准。

---

## 致谢与第三方

- [RapidOCR](https://github.com/RapidAI/RapidOCR)（`rapidocr-onnxruntime` / `onnxruntime`）：本地中文 OCR。
- [Pillow](https://python-pillow.org/)、[numpy](https://numpy.org/)、[psutil](https://github.com/giampaolo/psutil)、[pycryptodome](https://www.pycryptodome.org/)、[imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg)：图像处理、进程信息、加解密、`wxgf` 转 GIF。
- [WeFlow](https://github.com/hicccc77/WeFlow)（Apache-2.0）：`.dat` 解密算法与「纯文本导出 + emoji 中文名」思路参考；相关文件保留在 `tools/reference_weflow/`，不参与运行。
- `wechat-emojis`：`wx_core/emoji_assets/` 中 109 个微信小黄脸素材与中文名来源（仅素材，非代码）。
- [WechatMoments](https://github.com/tech-shrimp/WechatMoments)（Apache-2.0）：朋友圈导出项目；本项目内含其微信 4.x 适配分支（`wx4/`），仅供网页端朋友圈页签以子进程复用。
- `_refs/` 下两个 MIT 项目：`Wechat-Emoticon-Parser-4.0-plus`（表情包导出）与 `wechat-4x-decrypt-main`（全库消息检索 / 解密思路）的参考来源；对应能力已移植到 `Demo/tools_impl/`。

> 微信、Weixin 是腾讯公司的商标；本项目与腾讯无关联，仅通过操作系统公开接口与本人本地数据工作。

---

## 许可与免责声明

- 本仓库当前未附 `LICENSE` 文件；如需公开发布，请先补充许可证并确认所有随附第三方组件的许可要求。`WechatMoments/`、`tools/reference_weflow/` 与 `_refs/` 中的内容遵循其各自许可。
- 本项目仅供**学习研究与个人本机使用**。请仅在你**本人拥有并已登录**的账号、本人设备上使用；不得用于监控他人、窃取数据或任何违法用途。
- 读取本地缓存/数据库、一次性只读扫描密钥等行为可能触发安全软件告警，也可能违反你所在组织的 IT 政策；使用前请自行确认合规性并取得必要授权。
- 软件按「现状」提供，不保证与未来微信版本兼容；因使用或无法使用本软件造成的任何后果由使用者自行承担。
