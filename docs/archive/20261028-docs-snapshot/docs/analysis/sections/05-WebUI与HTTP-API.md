# 05 · WebUI 与 HTTP API 分区分析

> 仓库：`AstrBot-master`（v4.29.3，版本号来自 `astrbot/core/config/default.py` 的 `VERSION`，被 `astrbot/dashboard/server.py:20` 引入）
> 覆盖：后端 `astrbot/dashboard/**`（FastAPI，26 个路由模块 + 29 个服务模块）、前端 `dashboard/**`（Vue3 + Vuetify，186 个 `.vue`）
> 方法：全部结论来自 read/grep 实际读到的代码；行号为相对仓库根的真实行号；未读到的一律标注"未验证"。

---

## 1. 职责与边界

WebUI 分区是 AstrBot 的**运维与人机对话界面**，不与消息管线抢主循环，而是与 `AstrBotCoreLifecycle` 在**同一个 asyncio 事件循环里并列运行**：

- 启动器 `astrbot/core/initial_loader.py:26-54`：先 `core_lifecycle.initialize()`，再 `core_lifecycle.start()` 拿到 `core_task`，随后构造 `AstrBotDashboard(core_lifecycle, db, core_lifecycle.dashboard_shutdown_event, webui_dir)`，最后 `asyncio.gather(core_task, dashboard_server.run())` 一起 await。也就是说 HTTP 服务是 core 的**兄弟协程**，不是父进程。
- `AstrBotDashboard.run()`（`astrbot/dashboard/server.py:621-731`）本身只做三件事：解析 host/port/SSL（632-656）、端口占用检查（676-687）、用 **Hypercorn** `serve()` 托管 ASGI app（729-731）。`shutdown_event` 由 core 侧 `dashboard_shutdown_event.set()`（`astrbot/core/core_lifecycle.py:292,409,440`）触发。
- 边界清晰：Dashboard 拥有 `db`（`BaseDatabase`）与 `core_lifecycle`（配置、插件管理器、更新器、日志 broker）的**只读引用**（`server.py:271-285`），所有业务动作都通过 `app.state.services` 上的 29 个 service 转发（`astrbot/dashboard/api/app.py:113-160`）。Dashboard 不持有平台适配器、不直接跑 LLM 调用链，而是借用 `webchat` 平台源 + 队列把消息喂给 core 管线（见 §5）。
- 反向依赖存在一处：`dashboard` 层直接 import core 的 `file_token_service`（`astrbot/dashboard/services/file_service.py:3,13`）、`workspace.API_KEY_USERNAME_PREFIX`（`api/auth.py:9`）、`desktop_runtime.is_desktop_managed_backend`（`api/static_files.py:10`）等，属于**同进程紧耦合**而非独立进程边界。

---

## 2. 后端 API 架构（分层图 + 路由清单）

### 2.1 分层图

```
Hypercorn serve()                                  server.py:729
  └─ middleware(outer) body_limit                   server.py:303-322  → _check_body_limit 82-126
  └─ middleware(inner) auth                         server.py:295-301  → auth_middleware 326-388
       └─ FastAPI app  create_dashboard_asgi_app    api/app.py:85-226
            ├─ legacy /api/*  routers  (225 条)     api/app.py:198-223
            ├─ /api/v1  router  (328 条)            api/router.py:34-97
            └─ static_files_router (SPA)            api/app.py:225 / api/static_files.py:110-113
                 └─ Depends(ScopeDependency("xxx")) api/auth.py:49-63,167-195
                      └─ Depends(get_service)       api/*.py 各模块
                           └─ services/*.py (29 个)  app.py:113-160 实例化
                                └─ core_lifecycle / db / plugin_manager
```

三处关键机制：

1. **`/api/v1` 与 legacy `/api` 双轨**：每个模块都同时导出 `router`（v1）与 `legacy_router`（`include_in_schema=False`），例如 `api/chat.py:28-33`。`api/router.py:37-97` 在 include 前遍历子路由，把 `ScopeDependency` 的 scope 写进 `openapi_extra["x-astrbot-scope"]`（79-82）并追加到 `route.description`（83-95），这样 OpenAPI 文档天然带权限说明。
2. **Flask/Quart 兼容垫层**：`astrbot/dashboard/asgi_runtime.py`（715 行）提供 `contextvars` 版 `request` / `websocket` / `g` / `current_app` 代理（371-374）、`bind_request_context`（377-393）、`FastAPIAppAdapter`（636-713），甚至 `get_quart_compat_app()` 内嵌一个 Quart 实例给老插件用（648-654）。这解释了为什么 service 层存在大量 `*_from_dashboard_payload/from_dashboard_query` 包装方法（`services/chat_service.py:1554-2099` 几乎成对出现）：v1 走显式参数，legacy 走 Flask 风格全局上下文。
3. **统一响应包络**：`responses.py:14-22` 定义 `ok()` → `{"status":"ok","message":null,"data":{}}`、`error()` → `{"status":"error","message":...}`；异常收敛由 `api/app.py:162-196` 四个 handler 完成（`ApiError`/`ValueError`/`StarletteHTTPException`/`Exception`）。

### 2.2 路由清单（方法级装饰器计数，grep 实测）

合计 **553** 条（`/api/v1` 328 条 + legacy 225 条），另有 2 条通配 `api_route`（`api/plugins.py:1485` 的 `/api/plug/{plugin_path}`、`api/platform.py:112` 的 webhook）。

| 模块 | v1 前缀/主题 | 代表端点（file:line） | v1/legacy 条数 |
|---|---|---|---|
| `api/auth.py` | `/auth` | `POST /auth/login` :385；`POST /auth/totp/setup` :474 | 8 / 8 |
| `api/api_keys.py` | `/api-keys` | `GET /api-keys` :80；`POST /{key_id}/revoke` :97 | 4 / 4 |
| `api/backups.py` | `/backups` | 分片上传 5 连 `upload/{init,chunk,complete,abort,status}` :173-295 | 14 / 14 |
| `api/bots.py` | `/bots` | `GET /bots/stats` :84；`POST /bots/test` :137 | 14 / 4 |
| `api/chat.py` | `/chat` | `GET /chat/sessions/new` :111；`GET /chat/runs/{run_id}/stream` :191 | 13 / 13 |
| `api/chat_projects.py` | `/chat/projects` | workspace 文件树 :160-207 | 11 / 8 |
| `api/config_profiles.py` | `/config-profiles`,`/system-config`,`/config-routes` | `PUT /system-config` :194 | 15 / 16 |
| `api/conversations.py` | `/conversations` | `GET /conversations` :123（page/page_size :126） | 8 / 6 |
| `api/cron.py` | `/cron/jobs` | `POST /{job_id}/run` :109 | 5 / 5 |
| `api/extensions.py` | `/commands` | `GET /commands/conflicts` :98 | 3 / 5 |
| `api/files.py` | `/files` | `GET /files/tokens/{file_token}` :75（**无鉴权依赖**） | 12 / 1 |
| `api/knowledge_bases.py` | `/knowledge-bases` | `POST /{kb_id}/retrieve` :295 | 16 / 16 |
| `api/live_chat.py` | WebSocket | `/live-chat/ws` :35、`/unified-chat/ws` :40 | 2 / 2 |
| `api/logs.py` | `/logs`,`/trace` | `GET /logs/live`（SSE） :78；`GET /trace/history` :121 | 7 / 4 |
| `api/open_api.py` | 对外开放 API | `POST /chat` :173；`WS /chat/ws` :286；`POST /im/messages` :305 | 9 / 0 |
| `api/personas.py` | `/personas` | `POST /personas/move` :126；`/{persona_id}` :183 | 15 / 13 |
| `api/platform.py` | 平台注册/webhook | `POST /bot-types/{bot_type}/registration` :78 | 3 / 3 |
| `api/plugins.py` | `/plugins` | `GET /plugins` :697；`views/{view_name}/_t/{token}/` :1192 | 61 / 22 |
| `api/providers.py` | `/providers`,`/provider-sources` | `POST /providers/test` :323 | 31 / 11 |
| `api/sessions.py` | `/sessions` | `GET /sessions/rules` :142；`PATCH /sessions/provider` :196 | 12 / 12 |
| `api/skills.py` | `/skills` | `GET /skills/neo/candidates` :294 | 24 / 18 |
| `api/stats.py` | `/stats` | `GET /stats/versions` :77；`POST /system/restart` :148 | 13 / 12 |
| `api/subagents.py` | `/subagents` | `GET /subagents/config` :59 | 3 / 3 |
| `api/t2i.py` | `/t2i/templates` | `PUT /t2i/templates/active` :100 | 8 / 8 |
| `api/tools.py` | `/tools`,`/mcp/servers` | `PATCH /tools/{id}/permission` :197 | 13 / 9 |
| `api/updates.py` | `/updates`,`/pip/install` | `POST /updates/core` :137 | 7 / 7 |

（表中 per-module 数字为模块内装饰器计数，合计 v1 331 / legacy 224 = 555；因存在"同一函数多装饰器别名"（如 `api/plugins.py:885-886`）与 2 条通配 `api_route`，与全局 grep 精确值（`^@router\.` 328 条、`^@legacy_router\.` 227 条，其中方法级共 553 条）有约 3 条的归属差异。）

### 2.3 OpenAPI 与前端 SDK 生成流程

存在**两条互不相通**的 OpenAPI 出口：

- **运行时**：FastAPI 自身从装饰器生成，挂在 `/api/v1/openapi.json`、`/api/v1/docs`、`/api/v1/redoc`（`api/app.py:92-98`）。这是"代码即真相"。
- **契约/生成**：手写 YAML `openspec/openapi-v1.yaml`（`info.version: 0.1.0`，含 **241** 个 path 条目），前端 SDK 与公开文档都从它生成：
  - `dashboard/package.json:13` `generate:api` = 删旧目录 + `openapi-ts -i ../openspec/openapi-v1.yaml -o src/api/generated/openapi-v1 -c @hey-api/client-axios`
  - `package.json:14` `generate:docs:openapi` = `uv run python ../docs/scripts/update_openapi_json.py`，该脚本 README 式注释说明它读 YAML 产出 `docs/public/openapi.json` 与中英 scope 文档（`docs/scripts/update_openapi_json.py:12-15`），并硬编码 `PUBLIC_OPEN_API_EXCLUDED_PATHS = {"/api/v1/live-chat/ws","/api/v1/unified-chat/ws"}`（30-33）——即 live chat 的 WS 被排除在公开契约之外。
  - 生成物：`src/api/generated/openapi-v1/sdk.gen.ts`（3318 行）、`types.gen.ts`（3932 行）、`index.ts`，路径字符串与后端一一对应（如 `sdk.gen.ts:724` 的 `/api/v1/chat/ws`）。
  - 手写适配层 `src/api/v1.ts`（1910 行）在生成 SDK 之上再做业务门面：`openApiV1Client.setConfig({axios: httpClient, throwOnError: true})`（`v1.ts:66-69`），并以 `typed<T>()`（224-226）把响应断言成 `ApiEnvelope<T>`。它还保留了一条 **legacy 回退**：v1 报 404 或 message 含 "missing api key" 时自动重发 legacy `/api/*`（`v1.ts:228-269`），说明前端同时兼容两套 API。

---

## 3. 鉴权与安全边界

### 3.1 三层凭证

1. **Dashboard JWT**（人）：HS256，payload `{username, exp}`，7 天（`services/auth_service.py:447-458`）；同时下发 `astrbot_dashboard_jwt` cookie（`httponly=True, samesite="strict", secure` 默认按 adapter.debug/testing 推断，`path="/"`，`api/auth.py:234-247`；常量 `auth_service.py:82-83`）。前端却把 token 也存在 `localStorage`（`stores/auth.ts:25`），**双份凭证并存**。
2. **API Key**（机器）：`ApiKey  <key>` header / `?api_key=` / `X-API-Key` 三种取法（`api/auth.py:66-78`），DB 存 hash，按 scope 校验（132-164），并 `touch_api_key` 记录使用（158）。scope 全集 13 个：`bot/provider/persona/im/config/chat/data/file/plugin/mcp/skill` + `chat:admin`、`config:edit_admin`（`auth_service.py:55-80`），含继承 `config → (bot, provider)`（78-80）。`*` 通配可用（`api/auth.py:150`）。
3. **插件页 asset token**（受限资源）：`token_type="plugin_page_asset"`，TTL 7 天（`services/plugin_page_service.py:23-26`），以路径段 `_t/{token}` 出现在 URL 里（`api/plugins.py:1192-1193`），服务端校验 scope 是否匹配受保护路径（`plugin_page_auth.py:26-30`）。

### 3.2 中间件分界

`server.py:326-388` 的 auth 中间件有几个必须记住的**短路条件**：

- 非 `/api` 路径直接放行（328-329）——静态资源与 SPA 路由由后面的 static router 处理。
- **`/api/v1` 一律放行**（333-334），鉴权完全下移到每个路由的 `Depends(ScopeDependency(...))`。这意味着**任何一个 v1 路由忘写 Depends 就是裸奔**；这也解释了为什么像 `GET /files/tokens/{file_token}`（`api/files.py:75-80`，无 `_auth` 参数）是有意开放的。
- legacy 白名单：精确 5 条 + 前缀 5 条（336-349），其中包含 `/api/file`、`/api/v1/files/tokens`、`/api/platform/webhook`、`/api/backup/download`——**这些路径把凭证放在 URL 上**（`server.py:348` 注释明说"备份下载使用 URL 参数传递 token"）。
- 限流：仅 6 个端点（登录、TOTP setup、desktop-session、config 更新），按 IP 令牌桶，默认 `average_interval=1.0s / max_burst=3`（`server.py:129-138` 白名单 + 424-453 实现 + 163-197 注册表 1 小时过期）。测试模式 `ASTRBOT_TEST_MODE=true` 时跳过（430）。
- 登录暴破缓解：密码错误时 `await asyncio.sleep(3)`（`auth_service.py:314-315`），并对默认密码/旧 MD5 密码给出专门错误文案（87-102、316-320）。

### 3.3 密码与 TOTP

- 双哈希过渡：`pbkdf2_sha256` 为主、`password`(md5) 兼容旧数据（`password_state.py:82-94`；`_has_usable_pbkdf2_password` 校验迭代数/hex 长度 20-36）；`password_storage_upgraded`、`password_change_required` 两个标志位落盘（9-10、39-79）。
- `can_skip_default_password_auth()` 只有在 `ASTRBOT_DASHBOARD_SKIP_DEFAULT_PASSWORD_AUTH` 开启**且** host ∈ {127.0.0.1, localhost, ::1} 时才允许跳过默认密码（`auth_service.py:84-86,482-497`），且 host 解析顺序与 `server.py:637-641` 一致（env 优先）。
- TOTP：`astrbot/core/utils/totp.py`，恢复码 4×8=32 字符、PBKDF2 600k 迭代（16-23）；信任设备 cookie 30 天（`totp.py:16-17`、`api/auth.py:260-273`，`path="/api/auth"`）；登录时若命中恢复码会**自动关闭 TOTP 并吊销所有信任设备**（`auth_service.py:343-350`）。
- 前端配套：`DashboardTotp*` 共 4 个组件 + `AuthStageTotp.vue`；登录返回 `data.totp_required`（`auth_service.py:331-335`）驱动二级弹窗，`api/http.ts:76-91` 专门把它从"401 即踢回登录页"逻辑里豁免。

### 3.4 文件与静态资源边界

- **上传体限制**三层叠加：全局 `MAX_CONTENT_LENGTH = 128MB`（`server.py:291-293`）、按前缀覆盖表（`_BODY_LIMIT_OVERRIDES`，59-73，如分片 `CHUNK_SIZE*2`、`/api/v1/files` 为 `MAX_UPLOAD_FILE_SIZE_BYTES + 1MB`）、单文件保存时 `max_bytes` 校验（`api/multipart.py:28-33` → `core.utils.upload.save_upload_stream`）。multipart 缺 `Content-Length` 直接 411（82-126），避免 Starlette 先把大 body spool 到磁盘再拒。
- **分片上传**：init/chunk/complete/abort/status 五端点（`api/files.py:92-159`），实现下沉 `ChatService.upload_init/upload_chunk/upload_complete/...`（`services/chat_service.py:720-831`）与 `services/chunked_upload_service.py`；备份走同构的另一套（`api/backups.py:173-305`）。
- **URL token 文件**：`FileService.resolve_token_file` 直接委托 core 的 `file_token_service.handle_file`（`services/file_service.py:10-15`）。
- **静态托管**：`api/static_files.py:110-113` 把 `StaticFileService.INDEX_ROUTES`（19 条 SPA 入口，`services/static_file_service.py:7-27`）注册成 index 返回，其余 `/{static_path:path}` 走文件；路径穿越双重防护（`static_file_service.py:109-131`：先拒 `..`/绝对路径，再 `resolve()`+`relative_to(static_root)`）。HTML 与 `assets/version` 用 `Cache-Control: no-store`（`api/static_files.py:94-100`），其余 `no-cache`；支持读时 gzip（36-60 + `static_encoding.py`），Range 请求退回 `FileResponse`（44-45）。桌面端通过 `?astrbot_bundle=` 附带 `Clear-Site-Data: "cache"`（76-81、101-106）。
- **插件页 iframe 隔离**：CSP `object-src 'none'; base-uri 'self'` + `frame-ancestors 'self'`（非 launcher 模式）、`Referrer-Policy: no-referrer`、`X-Content-Type-Options: nosniff`，同时 `Access-Control-Allow-Origin: *`（`services/plugin_page_service.py:397-412`）。父子通信靠 `window.postMessage` RPC：channel `astrbot-plugin-page`、`requestId` 请求响应表、SSE 与 context 回调（`astrbot/dashboard/plugin_page_bridge.js:1-60`），并做 origin 白名单校验（16-41）。

---

## 4. 前端架构

### 4.1 技术栈（`dashboard/package.json`）

Vue `3.3.4`、vue-router `4.2.4`、pinia `2.1.6`、vuetify `3.7.11`、vite `6.4.1`、typescript `5.1.6`、vue-tsc `1.8.8`（19-77）。内容生态很重：`markstream-vue 1.0.5-beta.0`（流式 Markdown）、`shiki ^3.23.0`、`highlight.js 11.11.1`、`katex ^0.16.27`、`mermaid ^11.12.2`、`markdown-it ^14.1.1`、`dompurify ^3.3.2`、`monaco-editor ^0.52.2`、`@tiptap/* 2.1.7`、`event-source-polyfill ^1.0.31`、`@hey-api/client-axios 0.2.12`。

### 4.2 目录与规模（glob 实测）

- `.vue` **186** 个：`components/` 124（其中 `chat/` **37**（26 + `message_list_comps/` 11）、`shared/` **53**、`folder/` 8、`extension/` 10、`config/` 5、`provider/` 5、`platform/` 4、`conversation/` 1、根 1）、`views/` 56、`layouts/` 5、`App.vue` 1。
- `.ts` 约 **74** 个（`composables/` 13、`i18n/` 10、`stores/` 7、`router/` 4、`api/` 6（含 generated 3），其余为 utils/theme/types/plugins/layouts）。
- 单文件最大：`components/chat/Chat.vue` **2925** 行；`src/api/v1.ts` 1910；`views/Settings.vue` 1770；`composables/useMessages.ts` 1710；`components/chat/ChatInput.vue` 1532；`views/ExtensionPage.vue` 1370；`components/chat/MessageList.vue` 898。生成物 `types.gen.ts` 3932 / `sdk.gen.ts` 3318。

### 4.3 路由

`createWebHashHistory`（`src/router/index.ts:8-15`）—— **hash 路由**，所以后端只需要少数 index 入口即可深链接（与 `static_file_service.py:7-27` 的白名单对应）。路由文件拆三份：`MainRoutes.ts`（后台，`meta.requiresAuth`）、`AuthRoutes.ts`（login/setup）、`ChatBoxRoutes.ts`（独立聊天窗口，BlankLayout）。共 **53** 条 `path` 记录、**36** 条 `name` 声明（`PluginView`/`ExtensionMarketDetails` 各被 redirect 复用了两次，去重后约 34 个具名路由）。导航守卫只做"有 token 才放行 + 记住 returnUrl"（`index.ts:30-54`），不做权限分级（后端 scope 才是唯一权威）。

主要页面：`/welcome`、`/chat`(+`:conversationId`)、`/chatbox`、`/config`、`/providers`、`/platforms`、`/extension`（plugins/market/mcp/skills/components 五个子页 + `plugins/:pluginId` 详情）、`/persona`、`/session-management`、`/data`（statistics/conversations/logs/trace 四 tab）、`/cron`、`/subagent`、`/knowledge-base`、`/settings`、`/about`、`/plugin-view/:pluginName/:pageName`（插件页宿主）。此外保留大量 redirect 兼容（`/normal`→`/config`、`/console`→`/data?tab=logs`、`/extension-marketplace`→`ExtensionMarketplace` 等，`MainRoutes.ts:106-140,219-237`）。

### 4.4 状态与数据流

- **Pinia** 只装跨页轻状态：`auth`（token/username/version 检查）、`customizer`（主题）、`chatHeader`、`headerContext`、`mobileDrawer`、`personaStore`、`routerLoading`（7 个 store）。
- **重业务逻辑在 composables**：`useMessages`（1710 行，SSE/WS 双通道 + 重试 + 分片解析）、`useSessions`、`useConversations`、`useProjects`、`useChunkedUpload`、`useRecording`/`useVADRecording`（语音）、`useProviderModelSelection` 等 13 个。
- **API 客户端分层**：`api/http.ts`（axios 实例 + 拦截器）→ `api/generated/openapi-v1/*`（机器生成，按 operation 调）→ `api/v1.ts`（手写业务门面，按域组织 `authApi`/`chatApi`/`providerApi`/`t2iApi`/`logApi`…，并提供非标准接口如 `sendStreamUrl()` `unifiedWebSocketUrl()`，`v1.ts:822-835`）。`http.ts` 的两个安全细节：14/36-56 只在**同源**请求上附加 Bearer 与 `Accept-Language`；93-105 遇到 401 清 localStorage 并跳 `#/auth/login`（登录/设置类路径与 `totp_required` 除外）。
- **i18n**：自研轻量实现（非 vue-i18n 驱动 UI），`i18n/composables.ts:42-60` 按 `a.b.c` 拆键取值，缺键返回 `[MISSING: key]`（55）并 warn。词条按 **37 个 JSON bundle × 4 语言**（zh-CN/en-US/ru-RU/ja-JP，`locales/<lang>/{core,features,messages,features/alkaid,features/knowledge-base}/*.json`），运行时按语言异步 chunk 加载（25-37）。另有 `src/i18n/validator.ts` 做语言间比对，`scripts/check-i18n-keys.mjs` 做"源码用量 vs bundle"比对（见 §6）。

---

## 5. 实时通讯与渲染链路

### 5.1 WebChat 一句话链路

```
Chat.vue 发送 → useMessages.sendMessageStream(transport)
  ├─ SSE:  fetchWithAuth('/api/v1/chat')  → api/chat.py:84-108 StreamingResponse
  └─ WS :  new WebSocket('/api/v1/unified-chat/ws?token=') → api/live_chat.py:40
        ↓ 后端
ChatService.build_chat_stream (services/chat_service.py:1268)
  → platform_history_mgr.insert(用户消息)
  → webchat_queue_mgr.get_or_create_queue (1331-1360)
  → core 管线消费 → 逐条结果写回 run.back_queue
  → _consume_chat_run (1075-1260) 累积 BotMessageAccumulator 并 _publish_chat_run
        ↓ 订阅端
_subscribe_chat_run (972-1049): asyncio.Queue(maxsize=256) → "data: {json}\n\n"，1s 心跳 ": heartbeat\n\n"
```

关键设计点：

- **SSE 事件协议**：JSON payload 的 `type` 字段区分 `session_id`/`user_message_saved`/`run_snapshot`/`agent_stats`/`attachment_saved`/`message_saved`/`error`（`chat_service.py:1012-1045,1146-1245`）；消息 part 类型 `plain`/`think`/`tool_call`（`chat_service.py:208,226,237`）。
- **可恢复流**：`GET /chat/runs/{run_id}/stream`（`api/chat.py:191-210`）可对进行中的 run 二次订阅，先推 `run_snapshot`（含累积 content、revision），再按 `revision` 增量续推（`chat_service.py:994-1045`）；慢消费者被踢出队列（`QueueFull` → 丢弃并放 `None` 结束流，965-970），前端据此重连。
- **背压与超时**：订阅队列 256（42-43），心跳兜住空闲连接；`Chat.vue` 端 `resumeRunStreamUrl` 最多重试 5 次（`useMessages.ts:865-884`）。
- **双通道默认值**：`transportMode` 从 `localStorage("chat.transportMode")` 读，**默认 SSE**，只有显式存 `"websocket"` 才走 WS（`Chat.vue:819-826`）；`sendMessageStream` 按 transport 分支（`useMessages.ts:521-548`）。因此 WS 是可选加速路径而非主路。
- **live_chat 多路复用**：一条 WS 连接同时承载 `ct="chat"` 与 `ct="live"`（语音/虚拟人）两类消息，`force_ct` 可锁定（`api/live_chat.py:19-42`；`services/live_chat_service.py:168-269`），`ct=chat` 时 `t=send` 按 `message_id` 去重并 spawn 独立 task（212-246）；`live` 分支处理音频帧与语音回合（`LiveChatSession.start_speaking/add_audio_frame/end_speaking`，66-114）。
- **对外 WS**：`/api/v1/chat/ws` 用 API Key 鉴权并复用 `ChatService` 桥接（`api/open_api.py:286-302`）。
- **日志/Trace 实时**：`/api/v1/logs/live` 支持 `Last-Event-ID`（`api/logs.py:78-84`），前端用 `event-source-polyfill` 的 `EventSourcePolyfill` 消费（`ConsoleDisplayer.vue:402`、`TraceDisplayer.vue:638`）。

### 5.2 流式渲染与 T2I 配合

- Markdown 渲染统一走 `markstream-vue` 的 `<MarkdownRender>`，流式中开启 `smooth-streaming="auto"` 并把活跃节点上限压到 320（`message_list_comps/MarkdownMessagePart.vue:3-16`、`markdownRenderConfig.ts:3`）；`isDark` 与 `webSearchResults` 通过 `provide` 注入，供 RefNode 之类子组件取引用（`MarkdownMessagePart.vue:49-50`）。
- 代码高亮：`vite.config.ts:89-97` 把 `shiki` 别名替换为 `utils/shikiLimitedBundle.js`（裁剪语言子集），另有 `ThemeAwareMarkdownCodeBlock.vue`、`mermaid`/`katex` 按需引入。
- **T2I（文本转图片）是服务端渲染，前端只做模板编辑与预览**：模板 CRUD 与激活走 `/api/v1/t2i/templates*`（`api/t2i.py:71-152`），前端 `components/shared/T2ITemplateEditor.vue` 用 iframe `srcdoc` 做 live preview，并注入 `<script src="/t2i/shiki_runtime.iife.js">`（`T2ITemplateEditor.vue:312-325`）；该 runtime 是构建期从 core 目录拷贝进 dist 的静态资产（`vite.config.ts:10-56` 的 `t2iShikiRuntimeAsset`），从而**前端预览与后端出图共用同一份高亮 runtime**。配置项里以 `_special === 't2i_template'` 挂载编辑器（`ConfigItemRenderer.vue:52-53`）。

---

## 6. 构建与部署协同

### 6.1 前端产物如何被后端托管

- 后端优先序完全由 `astrbot/core/dashboard_assets.py:179-227` 决定：显式 `webui_dir` → 数据目录 `data/dist` → **随包内置** `astrbot/dashboard/dist` → 桌面模式下的降级。桌面托管后端拒绝版本不匹配的显式目录（196-215），非桌面只 warn 并照用（205-215）。
- **版本闸门**：dist 根下 `assets/version` 文件即版本（`_read_dashboard_version`，23-38），与 core `VERSION` 做**相等比较**（`_is_version_compatible` 71-95，经 `VersionComparator.compare_version == 0`）；同时校验 `index.html` 引用的 js/css 真实存在且无路径穿越（`_is_dist_complete` 114-156），缺一即视为不可用。
- **联网补包**：缺失/过期时从 `astrbot-registry.soulter.top/download/astrbot-dashboard/{version}/dist.zip` 下载，失败回退 GitHub release；40 位 SHA 走 `astrbot-release-harbour`（`dashboard_assets.py:310-365`），解压用 staging + 校验 + 替换（377-399）。
- 前端不直接读 dist 的版本文件，而是靠 `GET /api/v1/stats/versions` 返回的 `version` / `dashboard_version`（`services/stat_service.py:117,137,185-202`）在浏览器端比对：登录后若 core 与 dashboard 版本不等，抛 `upgrade_recovery_required` 并置 blocking 事件（`stores/auth.ts:85-113`），由 `UpgradeRecoveryDialog.vue:215` 接管。

### 6.2 CI 与质量闸门

`.github/workflows/dashboard_ci.yml`（65 行）在 Node 24.13.0 + pnpm 10.28.2 上执行：`pnpm run i18n:check`（29-31）→ `pnpm install --frozen-lockfile && pnpm run build`（33-37）→ 把 `git rev-parse HEAD` 写入 `dashboard/dist/assets/version` 并打 `dist.zip`（39-46）→ 上传 artifact，master push 时自动发到 `astrbot-release-harbour` 的 `release-<sha>` tag（56-65）。

构建链本身：`build = subset-mdi-font.mjs && vue-tsc --noEmit && vite build`（`package.json:9`），即**类型检查是构建前置**；`build:t2i-shiki-runtime` 单独生成 T2I 高亮 runtime（8）。vite 关闭 sourcemap、chunk 警告阈值 1MB（`vite.config.ts:109-112`），dev server 把 `/api` 代理到 `127.0.0.1:6185` 并开 `ws: true`（117-126）。

i18n 完整性双保险：`scripts/check-i18n-keys.mjs`（414 行）从 `index.ts` 结构化解析 bundle 树，比对"源码里静态可解析的 key"与 4 个语言包，注释里写清了动机——`[MISSING: key]` 是**真值**，`t(k) || fallback` 永不回退，且"所有语言都缺"的键在语言间比对里不可见（1-18, 29-31, 33-38, 83-85 还处理了 ru-RU 的 BOM）。

### 6.3 测试

dashboard 无前端单测脚本（`package.json` scripts 里只有 typecheck/lint/build/preview/i18n:check），后端测试在其他 workflow（`unit_tests.yml`/`coverage_test.yml`/`smoke_test.yml` 存在，未逐行细读；`server.py:430` 的 `ASTRBOT_TEST_MODE` 与 `asgi_runtime.py:710-712` 的 `test_client()` 说明后端 dashboard 有测试专用入口）。前端单测覆盖情况：**未验证**（未见 vitest/jest 依赖）。

---

## 7. 问题、风险与技术债 + 关键结论

### 7.1 风险清单

1. **双 API 面的维护成本**：553 条路由里 225 条是 legacy（`api/app.py:198-223` 一次性挂 25 个 legacy router），每个新能力都在一个文件里写两遍（`api/chat.py` 13+13、`api/knowledge_bases.py` 16+16）。legacy 全部 `include_in_schema=False`，既不在 OpenAPI 也不在测试契约里，属于**静默腐化区**。
2. **v1 鉴权全靠自律**：中间件对 `/api/v1` 直接 `return None`（`server.py:333-334`），安全性完全依赖 328 条路由各自写对 `Depends(ScopeDependency(...))`。已有先例说明这是"约定"而非"强制"：`api/files.py:75-80` 的 token 下载端点、`api/platform.py:90-101` 的 webhook 都无 scope 依赖。建议加一条启动期断言（"所有 v1 路由必须存在 ScopeDependency 或显式白名单"）。
3. **URL 即凭证**：`/api/file/{token}`、`/api/v1/files/tokens/{token}`、`/api/backup/download?token=`、插件页 `_t/{token}`（TTL 7 天）都把凭证放进 URL（`server.py:343-349`、`plugin_page_service.py:23-26`），会进浏览器历史/代理日志/Referer。
4. **错误契约不统一**：`responses.py:18-22` 的 `error()` 只产出 body，多数 route 直接 `return error(...)` → **HTTP 200 + `status:"error"`**（如 `api/chat.py:70-75`）；而 `ApiError` 走 4xx（`api/app.py:162-167`）、`ValueError` 统一 400、兜底 500（169-196）。前端因此必须同时判 `response.data.status` 与 HTTP code（`http.ts:108-113` 还要特判 429 的 message），客户端一致性成本高。
5. **巨型组件与巨型 composable**：`Chat.vue` 2925 行、`useMessages.ts` 1710 行、`Settings.vue` 1770 行、`ChatInput.vue` 1532 行。chat 域 37 个 `.vue` 全压在单页，且同时维护 SSE 与 WS 两条路径、`MessageList.vue` 与 `MessageListDEPRECATED.vue` 并存——单点改动回归面极大。
6. **两份 OpenAPI 真相源**：运行时由装饰器生成（`api/app.py:92-98`），文档/SDK 由手写 `openspec/openapi-v1.yaml`（241 path）生成（`package.json:13-14`）。328 条 v1 路由 vs 241 条 spec 条目，缺口靠 `include_in_schema=False` 与别名解释，但**没有 CI 校验二者一致**（`dashboard_ci.yml` 只跑 i18n + build），漂移只能靠人发现。
7. **Socket 层是唯一"活状态"**：`ChatService.chat_runs` 是进程内 dict（`services/chat_service.py:1068-1073` 直接 `self.chat_runs.get`），`webchat_queue_mgr` 也是内存队列（`webchat_queue_mgr.py:7-46`）。多 worker 部署（Hypercorn 多进程）下断流恢复会失效；重启后所有未完成 run 丢失。当前单进程 `serve()` 恰好规避，属**隐式约束**。
8. **兼容垫层长期化**：`asgi_runtime.py` 715 行 contextvars 代理 + Quart 内嵌 app（648-654），让一半 service 仍写在 Flask 全局上下文里（`request.xxx`）。这是"迁移未完成"的化石层，会增加阅读成本与隐式依赖（如 `g.username` 从 `server.py:375-377` 注入，`api/auth.py:81-91` 再兜底读）。
9. **前端凭证双写**：JWT 同时下发 httponly cookie（`api/auth.py:239-247`）与写 `localStorage`（`stores/auth.ts:25`）。cookie 侧已安全，但 `localStorage` 侧让 XSS 可直接窃取 7 天令牌。
10. **i18n 完整性依赖脚本自律**：`[MISSING: key]` 为真值（`composables.ts:52-56`）、`t(k) || fallback` 永不回退、全语言共缺的 key 在语言间比对中隐形（`check-i18n-keys.mjs:8-13` 自述）。虽有 CI 闸门，但仅在 dashboard 目录改动时生效，且 4 语言 × 37 bundle 的同名键维护成本高（ru-RU 有 BOM 这类历史包袱，`check-i18n-keys.mjs:83-85`）。
11. **插件页安全面**：`Access-Control-Allow-Origin: *` 与 `frame-ancestors 'self'` 同时存在（`plugin_page_service.py:403-410`），iframe 内 postMessage 用 origin 白名单兜底（`plugin_page_bridge.js:30-41`），但 `SELF_ORIGIN === "null"` 时直接放行任意 origin（37-40），sandbox/本地文件场景下偏松。
12. **上传体积常量分散**：`MAX_CONTENT_LENGTH`（`server.py:291`）、`MAX_UPLOAD_FILE_SIZE_BYTES`（`chat_service.py:45-46`，512MB）、`MAX_FILE_BYTES`（`services/config_service.py` 引入于 `server.py:32`）、`CHUNK_SIZE`（`services/backup_service.py`）四套阈值跨模块拼装（`server.py:59-73`），调一个要改四处。

### 7.2 关键结论（10 条）

1. **架构定性**：WebUI = "与 core 同事件循环并列的 Hypercorn/FastAPI 服务 + 全量业务下沉到 29 个 service"，HTTP 层只做参数校验、鉴权与 envelope 包装；真正的状态在 service + DB + 内存队列。证据：`initial_loader.py:36-54`、`api/app.py:113-160`、`api/router.py:37-97`。
2. **规模**：后端 26 个路由模块 / 29 个 service 模块 / 553 条方法级路由（v1 328 + legacy 225）；前端 186 个 `.vue`（chat 域 37、shared 53）+ 约 74 个 `.ts`，最大单文件 `Chat.vue` 2925 行。
3. **双轨 API 是当前最大结构债**：legacy 225 条不可见于 OpenAPI，而 v1 又只覆盖 241 条 spec 条目中的一部分；契约与实现之间没有自动一致性检查。
4. **鉴权是"分层而非集中"**：中间件只负责 legacy `/api` 与插件页桥，`/api/v1` 全权交给每路由的 `ScopeDependency`（13 个 scope、含 `chat:admin`/`config:edit_admin` 两个 admin 级、`config→{bot,provider}` 继承）。
5. **安全设计有意识也有缺口**：默认密码强制轮换 + 启动日志随机密码 + PBKDF2 升级 + TOTP/恢复码 + 登录 3s 延迟 + IP 令牌桶 + 上传 triple limit 都在；缺口是 URL 凭证、`localStorage` token、v1 依赖自律。
6. **实时链路已从 SSE 一元演进为 SSE+WS 双模**，SSE 用 `revision` 做断点续传、256 队列 + 心跳做背压/保活，WS 复用同一 `ChatService` 桥并做 `ct=chat/live` 多路复用；默认仍是 SSE，WS 属可选。
7. **前端渲染栈以"流式"为核心**：`markstream-vue` + 320 活节点上限 + shiki 裁剪包 + T2I 预览/serve 共用 `shiki_runtime.iife.js`，前端只编模板不做出图。
8. **版本协同是强约束**：dist 内 `assets/version` 必须与 core `VERSION` 严格相等，否则桌面端直接拒加载、非桌面端 warn；CI 用 commit SHA 盖版本戳并自动发到专用 release 仓库。
9. **可观测性闭环完整**：`/logs/live`（Last-Event-ID 续传）+ `/logs/history` + `/logs/export`（zip + 脱敏开关）+ `/trace/history`（cursor 分页、errors/models/plugins 过滤）+ `/trace/{id}`，前端有 `ConsoleDisplayer`/`TraceDisplayer`/`TracePage` 对应。
10. **用户量最大的功能是 chat，也最脆**：单文件 2925 行、双传输、流中编辑/重生成/线程/项目/工作区文件/语音全在一个域内，且 `MessageListDEPRECATED.vue` 说明重构未收尾；这是后续任何 chat 相关改动的头号回归风险点。

---

### 附：本篇未验证项

- `astrbot/api/web.py`：本仓库该文件**不存在**（glob 未命中），WebUI 入口实为 `astrbot/dashboard/server.py`。
- 前端单测/覆盖率、`dashboard/dist` 实际产物内容、`shikiLimitedBundle.js` 的语言子集范围：未逐行验证。
- 多 worker / Hypercorn 多进程部署下的实际表现：为代码推断，未运行验证。