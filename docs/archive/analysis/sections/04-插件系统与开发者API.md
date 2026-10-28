# 04 插件（Star）系统与开发者 API

> 分析对象：AstrBot v4.29.3，仓库根 `AstrBot-master/AstrBot-master`；范围 `astrbot/core/star/**`、`astrbot/api/**`、`astrbot/builtin_stars/**`、插件依赖安装工具。
> 引用格式：相对仓库根的 `路径:行号`，均为实际读到的行；未验证项会显式标注。

## 1. 职责与边界

插件（Star）系统是 AstrBot 唯一官方扩展面：外部代码通过「继承 `Star` + 装饰器声明」把处理器注入进程内全局注册表，再经核心消息管线调度。它承担四件事：**发现与生命周期**（`astrbot/core/star/star_manager.py:193` `PluginManager`）、**声明式注册**（`astrbot/core/star/register/star_handler.py:38` → `astrbot/core/star/star_handler.py:247` `StarHandlerMetadata`）、**运行时能力注入**（`astrbot/core/star/context.py:130-178`）、**对外契约包装**（`astrbot/api/all.py` 聚合导出）。

| 能做（插件侧） | 依据 | 做不到（需改核心） |
| --- | --- | --- |
| 注册命令/正则/事件监听/LLM 工具/Agent/平台适配器 | `register/star_handler.py:75,208,262,277,297,312,586,697` | 修改管线 Stage 顺序或插入新 Stage |
| 监听 16 种事件 | `star_handler.py:219-241`（`EventType`） | 替换 Provider 内部实现、改 LLM 协议 |
| 主动发消息、调 LLM/Agent、读写 KV 与配置 | `context.py:655,180,224`，`plugin_kv_store.py:14-28` | 绕过全局注册表直接改核心单例 |
| 注册 Web API、提供 Web 面板视图 | `context.py:764`，`dashboard/api/plugins.py:209-242` | 改 Dashboard 前端构建产物（只能放静态 views） |
| 声明 `requirements.txt` 自动装依赖 | `star_manager.py:353-379` | 依赖隔离（默认装进主环境，见 7.3） |

## 2. 插件模型与生命周期（含目录结构、file:line）

### 2.1 类模型：Star 基类与 StarMetadata

- `Star(CommandParserMixin, PluginKVStoreMixin)`：所有插件父类（`astrbot/core/star/base.py:18`），签名 `__init__(self, context: Context, config: dict | None = None)`（`base.py:27`），内部按模块名解析专属 logger（`base.py:34-51`）。
- **自动注册**：`Star.__init_subclass__` 在子类定义时即写入 `star_map`/`star_registry`（`base.py:53-64`）。故 v3.5.19 之后**不再需要** `@star.register`，该装饰器已废弃且只警告一次（`register/star.py:8-45`）。
- `StarMetadata` 是中枢数据结构（`star.py:17-88`）：`name/author/desc/short_desc/version/repo`、`star_cls_type/star_cls/module`、`root_dir_name`、`reserved`（保留插件不可卸载）、`activated`、`config`、`star_handler_full_names`、`display_name`、`logo_path`、`support_platforms`、`astrbot_version`、`i18n`、`views`；`plugin_id` 为 `author/name` 小写归一（`star.py:78-82`）。
- 基类内置 `text_to_image()`（`base.py:66`）与 `html_render()`（`base.py:104`），委托 `context.html_renderer`；`initialize()`/`terminate()` 为生命周期钩子（`base.py:141-148`，`__del__` 已废弃）。

### 2.2 插件目录结构（由加载器实际读取的文件决定）

```
data/plugins/<plugin_dir>/            # 或 astrbot/builtin_stars/<plugin_dir>/（reserved）
├── main.py  |  <plugin_dir>.py       # 二选一，决定模块名（star_manager.py:291-320）
├── metadata.yaml | metadata.yml      # 必需（updater.py:28）
├── requirements.txt                  # 可选，自动 pip 安装（star_manager.py:358）
├── _conf_schema.json / logo.png      # 可选，配置 schema 与插件图标（star_manager.py:212-213,1161-1174）
├── README.md / CHANGELOG.md          # 供 Dashboard 展示（dashboard/api/plugins.py:832,844）
├── views/ | pages/                   # 可选，Web 面板视图根（plugin_page_service.py:29）
└── .astrbot-plugin/i18n/<locale>.json  # 可选，i18n（star_manager.py:568-608）
```

`metadata.yaml` 的**强制字段仅 4 个**：`name`、`desc`、`version`、`author`，须为非空字符串（`astrbot/core/star/updater.py:29,377-396`）；`description` 作为 `desc` 别名兼容（`star_manager.py:523-524`）。可选字段 `repo`（决定能否更新）、`display_name`、`support_platforms`、`astrbot_version`（PEP 440 范围）、`views`/`pages`、`i18n`（`star_manager.py:530-563`）。保留插件示例：`astrbot/builtin_stars/astrbot/metadata.yaml:1-4`。

### 2.3 加载流程（`PluginManager.load`，`star_manager.py:1076`）

1. 读全局偏好 `inactivated_plugins` / `inactivated_llm_tools` / `alter_cmd`（`1095-1097`）——**启用状态持久化在 shared_preferences，不在插件目录**。
2. 扫描模块清单（`1099` → `_get_plugin_modules` `322-331` → `_get_modules` `291-320`），跳过 `.plugin-install-` / `.plugin-upload-` / `.plugin-backup-` 暂存目录（`298`），并计算模块路径 `data.plugins.<dir>.<module>` 或 `astrbot.builtin_stars.<dir>.<module>`（`1122-1123`）。
3. 带依赖恢复地导入：`_import_plugin_with_dependency_recovery`（`435`，调用点 `1135`），恢复策略四档见 `ImportDependencyRecoveryMode`（`89-93`）。
4. 若有 `_conf_schema.json`，构建 `AstrBotConfig`（`1161-1173`），配置文件落 `data/config/<dir>_config.json`（`1168-1171`）。
5. **元数据双轨**：类经 `__init_subclass__` 进入 `star_map[path]`（`1176-1178`），随后 **yaml 覆盖**（`1182-1196`，yaml 优先）；若模块未注册 `Star` 子类，则走 v3.4 及更早的"类名以 Plugin 结尾或名为 Main"启发式（`1313-1373`，`_get_classes` `279-288`）。
6. 版本校验：`astrbot_version` 不满足当前 `VERSION` 即抛 `PluginVersionUnsupportedError` 拒绝加载（`1203-1213`，实现 `674-705`）。
7. 注入类属性 `name`/`author`/`plugin_id` 后实例化（`1220-1246`）；未激活插件只注册元数据不实例化（`1226-1249`）。
8. **把装饰器函数绑到实例**：对同模块 handler 做 `functools.partial(raw_handler, star_cls)`（`1263-1279`）；LLM 工具同样处理并按 `inactivated_llm_tools` 置 `active`（`1282-1311`）。
9. 应用 `alter_cmd` 保存的权限：已有 `PermissionTypeFilter` 则改写、否则插入（`1390-1418`）。
10. `await star_cls.initialize()`（`1423-1424`）→ 触发 `OnPluginLoadedEvent`（`1427-1437`）→ 清理 pip 引入的 root logging handler（`1470-1472`）→ `sync_command_configs()`（`1474`）。
11. 任何异常 → 记入 `failed_plugin_dict` 并 `_cleanup_plugin_state` 回滚（`1439-1455`，`776-819`）；失败插件可单独重载（`reload_failed_plugin` `990-1016`）。

### 2.4 卸载 / 禁用 / 重载 / 热重载 / 依赖安装

- **卸载** `uninstall_plugin`（`1678`）：reserved 插件直接拒绝（`1699`），错误文案集中（`error_messages.py:3-10`）。**禁用** `turn_off_plugin`（`1905-1929`）：`_terminate_plugin` → 写 `inactivated_plugins` → LLM 工具 `active=False`；**启用** `turn_on_plugin`（`1981-1999`）反向后 `reload`。
- **终止** `_terminate_plugin`（`1932-1979`）：子类自定义 `__del__` 走线程池（`1947-1964`），否则 `await terminate()`（`1965-1966`），随后触发 `OnPluginUnloadedEvent`（`1968-1979`）。
- **重载** `reload`（`1018-1074`）：不指定插件名时终止**所有**插件并 `star_handlers_registry.clear()` / `star_map.clear()` / `star_registry.clear()`（`1054-1056`）后 `load()`——"全量重载"是一次注册表推倒重建。
- **解绑** `_unbind_plugin`（`1799-1861`）：删 map/registry 条目、按模块路径删 handler（`1814-1828`）、按 `handler_module_path` 删 LLM 工具（`1830-1841`）、按模块前缀注销平台适配器（`1845-1853`）、从 `sys.modules` 清模块（`1858-1861` → `_purge_modules` `742-774`）。
- **热重载**：仅当 `ASTRBOT_RELOAD=1` 时启动 `watchfiles.awatch`（`222-223`、`225-240`），按 `commonpath` 判定归属并 `reload(plugin_name)`（`264-277`）；缺 `watchfiles` 只警告（`59-65`）。
- **依赖安装**：`requirements.txt` → `_install_requirements_with_precheck`（`139-190`）→ `plan_missing_requirements_install`（`core/utils/requirements_utils.py:465`）只装缺失项 → `pip_installer.install`（`core/utils/pip_installer.py:1019-1082`），默认带核心约束 `-c`（`1070-1076`），仅打包桌面端 `--target data/site-packages`（`1053-1067`）。
- **安装/更新**：`install_plugin`（`1620`）先下载到 staging（`1645-1653`），`_install_plugin_from_directory`（`2037`）执行"备份 → 替换 → 加载 → 失败回滚"（`2097-2146`）；`update_plugin`（`1863`）更新后补装依赖再 `reload`（`1897-1903`）。

## 3. 处理器注册与过滤器链（表格 + 示例链路）

### 3.1 注册表与元数据

- `StarHandlerRegistry`（`star_handler.py:14`）持有 `star_handlers_map: dict[full_name, md]` 与有序列表 `_handlers`；`append()` 每次插入后**按 priority 降序全量重排**（`19-26`），缺省 0。
- Handler 全名恒为 `f"{handler.__module__}_{handler.__name__}"`（`register/star_handler.py:31-35`），是去重与命令配置主键。
- `get_handlers_by_event_type(event_type, only_activated=True, plugins_name=None)`（`150-186`）过滤：事件类型（`159`）、`enabled`（`161`）、插件 `activated`（`164-167`）、`plugin_set` 白名单——但 `OnAstrBotLoaded`/`OnPlatformLoaded`/`OnPluginLoaded`/`OnPluginUnloaded` 与 `reserved` 插件豁免（`175-183`）。
- `StarHandlerMetadata`（`247-281`）：`event_type`、`handler_full_name`、`handler_name`、`handler_module_path`、`handler`（异步函数或 partial）、`event_filters: list[HandlerFilter]`、`desc`（默认取 `__doc__`，`register/star_handler.py:63-67`）、`extras_configs`（含 priority）、`enabled`。
- `EventType` 16 种（`219-241`）：`OnAstrBotLoaded/OnPlatformLoaded/AdapterMessageEvent/OnWaitingLLMRequest/OnLLMRequest/OnLLMResponse/OnAgentBegin/OnAgentDone/OnDecoratingResult/OnCallingFuncTool/OnUsingLLMTool/OnLLMToolRespond/OnAfterMessageSent/OnPluginError/OnPluginLoaded/OnPluginUnloaded`。

### 3.2 装饰器全表（`astrbot/core/star/register/star_handler.py`）

| 装饰器 | 定义行 | 目标 EventType | 追加的过滤器/副作用 |
| --- | --- | --- | --- |
| `command(name, sub_command=None, alias=None, **kw)` | `75` | `AdapterMessageEvent` | `CommandFilter`（`108,121-123`） |
| `command_group(name, sub_command, alias)` | `208` | `AdapterMessageEvent` | `CommandGroupFilter`，返回 `RegisteringCommandable` 供链式注册子指令（`236-259`） |
| `custom_filter(cls, raise_error=True)` | `129` | `AdapterMessageEvent` | 实例化 `CustomFilter`，挂 handler 或指令组（`155-201`） |
| `event_message_type(t)` | `262` | `AdapterMessageEvent` | `EventMessageTypeFilter`（`271`） |
| `platform_adapter_type(t)` | `277` | `AdapterMessageEvent` | `PlatformAdapterTypeFilter`（`289-291`） |
| `regex(pattern)` | `297` | `AdapterMessageEvent` | `RegexFilter`（`306`） |
| `permission_type(t, raise_error=True)` | `312` | `AdapterMessageEvent` | `PermissionTypeFilter`（`329-331`） |
| `on_astrbot_loaded` / `on_platform_loaded` | `337` / `347` | 同名事件 | 无过滤器 |
| `on_plugin_error` | `357` | `OnPluginErrorEvent` | 参数 `event, plugin_name, handler_name, error, traceback_text`（`360-365`） |
| `on_plugin_loaded` / `on_plugin_unloaded` | `375` / `392` | 同名事件 | 参数 `metadata` |
| `on_waiting_llm_request` | `409` | `OnWaitingLLMRequestEvent` | "思考中"提示用 |
| `on_llm_request` / `on_llm_response` | `433` / `456` | 同名事件 | 参数 `(event, req)` / `(event, response)` |
| `on_agent_begin` / `on_agent_done` | `479` / `507` | 同名事件 | 参数含 `ContextWrapper[AstrAgentContext]` |
| `on_using_llm_tool` / `on_llm_tool_respond` | `537` / `561` | 同名事件 | 参数 `tool, tool_args[, tool_result]` |
| `llm_tool(name=None)` | `586` | `OnCallingFuncToolEvent` | 解析 docstring 生成 JSON Schema 并 `llm_tools.add_func`（`631-670`） |
| `agent(name, instruction, tools, run_hooks)` | `697` | — | 构造 `Agent` + `HandoffTool` 塞进 `llm_tools.func_list`（`714-725`） |
| `on_decorating_result` / `after_message_sent` | `730` / `744` | 同名事件 | 无过滤器 |
| `register_star`（已废弃） | `register/star.py:8` | — | 仅写元数据，已无必要 |

短名映射在 `astrbot/api/event/filter/__init__.py:11-41`（`command`/`regex`/`llm_tool`/`on_llm_request` …），因此插件写 `from astrbot.api.event import filter` 后可用 `filter.command`。

### 3.3 过滤器链语义

- 抽象基类 `HandlerFilter.filter(event, cfg) -> bool`（`filter/__init__.py:8-12`）；实际使用为 `if not filter.filter(...): passed=False`（`waking_check/stage.py:182-184`），即**返回 False 表示不放行**。
- 组合规则：同一 handler 的 `event_filters` 为 **AND、按列表顺序短路**（`waking_check/stage.py:169-195`）；`CommandFilter` 内部的 `custom_filter_list` 亦为 AND（`filter/command.py:87-91`）；`CustomFilter` 支持 `&`/`|` 组合（`filter/custom_filter.py:9-34,37-62`）。
- `CommandFilter`（`filter/command.py:31`）：受 `wake_prefix` 约束（`192-193`），整词匹配并归一空白（`199`），支持 alias 与父子指令拼接（`175-183`）；注册时用 `inspect.signature` 建参数表并**跳过前两个参数（self、event）**（`66-79`）；`GreedyStr` 必须为最后一个参数，否则报错（`15,102-113`）；类型强转覆盖 `int/float/bool/str/Union[T,None]`（`93-173`）。
- `RegexFilter`：**不受 wake_prefix 约束**（`filter/regex.py:9,17-18`）。`CommandGroupFilter`：整词组名匹配以区分 `math` 与 `mathematics`（`filter/command_group.py:113-120`），子指令靠 `parent_group.get_complete_command_names()` 拼接（`40-62`）。
- `EventMessageTypeFilter` 用 `enum.Flag` 位与（`filter/event_message_type.py:10-14,28-33`）；`PlatformAdapterTypeFilter` 支持 20 个平台名 + `ALL`（`filter/platform_adapter_type.py:9-54,57-74`）。
- `PermissionTypeFilter` 是异类：`ADMIN`/`GROUP_ADMIN` 走 `event.is_admin()`，`SHARED_GROUP_ADMIN` 额外放行 `_session_isolated`（`filter/permission.py:43-52`），且失败时**不立即短路**而是延后统一处置（见下）。
- **排序与中断**：handler 按 priority 降序执行（`star_handler.py:26`），例如 `astrbot/builtin_stars/astrbot/main.py:41` 用 `priority=maxsize-1` 抢在所有插件前处理"空 @"。执行侧 `StarRequestSubStage.process`（`process_stage/method/star_request.py:39`）：`event.is_stopped()` 立即 break（`40-41`）；每个 handler 返回后 `event.clear_result()` 防结果串味（`88-90`）；异常 → 记 trace → 触发 `OnPluginErrorEvent`（`108-115`）→ 若仍是唤醒命令则默认回显错误（`117-121`）→ `stop_event()`（`123`）。
- **真实调用链**（内置 `/name`，与第三方插件无差别）：适配器收到消息 → `WakingCheckStage.process`（`waking_check/stage.py:158`）遍历 `AdapterMessageEvent` handlers → 对每个 handler 依次跑 `CommandFilter`（`filter/command.py:191`，含 `is_at_or_wake_command` 与整词匹配）→ `PermissionTypeFilter`（`filter/permission.py:26`）→ 通过则 `event.set_extra("activated_handlers", ...)`（`237`）→ `StarRequestSubStage` 用 `call_handler(event, handler.handler, **params)` 注入解析后参数（`star_request.py:74`）→ `builtin_commands/main.py:40` 的 `name(self, event, alias: GreedyStr)` → 业务类 `commands/name.py:10` → `event.set_result(...)`。

## 4. 插件上下文与能力清单（API 表格）

`Context.__init__` 注入 12 个管理器（`context.py:141-178`），字段公开可读：`provider_manager`、`platform_manager`、`conversation_manager`、`message_history_manager`、`persona_manager`、`astrbot_config_mgr`、`kb_manager`、`cron_manager`、`subagent_orchestrator`。

| 能力 | API | 位置 |
| --- | --- | --- |
| 调 LLM（单轮，不执行工具） | `await context.llm_generate(chat_provider_id=..., prompt=..., system_prompt=..., contexts=...)` | `context.py:180-222` |
| 跑 Agent 工具循环 | `await context.tool_loop_agent(event=..., chat_provider_id=..., tools=..., max_steps=128, tool_call_timeout=120)` | `context.py:224-364` |
| 取当前会话模型 ID / Provider | `get_current_chat_provider_id(umo)`、`get_using_provider_async(umo)`（同步版已废弃 `493`） | `context.py:366-381,519-548` |
| 按 ID / 全量取 Provider | `get_provider_by_id`、`get_all_providers`、`get_all_tts/stt/embedding_providers` | `context.py:452-491` |
| 主动发消息 | `await context.send_message(session\|umo, MessageChain(...))`（qq_official 不支持） | `context.py:655-736` |
| 读写配置 | `context.get_config(umo=None) -> AstrBotConfig` | `context.py:638-653` |
| KV 存储 | `await self.put_kv_data(k,v)` / `get_kv_data(k,default)` / `delete_kv_data(k)`，靠 `self.plugin_id` 分区 | `plugin_kv_store.py:14-28`；`plugin_id` 由加载器注入 `star_manager.py:1224,1246` |
| 注册/启停 LLM 工具 | `context.add_llm_tools(*tools)`、`activate_llm_tool_async`、`deactivate_llm_tool_async`（同步版废弃 `397,429`） | `context.py:738-762,412-450` |
| 注册 Web API | `context.register_web_api(route, handler, methods, desc)` | `context.py:764-786` |
| 插件互查 / 工具管理器 | `get_registered_star(name)`、`get_all_stars()`、`get_llm_tool_manager()` | `context.py:383-395` |
| 平台实例 | `get_platform_inst(platform_id)`（`get_platform` 已废弃 `796`） | `context.py:820-834` |
| 数据库 / 事件队列 / Provider 注册 | `get_db()`、`get_event_queue()`、`register_provider()` | `context.py:836,792,844` |
| T2I / Jinja2 渲染 | `self.text_to_image(...)`、`self.html_render(...)` | `base.py:66-139` |
| 便捷工具 | `StarTools.send_message/create_message/create_event`、`activate_llm_tool_async` | `star_tools.py:35,57,104,155` |
| 等待用户下一条消息 | `@session_waiter(timeout)` + `SessionController` | `api/util/__init__.py:1-7`；实例 `builtin_stars/astrbot/main.py:102-121` |

**会话级开关**（持久化在 `sp` 的 `umo` scope，非内存）：`SessionPluginManager.is_plugin_enabled_for_session` / `filter_handlers_by_session`（`session_plugin_manager.py:11,49`）按 `session_plugin_config.disabled_plugins` 过滤 handler，**reserved 插件永不过滤**（`86-88`），无配置默认启用（`46`）；`SessionServiceManager` 管 `llm_enabled`/`tts_enabled`/`session_enabled`（`session_llm_manager.py:15,86,162`，写 `session_service_config`）。全局限定：`plugin_set` 决定哪些插件参与 `AdapterMessageEvent`（`waking_check/stage.py:150-161`），`disable_builtin_commands` 整体关内置命令（`162-167`）。

### 4.1 `astrbot/api` 的对外契约设计

- `astrbot/api/all.py`（55 行）是**一站式聚合面**：一次再导出 `AstrBotConfig`、`logger`、`html_renderer`、`llm_tool`、`MessageEventResult/MessageChain/CommandResult/EventResultType`、`AstrMessageEvent`、`command/command_group/event_message_type/regex/platform_adapter_type/register`、`Context`、`Star`、`config.*`、`Provider/ProviderMetaData/Personality`、`Platform/AstrBotMessage/MessageMember/MessageType/PlatformMetadata`、`register_platform_adapter`、`message_components.*`（`api/all.py:1-55`）。意义：给老插件一个**永不 ImportError 的稳定名字空间**，把内部重构挡在插件之外。
- 分层边界：`api/__init__.py:73-83` 只导出 `AstrBotConfig/BaseFunctionToolExecutor/FunctionTool/ToolSet/agent/html_renderer/llm_tool/logger/sp`；`api/star/__init__.py:1-7`、`api/event/__init__.py:1-17`、`api/event/filter/__init__.py:1-72`、`api/provider/__init__.py:1-18`、`api/platform/__init__.py:1-22`、`api/util/__init__.py:1-7` 各自维护 `__all__`。
- 兼容/弃用四类手法：① `deprecated` 装饰器 + 文档迁移，`Context` 内 10 处（`context.py:397,429,493,550,594,796,852,885,898,940`）；② 保留旧入口只发警告（`register/star.py:38-45`）；③ 双轨加载兜底，老式"类名以 Plugin 结尾 / Main"仍可加载（`star_manager.py:279-288,1313-1373`）；④ 字段/路由别名，`description`→`desc`（`star_manager.py:523-524`）、`pages`→`views`（`star_manager.py:556-561`、`plugin_page_service.py:27-29`、`dashboard/api/plugins.py:885-902`）。
- 插件 Web 请求对象也收敛在 `api` 层：`PluginRequest`（`api/web.py:166`）、`PluginRequestProxy`（`255`）、`PluginUploadFile`（`97`）、`PluginMultiDict`（`22`），避免插件直接依赖 starlette。

### 4.2 插件作者最常用的 20 个 API

| # | API | 位置 |
| --- | --- | --- |
| 1 | `class X(star.Star)` + `super().__init__(context)` | `base.py:18,27` |
| 2 | `@filter.command("cmd")` | `register/star_handler.py:75` |
| 3 | `@filter.command_group("grp")` | `register/star_handler.py:208` |
| 4 | `@filter.regex(r"...")` | `register/star_handler.py:297` |
| 5 | `@filter.event_message_type(EventMessageType.ALL, priority=N)` | `register/star_handler.py:262` |
| 6 | `@filter.permission_type(PermissionType.ADMIN)` | `register/star_handler.py:312` |
| 7 | `@filter.on_llm_request()` / `@filter.on_llm_response()` | `register/star_handler.py:433,456` |
| 8 | `@filter.on_decorating_result()` / `@filter.after_message_sent()` | `register/star_handler.py:730,744` |
| 9 | `@llm_tool(name=...)` | `register/star_handler.py:586` |
| 10 | `@filter.on_waiting_llm_request()` | `register/star_handler.py:409` |
| 11 | `event.set_result(MessageEventResult().message(...))` | `astr_message_event.py:370` |
| 12 | `event.plain_result(text)` / `event.chain_result([...])` | `astr_message_event.py:455,468` |
| 13 | `await event.send(MessageChain().message(...))` | `astr_message_event.py:534` |
| 14 | `event.stop_event()` / `event.is_stopped()` | `astr_message_event.py:399,415` |
| 15 | `event.request_llm(prompt=..., session_id=...)` | `astr_message_event.py:476` |
| 16 | `event.unified_msg_origin` / `get_sender_id()` / `get_group_id()` | `astr_message_event.py:157,254,246` |
| 17 | `self.context.get_config(umo=...)` | `context.py:638` |
| 18 | `await self.get_kv_data(...)` / `put_kv_data(...)` | `plugin_kv_store.py:22,14` |
| 19 | `await self.context.llm_generate(...)` / `tool_loop_agent(...)` | `context.py:180,224` |
| 20 | `self.context.register_web_api(...)` / `from astrbot.api import sp` | `context.py:764`，`api/__init__.py:4,82` |

另：`from astrbot.api import message_components as Comp` 提供 `Plain/Image/At/Reply/Poke/File` 等 22 个组件类（`api/message_components.py:1` → `core/message/components.py:46-770`）。

## 5. 内置插件与 Skills 机制

### 5.1 `builtin_commands`（reserved 插件，不可卸载）

`astrbot/builtin_stars/builtin_commands/main.py:16` 的 `Main(star.Star)` 只做分发，业务在 `commands/*.py`（`commands/__init__.py:3-9`），是最规范的装饰器样板：`/help`（`main.py:28`）、`/sid`（`33`）、`/name <alias>`（`38-42`，`@filter.permission_type(ADMIN)` + `@filter.command` + `GreedyStr`）、`/reset` 与 `/new`（`44,55`，`SHARED_GROUP_ADMIN`）、`/stop` `/stats`（`50,61`）、`/provider`（`66-75`）、`/dashboard_update`（`77-81` → `commands/admin.py:10-14`）、`/set` `/unset`（`83-91` → `commands/setunset.py:9-36`，用 `sp.session_get/session_put` 存会话变量）。

命令的**运行时可变性**由 `astrbot/core/star/command_management.py` 提供：`toggle_command`（`63`）、`rename_command`（`94`，含重名校验 `107-120`）、`update_command_permission`（`146`）、`list_commands`（`191`）、`list_command_conflicts`（`229`）、`sync_command_configs`（`51`，在 `load()` 末尾调用 `star_manager.py:1474`）。配置存 DB，`_apply_config_to_runtime`（`446`）直接改写 `handler.event_filters` 里的 `CommandFilter` 名与 `PermissionTypeFilter`——**命令名/权限/启停可零改代码调整**。

### 5.2 `astrbot` 主插件

`builtin_stars/astrbot/main.py:29`：纯事件驱动、无命令，覆盖大部分钩子——`event_message_type(ALL, priority=maxsize-1)` 处理"只 @ 不说话"（`41-127`，含 `@session_waiter(60)` 与 `event.get_event_queue().put_nowait` 重投递 `102-125`）、群消息持久化（`138`）、`on_message` 主动回复（`184`）、`on_llm_response`（`268`）、`on_llm_request`（`320`）、`after_message_sent`（`331`）；辅助逻辑在 `group_chat_context.py:43`。它同时是"reserved 插件 + 自带 skills 目录"的样板。

### 5.3 Skills 的加载机制（与 Agent 工具是两套东西）

- **位置**：插件目录下 `skills/<skill_name>/SKILL.md`。加载器扫描两类来源：`plugins_root` 下所有插件目录 + `star_registry` 里 reserved 插件（`astrbot/core/skills/skill_manager.py:305-380`），也支持 `<plugin>/skills/SKILL.md` 直接把插件名当技能名（`348-353`）。
- **不是 LLM 工具**：`build_skills_prompt()`（`skill_manager.py:213-289`）把技能清单以"`- **name**: description` + 文件绝对路径"**拼进 system prompt**（`241-243,255-289`），强制模型先用 shell/文件工具读 `SKILL.md`（"Mandatory grounding"，`270-274`），并要求**渐进披露**（只读直接引用的 `scripts/`、`assets/`，`275-280`）。
- 所以 Skills 的真实能力来自 Agent 的通用工具（shell / 文件读写），技能本身只是提示词 + 资源目录；这与 `@llm_tool`（真注册 JSON Schema 工具，`register/star_handler.py:586-683`）是两条独立通道。
- 内置 4 个技能：`builtin_stars/astrbot/skills/{documents,pdf,spreadsheets,skill-creator}/SKILL.md`，各带 `scripts/` 可执行脚本（如 `skills/documents/scripts/validate_docx.py`）；`skill-creator` 是"如何写 Skill"的元技能（`skills/skill-creator/SKILL.md:3,63-110`）。

## 6. 扩展性评估（附最小插件示例代码 20–40 行，基于真实 API）

### 6.1 最小插件要多少代码？

**两个文件、约 12 行**（`metadata.yaml` 4 行 + `main.py` 8 行）。仓库内有可直接对照的最小样例：`tests/fixtures/plugins/metadata.yaml:1-5` 与 `tests/fixtures/plugins/fixture_plugin.py:1-40`（后者 40 行是因为塞了命令/工具/正则三种入口）。**必需装饰器只有 1 个：`@filter.command`**（或 `@filter.regex` / `@filter.event_message_type` 任一入口）；`@star.register` 已废弃不需要（`base.py:53-64`，`register/star.py:17-19`）。严格说 0 个装饰器也能加载（只有 `Star` 子类 + `initialize()`），但那样插件没有入口。硬性要求：`metadata.yaml` 四字段（`updater.py:29`）+ 主类继承 `astrbot.api.star.Star`，否则加载器报"未通过 Star 注册，也没有找到旧版插件类"（`star_manager.py:1320-1324`）。

### 6.2 最小可用插件（API 全部已 grep 确认存在）

```yaml
# metadata.yaml
name: hello_world
desc: 最小可用的 AstrBot 插件示例
version: 1.0.0
author: your-name
```

```python
# main.py
from astrbot.api import llm_tool, star
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.core.star.filter.command import GreedyStr


class HelloWorld(star.Star):
    def __init__(self, context: star.Context) -> None:
        super().__init__(context)  # base.py:27

    async def initialize(self) -> None:  # base.py:141
        self.count = await self.get_kv_data("count", 0) or 0  # plugin_kv_store.py:22

    @filter.command("hello")  # register/star_handler.py:75
    async def hello(self, event: AstrMessageEvent, name: GreedyStr = "") -> None:
        """打招呼；name 吃掉命令后所有剩余文本（command.py:15,102-113）。"""
        self.count += 1
        await self.put_kv_data("count", self.count)  # plugin_kv_store.py:14
        prefix = self.context.get_config(  # context.py:638
            umo=event.unified_msg_origin
        ).get("wake_prefix", ["/"])[0]
        event.set_result(
            MessageEventResult().message(f"Hello {name or 'world'}! #{self.count} 前缀 {prefix}")
        )

    @llm_tool("hello_counter")  # register/star_handler.py:586
    async def hello_counter(self, what: str) -> str:
        """查询某个问候被调用了几次。

        Args:
            what(string): 问候对象名
        """
        return f"{what}: {self.count}"
```

`@llm_tool` 的 docstring `Args: name(type): desc` 是**强校验**的，缺类型注释直接抛 `ValueError`（`register/star_handler.py:639-655`）。

### 6.3 可覆盖的扩展点

| 扩展点 | 机制 | 位置 |
| --- | --- | --- |
| 命令 / 指令组 / 子指令 | `@command`、`@command_group`、`RegisteringCommandable.command` | `register/star_handler.py:75,208,251-259` |
| 正则监听（不受 wake_prefix 约束） | `@regex` | `register/star_handler.py:297`，`filter/regex.py:9` |
| 全量消息监听 | `@event_message_type` | `filter/event_message_type.py:24-33` |
| 平台/权限限定 | `@platform_adapter_type`、`@permission_type` + `alter_cmd` | `filter/platform_adapter_type.py:57-74`，`filter/permission.py:26-52`，`star_manager.py:1390-1418` |
| 自定义过滤器 | `CustomFilter` 子类 + `&`/`\|` + `@custom_filter` | `filter/custom_filter.py:21-62` |
| LLM 请求/响应改写 | `@on_llm_request`（改 `req.system_prompt`）/`@on_llm_response` | `register/star_handler.py:433-476` |
| 工具调用前后、结果装饰、发送前后 | `on_using_llm_tool`/`on_llm_tool_respond`/`on_decorating_result`/`after_message_sent` | `register/star_handler.py:537,561,730,744` |
| Function-calling 工具 | `@llm_tool` 或 `context.add_llm_tools` | `register/star_handler.py:586`，`context.py:738` |
| 子 Agent / Handoff | `@agent(name, instruction, tools, run_hooks)` | `register/star_handler.py:697-727` |
| 平台适配器 | `register_platform_adapter` 装饰 `Platform` 子类 | `api/platform/__init__.py:11`，`core/platform/register.py:11` |
| Web API | `context.register_web_api(route, handler, methods, desc)` | `context.py:764`；由 `dashboard/api/plugins.py:209-242` 匹配，前缀 `/plugins/extensions/...`（`plugins.py:398-441`） |
| Web 面板页面 | 插件目录 `views/<name>/`（`pages/` 为别名） | `plugin_page_service.py:29,463-504`，`dashboard/api/plugins.py:885-917` |
| 面板配置界面 | `_conf_schema.json` | `star_manager.py:212,1159-1173` |
| 命令改名/禁用/改权限 | Dashboard API + `command_management` | `command_management.py:63,94,146` |
| 会话级插件/模型/TTS 开关 | `SessionPluginManager`/`SessionServiceManager` | `session_plugin_manager.py:11,49`，`session_llm_manager.py:15-162` |
| 主动推送消息 / 伪造事件 | `context.send_message`、`StarTools.create_message/create_event` | `context.py:655`，`star_tools.py:35,57,104` |
| 插件间协作 | `context.get_registered_star(name)` 取对方 `star_cls` | `context.py:383-388` |

### 6.4 插件做不到的事

1. 改管线结构或 Stage 顺序（`astrbot/core/pipeline/bootstrap.py:8` 是写死的 stage 列表）；替换/包装 Provider 实现（只能在 `register_provider` 里追加，`context.py:844`）。
2. 真正的运行时隔离：所有插件同进程、同 `sys.modules`、同依赖树（见 7.3）；自定义 Dashboard 前端构建产物（只能以 `views/` 静态页挂载）。
3. 卸载 reserved 插件或按会话关掉内置命令（`star_manager.py:1699`，`session_plugin_manager.py:86-88`；只能全局 `disable_builtin_commands`）。
4. 干净的异常边界：一个 handler 抛异常只终止当前事件，但全局注册表污染会波及后续加载（`_cleanup_plugin_state` 仅在加载失败时回滚，`star_manager.py:776-819`）。

## 7. 问题、风险与技术债 + 关键结论 8–12 条

1. **全局可变单例、零命名空间隔离**。`star_map`/`star_registry`（`star.py:9-10`）、`star_handlers_registry`（`star_handler.py:216`）、`llm_tools` 均为模块级全局；Handler 主键是 `<module>_<func>`（`register/star_handler.py:35`），`star_handlers_map` 同 key 直接覆盖（`star_handler.py:24`），**运行时不做冲突检测**——重名冲突只能事后在 Dashboard 发现（`command_management.py:229,474-484`）。
2. **`Context.registered_web_apis` 是类属性**（`context.py:133`）。所有插件共用一个 list，而 `_unbind_plugin`（`star_manager.py:1799-1861`）只清 handler/tool/adapter/`sys.modules`，**没有清理 web api** → 插件卸载或重载后旧路由仍指向已废弃 handler，形成悬挂路由与内存泄漏。
3. **依赖直接装进主环境**。插件 `requirements.txt` 走 `pip_installer.install`（`star_manager.py:153,172,187` → `pip_installer.py:1019-1082`），只有 `-c` 核心约束（`1070-1076`）与"打包桌面端 `--target data/site-packages`"（`1053-1067`）两层缓解；源码部署时仍会改动宿主 site-packages，随后 `importlib.invalidate_caches()`（`1090`）继续加载。
4. **热重载是半成品**。必须显式 `ASTRBOT_RELOAD=1`（`star_manager.py:222-223`），缺 `watchfiles` 时静默降级（`59-65`）；全量重载要 `star_handlers_registry.clear()/star_map.clear()/star_registry.clear()`（`1054-1056`），期间只靠管理侧 `_pm_lock`（`215`）串行，管线侧不持锁，并发事件可能读到半空注册表。
5. **卸载按字符串前缀启发式清理**。LLM 工具移除要求 `handler_module_path.startswith(plugin_module_path)` 且 `not endswith(("astrbot.builtin_stars","data.plugins"))`（`star_manager.py:1830-1841`），平台适配器按"模块路径去掉最后一段"注销（`1845-1853`），`sys.modules` 清理按 `data.plugins.<dir>` 前缀（`742-774`）——插件动态 import 的子系统与 `importlib` 扩展会留在进程内。
6. **handler 实例绑定靠全局改写 `functools.partial`**。加载时把 `handler.handler` 逐个替换为 `partial(raw_handler, star_cls)`（`star_manager.py:1263-1279`，先无条件恢复 raw 再按需重绑），llm_tool 上重复同一逻辑（`1282-1311`）；重复加载/失败重试时易出现旧实例泄漏或重复绑定。
7. **版本兼容只有"拒绝加载"，没有 API 版本化**。`astrbot_version` 用 PEP 440 范围校验，不满足即 `PluginVersionUnsupportedError`（`star_manager.py:1203-1213,674-705`）；插件面 API 的兼容策略是"`deprecated` 装饰器 + 继续导出"（`context.py:397,429,493,550,594,796,852,885,898,940` 与 `api/all.py` 全量再导出），**没有任何按 AstrBot 版本切换行为的机制**。
8. **权限过滤器是过滤器链里的异类**。`PermissionTypeFilter` 失败不立即短路，而记录 `permission_not_pass` 延后统一处置（`waking_check/stage.py:178-214`）；`SHARED_GROUP_ADMIN` 语义依赖 `event.get_extra("_session_isolated")` 这一由管线写入的隐式约定（`filter/permission.py:47-52`），插件自定义过滤器无法复用该语义。
9. **每条消息对全部 handler 全量求值**。`WakingCheckStage` 遍历所有 `AdapterMessageEvent` handler 及其全部过滤器（`waking_check/stage.py:158-235`），`CommandFilter` 还要做正则归一与完整命令名匹配（`filter/command.py:175-200`），部分过滤器会读配置。1000+ 插件生态下这是 O(插件×过滤器) 的固定成本，且无任何索引（如按 wake_prefix 或命令首字建倒排）。
10. **会话级开关对内置命令无效**。`filter_handlers_by_session` 无条件放行 `plugin.reserved`（`session_plugin_manager.py:86-88`），内置命令无法在单个群/会话里关闭，只能全局 `disable_builtin_commands`（`waking_check/stage.py:162-167`），与"会话级精细控制"的产品叙事有落差。
11. **元数据缺 schema 校验**。`validate_plugin_metadata` 只校验 4 个必填字符串（`updater.py:377-396`）；`views`、`i18n`、`support_platforms`、`astrbot_version` 都是"类型不对就当空"的宽松解析（`star_manager.py:534-563`），错误静默降级，作者很难定位"面板没出现/版本被忽略"。
12. **命令配置与运行时注册表是双向可变状态**。`command_management` 直接改写 `handler.event_filters` 内对象（`command_management.py:446-462`、`_set_filter_fragment` `484`、`insert(0,...)` `185`），`load()` 又按 `alter_cmd` 插入/改写 `PermissionTypeFilter`（`star_manager.py:1390-1418`），两处写同一结构且顺序敏感，存在"配置在重载后被覆盖"的竞态面。

### 关键结论

1. **最小成本极低、上限很高**：4 行 `metadata.yaml` + 8 行 `main.py`、**只需 1 个 `@filter.command`** 即为可用插件；`@star.register` 已是历史包袱，新插件不该再写（`base.py:53-64`，`register/star.py:8-45`）。
2. 插件体系的核心不是"插件"，而是**一张全局 handler 表 + 一条 AND 过滤器链**（`star_handler.py:14-216`，`waking_check/stage.py:169-195`）。理解这两点即可推导出全部扩展点。
3. 架构上是**"约定优于框架"**：无接口基类、无 DI 容器、无插件协议版本，靠 `Star` 子类 + `metadata.yaml` + 装饰器副作用完成注册；学习曲线极平缓，代价是隔离性接近零。
4. `astrbot/api/**` 是**纯再导出层**（无实现逻辑），价值在"给插件一个永不破的名字空间"；真正契约其实是 `Context` 与 `AstrMessageEvent` 两个具体类的公开方法。
5. 生命周期钩子密度是真正的卖点：16 种 `EventType` 覆盖 AstrBot/平台加载、消息、LLM 请求响应、Agent 起止、工具调用前后、结果装饰、发送后、插件错误与加载卸载（`star_handler.py:219-241`），比同量级框架更细。
6. **`Context` 是唯一官方句柄但过于肥胖**（`context.py:130-953`，40+ 公开方法，含 10 个已废弃方法），插件作者很容易依赖内部字段（`provider_manager` 等），一旦重构就破坏兼容。
7. **会话级能力是"配置层过滤"而非"进程层隔离"**：handler 照样注册、照样被遍历，最后被 `filter_handlers_by_session` 丢掉（`session_plugin_manager.py:49-102`），因此并不省 CPU。
8. **Skills 与 Agent 工具是两套并行机制**：Skills = 拼进 system prompt 的"提示词包 + 脚本"，靠模型自己读 `SKILL.md`（`skill_manager.py:213-289`）；`@llm_tool` = 真注册的 function-calling 工具。二者共同构成"最小内核 + 能力外挂"。
9. 内置 `builtin_commands` 与 `astrbot` 是事实上的参考实现，且都以 `reserved=True` 享受豁免（不可卸载 `star_manager.py:1699`、不受会话级禁用 `session_plugin_manager.py:86-88`、不受 `plugin_set` 白名单限制 `star_handler.py:182`）——这本身是一处"内置与第三方不同权"的技术债。
10. **最大工程风险不是崩溃而是"污染"**：全局注册表（`star.py:9-10`）、类属性 `registered_web_apis`（`context.py:133`）、共享 site-packages（`pip_installer.py:1019-1082`）、`sys.modules` 前缀清理（`star_manager.py:742-774`）四者都只有"尽力清理"，没有隔离边界。1000+ 插件的官方数字下，稳定性依赖"社区插件都比较守规矩"。
11. **可观测性优于隔离性**：每次 handler 执行都有 `event.trace.record("handler_start"/"handler_end", ...)`（`star_request.py:61-107`），含插件名、handler 名、priority、category（command/listener）；插件异常还有专门的 `OnPluginErrorEvent`（`register/star_handler.py:357-372`）。调试友好，隔离无益。
12. 演进投入产出比最高的三件事：(a) 给 `registered_web_apis` 加插件归属与卸载清理；(b) 用命令/前缀倒排索引替代每消息全量过滤；(c) 把 `requirements.txt` 安装默认改为 `--target` 隔离目录（桌面打包路径已如此，`pip_installer.py:1053-1067`，源码路径尚未跟进）。