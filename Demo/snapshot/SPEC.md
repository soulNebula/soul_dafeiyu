# 微信好友消息监听器（wx_box）—— 架构与接口约定 v1

> 运行环境：Windows，Python 3.14（`C:\Python314\python.exe`），微信电脑版 **4.1.15.56**（Qt5，进程 `Weixin.exe`，主窗口类名 `Qt51514QWindowIcon`，标题 `微信`）。
>
> 红线（必须遵守）：**不读取/不修改微信进程内存，不注入 DLL，不 Hook API，不破解/解密微信数据库，不修改微信安装目录与源码。**
> 只允许"外部观察"：Win32 窗口枚举 + `PrintWindow(PW_RENDERFULLCONTENT)` 截图 + 窗口消息/前台键鼠 + 屏幕 OCR + 我们自己落盘的文件。

## 1. 已验证的技术事实（Lead 实测，勿重复踩坑）

| 探测 | 结果 | 结论 |
|---|---|---|
| UIAutomation 树 | 仅 `Weixin`/`MMUIRenderSubWindowHW` 两个空 Pane | ❌ 元素级自动化不可用 |
| MSAA (IAccessible) | 有顶层对象，`accChild` 返回 `E_INVALIDARG` | ❌ 元素级不可用 |
| 子窗口 HWND | 只有 `MMUIRenderSubWindowHW` | 内容全部自绘 |
| `PrintWindow(hwnd, hdc, 2)` | 成功，即使窗口被其他窗口遮挡也能拿到完整画面 | ✅ **唯一的画面来源**（先 `SetProcessDpiAwareness(2)`；窗口最小化时会失败/黑屏，需检测并提示） |
| Windows OCR | `zh-Hans-CN` 可用（PowerShell 5.1 WinRT） | ✅ 文字识别路线可行 |
| 微信主题 | 当前为深色主题，聊天列表在左、聊天区在右，输入框在底部 | 布局靠颜色/几何分析，不能写死像素 |

## 2. 目录结构与**文件所有权**（多人协作，禁止越界改别人文件）

```
wx_box/
├─ app.py                     # [GUI] 入口：启动 Tkinter 主窗口，注入真实后端
├─ run.bat                    # [DOCS] 双击启动
├─ requirements.txt           # [DOCS]
├─ SPEC.md                    # [LEAD] 本文件
├─ config.json                # [CORE] 运行时生成（首次启动写默认值）
├─ wx_core/
│  ├─ __init__.py             # [LEAD]
│  ├─ models.py               # [LEAD] Friend / Message / MonitorEvent 等数据模型
│  ├─ api.py                  # [LEAD] 后端抽象协议（GUI 只依赖这里）
│  ├─ win.py                  # [CORE] 窗口发现、DPI、PrintWindow 截图、输入(点击/滚轮/按键)
│  ├─ ocr.py                  # [OCR ] Windows OCR 常驻 worker（PowerShell 5.1 长连接，JSON 行协议）
│  ├─ layout.py               # [CORE] 面板定位(聊天区/标题/列表/输入框)、消息气泡切分、颜色分析
│  ├─ extract.py              # [CORE] 从一帧里抽取消息块 → Message（文本/图片/表情包/文件）
│  ├─ monitor.py              # [CORE] 轮询线程、去重、自动切到被监听好友、事件队列
│  ├─ friends.py              # [FRIENDS] 打开"通讯录"翻页枚举好友（数量+名称）
│  ├─ store.py                # [CORE] 媒体落盘 media/、消息日志 log/messages.jsonl
│  └─ config.py               # [CORE] 配置读写
├─ ui/
│  ├─ __init__.py             # [GUI]
│  ├─ main_window.py          # [GUI] 主界面
│  └─ widgets.py              # [GUI] 表情包/图片预览、动画 GIF 播放控件
├─ tests/                     # [各人自测；Lead 汇总]  用 recon/*.png 做离线样本
├─ media/                     # 运行时生成：收到的图片/表情包/文档缩略图
├─ log/                       # 运行时生成：messages.jsonl
└─ docs/
   ├─ README.md               # [DOCS] 使用说明（对小白）
   ├─ 风控与合规说明.md        # [DOCS] 为什么合规、风险点、使用建议
   └─ 常见问题.md              # [DOCS]
```

## 3. 数据模型（`wx_core/models.py`，Lead 维护，其他人只读）

```python
KIND_TEXT, KIND_IMAGE, KIND_EMOJI, KIND_FILE, KIND_SYSTEM, KIND_UNKNOWN = "text","image","emoji","file","system","unknown"

@dataclass
class Friend:
    name: str            # 显示名（备注优先）
    wxid: str = ""       # 无法获取时留空
    letter: str = ""     # 通讯录分组字母（可选）

@dataclass
class Message:
    id: str              # 稳定哈希：friend|kind|text|媒体内容 sha1|时间(秒级)，用于去重
    friend: str          # 所属好友会话名
    sender: str          # "friend" | "self"
    kind: str            # 上面 6 个常量
    text: str = ""       # 文本内容 / 文件名
    media_path: str = "" # 媒体落盘相对路径（image/emoji/file 缩略图）
    thumb_path: str = "" # 文件消息的图标/缩略图
    extra: dict = field(default_factory=dict)   # {"animated":bool,"size_text":"12.3K","n_frames":6,"time_text":"16:34"}
    ts: float = 0.0      # 发现时间（epoch 秒）
```

`MonitorEvent` 是 `dict`，统一形状（GUI 只认这个）：
```python
{"type":"status",  "data":{"running":bool,"wechat_found":bool,"chat_title":str,"monitoring":[names],"error":str|None,"tick":int}}
{"type":"message", "data": Message 转 dict}
{"type":"friends", "data":{"total":int,"friends":[Friend dict...]}}
{"type":"log",     "data":{"level":"info|warn|error","text":str}}
```

## 4. 后端协议（`wx_core/api.py`）

```python
class Backend(Protocol):
    events: "queue.Queue[dict]"          # GUI 唯一的事件来源，非阻塞 get
    def start(self) -> None: ...          # 启动轮询线程（不阻塞）
    def stop(self) -> None: ...
    def get_state(self) -> dict: ...      # 同步读取当前状态（线程安全快照）
    def list_friends(self, refresh: bool = False) -> list[Friend]: ...   # 缓存优先；refresh=True 触发后台枚举
    def set_monitored(self, names: list[str]) -> None: ...               # 设置监听名单（空列表=停止监听）
    def is_running(self) -> bool: ...
```

GUI 线程只做：`queue.get_nowait()` 循环 + `after(100, ...)` 定时刷新；**严禁在 GUI 线程里调用截图/OCR/输入**。

## 5. 关键实现约定

1. **画面来源**：`win.py::capture()` → PIL.Image（整窗），内部 `PrintWindow` + `PW_RENDERFULLCONTENT`；必须先 DPI-aware。窗口最小化时返回 `None` 并置状态 `wechat_found=False`。
2. **OCR**：`ocr.py::OCRWorker.ocr(image) -> list[Line]`，`Line = {"text":str,"box":(l,t,r,b),"words":[{"text","box"}...]}`。常驻 PowerShell 进程，stdin 传 PNG 临时文件路径，stdout 回 JSON；启动失败自动降级为每次调用子进程（慢但对）。中文识别质量决定文本消息质量。
3. **布局**：`layout.py` 从一帧自动测面板边界（列方向颜色方差 + 竖直分隔线），不能硬编码坐标；找出：左侧栏/聊天列表/聊天区/标题栏/输入区，以及聊天区可用高度（去掉底部输入框）。
4. **消息切分**：聊天区内按"行"聚类 OCR 文本 + 气泡色块检测（深色主题气泡色 ~`#2c2c2c`，自己发出 ~`#3eb575`/绿色系；以实测为准）。图片/表情包/文件为无文字块：检测圆角矩形/异色块，结合尺寸、宽高比、是否有文件名文本（`.*\.(pdf|docx?|xlsx?|zip|rar|7z|txt|pptx?)$` + 大小文本 `\d+(\.\d+)?\s*[KMG]B?`）分类。
5. **去重**：对每个消息块算 `visual_hash = sha1(crop_bytes)`，并按 `(friend, sender, kind, text, visual_hash)` 做短期集合；滚动导致的位置变化不影响 hash。首帧只建立基线不推送（避免刷屏），可由 `config["emit_history_on_start"]` 控制。
6. **动画表情检测**：同一位置连续两帧（间隔 250ms）像素差 > 阈值 → `animated=True`；若为 animated 且 `kind==emoji`，抓 5~8 帧合成 GIF 存 `media/`（Pillow），供 GUI 动图播放。
7. **自动切到被监听好友**：仅当监听到的窗口标题（聊天区顶部 OCR）≠ 目标好友时切。切换方式优先级：① 调微信搜索框点选（最稳）；② 聊天列表/通讯录点选。点击用 `PostMessage(WM_LBUTTONDOWN/UP)` 到 `MMUIRenderSubWindowHW`（先试，不抢焦点）；不行再 `SetForegroundWindow`+`SendInput`。**每次切换后必须校验标题，失败则退避 10s 并提示用户手动打开。**
8. **绝不发送消息**：本工具只读。任何"发送"按钮都不要做。

## 6. 验收标准（Lead 最终跑）

- [ ] `python app.py` 启动出界面；自动识别微信窗口；显示"已连接/未连接"
- [ ] 好友列表：显示总数 + 名称列表（可滚动/搜索）；支持"重新同步"
- [ ] 勾选 1 个好友 → 开始监听 → 界面显示该好友发来的**文字/图片/表情包(GIF可动)/文件**；语音忽略并记一条"语音消息(未接收)"
- [ ] 子进程退出/微信关闭/最小化 时界面给出明确提示且不崩
- [ ] `python -m pytest tests -q` 全绿（离线样本测试）
- [ ] 全程无内存读取/Hook/注入；`docs/风控与合规说明.md` 说明清楚

---

# 交付状态 v2（Lead 最终核对，2026-10-22 03:10）

## 已验证跑通（真实微信 4.1.15.56）
| 能力 | 状态 | 证据 |
|---|---|---|
| 找微信主窗口（排除「聊天记录」独立窗口、最小化时取恢复尺寸） | ✅ | `win.find_wechat_window`，实测选中 `hwnd=3606706` |
| PrintWindow 截图（被遮挡也可截，~20ms/帧） | ✅ | `recon/live_final.png` |
| 布局自动识别（面板/标题栏/输入框/气泡配色） | ✅ ~100ms/帧 | `recon/layout_overlay.png` |
| OCR（RapidOCR，整块消息区 ~364-460ms，中文准确） | ✅ | 复现 `recon/ocr_detect.txt`、`tests/test_ocr.py` |
| 消息解析：文字/图片/表情包/文件/语音/系统时间 | ✅ | 文字与语音时长、文件名+体积都正确 |
| 动图表情包：抓帧合成真 GIF（含 animated 标记） | ✅ | `media/*_emoji.gif`，Pillow 可打开 |
| 好友列表同步（通讯录翻页，返回名单+总数校验） | ✅ | 4 位好友，4.5s，`recon/friends_result.txt` |
| 自动切到被监听好友（优先点会话列表，不抢焦点） | ✅ | 实测切换「文件传输助手」成功 |
| 去重（同帧不重推、OCR 抖动近似匹配、动图不重复推、半条消息跳过） | ✅ | `tests/test_core.py::test_diff_frames_*` |
| 文档原始文件：从微信 `msg/file` 明文缓存复制（保留原名） | ✅ | `media/files/BOM_Board1_Schematic1_2026-10-19.xlsx` |
| GUI（深色、好友列表、消息卡片、GIF 播放、语音提示、日志、折叠） | ✅ | `recon/gui_final_real.png` |
| 全套离线测试 | ✅ | `python -m pytest tests -q` → 63 passed |

## 媒体原文件的边界（重要）
* **文档**：微信 `msg/file/<yyyy-mm>/<原文件名>` 是明文，直接复制 → 拿到原始文件。
* **图片 / GIF 表情包**：微信缓存是 V2 加密容器（AES-128-ECB 头 + 单字节异或尾），
  AES 密钥每个账号一份、只存在于 `Weixin.exe` 进程内存。
  - 本机实测：离线爆破 `2 × 2^24` 组 cfgDword 派生（26~60s/轮）**未命中**；
  - 内存扫描 1.1GB（ASCII 16 位候选 20.8 万个 + 任意 16 字节窗口 7277 万个）**同样未命中**；
  - 因此**放弃**"从进程内存取密钥"，交付版**不含任何内存读写代码**（`wx_core/dat_key.py` 已删）。
  - 现状：图片/表情包保存"聊天区高质量裁剪"+动图合成真 GIF；`wx_core/dat_decrypt.py` 保留为
    纯算法模块，用户若自行拿到 `image_aes_key` 可写入 config 后离线解密（默认不启用）。
  - 如需**100% 原图/原 GIF**，下一步可采用"模拟微信右键→另存为"的导出路线（不碰加密），
    或用户直接提供密钥。

## 红线（最终）
1. 不读微信进程内存、不注入、不 Hook、不修改微信安装目录与源码；不解密微信 SQLite 数据库。
2. 只读取**当前登录用户自己的**微信本地缓存文件（`msg/file` 明文文档、`msg/attach` 图片缓存、`cache/*/Emoticon`）。
3. 只发窗口消息/标准键鼠，绝不代替用户发送任何消息。
4. 所有产出只写项目 `media/`、`log/`、`config.json`。

---

# 交付状态 v3（采纳 WeFlow 调研结论，2026-10-22）

## 从 [WeFlow](https://github.com/hicccc77/WeFlow) 学到/复用的部分
WeFlow（Electron+TS 的本地聊天查看/导出工具）源码核实结论：
1. **它也不内置密钥获取**：`electron/services/keyProviderService.ts` 只是 spawn 用户自备的
   "第三方可插拔组件"（key provider exe），协议见其 `docs/third-party-components.md`；
2. **它也不内置 .dat 解密**：`nativeImageDecrypt.ts` 需要用户配置外部 `wedecrypt` 原生插件
   （`imageNativeAddonPath`）；仓库里没有该算法实现，`resources/runtime` 只有 MSVC 运行库；
3. 它的 **JS 兜底解密实现**（`imageDecryptService.ts::decryptDatV4WithJs/V3WithJs`）我们已对齐：
   V4 = 15 字节头 + AES-128-ECB 头段 + 明文段 + 单字节异或尾（与我们的 V2 实现完全一致，
   `aligned = aesSize + (16 - aesSize%16)`）；V3 = 整文件单字节异或；V1 = 固定 AES 密钥。
4. 它的 emoji 中文化用 `wechat-emojis` npm 包（109 个表情的中文名 + PNG）；我们已把
   "unicode emoji → [中文名]" 映射内置到 `wx_core/textlog.py`（😊→[微笑] 等 150+ 常用）。

## 本次新增能力
| 能力 | 状态 | 说明 |
|---|---|---|
| 纯文本聊天记录 `log/聊天记录.txt` | ✅ | 每行 `好友名: 内容` / `我: 内容`；媒体记 `[图片]/[表情包]/[文件]/[语音]` |
| Emoji 中文名 | ✅ | unicode emoji 自动转 `[微笑]`；微信原文里的 `[微笑]` 原样保留 |
| 精确文本（不靠 OCR） | ✅ | 文本消息"右键→复制"读剪贴板，读完还原用户剪贴板；实测拿到 `可以可以。` |
| 缓存 .dat 本地解密 | ✅ 待密钥 | `wx_core/dat_decrypt.py` 支持 V1/V2/V3/V4 自动识别与解密；config 填 `image_xor_key` + `image_aes_key` 即自动把新到的图/表情包解出**原格式文件**（GIF/JPG/PNG 原样）；本机目前没有可用密钥，功能待启用 |
| 原图导出（微信自带另存为，窗口消息） | ⚠️ 可选 | 纯 PostMessage/BM_CLICK，不动真实鼠标；默认关闭（用户不推荐），config `export_original_media` |

## 密钥这关的实测结论（为什么还需要外部组件）
微信 4.1.15.56 上实测：离线 cfgDword 爆破（2×2^24）、ASCII/二进制全内存扫描（1.1GB / 7277 万候选）、
参考开源脚本 `find_image_key.py` 原样运行、图片查看器打开时扫描 —— **全部未命中**。
WeFlow 的架构也印证：这一步在社区里同样是"用户自备外部密钥组件"。

**接入方式（已预留）**：把外部组件输出的密钥填进 `config.json`：
```json
"image_xor_key": "183",     // 十进制或 0x 十六进制
"image_aes_key": "0123456789abcdef"
```
之后新收到的图片/表情包会自动从 `msg/attach/**/Img/*.dat` 解密成原始格式。
如需对接 WeFlow 的 key provider 协议（NDJSON stdin/stdout），可以再加一个适配器。

---

# 交付状态 v4（原图解密打通，2026-10-22）

## 关键突破：图片/表情包原格式解密（不再截图）
1. `wx_core/wechat_keys.py`：从 `Weixin.exe` 内存**一次性只读**定位 `Config.Cipher` 结构
   （weixin.dll 里 `global_config` 地标 → 指针链 → `cfg+0x40 cfgDword`、`cfg+0x48 wxId`），
   用"能否解密最近 .dat"自校验偏移（不依赖版本硬编码），派生：
   - `imageXorKey = cfgDword & 0xFF`
   - `imageAesKey = md5(str(cfgDword) + wxId).hexdigest()[:16]`
   本机实测命中：`cfgDword=0xe8f3b852`、`wxid=wxid_hsqe9w1lhl1j22` → `xor=0x52`、`aes=0669b78f42c9a7d7`。
2. `wx_core/media_cache.py`：按"文件名 stem 分组 + 优先 `_h.dat`（高清原图）"解密，
   并用消息气泡宽高比挑选最匹配的缓存文件；解出的 PNG 与微信自带"另存为"导出**字节完全一致**。
3. `wx_core/dat_decrypt.py`：V1/V2/V3/V4 自动识别（V2=AES-ECB 头+明文段+异或尾；
   V3=整文件异或；V1=固定 AES 密钥），对齐 WeFlow 的 `decryptDatV3/V4WithJs`。

## 聊天记录（纯文本）与 Emoji
- `log/聊天记录.txt`：每行 `好友名: 内容` / `我: 内容`；媒体行 `[图片]/[表情包]/[文件]/[语音]`。
- 文本优先用"右键→复制"取**精确原文**（`media_export.copy_message_text` + `clipboard.py`），
  读完还原用户剪贴板；失败退回 OCR 文本。
- `textlog.py` 内置 Unicode emoji → 微信中文名（😊→[微笑] 等 150+），微信原文 `[微笑]` 原样保留。

## 关于 WeFlow（github.com/hicccc77/WeFlow）
- 已克隆阅读源码：它**不内置**密钥获取（`keyProviderService.ts` 只是 spawn 用户自备的外部 exe）
  与 .dat 解密（`nativeImageDecrypt.ts` 需要用户配置外部 `wedecrypt` 原生插件）。
  其 `docs/third-party-components.md` 明确要求"用户自行配置第三方可插拔组件"。
- 我们复用了：它的 V3/V4 解密算法（已对齐验证）、"纯文本导出 + emoji 中文名"思路、
  以及 `wechat-emojis` 表情名资源（我们内置了名称映射）。
- 参考文件保留在 `tools/reference_weflow/`（含 Apache-2.0 LICENSE），不参与运行。

## 仍待解决
- **独立表情包（贴纸）原文件**：缓存在 `cache/*/Emoticon/*`，用另一套加密（非 V2/AES/XOR 组合，
  实测无法用图片密钥解开），需要表情数据库密钥；目前回退为"聊天区抓帧合成真 GIF"。
- 语音按需求不接收，只提示 `[语音] N秒`。

---

# 交付状态 v5（数据库直读打通，2026-10-22）

## 新能力：WCDB 数据库只读直读（用户明确授权）
- `wx_core/wcdb.py`：内存扫描 `com.Tencent.WCDB.Config.Cipher` 对象链 → XOR 27 字节掩码 →
  `x'<64hex key><32hex salt>'` → 逐库 HMAC-SHA512 页1 校验 → **每个库自己的 AES-256-CBC 密钥**；
  AES-256-CBC 逐页解密（page=4096, reserve=80）+ **WAL 帧叠加**（拿最新消息），
  密钥缓存到 config.json 的 `db_keys`（二次启动 10ms 内完成）。
- `wx_core/db_source.py`：把数据库消息转成统一 Message，**与消息一一对应**：
  - 文字：原文（含 `[微笑]`），不再 OCR/剪贴板；
  - 图片：XML md5/尺寸 + `msg/attach/.../Img/*.dat` 尺寸指纹精确匹配 → 本地解密原图；
  - 表情包：XML md5 ↔ `cache/<月>/Emoticon/<2位>/<md5>` 精确对应 → 解密成动画 GIF/原始格式；
  - 语音：`voicelength` 毫秒 → `[语音] 2秒`；文件：appmsg title → `msg/file/` 原文件；
  - 发送者：message 库 `Name2Id`，正确区分「我/对方」。
- 监听主流程：`db_direct=True` 默认优先直读（微信窗口最小化也能监听），不可用时自动回退 OCR。
- 实测（白云会话 16 条）：文字/图片原图/贴纸 GIF/语音 2 秒/文件 xlsx 全部精确对应，
  `log/聊天记录.txt` 逐行严格格式；`pytest` 78 passed；性能：缓存后初始化 0.01s、增量轮询 2ms。
- 新增配置：`db_direct`、`db_history_count`、`db_poll_limit`、`db_keys`。

---

# 交付状态 v6（网页端 + 朋友圈 + 大模型分析，2026-10-22）

## 新增能力
| 能力 | 入口/文件 | 说明 |
|---|---|---|
| 网页端 | `web_app.py`、`run_web.bat`、`webui/` | 本地 `http://127.0.0.1:8080`；与桌面版共用同一个后端（只读采集），网页端独占消费事件队列并以 SSE 实时推送 |
| 实时同步 | `webui/bridge.py`、`webui/server.py` | 首屏 `/api/bootstrap` 快照（状态/好友/最多 3000 条消息/日志）+ `GET /api/events` SSE；消息按 id 去重、媒体经 `/media/` 展示 |
| 网页控制 | `webui/server.py` | 好友勾选监听（写回 `config.json.monitored`）、重新同步好友、启停后端、日志抽屉 |
| DeepSeek 分析 | `webui/llm.py` | 页面内配置 API Key + 模型；当前唯一模型 **DeepSeek V4.1 Flash 1017**（模型 id `deepseek-flash`，官方 base_url `https://api.deepseek.com`）；流式输出、思考过程、停止、失败中文提示 |
| 朋友圈 | `webui/moments.py`、`webui/moments_worker.py` | 复用同目录 `WechatMoments/wx4`（Python 3.12 venv 子进程）；网页展示时间线/图片/评论点赞/头像，并能交给大模型分析 |
| 安全 | — | 仅监听 127.0.0.1；页面内嵌随机会话令牌（所有 `/api/*` 校验）；Host 头校验防 DNS rebinding；API Key 只存本机 `web_config.json`、只回显脱敏值 |

## 文件与端口
* 网页端默认 `127.0.0.1:8080`（`--port` 可改；端口占用会给出中文提示）。
* 网页端**不新增 pip 依赖**：HTTP/SSE/DeepSeek 流式全部用标准库。
* 朋友圈依赖 `WechatMoments/.venv`（已存在）；缺失时仅朋友圈页签不可用，聊天/AI 不受影响。
* 桌面版 `app.py` 保持不变；**不要同时运行两个监听实例**（会争抢窗口焦点/重复读库）。

## 红线（网页端同样适用）
1. 网页端不发消息、不提供任何"发送"入口；只读展示与本地 AI 分析。
2. DeepSeek 分析会把选中的聊天/朋友圈文本（开启"附带图片"时还有最多 6 张图片）发到
   `api.deepseek.com`，属于用户主动操作；密钥与分析内容不经第三方中转。
3. 朋友圈读取复用 `WechatMoments` 的只读路径（一次性只读进程内存取密钥 + 只读 `xwechat_files`），
   不写微信目录、不注入、不 Hook。
4. 只写项目自己的 `media/`、`log/`、`config.json`、`web_config.json`、`webui/cache/`。

## 验收命令
```bat
C:\Python314\python.exe web_app.py            :: 真实微信 + 朋友圈，打开 http://127.0.0.1:8080
C:\Python314\python.exe web_app.py --fake     :: 演示模式（无需微信）
C:\Python314\python.exe -m pytest tests -q    :: 离线测试全绿
```


## 多账号切换（2026-10-22 晚）
- 自动识别当前账号：取 `<数据根>/wxid_*_<后缀>` 中最近活动目录；wxid 从目录名从右切（wxid 自身含下划线）。
- 图片/贴纸密钥按 wxid 缓存（`image_keys_by_account`），切回旧账号 0.04s 直接用缓存；首次进新账号自动重提（约 0.3s）。
- 数据库密钥按账号目录缓存（`db_keys_by_account`），切换账号后首次约 15s 重扫，之后秒开。
- 运行中检测到账号切换会自动断开并重连数据库直读；监听名单是旧账号的显示名，需在界面「重新同步好友」后重新勾选。

## 好友列表同步提速（2026-10-22）
- 旧实现：`wx_core/friends.py::enumerate_friends` 界面滚动通讯录（截图+OCR+真实滚轮，超时 90s/最多 120 次滚动），
  联系人多的账号（实测 3020 个）要翻几百屏，所以慢且可能不全。
- 新实现：`db_source.enumerate_friends()` 从数据库直读（`friends_source` 配置）：
  - `recent`（默认）：最近有消息的会话，实测 0.19s / 39 个；
  - `all`：通讯录全部联系人（过滤公众号/群/系统号），实测 0.13s / 3020 个；
  - `ui`：保留旧的界面滚动方式（数据库不可用时自动回退）。
- GUI「重新同步好友」现在走数据库秒读；换账号后会自动清空并重新同步。

## 同步历史聊天记录（临时数据，不落盘，2026-10-22）
- GUI 新增「同步历史」按钮（开始监听/清空消息/打开媒体目录旁边）：弹窗选择 5/10/20/50/100/全部。
- 后端 `MonitorBackend.sync_history(names, count)`：从 WCDB 会话表读取所选好友历史，逐条转 Message
  发到界面；`count=None` 表示本机缓存到的全部。
- 不足提示：实际条数 < 请求条数时发出 history 事件，GUI 弹窗「没有这么多条聊天记录：X 只有 N 条」。
- 临时性：消息只进界面内存；不写 log/messages.jsonl、不写 log/聊天记录.txt；
  图片/表情/文件解密到 `%TEMP%/wxbox_history_*`，程序关闭（backend.stop/atexit）自动删除。
- 实测：刘工会话本机缓存 15 条；请求 5 → 同步 5；请求 100 → 提示不足并同步 15；全部 → 15；
  log 文件大小前后一致（未写入）；临时目录 stop 后清除。

## _refs 参考项目功能移植
- `wx_core/emoticon_export.py` + `tools/export_emoticons.py`：
  仿 Wechat-Emoticon-Parser，导出商店/收藏/未分类表情包（按包名+标题命名，wxgf 转 GIF），
  输出 `media/emoticons_export/<store|favorite|unknown>/...` + manifest.json；`--limit N` 可小样本调试。
- `wx_core/message_search.py` + `tools/search_messages.py`：
  仿 wechat-4x-decrypt Skill，按关键词/时间/联系人检索全库历史消息（读 message_fts.db 全文索引，
  mode=ro），可导出 txt/json。

## 好友/群列表按"有历史聊天记录"过滤（2026-10-22）
- `db_source.enumerate_friends(mode)` 重写：先统计 message DB 中所有"有消息记录"的会话（Msg_* 表 count>0），
  再据此过滤：
  - `recent`（默认）：有记录的会话按最近消息时间倒序（好友+群都有）；
  - `all`：好友(local_type=1)+群(local_type=2) 中所有有历史记录的；
  - `ui`：界面滚动枚举（数据库可用时也按历史记录过滤）。
- 没聊过的联系人/群不再出现在好友列表；每次启动自动重新同步一次，替换掉旧缓存。
- 实测本机：`recent` 39 个会话（含 12 个群），`all` 35 个（有记录的好友+群）。

## 红包 / 转账 消息识别（2026-10-22）
- `db_source._convert`：系统消息里的 XML/HTML 先清洗成纯文本（去 <img>/<wc_custom_link> 等标签）；
  含"红包"的识别为 `redpacket` 类型（显示如"你领取了妈妈的红包"，标签「红包」），
  含"转账"的识别为 `transfer`。
- 红包/转账的 appmsg（XML `<type>2001/2000`）解析 title/des/feedesc：
  红包 →「红包」类型；转账 →「微信转账（收到转账9.90元…）」。
- GUI 卡片标签、日志标签均已支持「红包」「转账」。
