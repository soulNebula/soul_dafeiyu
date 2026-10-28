# wx_box 网页端 · 接口契约 v1（Lead 维护，改动必须先在此更新）

> 目标：把 `wx_core` 后端监听到的**全部数据**（状态 / 好友 / 文字 / 图片 / 表情包 GIF / 文件 / 语音 / 日志）
> 实时同步到 `http://127.0.0.1:8080` 的网页；网页可配置 DeepSeek（当前唯一模型
> **DeepSeek V4.1 Flash 1017**，模型 id `deepseek-flash`），并用大模型分析这些数据。
>
> 红线不变：只读、不发送消息、不读微信内存（密钥提取为项目原有的一次性只读逻辑）、
> 只写项目自己的 `media/`、`log/`、`web_config.json`。网页端使用 **Python 标准库**
> （`http.server` / `urllib`），不新增 pip 依赖。

## 0. 文件所有权

| 文件 | 所有者 | 说明 |
|---|---|---|
| `webui/CONTRACT.md`、`webui/__init__.py`、`webui/bridge.py`、`webui/replay.py`、`web_app.py` | Lead | 事件桥、只读跟随后端、入口、最终集成 |
| `webui/server.py`、`webui/llm.py` | `server`（后端 teammate） | HTTP/SSE 服务、DeepSeek 客户端 |
| `webui/moments.py`、`webui/moments_worker.py` | `moments`（朋友圈 teammate） | 朋友圈数据桥（复用 `WechatMoments/wx4`，子进程隔离依赖） |
| `webui/static/index.html`、`app.js`、`style.css`、`favicon.svg` | `frontend`（前端 teammate） | 页面、样式、交互（原生 JS，无 CDN） |
| `tests/test_webui.py`、`docs/网页端使用说明.md` | `qa-docs`（测试文档 teammate） | 离线测试、使用文档 |
| `run_web.bat`、`requirements.txt` 注释、`SPEC.md`、`docs/README.md` 链接 | Lead | 启动脚本与总文档 |

### 0.1 朋友圈模块（2026-10-22 追加需求）

用户在网页端新增「朋友圈」模块：复用同目录 `WechatMoments/`（已适配微信 4.x）的 `wx4`
数据层，把朋友圈时间线（文字、图片、位置、分享链接、评论、点赞、作者头像）加载到网页，
并支持交给 DeepSeek 分析。`_refs/` 保持只读参考，不参与运行。

* `WechatMoments/.venv/Scripts/python.exe`（Python 3.12，已装 `xmltodict/dataclasses_json/
  cryptography/psutil/zstandard`）是运行 `wx4` 的隔离环境；网页主进程是 Python 3.14，
  **不能直接 import wx4**。所有朋友圈读取在子进程 `webui/moments_worker.py` 里完成。
* 朋友圈读取沿用 `WechatMoments` 的只读红线：只读自己的 `xwechat_files` + 一次性只读
  进程内存取库密钥；不写微信目录、不注入、不 Hook。
* 解密/缓存产物只写 `media/moments/`（图片、头像、视频封面）与 `webui/cache/moments.json`
  （用于页面刷新后秒开）；由 `webui/moments.py` 统一管理。
* 朋友圈同步失败（未登录微信 / 依赖缺失 / 密钥失效）不得影响聊天监听和网页其他功能，
  只在朋友圈页签显示可读原因 + 重试按钮。

两个人不要改同一个文件；需要改别人的文件先发消息。

## 1. 运行方式

```bat
python web_app.py                 :: 真实微信后端；默认 127.0.0.1:8080，自动开浏览器
python web_app.py --fake          :: 假后端演示（无需微信，每 2 秒一条示例消息）
python web_app.py --port 8081 --no-browser
```

`web_app.py`（Lead 实现）负责：读 `config.json` → 用 `app.load_backend()` 创建后端
（真实失败自动回退 `ui/dev_backend.DevBackend` 并提示）→ `WebBridge` → `LLMService`
→ `webui.server.create_server()` → 打开浏览器 → `serve_forever()`。

## 2. 数据模型（沿用 `wx_core/models.py`，只读）

`Message.to_dict()` 字段：`id, friend, sender, kind, text, media_path, thumb_path, extra, ts,
kind_label, sender_label, time_text`。`kind ∈ {text,image,emoji,file,system,voice,unknown}`，
`sender ∈ {friend,self}`。

* `media_path` / `thumb_path` 是**项目根目录相对路径**，如 `media/originals/xxx.png`。
  网页访问：去掉开头的 `media/`，拼成 `/media/<剩余部分>?token=<会话令牌>`。为空则不显示媒体。
* `extra` 里可能有：`size_text, animated, n_frames, duration, original_name, format, emoji_name,
  original, db_local_id, notice` 等；前端按需展示，不要假设一定存在。

## 3. 进程内接口（Python 模块之间）

### 3.1 `webui/bridge.py`（Lead 实现，server 调用）

```python
class WebBridge:
    def __init__(self, backend, *, mode: str, started_at: float | None = None,
                 max_messages: int = 3000): ...
    def start(self) -> None                 # 启动消费 backend.events 的后台线程
    def stop(self) -> None                  # 停止消费线程（不 stop backend）
    backend                                 # 属性：底层 backend 对象（只读约定，不要替换）
    def subscribe(self) -> tuple[queue.Queue[dict], int]   # (队列, 订阅 id)
    def unsubscribe(self, sub_id: int) -> None
    def broadcast(self, event: dict) -> None               # LLM 服务复用它推 llm_* 事件
    def snapshot(self) -> dict              # 见 4.2 /api/bootstrap
    def state(self) -> dict                 # backend.get_state() + 计数
    def set_monitored(self, names: list[str]) -> dict      # {"ok":True,"monitoring":[...]}
    def refresh_friends(self) -> dict
    def start_backend(self) -> dict
    def stop_backend(self) -> dict
    def scope_messages(self, scope: dict, limit: int) -> list[dict]
        # scope = {"type":"friend","friend":"白云一片去悠悠"} / {"type":"all"} /
        #         {"type":"monitored"}（"monitored" 时可选 "friend_names":[...] 进一步限定）
        # 返回按时间升序的 Message dict（最多 limit 条，取最近的消息）
```

事件写入 `broadcast` 前由 bridge 统一补 `"ts": time.time()` 信封字段（`message` 事件同样补，
便于排序；`data.ts` 仍保留消息自身时间）。

### 3.2 `webui/llm.py`（server teammate 实现，server 调用）

```python
MODEL_CATALOG = [
    {"id": "deepseek-flash", "label": "DeepSeek V4.1 Flash 1017",
     "description": "1M 上下文 · 支持深度思考与图像理解",
     "base_url": "https://api.deepseek.com"},
]

class LLMConfigStore:
    def __init__(self, path: pathlib.Path): ...
    def load(self) -> dict        # 完整配置，含 api_key 明文
    def public(self) -> dict      # 给前端的脱敏配置，见 4.2 的 llm 字段
    def save(self, patch: dict) -> dict   # 只接受白名单键，写盘后返回 public()

class LLMService:
    def __init__(self, store: LLMConfigStore, bridge, *, logger=print): ...
    def public_config(self) -> dict
    def save_config(self, patch: dict) -> dict
    def test_connection(self) -> dict          # {"ok":bool,"latency_ms":int|None,"error":str|None,"models":[...]}
    def start_analysis(self, *, scope: dict, scope_label: str, question: str,
                       history: list[dict], options: dict) -> dict
        # 成功 {"ok":True,"job_id":"..."}；失败抛 ValueError（含 code）
        # options = {"thinking":bool,"attach_images":bool,"limit":int}
    def cancel(self, job_id: str) -> bool
    def is_busy(self) -> bool
```

`LLMService` 在后台线程里调用 DeepSeek，所有过程通过 `bridge.broadcast(...)` 推 SSE 事件
（见 5.3）。同一时刻只允许一个分析任务；忙时 `start_analysis` 抛 `ValueError("busy")`。

DeepSeek 调用（已核对官方文档）：
* `POST {base_url}/chat/completions`，`Authorization: Bearer <api_key>`；
* 模型 id **`deepseek-flash`**（官方说明：即 DeepSeek-V4.1-Flash-1017）；
* `stream: true`，`stream_options: {"include_usage": true}`；
* 思考模式：`thinking: {"type": "enabled"}` / `{"type": "disabled"}`，
  开启时 `max_tokens` 建议 16384，关闭时 8192；
* 流式格式：`data: {json}` 每行，`choices[0].delta.content` / `delta.reasoning_content`，
  最后 `data: [DONE]`；最后一个 chunk 带 `usage`；
* 连通性测试：`GET {base_url}/models`（Bearer 认证）→ 200 即成功。

### 3.3 `webui/moments.py`（moments teammate 实现，server / web_app 调用）

```python
class MomentsService:
    def __init__(self, project_root: pathlib.Path, *,
                 bridge=None,                 # 有 broadcast(event) 即可，用于推 SSE
                 python: str | None = None,   # 默认 WechatMoments/.venv/Scripts/python.exe
                 worker: pathlib.Path | None = None,
                 moments_dir: pathlib.Path | None = None,
                 media_dir: pathlib.Path | None = None,
                 cache_path: pathlib.Path | None = None,
                 runner=None,                 # 测试注入：runner(args)->(rc, stdout, stderr)
                 logger=print): ...
    def start(self) -> None      # 读缓存；若可用且缓存为空/超过 30 分钟则后台异步同步一次
    def stop(self) -> None       # 等后台同步线程退出（不杀子进程也安全，daemon 线程）
    def status(self) -> dict
        # {"available":bool,"reason":str,"syncing":bool,"count":int,"synced_at":float|None,
        #  "account":{"wxid":str,"nick_name":str},"error":str|None,
        #  "python":str,"moments_dir":str}
    def items(self, limit: int = 200) -> list[dict]   # 时间倒序的朋友圈 dict（见 3.3.1）
    def get(self, moment_id: str) -> dict | None
    def sync_async(self) -> dict      # {"ok":True,"syncing":True} 或 {"ok":False,"error":...}
    def wait_sync(self, timeout: float = 120) -> bool   # 测试用
    def scope_items(self, scope: dict, limit: int = 50) -> list[dict]
        # scope={"type":"moments"} / {"type":"moment","id":"..."}；给 LLM 用
```

子进程协议（`moments_worker.py`）：
* 由 venv Python 执行：`<venv python> webui/moments_worker.py --moments-dir <WechatMoments> --media-out <media/moments>`
* 只输出**一行 JSON 到 stdout**（UTF-8），诊断信息一律写 stderr；
  `{"ok":true,"account":{"wxid":...,"nick_name":...},"synced_at":<epoch>,"moments":[...]}`；
  失败输出 `{"ok":false,"error":"中文原因","code":"wechat_not_ready|deps_missing|parse_error|..."}`。
* 子进程超时 120 秒；父进程用 `subprocess.run(..., capture_output=True, timeout=120, cwd=<WechatMoments>)`。

#### 3.3.1 朋友圈 dict 形状（worker 输出 = API 返回）

```json
{"id":"15034350704629592685","username":"wxid_xxx","author":"显示名（备注优先）",
 "avatar":"media/moments/avatars/<sha1>.jpg","create_time":1760000000,
 "time_text":"10-17 18:51","content":"文字内容（可空）","location":"地点（可空）",
 "link":{"title":"分享标题","url":"https://..."},
 "media":[{"type":"image","path":"media/moments/images/<id>_0.jpg","width":1280,"height":990},
          {"type":"video","path":"","cover":"media/moments/covers/x.jpg","duration":15}],
 "comments":[{"author":"显示名","content":"评论内容"}],
 "likes":["显示名1","显示名2"],
 "raw_comment_count":2}
```

* `path`/`cover` 是项目根相对路径，为空字符串表示本地没有缓存（前端显示占位）。
* `thumb`（新增，图片可选）：本地缓存没有原图时，worker 会用朋友圈 XML 里的 CDN
  `thumb` URL + token 在线下载并解密出的缩略图（项目相对路径，放
  `media/moments/thumbs/`）；前端优先显示 `path`，没有时显示 `thumb`，并标注「缩略图」。
  远程拉取受腾讯 CDN token 时效限制，失败就保持为空，不阻塞同步。
* 远程解密算法（已实测）：ISAAC-64（种子常量 `0x9e3779b97f4a7c13`，seed=XML `key`
  属性十进制），密钥流按 64 位大端逐块生成后与密文全文件 XOR；CDN 响应头
  `x-Enc: 1` 表示需要解密，解密后校验 JPEG/PNG/GIF/WebP 魔数。纯 Python 实现，
  放 `webui/moments_crypto.py`（不依赖 Node/WASM/第三方包）。
  头像文件放 `media/moments/avatars/`，缩略图放 `media/moments/thumbs/`。
* 图片文件由 worker 复制到 `media/moments/images/`，头像到 `media/moments/avatars/`，
  视频封面到 `media/moments/covers/`，文件名只含 `[0-9a-zA-Z_.-]`（避免 URL 编码问题）。
* 同一 `id` 去重；按 `create_time` 倒序；单次最多返回 2000 条，`items(limit)` 再截断。
* 评论/点赞作者名从 `contact.db` 映射（昵称/备注）；映射不到显示 `wxid` 原值。
* 视频绝大多数情况下本地无缓存：`path` 为空、`cover` 可能为空，前端显示占位卡片。

### 3.4 `webui/server.py`（server teammate 实现，`web_app.py` 调用）

```python
@dataclass
class WebContext:
    bridge: WebBridge
    llm: LLMService
    token: str            # 32+ 位随机十六进制，页面内嵌；所有 /api/* 带 X-WXBOX-Token
    host: str
    port: int
    mode: str             # "real" | "fake" | "follow"（follow=只读跟随桌面版日志）
    version: str = "1.0.0"
    backend_note: str = ""  # 真实后端回退原因（伪后端时用于页面提示）
    moments: MomentsService | None = None   # 朋友圈服务；None 时 /api/moments 返回 available=false

def create_server(context: WebContext, host: str, port: int) -> ThreadingHTTPServer: ...
```

`create_server` 返回已绑定端口的 server，不调用 `serve_forever()`（便于测试）。
绑定失败（端口占用）抛 `OSError`，由 `web_app.py` 打印友好提示并退出码 1。

## 4. HTTP API（全部同源，`Content-Type: application/json; charset=utf-8`）

鉴权：
* `/api/health`、`GET /`、`GET /static/*` 不校验令牌；其余 `/api/*` 必须带
  `X-WXBOX-Token: <token>`（或 `?token=<token>`），否则 401。
* `/media/*` 需要 `?token=`（图片 `<img src>` 不能带自定义头），错误 401。
* Host 头必须是 `127.0.0.1:<port>` / `localhost:<port>` / `[::1]:<port>`，否则 403（防 DNS rebinding）。
* 不返回 `Access-Control-Allow-Origin`。

### 4.1 基础

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | `{"ok":true,"version":"1.0.0","mode":"real","uptime_s":12.3}` |
| GET | `/api/bootstrap` | 首屏全量快照（见 4.2） |
| GET | `/api/events` | SSE 实时事件流（见第 5 节） |
| POST | `/api/monitored` | body `{"names":["好友A"]}` → `{"ok":true,"monitoring":[...]}` |
| POST | `/api/friends/sync` | `{}` → `{"ok":true}`（触发后台重新枚举，结果走 friends 事件） |
| POST | `/api/backend/start` | `{}` → `{"ok":true,"running":true}` |
| POST | `/api/backend/stop` | `{}` → `{"ok":true,"running":false}` |
| POST | `/api/llm/config` | 保存配置，见 4.3 → `{"ok":true,"config":{...}}` |
| GET | `/api/llm/config` | 同 `bootstrap.llm` |
| POST | `/api/llm/test` | `{}` → `{"ok":true,"latency_ms":321,"error":null,"models":[...]}` |
| POST | `/api/llm/analyze` | body 见 4.4 → `{"ok":true,"job_id":"..."}` |
| POST | `/api/llm/cancel` | `{"job_id":"..."}` → `{"ok":true}` |
| GET | `/api/moments` | 朋友圈快照：`{"ok":true,"status":{...3.3 status...},"items":[...],"count":N}`；默认最多 200 条，可 `?limit=500` |
| POST | `/api/moments/sync` | `{}` → `{"ok":true,"syncing":true}`；后台同步，结果走 SSE `moments` 事件 |
| GET | `/media/<path>` | `media/` 目录内文件；拒绝 `..`、绝对路径、目录外路径 → 404/403 |

所有错误响应统一：`{"ok":false,"error":"人话错误信息","code":"bad_request|unauthorized|not_found|busy|not_configured|llm_error|server_error"}` + 合适状态码。

### 4.2 `GET /api/bootstrap`

```json
{
  "server": {"version":"1.0.0","mode":"real","started_at":1750000000.0,"now":1750000010.0,
             "host":"127.0.0.1","port":8080,"backend_note":""},
  "state": {"running":true,"wechat_found":true,"chat_title":"","monitoring":[],
            "error":null,"tick":12,"monitor_state":"idle","last_ocr_ms":0,
            "friends_count":4,"message_total":123,"per_friend_counts":{"好友A":3}},
  "friends": [{"name":"好友A","wxid":"","letter":"","count":3,"last_ts":1750000000.0}],
  "messages": [ /* Message dict，按时间升序，最多 3000 条 */ ],
  "logs": [{"ts":1750000000.0,"level":"info","text":"..."}],
  "llm": {"configured":true,"model":"deepseek-flash","model_label":"DeepSeek V4.1 Flash 1017",
          "base_url":"https://api.deepseek.com","api_key_masked":"sk-…abcd",
          "thinking":true,"attach_images":true,"busy":false,
          "models":[{"id":"deepseek-flash","label":"DeepSeek V4.1 Flash 1017","description":"..."}]},
  "moments": {"status":{...同 3.3 status...},"count":7,"synced_at":1760000000.0,"account":{...}}
}
```

### 4.3 `POST /api/llm/config`

```json
{"api_key":"sk-xxx","model":"deepseek-flash","base_url":"https://api.deepseek.com",
 "thinking":true,"attach_images":true}
```

* `api_key` 省略或空字符串 = 保留已保存的 key（允许只改其它项）；传 `"clear"` 可清空。
  保存前 `strip()`，非 `sk-` 开头也接受（方便代理/中转），长度 8..512。
* `model` 只接受 `MODEL_CATALOG` 里的 id，其它值 400。
* `base_url` 默认 `https://api.deepseek.com`，必须 http/https。
* 响应：`{"ok":true,"config":{...同 bootstrap.llm...}}`。
* 配置文件：项目根 `web_config.json`（权限尽量 600；Windows 忽略），结构：
  ```json
  {"llm":{"api_key":"","model":"deepseek-flash","base_url":"https://api.deepseek.com",
          "thinking":true,"attach_images":true}}
  ```
  兼容环境变量 `DEEPSEEK_API_KEY`（无保存 key 时兜底，不回显明文）。`GET` 永远只回脱敏后的
  `sk-…后4位`。不记录 key 到日志。

### 4.4 `POST /api/llm/analyze`

```json
{"scope":{"type":"friend","friend":"白云一片去悠悠"},
 "scope_label":"白云一片去悠悠",
 "question":"请总结最近对话并列出待办事项",
 "history":[{"role":"user","content":"..."},{"role":"assistant","content":"..."}],
 "thinking":true,"attach_images":true,"limit":80}
```

* `scope.type`: `friend` | `all` | `monitored` | `moments` | `moment`；`monitored` 可带 `friend_names`，
  `moment` 必须带 `"id":"<朋友圈 id>"`。朋友圈范围时，服务端用 `moments.scope_items(scope, limit)`
  拼朋友圈转写稿（作者、时间、正文、位置、点赞、评论、媒体标记），`attach_images` 时把图片
  内容块附上（最多 6 张，来自 `media/moments/`）。`scope_label` 由前端给，缺省时社交范围用
  「全部朋友圈 / 某条朋友圈」。
* `question` 1..4000 字；为空 400。
* `history` 最多 12 条、仅 `user/assistant`、每条 ≤ 8000 字（前端自己做同样的限制）。
* `limit` 5..300，默认 80（取最近 N 条消息拼上下文）。
* 服务端把消息拼成中文 Markdown 转写稿（含时间、好友、发送者、类型、文本；
  图片/表情包在 `attach_images` 时按顺序附在 user 消息的 `image_url` 内容块里，最多 6 张）。
* 图片：只取 `media_path` 存在且后缀为 png/jpg/jpeg/gif/webp、单文件 ≤ 8MB 的，base64 data URL。
* 立即返回 `{"ok":true,"job_id":"..."}`，随后推送 `llm_*` SSE 事件。

## 5. SSE 事件（`GET /api/events`）

响应头：`Content-Type: text/event-stream; charset=utf-8`、`Cache-Control: no-cache`、
`Connection: close`（每连接一线程；HTTP/1.1 chunked 或直接 close 均可，客户端 EventSource 兼容）。
每条事件格式（注意 `event:` 行 = 事件类型，`data:` = 完整 JSON）：

```
event: message
data: {"type":"message","data":{...},"ts":1750000000.0}

```

* 连接建立先发一次 `event: hello`，data `{"type":"hello","data":{"version":"1.0.0","now":...}}`。
* 每 15 秒发一行注释心跳：`: ping\n\n`（不是事件）。
* 事件类型与 data：`status` / `friends` / `message` / `log` 的 data 形状与 `wx_core/models.py`
  的事件构造器一致；另外补 5.1~5.3 的 `llm_*` 事件。
* 事件队列满时丢弃最旧事件并给该客户端补发 `event: resync`（data `{"reason":"overflow"}`），
  前端收到后重新拉 `/api/bootstrap`。

### 5.1 `llm_start`
```json
{"type":"llm_start","data":{"job_id":"...","model":"deepseek-flash",
 "model_label":"DeepSeek V4.1 Flash 1017","scope_label":"白云一片去悠悠",
 "message_count":42,"image_count":2,"thinking":true,"started_at":1750000000.0}}
```
### 5.2 流式片段
```json
{"type":"llm_reasoning","data":{"job_id":"...","delta":"..."}}
{"type":"llm_delta","data":{"job_id":"...","delta":"..."}}
{"type":"llm_usage","data":{"job_id":"...","usage":{"prompt_tokens":1,"completion_tokens":2,
 "total_tokens":3,"reasoning_tokens":0}}}
```
`llm_usage` 可能不出现；前端不得依赖它。
### 5.3 结束/失败
```json
{"type":"llm_done","data":{"job_id":"...","content":"最终全文","reasoning":"思考全文",
 "usage":{...},"elapsed_ms":12345,"cancelled":false,"finish_reason":"stop"}}
{"type":"llm_error","data":{"job_id":"...","error":"人话错误","code":"llm_error"}}
```
取消时发 `llm_done` 且 `cancelled:true`、`content` 为已生成部分。HTTP 401/402/429 要映射成
中文提示（key 无效 / 余额不足 / 请求过于频繁）。

### 5.4 朋友圈事件

```json
{"type":"moments_status","data":{"syncing":true}}
{"type":"moments","data":{"ok":true,"count":7,"synced_at":1760000000.0,"error":null,
 "account":{"wxid":"...","nick_name":"..."}}}
// 失败：{"type":"moments","data":{"ok":false,"count":0,"synced_at":null,"error":"中文原因"}}
```
前端收到 `moments` 后重新拉 `GET /api/moments`；`moments_status` 只用于显示同步中状态。

## 6. 前端页面要求（`webui/static/`）

单页、深色、与桌面版同一套配色（见 `ui/widgets.py`：`#1e1f22 #232428 #3eb575` 等）。
无外部 CDN / 字体，纯 HTML+CSS+JS（ES6，`<script type="module">` 可选）。移动端可折叠侧栏。

布局（桌面 ≥1280px）：
0. **顶部中间主导航**：`消息` / `朋友圈` 两个 tab（带未读数/条数徽标），切换中栏与左栏内容；
   当前 tab 决定 AI 分析面板的默认范围（消息 / 朋友圈）。
1. **顶栏**：品牌 `WXBOX · 消息分析台`；微信连接状态点 + 文案；当前会话；监听中列表；
   右侧「模型设置」按钮、后端模式徽标（真实/演示）、日志开关。
2. **左栏 280~320px 好友**：总数、搜索框、全选/清空/重新同步、全部好友、好友行（勾选框=监听，
   点击=筛选消息，显示条数/最近时间）；勾选变化 500ms 防抖后自动 `POST /api/monitored`。
   朋友圈 tab 下左栏切换为「朋友圈」信息面板：账号昵称、可见条数、最近同步时间、
   `同步朋友圈` 按钮（同步中禁用+转圈）、作者筛选列表、只看有图片/评论的开关。
3. **中栏消息流**：会话标题 + 条数；筛选条（全部/文字/图片/表情包/文件/语音、搜索、
   自动滚动开关）；消息卡片（对方左、我右，系统居中；图片/GIF 可直接显示并点击放大；
   文件卡片显示文件名+大小+可下载；语音显示时长；空状态提示）；新消息进入动画；
   向上滚动时暂停自动滚动并显示「回到最新」浮标；每栏最多渲染 400 张卡片（虚拟截断）。
   **朋友圈 tab 中栏**：时间线卡片（作者头像+名字、时间、正文、位置；图片九宫格/单图大图、
   点击灯箱；视频封面+占位；分享链接卡片；点赞与评论列表；「分析这条」按钮）；
   顶部有作者/关键词筛选与 `同步朋友圈`；同步中骨架屏；无数据/不可用时给 CTA 与原因
   （如「检测到朋友圈项目但微信未登录」「朋友圈依赖缺失」）。
   **时间显示**：非当前年份的朋友圈必须显示完整年份（`YYYY-MM-DD HH:MM`），避免被误读成
   今年；当前年份可显示 `MM-DD HH:MM`。
   **数据新鲜度提示**：若本地朋友圈最新一条早于当前时间 48 小时，在时间线顶部显示一条
   可关闭的提示：「本地朋友圈最新一条是 X 天前。朋友圈只缓存电脑微信浏览过的内容，
   请先在电脑微信打开朋友圈刷新后，再点『同步朋友圈』」。这条提示直接解释"最新朋友圈
   没显示"的常见原因。
   **缩略图**：图片 `path` 为空但 `thumb` 存在时显示缩略图，并在图片角标/灯箱标注
   「缩略图（原图未缓存）」；点击仍可放大缩略图，但不要显示成"加载失败"。
4. **右栏 360~400px AI 分析**：模型徽标（未配置时显示「配置模型」CTA 打开设置弹窗）；
   范围选择（消息 tab：当前会话/全部消息/监听好友；朋友圈 tab：全部朋友圈/当前动态）；
   快捷指令 chips（消息：摘要/待办/情绪/风险/关键结论；朋友圈：摘要/人物关系/情绪/话题/风险）；
   问题输入框；选项（深度思考、附带图片）；开始/停止按钮；输出区（思考过程可折叠、
   Markdown 渲染、流式光标、耗时/usage、复制、清空）；未配置/报错/繁忙状态都要有清晰提示。
   分析朋友圈时 `POST /api/llm/analyze` 的 `scope` 用 `{"type":"moments"}` 或
   `{"type":"moment","id":"..."}`，并带 `scope_label`。
5. **底部日志抽屉**：可折叠，按 level 着色，最多 500 行，自动滚到底。
6. **模型设置弹窗**：API Key（password + 显示切换）、模型下拉（当前只有
   DeepSeek V4.1 Flash 1017）、Base URL（高级折叠，默认 `https://api.deepseek.com`）、
   默认思考/附带图片开关、`测试连接`、`保存`；提示 key 只保存在本机 `web_config.json`，
   附 DeepSeek 开放平台申请 key 的链接；保存成功后刷新徽标。
7. 前端必须处理 SSE 断线重连（EventSource 自带）以及 `resync`：重新拉 bootstrap 并去重合并。
8. 所有动态文本必须 `textContent` / 自建安全 Markdown 渲染（转义 HTML），禁止直接 innerHTML
   拼接消息内容；链接只允许 http/https。

## 7. 验收

* `python web_app.py --fake` → `http://127.0.0.1:8080` 能打开；每隔 2 秒自动出现示例消息，
  文字/图片/GIF/文件/语音/自己发送 都能显示；刷新页面历史还在（最多 3000 条）。
* 点「朋友圈」tab：会自动/手动同步 `WechatMoments` 的朋友圈，显示作者/时间/正文/图片/点赞/评论；
  同步失败时给出可读原因且不影响聊天页；对「全部朋友圈」或单条动态点分析能流式输出中文结果。
* 勾选好友 → 页面显示「监听中」且后端 `config.json.monitored` 更新；重新同步好友有反馈。
* 设置里填 API key + 测试连接成功；选当前会话 + 「摘要」→ 流式输出中文分析；
  停止按钮能中断；未填 key 时按钮引导去配置。
* `python -m pytest tests -q` 全绿（含新增 `tests/test_webui.py`）。
* 浏览器控制台无报错；1440×900 与 1100×800 下布局不破。
* 不新增 pip 依赖；不引入任何外部 CDN 资源。

---

# v2 需求变更（2026-10-22 18:20，优先级高于上文冲突项）

> 用户新要求：**注意风控**；朋友圈/聊天历史数据**全部只存内存，程序关闭即清空，不落盘占空间**；
> 朋友圈只同步「最近 5/10/20 条」、最多显示 30 条；朋友圈和聊天记录都可清空后重新同步；
> 新增「同步所选好友历史聊天记录」（5/10/20/50/100/全部，不足提示）；服务端口 **不再用 8080**。

## V2-1 端口与数据生命周期
* 默认监听改成 **127.0.0.1:8081**（8080 被其他项目占用）。`web_app.py` / `run_web.bat` / 文档 / 测试默认值全部同步。
* 网页端产生的**朋友圈数据、图片/头像字节、同步进来的历史聊天记录**只放进程内存：
  * 不再写 `webui/cache/moments.json`、不再写 `media/moments/`（现有文件由 Lead 清理，worker 不得再创建）；
  * 程序退出/重启后这些数据自然消失，页面重新同步；
  * 聊天监听后端原有的 `log/`、`media/` 行为不变（那是桌面版功能，不属于网页端临时数据）。
* 朋友圈同步只能由用户点按钮触发，`MomentsService.start()` **不得自动同步**。

## V2-2 朋友圈（MomentsService / worker 变更）
* `sync_async(limit: int = 10)`：`limit` 只允许 **5 / 10 / 20**（其它值按 10 处理）；只取**时间最新**的 N 条。
* `status()` 增加 `"limit": int`、`"count": int`；`items()` 最多返回 **30** 条。
* 新增 `clear() -> dict`：清空内存 items 与媒体字节，返回 `{"ok":True,"count":0}`。
* 新增 `media(key: str) -> tuple[bytes, str] | None`：按内存媒体 key 取 `(bytes, mime)`。
* 媒体只在内存：worker 输出 JSON 里每个媒体用
  `"thumb_key": "t_<feedid>_<idx>"`, `"thumb_mime": "image/jpeg"`, `"thumb_b64": "<base64>"`，
  `"avatar_key": "a_<sha1>", "avatar_mime": ..., "avatar_b64": ...`；
  `MomentsService` 解码到 `_media` 字典，`items()` 返回时**去掉 `*_b64` 字段**，保留 key/mime。
  `path`/`thumb` 旧字段可保留为空字符串（向前兼容），前端不再依赖它们。
* Worker 调用：`<venv python> webui/moments_worker.py --moments-dir <WechatMoments> --limit <N> --inline-media`；
  不再接受/写出媒体目录（现有 `--media-out` 参数可保留但忽略）。
* 缩略图策略（风控）：只用 CDN `thumb`（150 宽）在线补图，最多 60 张/次，并发 ≤3，超时 10s，不重试；
  本地有缓存原图时用 Pillow 压到最长边 ≤720 存内存 JPEG（质量 80），不再保留原图字节。
* `moments` SSE 事件 data 增加 `limit`；`moments_status` 不变。同步失败不影响聊天。

## V2-3 历史聊天记录（新增 `webui/history.py` + API）
```python
class HistoryService:
    def __init__(self, config: dict, log=print): ...
    def load(self, friend: str, limit: int) -> dict
        # limit: 5/10/20/50/100，或 0 = 全部（本机缓存到的）
        # 返回 {"ok":True,"friend":str,"requested":int|"all","loaded":int,
        #       "available":int,"notice":str|None,"items":[Message dict 时间升序]}
    def close(self) -> None
```
* 读取当前登录账号的 WCDB `message_*.db`（经 `wx_core.db_source.DbBridge` + `wx_core.wcdb`），
  只读、不写日志、不落盘；每次调用后数据由调用方放内存。
* 请求条数大于本机可用条数时：仍返回可用条数，`notice` 给中文提示，例如
  「「白云一片去悠悠」在本机只缓存了 7 条聊天记录，不足 50 条」；`available` 为总数。
* `all`（limit=0）返回本机该好友全部缓存消息（按 `local_id` 升序）。
* 聊天消息增量入 `WebBridge` 内存（去重），通过 SSE `message` 广播；`POST /api/messages/clear`
  清空全部内存消息；`POST /api/history/load` 返回的 items 也会广播，刷新页面后仍在（服务器未退出前）。

## V2-4 新增/变更 HTTP API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/moments` | `{"ok":true,"status":...,"items":[...],"count":N}`，最多 30 条 |
| POST | `/api/moments/sync` | body `{"limit":5|10|20}`（缺省 10） |
| POST | `/api/moments/clear` | `{}` → `{"ok":true,"count":0}` |
| GET | `/api/moments/media/<key>?token=` | 内存媒体字节；404 表示已清空/不存在；`Cache-Control: no-store` |
| POST | `/api/history/load` | body `{"friend":"白云一片去悠悠","limit":20}`（0=全部）→ 见 V2-3 `load()` 返回 |
| POST | `/api/messages/clear` | `{}` → `{"ok":true,"cleared":N}`；清空 bridge 内存消息、计数、日志（日志也属临时数据） |
| POST | `/api/backend/start` / `stop` | 不变 |
* `WebContext` 增加 `history: HistoryService | None = None`；`web_app.py` 负责创建并传入。
* 端口：`web_app.py --port` 默认 8081；`run_web.bat` 文案同步。
* 所有 `/api/*` 继续校验令牌；媒体路由也校验 `?token=`。

## V2-5 前端新增控件
* 朋友圈左栏：同步条数选择（5/10/20）+「同步朋友圈」；「清空朋友圈」按钮（带确认）；
  文案提示「数据仅在内存，关闭网页端后清空」；最多渲染 30 条。
* 消息区：选择好友后显示「同步历史」按钮，弹层选 5/10/20/50/100/全部；结果用 toast 显示
  `notice`（不足提示）；「清空聊天」按钮（确认后调 `/api/messages/clear`）。
* 媒体 URL：`thumb_key`/`avatar_key` 走 `/api/moments/media/<key>?token=<页面token>`，
  前端 `mediaUrl()` 要支持这两种 key；`path`/`thumb` 兼容保留。
* 仍保持「数据临时」提示：关闭网页/重启后需重新同步。

## V2-6 验收
* 默认地址 `http://127.0.0.1:8081`；8080 不再被本项目占用。
* 朋友圈同步 5/10/20 条，页面最多 30 条；图片/头像来自内存接口；关闭网页端后 `webui/cache/` 与 `media/moments/` 不新增文件。
* 朋友圈、聊天记录都能清空并重新同步；历史同步不足条数时 toast 提示。
* 历史同步仅触发用户所选好友；不发送消息；不写微信目录；CDN 请求遵守 V2-2 风控限制。

---

# v3 需求追加：群聊（2026-10-22 19:55）

> 用户要求：「聊群也做一下吧，现在只有好友」——网页端左栏需要能列出/选择/监听/同步历史/AI 分析**群聊**。

## V3-1 群聊数据（只读、内存缓存）
* 新增 `webui/groups.py`：
  ```python
  class GroupsService:
      def __init__(self, config: dict | None = None, *, log=print, ttl: float = 60.0): ...
      def list(self, refresh: bool = False) -> dict
          # {"ok":True,"count":N,"groups":[{"name","username","kind":"group","member_hint":""}],
          #  "source":"db","cached":bool}
      def is_group(self, name: str) -> bool
      def close(self) -> None
  ```
  从当前账号 `contact.db` 只读查询 `username LIKE '%@chatroom'`、`delete_flag=0`，显示名 `remark or nick_name`；
  结果只放内存缓存（默认 60 秒，`refresh=True` 强制刷新）；禁止落盘。
* `WebContext` 增加 `groups: GroupsService | None = None`。
* 新增 `GET /api/groups?refresh=1`（令牌校验）：返回 `{"ok":true,"count":N,"groups":[...]}`；
  服务不可用/DB 失败 → `{"ok":false,"error":"中文原因"}` + 503，不影响好友/聊天。
* 群聊的监听沿用 `POST /api/monitored {"names":[群名]}`（核心 `DbBridge.resolve_username` 已实测能按群名解析
  `xxx@chatroom`；数据库直读消息可读）。

## V3-2 历史聊天 / AI（群成员名）
* `HistoryService.load(friend, limit)`：当 friend 解析出的 username 以 `@chatroom` 结尾时：
  * `extra["is_group"]=True`，`extra["sender_name"]` 为发送人群昵称（查 contact.db：remark 优先、nick_name 次之），
    自己发的消息不填；
  * 现有字段/条数/不足提示逻辑不变。
* LLM 转写稿（`webui/llm.py`）对群聊消息在行首加上发送人：
  `[时间] 群名/群成员名（对方）: 内容`（有 `extra.sender_name` 时；自己仍为「我」）。
* 实时监听到的群消息若核心暂未提供成员名，前端只显示群名，不报错（后续脚本侧补充）。

## V3-3 前端
* 左栏顶部加「好友 / 群聊」切换（数字徽标）：群聊列表来自 `GET /api/groups`，行样式同好友，
  带「群」小徽标；支持搜索、勾选监听、点击筛选消息、显示条数/最近时间。
* 选中群聊后：消息区标题显示群名；「同步历史」按钮同样可用（5/10/20/50/100/全部，不足提示）；
  AI 分析「当前会话」范围用群名作 scope_label。
* 群聊消息卡片：若消息 `extra.sender_name` 存在，在气泡上方显示「成员名」（自己/对方都适用）；
  「全部消息」视图也保留。
* 群聊与好友共用「清空聊天」「同步历史」逻辑；无数据时正常空状态；群列表加载失败时提示但不影响好友。

---

# v4 需求：头像拼音首字母 + 联系人按历史记录过滤（2026-10-22 20:15）

> 用户反馈：群聊里不同用户的头像都是同一个字；好友/群聊列表里有很多「暂无消息」的空联系人。
> 要求：头像统一用**拼音首字母**（刘→L、何→H；英文名取首字母大写），**不同用户颜色不同**；
> 好友/群聊列表**只保留有历史聊天记录的联系人**。

## V4-1 后端 `webui/contacts.py`（Lead 实现）
```python
class ContactsService:
    def __init__(self, config=None, *, log=print, ttl: float = 60.0): ...
    def list(self, refresh: bool = False, include_all: bool = False) -> dict
        # {"ok":True,"include_all":bool,
        #  "friends":[{"name","username","kind":"friend","initial","has_history"}],
        #  "groups":[{"name","username","kind":"group","initial","has_history"}],
        #  "count":int,"friend_count":int,"group_count":int,
        #  "initials":{name:letter},"history_names":[name...],"cached":bool}
    def has_history(self, name: str) -> bool
    def close(self) -> None
```
* 数据源：`contact.db`（好友/群聊、remark 优先）+ `session/session.db` 的 `SessionTable`
  （`last_msg_locald_id>0` 或 `last_timestamp>0 且 summary<>''` 视为有历史记录）。
* 默认 `include_all=False` 只返回有历史记录的联系人；`include_all=True` 返回全部并带 `has_history`。
* 只读、内存 TTL 60s、不落盘；失败返回 `ok:false` + 中文原因，不影响好友/聊天。
* 拼音首字母：`webui/pinyin.py::pinyin_initial`（GB2312 区间表；英文首字母大写；数字/生僻字 `#`）。
* 群历史消息：`history.py` 增加 `extra.sender_initial`（拼音首字母）与 `extra.sender_key`（username）。

## V4-2 HTTP
* `WebContext` 增加 `contacts: ContactsService | None = None`。
* 新增 `GET /api/contacts?refresh=1&all=0`（令牌）：返回 `ContactsService.list()` 原结构；
  `contacts=None`/异常/`ok:false` → 503 可读错误。
* `/api/bootstrap` 增加 `"contacts":{"available":bool}`；`/api/groups` 保留兼容。

## V4-3 前端
* 启动后调用 `GET /api/contacts`（刷新按钮带 `?refresh=1`）；好友列表只渲染：
  `name ∈ history_names` **或** 当前页面内存里有该好友消息；「全部好友」行保留，全选只作用于可见好友。
* 群聊列表优先用 `contacts.groups`（已过滤），`/api/groups` 仅作 fallback；失败不阻塞好友/聊天。
* 所有头像（好友行、群行、消息头像、朋友圈作者兜底）统一用拼音首字母：
  优先 `contacts.initials`/`group.initial`/`message.extra.sender_initial`；英文首字母大写；
  兜底首字。颜色继续用 `hashHue(name)`，群成员必须用 `sender_name/sender_key` 计算，保证不同成员不同色。
* 左栏加弱提示「仅显示有聊天记录的好友与群聊」；空状态要说明可点刷新。

---

# v6 需求：清空/刷新好友+群聊列表、修复切换微信账号后列表不更新（2026-10-22 22:00）

> 用户现象：电脑微信切换账号后，网页仍显示上一个账号的好友/群，刷新也不更新、删不掉。
> 根因：① `media_cache.active_account_dir()` 按媒体缓存 mtime 判断，切号后可能仍返回旧账号；
> ② 页面/后端缓存未清；③ 监控线程的数据库直读桥仍指向旧账号。

## V6-1 账号检测（`webui/account.py`，Lead 实现）
* 以 `<xwechat_files>/all_users/login/<wxid>/key_info.db(-wal)` 的最近写入时间判断当前登录账号，
  不再依赖媒体缓存 mtime；找不到时回退旧逻辑。
* `HistoryService` / `GroupsService` / `ContactsService` 均使用该检测；缓存命中前先比较账号，
  账号变化强制重读。
* 读取新账号数据库密钥（首次 ~10-20 秒内存扫描）后写回 `config.json` 的
  `db_keys_by_account` / `image_wxid`，后续刷新秒开。

## V6-2 接口（server 实现）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/contacts/refresh` | 强制重读当前账号联系人；替换 bridge 好友列表；`account_changed` 时清空消息/监听名单、`bridge.reset_backend_db(wxid)`、更新 config；返回 `{ok,contacts,replaced_friends,account_changed,monitoring}` |
| POST | `/api/contacts/clear` | 清空 contacts/groups 内存缓存 + bridge 好友列表 + `config.friends_cache`；返回 `{ok,cleared}` |
* 失败只影响这两个接口，不能影响聊天/朋友圈/AI；均需令牌。
* bridge 新增：`replace_friends(list)`、`clear_friends()`、`reset_backend_db(wxid)`。

## V6-3 前端
* 左栏新增「刷新列表」「清空全部列表」按钮；刷新后更新好友/群/首字母/历史集合，切换账号时清空旧消息与监听勾选；
  显示「当前账号：<wxid>」；清空后空状态提示可点刷新。
* 保留原有勾选/搜索/群聊 tab；按钮 1100 布局不溢出。

---

# v7 需求：视频消息（2026-10-22 22:10）

> 用户发送视频后网页显示「未知 / [视频]」且不能播放。要求识别为视频卡片；本机有缓存时可播放，
> 没缓存时明确提示「视频未缓存」，不再显示原始 XML/未知。

## V7-1 解析
* `local_type=43` 的 `<videomsg md5="..." newmd5="..." playlength="N">`：
  * `kind="video"`（前端标签「视频」），`text="视频 N秒"`，`extra.app_type="video"`、
    `extra.duration=N`、`extra.video_md5=...`；
  * 实时与历史都走 `webui/wechat_text.normalize_message`（核心已识别的 `[视频]` 文本也映射成 video）。
* 不再把语音通话/视频等 XML 当正文；未知 XML 兜底 `[消息]`。

## V7-2 本机视频缓存与播放（`webui/videos.py`，Lead 实现）
* 微信 4.x 视频缓存在 `<账号目录>/msg/video/<YYYY-MM>/<md5>.mp4`（同名 `.jpg` 为封面）；
  只有电脑微信播放/接收过的视频才可能留下缓存；自己刚发送的视频通常**没有**本地文件。
* `VideoService.resolve(friend, local_id) -> {"path","mime","size","cover"}|None`：
  只读当前账号 DB 行，按 `md5/newmd5/originsourcemd5` 查找 `msg/video/**`，不复制、不转码。
* 新增 `GET /api/video?friend=<显示名>&local_id=<int>&token=`：
  200 流式原文件、支持 Range（206 + Content-Range）、`Accept-Ranges: bytes`、
  `Cache-Control: no-store`；缺失/未缓存 404，参数错 400，无令牌 401；只读 service 返回的路径。
* `WebContext` 增加 `videos: VideoService | None`；`web_app.py` 注入。

## V7-3 前端
* `KIND_LABELS` 加 `video:'视频'`；`kind==='video'` 或 `extra.app_type==='video'` 时渲染
  `<video controls>`（src 为 `/api/video?...&token=`），限宽 420px/高 300px；加载失败或无
  `db_local_id` 时显示紧凑占位「视频未缓存 / 无法播放（微信只保留播放过的视频）」；
* 显示时长角标；不再出现「未知」标签；mock 加视频占位。
