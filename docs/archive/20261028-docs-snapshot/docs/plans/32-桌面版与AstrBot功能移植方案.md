# 32. 桌面版与 AstrBot 功能移植方案

> 用户诉求（2026-10-26）：喜欢 AstrBot 的布局与功能，想直接把好用的功能移植过来，同时也想要桌面版。
> 克隆位置：`C:\Users\Administrator\Desktop\AGI\AstrBot-desktop\AstrBot-desktop-main`
> 上游 UI：`C:\Users\Administrator\Desktop\AGI\AstrBot-master\AstrBot-master\dashboard`

## 一、AstrBot Desktop 是什么

- **Tauri 2.10 桌面壳**（Rust）+ **打包的 CPython 3.12 后端** + **内置 AstrBot WebUI**
- `src-tauri/src/` 的关键模块（可以借鉴设计，不建议直接搬代码）：
  - `process_control.rs` / `lifecycle/` / `restart_backend_flow.rs`：后端进程生命周期
  - `tray/`：托盘菜单；`startup_task.rs`：开机自启
  - `update_channel.rs`：stable/nightly 更新通道
  - `single_instance`、`window/`、`logging.rs`、`packaged_webui.rs`
  - `scripts/backend/build-backend.mjs` + `runtime-manifest`：把 Python 运行时和依赖打进安装包
- UI 侧：AstrBot WebUI（Vue3 + Vuetify + OpenAPI 生成客户端）

**结论：它的"桌面分发工程"很重（Rust + 打包流水线），直接照抄成本高；它的"功能与交互"值得抄。**

## 二、功能映射：AstrBot 有 → 我们有/该补

| AstrBot 页面 | 功能 | 我们的对应 | 状态 |
|---|---|---|---|
| Welcome | 总览/状态 | 控制台「总览」 | ✅ 已有 |
| **ChatPage / ChatBox** | **直接在界面里和 AI 对话（流式）** | 无 | 🔨 P7-b 做「对话测试」页（隔离库） |
| **ProviderPage** | **模型/API 配置** | 无（只能改 config.json） | 🔨 P7-b 做「模型配置」页 |
| PlatformPage | 平台连接状态 | 控制台「总览」里的 gateway 状态 | ✅ 已有（简化） |
| ConsolePage / **TracePage** | **日志/追踪** | 只有「对话流」 | 🔨 P7-b 做「日志」页 |
| PersonaPage | 人格/提示词 | 控制台「提示词」（11 键可编辑） | ✅ 已有，更强 |
| KnowledgePage | 知识库 | 「记忆」页（指标+列表） | ✅ 基础有，缺编辑（P7-c） |
| **DataPage** | **数据导入/导出/备份** | 只有恢复出厂（含备份） | 🔨 P7-c 做「数据管理」 |
| SessionManagementPage | 会话管理/多联系人 | 单联系人 | 🔨 后续（多联系人支持） |
| CronJobPage | 定时任务 | 「设置」里的主动消息调度 | ✅ 已有 |
| ExtensionPage / PluginView | 插件市场 | 无插件体系 | ❌ 不移植 |
| SubAgentPage | 子代理 | 无 | ❌ 不移植 |
| — | 托盘/开机自启/桌面通知 | 无 | 🔨 P7-c 桌面增强 |

## 三、我们的路线（三步，先可用再打磨）

### P7-a 桌面启动器（零依赖，先能用）
- 我们已有两个 HTTP 服务：网关 `801` + 调试控制台 `802`
- 桌面版 = 自动起服务 → `msedge --app=http://127.0.0.1:802` 独立窗口（WebView2，系统自带）
- `tools/desktop/launcher.py`：健康探测、单实例、`--no-open/--stop/--status`、state 文件只停自己启动的 PID
- 双击 `启动桌面版.bat` 即可
- **不引入 pip 依赖、不引 Rust**

### P7-b 控制台补三页（功能移植核心）
1. **对话测试（ChatUI 等价）**：`/api/chat-test` 用**隔离库** `console_test.sqlite3` 跑 `SelfLearnAgent.send()` → 网页上直接试提示词/语气，不影响真实联系人
2. **模型配置（Providers 等价）**：读写 `llm_model/temperature/max_tokens/soul_server_url/soul_timeout_s/inner_schema`；DeepSeek key 只显示配置状态
3. **日志（Trace 等价）**：gateway.log / inner.log / prompts.log / regen_failed，支持搜索与 JSON 展开

### P7-c 后续（按用户优先级排）
- 数据管理：备份下载、导入、按联系人导出聊天记录（对应 DataPage）
- 记忆编辑：搜索/修改/删除/标记重要、导出（对应 KnowledgePage 的写操作）
- 托盘 + 桌面通知 + 开机自启（对应 tray/startup_task）
- 真要打包成 exe：两个选项
  - `pywebview` + `pyinstaller`：Python 内打包，简单
  - Tauri：体验最好、包最小，但要 Rust 工具链 + 构建流水线（照 AstrBot 的做法）

## 四、明确不搬的东西

1. **不搬 Vue/Vuetify 前端源码**：AstrBot dashboard 依赖 OpenAPI 生成客户端、Pinia、i18n、Monaco 等，搬过来的改造量 > 重写；我们保持"无构建 单页 HTML/JS"。
2. **不搬插件市场/子代理**：我们没有插件体系，硬搬只会带来一堆空壳。
3. **不搬 Tauri 打包脚本**：先解决"能用"，再考虑"分发"。

## 五、验收标准（本阶段）

- 双击桌面启动器 → 独立窗口打开控制台，网关/控制台自动就绪
- 在「对话测试」页直接聊 10 句，能看到回复 + 内状态 + 媒体决策 + 延迟
- 在「模型配置」页改 temperature / soul 地址，保存后下一条消息生效
- 在「日志」页能定位"这条消息为什么被拦/为什么没发媒体"
