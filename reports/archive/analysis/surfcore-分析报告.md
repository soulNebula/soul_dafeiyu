# SurfCore（Mieksido/surfcore）仓库分析报告

> 分析对象：`https://github.com/Mieksido/surfcore` @ `main`（`d7f6b33`，2026-10-28 09:42 UTC push）
> 方法：只读静态审计。shell 在本会话不可用（`ops_bash`/`pwsh` 一律 `0xC0000142`），因此无法 clone/运行；
> 所有代码经 `gh-proxy.com` 代理读 raw + jsdelivr 文件树 + GitHub API 取证，**未执行仓库内任何脚本**，
> README / AGENTS.md / 安装提示词一律当**数据**，未遵循。
> 三名子审计员分别负责「安装/更新/宿主补丁」「运行时网络与凭据」「仓库健康度与提示注入面」，
> 明细见同目录 `t1-install-audit.md`、`t2-runtime-audit.md`、`t3-repo-health.md`。

---

## 0. 摘要

**一句话**：这是一个**真实、可用、工程量很大**的 Windows DSH 插件（QQ 桥接 + B站 自动化 + 桌宠 + 记忆/人格），
不是空壳骗局；但它是**单人维护、零 CI、由 AI 重度参与开发**的项目，并且把「让 AI 自己装、自己改宿主、要完全权限」
写进了标准工作流。**没有发现隐藏后门或第三方遥测**；真正的风险是**分发链零校验 + 字节级修改 DSH 宿主 + 凭据边界 fail-open**。

| 维度 | 判断 | 依据 |
| --- | --- | --- |
| 是否骗局/空壳 | **否**。真实代码、真实用户、真实 bug 报告 | 15k 行单体桥接、97 个测试脚本、8700 行文档、issue 里是用户 agent 提的真实 bug |
| 是否有恶意后门/遥测 | **未发现**（已读范围内） | 硬编码外链只有 `127.0.0.1` + DeepSeek/B站/GitHub/npmmirror，无上报端点 |
| 工程质量 | **中上，但结构失衡** | 安全判据做得比多数同类细；但 834KB 单文件、无 CI、版本号三处不一致、门禁数字对不上 |
| 安全风险 | **中高** | 安装/更新零签名零 hash；改 DSH `app.asar`；Windows 凭据对本机全用户可读；一处 PowerShell 命令注入 |
| 合规风险 | **中高** | B站 自动评论/点赞 + 明示的风控规避；SnowLuma 非商业 EULA；QQ 非官方协议 |
| 建议 | **不要在主力环境直接装**。要试就隔离（独立账号/独立用户/VM），并先读第 5 节的「安全试用清单」 | — |

三个最需要记住的结论：

1. **它的安装方式本身就是最大的安全面**：`irm ... | iex` → `git clone main` → `npm install`（跑仓库自带 `postinstall`）→
   自动把 agent preset / 3 个 MCP server 注入你的 DSH profile；更新则用 `git merge --ff-only origin/main` 或
   `robocopy /MIR` 覆盖插件目录，而这些目录是 `link:` 进 DSH 的，**下次启动就以宿主进程权限执行**。全程无 hash、无签名、无版本 pin。
2. **它真的会改 DSH 应用本体**：用等长字节替换修补 `resources/app.asar` 里
   `@deepseek-ai/dsh-session-persistence-jsonl` 的一条 `throw new Error(...)`，把「会话头 cwd 与物理路径不符」从硬错误
   降级为「当损坏条目静默跳过」；并注册计划任务 `DshSessionGuard`，等 DSH 退出后换文件。动机是修一个真实 DSH 崩溃，
   但这是**第三方项目修改宿主二进制**，会破坏签名/asar 完整性，且卸载不还原。
3. **凭据边界做得很糟**：作者自己在代码注释里实测——Windows 上 `mode: 0o600` 是空操作，`state/` 的 ACL 是
   `Authenticated Users:(M)`，收紧失败只打一行警告就继续跑（fail-open）。控制台令牌、SnowLuma accessToken、
   会话 token、全部 QQ 聊天记录，对本机任一登录用户可读。

---

## 1. 它是什么

### 1.1 定位

不是「把大模型接进 QQ」的机器人框架，而是**给一个 DSH agent 一套身份 / 记忆 / 作息 / 两只手（QQ、B站）**：

- **QQ 侧**：`src/bridge.js` 把群/私聊消息变成 DSH 会话里的用户消息，把 agent 的回复、提问、审批发回 QQ；
  agent 自己决定看消息、发言、引用、发表情、发语音、等待、潜水、收尾。
- **B站 侧**：同一个角色以 B站 用户身份活动——看视频、打分、评论、回帖、发动态、分享、私信、直播监听。
- **记忆/人格**：QQ / B站 / 桌宠 共享同一张衰减记忆卡 + 全量存档卡；角色包整包切换。
- **桌宠**：仓库外的 Electron 分身（`Mieksido/surfpet`），通过本地令牌接口与插件对话。
- **主动机制**：从记忆里提炼「想做点什么」，在安静时段主动找主人（QQ 或桌宠，互斥）。

### 1.2 进程与端口

```
DSH 宿主（Electron）
 └─ surfcore 插件（bundle：设置页 14 标签 + B站 调度 + 记忆 + 桌宠 API）
      │ HTTP（会话 Cookie，桥接自己铸造）+ 子进程管理
      ▼
   QQ 桥接进程（node src/bridge.js，单实例锁）
      ├─ OneBot WS  → SnowLuma2（第三方 QQ 运行时，用户自装自登）
      ├─ stdio      → 3 个 MCP server（snowluma / snowluma-host / web-search-safe）
      ├─ HTTP       → DSH Web API（动态端口，靠 discoverDshBaseUrl 发现）
      └─ HTTP :3100 → 本机控制台（令牌 + 回环 + Host/Origin 校验）
```

- 语音引擎：本地 `:8765`（GPT-SoVITS）或云端 `:8767`，仓库外可选装。
- 两套状态根：`<仓库根>/state/`（桥接）、`~/.dsh/surfcore/`（插件）。
- 三份配置：仓库根 `config.json`、`~/.dsh/surfcore/config.json`、`~/.dsh/surfcore/bili.config.json`。

### 1.3 血统（重要）

`LICENSE` 版权写的是 **`Derpyu520 and qq-bridge / QSH contributors`**，仓库自称「qq-bridge，将来的 QSH」。
前身仓库 `Derpyu520/qq-bridge`（0.2.1-r1）公开可读，脚本与依赖高度同构。
**所以 11.7MB 代码 + 8700 行文档不是这三天写的**——是旧工程改名重启 + AI 全量重写文档 + 高频 AI 迭代。

---

## 2. 工程成色

### 2.1 规模（实测）

| 项目 | 数值 | 备注 |
| --- | --- | --- |
| `src/bridge.js` | **834 KB / ≈14,964 行** | 单文件包含 153 条 `if (url.pathname === ...)` 分支（约 2.5k 行控制台） |
| `plugins/surfcore/lib/client.js` | 343 KB | 设置页前端 bundle |
| `plugins/surfcore/lib/index.js` | 134 KB | 插件宿主装配 |
| `src/` 纯引擎 | 40+ 模块 | 记忆卡、目的机制、收尾、额度、计价、人格、B站、SSRF 抓取、语音 |
| 文档 | 12 篇 ≈8,737 行 + `PITFALLS.md` 187 KB + `HANDOFF.md` 200 KB | 与 README 声称一致 |
| 测试 | `scripts/test-*.mjs` **97 个** + 插件侧脚本 | 声称 75，实际更多（不夸大） |
| 直接依赖 | 6 个（`@snowluma/*`、`@modelcontextprotocol/sdk`、`js-yaml`、`zod`、`@deepseek-ai/schemastery`、`dsh-host-apiproxy`） | 依赖面小，加分 |

### 2.2 做得好的地方（不是客套）

- **安全判据集中在前置关卡**：控制台在 153 条端点分支**之前**做 Host 白名单 → 令牌定长比较 → agent 混淆代理防护 → 写操作强制 JSON，新端点默认继承。
- **QQ 会话工具面 fail-closed**：`plugins/surfcore/qq-tool-restrict.mjs` 两层——注册期 `tools.restrict` 隐藏 67 个危险工具名，执行期 `tools.guard` 只放行 `mcp__snowluma__*` / `mcp__snowluma-host__*` / `mcp__web-search-safe__*` + `ask_user_question` / `todo_write`，其余一律拒绝。给群友的 agent **拿不到本地执行能力**，这个方向是对的。
- **SSRF 防护是真货**：`src/safe-fetch.js` 对 DNS **全部**解析结果判定私网（含 IPv4-mapped / NAT64 / 6to4 / ULA / link-local / 十进制八进制十六进制 IPv4），重定向每跳重校验，**连接 pin 到已校验 IP** 同时保留 Host/SNI——真正挡住了 DNS rebinding。比多数库细。
- **文档诚实度罕见**：`LICENSE` 附录主动自曝 `@snowluma/*` 包「声明了 LICENSE 却没有 LICENSE 文件」、承认 `npm install` 会修改 `node_modules` 里的第三方 dist、列出完整「数据出机清单」；`docs/RISK_REGISTER.md` 有 39 条风险台账（含未修项）、`SECURITY_BASELINE.md` 有「已知残余风险」。
- **删除先隔离**：`_backups/` / `~/.dsh/_quarantine/`，不可逆删除已被移除；`uninstall.mjs` 默认演练。

### 2.3 明显的问题

| 问题 | 事实 |
| --- | --- |
| 版本号三种现值 | `package.json` **0.6.22** / `README.md` **0.6.20** / `docs/guides/VERSIONS.md` **0.3.0** |
| 门禁数字对不上 | README 75 个脚本、VERSIONS 76、CHANGELOG 多处 77/77；用户 PR #6 实测 **70/77（7 个失败）** |
| 发布通胀 | 3 天内 42 个版本号 / 41 个 tag（缺 `v0.1.0`、`v0.6.6`、`v0.4.3`），tag 全是轻量 tag，无 Release 证据 |
| CHANGELOG 生成痕迹 | 18 个版本小节只有空的 `### 变更` + `- `；`0.6.6` 标题重复两行 |
| 零 CI | 没有 `.github/`，门禁全靠人记得跑 |
| 维护响应 | 9 个 open item（5 issue + 4 PR），**维护者 0 回复 0 合入**（观察窗口仅 ~24h，不下定性） |
| 单文件巨石 | `bridge.js` 一处改动影响面极大；作者自己在风险台账里标 🔴 |
| 默认值漂移 | `config.example.json` 的 `biliBridge.memory.enabled: true` vs `store.js` 的 `qqToBili.enabled: false` |
| 未经处理的已知 bug | issue #8 `listenTick()` 里 `const L` 重复声明导致 TDZ `ReferenceError`（@/评论监听从未真正触发）；#9 PowerShell 转义错误 |

**社区信号**：155 stars / 12 forks / **0 watchers**，4 个配套仓库在 70 秒内创建（surfvoicelocal 1★、surfvoicecloud 0★、surfpet 1★）；
B站 以 "SurfCore" 为名 0 结果。热度来源与刷量与否无法证实，只能记为「年轻仓库 + 单人维护」。

---

## 3. 安全评估

### 3.1 先排除怀疑：没发现的东西

- **无第三方遥测/上报**：`src/bridge.js` 前 2479 行的硬编码外链只有 `127.0.0.1:3080/3000/3001/8765/8767`；已读文件里的出网点只有 DeepSeek API、B站 官方 API、Bing 兜底、GitHub/npmmirror 安装包下载。
- **无隐藏进程/COM/注册表 Run 键/服务**；唯一的持久化是显式的计划任务（见 3.3），且成功后自删。
- **MCP 工具面无命令执行/文件读写面**：`mcp-host-server.js` 只有 1 个只读工具（且显式删除了 `start_snowluma`/`stop_snowluma`）；`mcp-snowluma-safe.js` 46 个工具全部转发到桥接 HTTP；`mcp-web-search-safe.js` 只有 `web_search`/`web_fetch`。
- **隐私闸门是确定性正则**（不信模型判断）：私信硬隔离（QQ 侧只读 `group:*`）、密钥/证件/手机/邮箱/住址命中整行丢弃、零宽字符剥离。

> 置信度声明：本会话无法全仓 grep（shell 坏），上述结论覆盖已读文件；`bridge.js` 2479 行之后、`client.js` 中段、
> `updates.js` 全文未逐行读。详见第 6 节「未验证」。

### 3.2 【中高·系统性】安装与更新链：零完整性校验，且以宿主权限执行新代码

- **官方安装方式**：`powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/Mieksido/surfcore/main/scripts/bootstrap.ps1 | iex"`
  → `git clone --depth 1 main`（或 `archive/refs/heads/main.zip`）→ `npm install --no-audit --no-fund` → `node scripts/setup-dsh.mjs <profile>`。
  **无 hash、无签名、无版本 pin**：作者（或任何能改仓库/劫持 raw 的人）任何时刻都能改你机器上的代码。
- **`npm install` 会执行仓库自带的 `postinstall`**：`scripts/patch-snowluma-sdk.mjs` 遍历 `node_modules/@snowluma/{sdk,mcp}` 的
  `dist/**/*.js`，把 `from './x'` 改成 `from './x.js'` **就地覆写**（作者在 LICENSE 里据实承认了这一点，这点值得肯定）。
- **`setup-dsh.mjs` 改的是"你未来每个会话的能力面"**：写 `~/.dsh/profiles/<p>/cordis.patch.yml`（增删 3 个 MCP，写前备份最近 5 份）、
  往 profile `package.json` 注入 `link:` 依赖与 bundle、建两层 junction、重新生成 preset patch。
  **不写**注册表/环境变量/启动项/服务，旧 preset 目录只报告不删——这部分是克制的。
- **自动更新是同一缺口的续期**：`plugins/surfcore/lib/updates.js` 里 `git fetch` + `git merge --ff-only origin/main`，
  或下载 `.../archive/refs/heads/main.zip`，由脱离进程的 `scripts/apply-update.mjs` 用 **`robocopy /MIR`** 覆盖插件目录
  （排除 `state/ backups node_modules .git ...` 与配置文件）。该目录正是被 `link:` 接进 DSH 的那棵树，
  **下次 DSH 启动时 `plugins/surfcore/lib/index.js` 就在宿主进程里 import 执行**（它能用 `fs`、`child_process.spawn`、`powershell`）。
  zip 通道无 sha256；release 通道的 sha256 来自同一份可被作者改写的清单 = 自签自验。
  `/MIR` 还会删除"源里没有"的文件——排除表漏一项就是用户数据丢失。

**结论**：这不是漏洞，是**分发模型**——等价于「作者永久性地持有你 DSH 进程的执行权」。单机自用、你信任作者时成立；否则就是供应链风险。

### 3.3 【中高·宿主完整性】字节级修改 DSH `app.asar` + 计划任务守候

- `scripts/patch-dsh-session-guard.mjs`：打开 `resources/app.asar`，自己解 asar 头，定位
  `/dsh/node_modules/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`，把
  `throw new Error(\`corrupt session log ... cwd identify ...\`)` **等长替换**为
  `throw new SessionPersistenceCorruptionError(\`corrupt session log ... cwd mismatch ...\`)`。
  等长保证 asar 头/偏移不用重算；`--apply` 走 rename 原子替换，`--apply-in-place` 对运行中的 Electron 做同长度原地写。
- `scripts/install-session-guard.mjs`：`schtasks /Create /TN DshSessionGuard /SC ONCE /ST <h+1min> /RL LIMITED`，
  后台 `--watch` 每 1.5s `tasklist` 探活，确认 DSH 退出后 rename 换入 `.guarded` 副本 → 用
  `ELECTRON_RUN_AS_NODE=1` 冒烟 → 失败自动回滚 → 成功后删任务。有 SHA256 指纹防「DSH 升级后套旧补丁」。
- **性质判定**：**不是**放宽权限/沙箱/工具白名单；它把 DSH 的一条**会话完整性判定降级为静默忽略**
  （原本「会话文件物理路径 ≠ 头部 cwd/id」是硬错误，可作为「文件被移动/串目录/藏匿」的报警，现在跳过不报）。
  同时它**改的是应用本体**，会破坏代码签名 / asar 完整性校验（作者自己的冒烟测试就是为此写的）。
- **工程瑕疵**：`ASAR_DEFAULT = 'D:\\deepseek\\resources\\app.asar'`、`EXE = 'D:\\deepseek\\DeepSeek Harness.exe'`
  写死作者机器路径，普通安装位置默认不命中，需显式 `--asar`。
- **卸载不还原**：`uninstall.mjs` 不还原 asar 补丁、`app.asar.old-*`/`.guarded` 残留、ACL 改动、`DshSessionGuard` 计划任务。

**这是全仓库最需要被讨论的一处设计**：动机可理解（坏会话让整个 workspace 服务起不来，作者自己 2026-10-22 被咬过），
但正确做法是上报上游 DSH 修（把该错误归入 corruption 类），而不是让每个用户去改宿主二进制。

### 3.4 【高·凭据边界】Windows 上 `state/` 对本机所有用户可读，且失败仅告警

- `src/bridge.js:275` 用 `fs.writeFileSync(..., { mode: 0o600 })`——**Windows 上这是空操作**。
- 作者自己在 `src/bridge.js:298-301` 注释里写实测结果：`state/console-token` 的 ACL 是
  `NT AUTHORITY\Authenticated Users:(M)`，即**本机任何能登录的用户**都能读走控制台令牌、SnowLuma accessToken、
  各会话 agent token、全部 QQ 聊天记录。
- `src/bridge.js:315-330`：`hardenStateDirAcl()` 失败时只 `log('⚠️ 未能收紧 state/ 目录权限...')` 然后继续启动——**fail-open**。
- 另外 `src/snowluma-conn.js` 会遍历 SnowLuma 的 `config/onebot_*.json` **收集所有账号的 token**，并明文回写仓库根 `config.json`
  （不在 `state/` 的 ACL 收紧范围内）。
- B站 侧 `~/.dsh/surfcore/bili/credentials.json` 明文存 `SESSDATA` + `bili_jct`（拿到即账号控制权，无二次验证）。

### 3.5 【中高】设置页 PowerShell 命令注入

`plugins/surfcore/lib/index.js:1881-1899`（`handleBrowse`）：把 URL 里的 `title` 用 `JSON.stringify()` 拼进
`powershell.exe -STA -NoProfile -Command <ps>`。**JSON 转义 ≠ PowerShell 转义**（PS 用反引号，`\` 是普通字符），
`?title=x"; calc; #` 即可闭合字符串并执行命令。门槛仅 `isLoopbackReq` + `sameOrigin`（无 Origin 视为通过）+ DSH 请求过滤，
即「本机进程」级别；与 3.4 串起来就是「普通本机用户读 token → 命令执行」。
`plugins/surfcore/lib/pet.js` 的 `Expand-Archive -LiteralPath '${zip}'` 同样未做单引号转义（当前参数不可控，属潜在模式缺陷）。

### 3.6 【中·架构】桥接进程离线伪造 DSH 会话 Cookie

`src/dsh-client.js` 读 `~/.dsh/.credentials.yaml` 的 `client-connection/browser-session` 签名密钥，
`mintBrowserSessionCookie()` 用它 HMAC 出合法 `dsh-auth-*` Cookie（60 分钟有效），**完全绕开启动 token**；
另有 `discoverDshLaunchToken()` 从 `~/.dsh/logs/server-*.log` 正则抓 `?token=`。
权限没有提升（桥接本来就能换到同权），但**攻击面多了一份等效凭据**：任何能读该密钥的进程都能以「已登录 DSH Web」身份调
`settings/update`、`session/control`。作者把 `state/` 收紧的逻辑与此自洽，但 3.4 让这套自我约束漏了气。

### 3.7 【高·合规/账号】B站 自动化是真实写操作 + 明示的风控规避

`plugins/surfcore/tools/commands/comment.mjs`（文件头自称 "Post ONE bilibili comment on the user's behalf"）：

- 真发评论：`POST https://api.bilibili.com/x/v2/reply/add`，带 WBI 签名与 `csrf=bili_jct`，模拟 Chrome UA / `origin`。
- 真点赞（鱼群保护顺带给别人的评论点赞，有每日上限）；`--share` 模式**强制正文以 `@<owner>` 开头**，公开关联你与 B站 身份。
- **为规避平台风控而做的工程处理**（代码注释原文）：带链接被判 `state=17`（仅自己可见）→ 自动把 URL 换成 BV 号；
  「刚发完评论就同视频连点容易 **-509**」→ 延迟点赞；「鱼太多别发新楼层，改成回复一条鱼」防被识别为刷屏号；
  多本账限额（30 条评论/天、50 回复/天、同 UP 10 分钟 1 条…）。
- 这些限额本身是「少发点」，但设计目的是「看起来像真人」——属平台协议明确禁止的自动化行为，常见处置是限流 → 评论仅自己可见 → 封禁。

### 3.8 【中高·提示注入面】仓库里有三类「写给 AI 的指令」

1. **`AGENTS.md`（4.1 KB）依赖 DSH 自动注入**——只要把仓库 clone 进工作区，这些「铁律」就进了 agent 上下文，零点击。其中两句会主动推 agent 越界：
   - 「本机有 ACL 类沙箱报错时：需要 `danger-full-access` 会话，别硬试」——等于授权 agent 去要更宽权限而不是停下问人；
   - 「DSH 一升级就会覆盖 `app.asar` → 升级后第一件事 `npm run guard:check`」——把「每次宿主升级后重新打宿主二进制补丁」固化成例行动作。
2. **`dist/强制更新提示词.txt`**：教 agent 下载 zip（codeload / gh-proxy / ghproxy，**均无 sha256**）→ 写 `apply.cmd`（含 `robocopy /MIR`）→ `start /b cmd /c` 起脱离进程 → 等用户退出 DSH 后覆盖安装目录 → 再拉起 DSH；并明确要求「受限模式下会拒绝执行，请临时切到**完全权限**」。
3. **`docs/guides/AGENT-INSTALL-PROMPT.md`**：明确让 agent 执行 `git clone` → `npm install` → `setup-dsh.mjs` → 读 `~/.dsh/profiles/<p>/package.json` 自检；优点是写了不少「停在这里、不要代扫码、不要 push」的停点，比同类克制。

**定性**：不是「藏了后门的仓库」，而是**默认假设 agent 拥有完全本机权限的仓库**。单机自用时这个假设成立；
但「把链接丢给 AI」的场景里，最终确认权从人手里被拿走了——这才是它该被降级评估的原因。

### 3.9 【中】其它

- **ACL 硬化范围受控**：`src/state-acl.mjs` 只作用于 `<repo>\state` 与 `config.json`（`/inheritance:r` + 按 SID 删 Users/Everyone + 显式 grant），不碰系统目录；
  但 `harden-state-acl.mjs` 失败会 **UAC 提权**重跑自身，且无恢复脚本。
- **`state/` 切断继承后，DSH 沙箱会话写不进 `state/`**（有意取舍，文档写明）；作者还发现 `plugins/**` 也被非预期切断（来源不明）。
- **桌宠**：从 npmmirror/GitHub 下 Electron **无 hash**；用户填任意 URL 的 zip → `rmSync(appDir, recursive)` → 解压 → 以 **`--no-sandbox`** 启动（默认未安装，启用即降低安全性）。
- **依赖许可**：`@snowluma/sdk` / `@snowluma/mcp` 声明 `SEE LICENSE IN LICENSE` 却**没有 LICENSE 文件**；SnowLuma 本体是「源码可见非商业许可」，
  EULA §5.4 禁止「并入第三方安装包或 Docker 镜像、通过自动化脚本部署」。项目声称只探测不部署，但 `postinstall` 确实修改了其 npm 包本地副本（作者已据实披露）。

---

## 4. 风险分级

| 级别 | 条目 | 触发条件 | 影响 |
| --- | --- | --- | --- |
| **高** | Windows 凭据 ACL fail-open（`state/` 对本机全用户可读） | 多用户机器/远程桌面/共享机 | 控制台令牌、QQ 聊天记录、B站 cookie 泄露 |
| **高** | B站 自动评论/点赞 + 风控规避 | 启用 B站 子系统 | 账号限流/封禁；平台 ToS 违规；身份公开关联 |
| **中高** | 安装/更新链零校验 | 照 README 安装或开启自动更新 | 作者/中间人可向你的 DSH 进程注入代码 |
| **中高** | 字节级改 DSH `app.asar` + 计划任务 | 主动运行 `guard:*` | 破坏宿主签名/完整性；会话完整性判定被降级；卸载不还原 |
| **中高** | 设置页 PowerShell 命令注入 | 本机进程/页面 XSS 可打设置页 API | 以当前用户身份执行任意命令 |
| **中高** | 提示注入面（AGENTS.md / 强制更新提示词） | 把仓库交给 agent 处理 | agent 自动装、自动改宿主、自动要全权限 |
| **中** | 伪造 DSH 会话 Cookie | 能读 `~/.dsh/.credentials.yaml` 即可 | 以「已登录 DSH」身份操作宿主 |
| **中** | SnowLuma token 全量收集 + 明文回写 | 机器上有多个 QQ 账号 | 多账号 token 集中单点泄露 |
| **中** | `robocopy /MIR` 覆盖 + 排除表漂移 | 自动更新 | 用户数据被删 |
| **中** | 单人维护 / 零 CI / 版本漂移 | 长期使用 | 回归风险、修复不可预期 |
| **低-中** | 桌宠 `--no-sandbox` + 任意 zip | 启用桌宠 | 桌宠进程内代码可逃逸 Chromium 沙箱 |
| **低** | `safe-fetch` 不限端口 | agent 被诱导 | 可探测公网上的 Redis/ES 等私有服务（非内网穿越） |

---

## 5. 建议

### 5.1 如果你是使用者（想装来玩）

**结论：不要装在「主力 DSH + 主力 QQ 号 + 主力 B站 号 + 主力 Windows 账户」上。**

安全试用清单：

1. **隔离**：单独 Windows 本地用户（降低 3.4 的 ACL 泄露面），或直接 VM/备用机。B站/QQ 用小号。
2. **不要照抄安装命令**：至少先把仓库 clone 到本地读一遍再装；拒绝 `irm | iex`，拒绝 `dist/强制更新提示词.txt` 的任何步骤。
3. **不要让 agent 切 `danger-full-access`**；不要把含 `AGENTS.md` 的仓库目录作为 agent 工作区（先把 `AGENTS.md` 删掉再分析）。
4. **装前/装后核验宿主是否被动过**（需在无沙箱终端、DSH 退出时）：
   - `%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\` 下是否有 `app.asar.old-*` / `app.asar.guarded`；
   - `schtasks /Query /TN DshSessionGuard`；
   - `~/.dsh/profiles/<p>/cordis.patch.yml` 里是否有 `# === qq-bridge MCP BEGIN ===`。
5. **关掉自动更新**（`updates` 的自动检查），只在你想升级时手动 `git pull` 并自己看一眼 diff。
6. **B站 子系统默认别开**；开了就接受封号风险，且别用主账号。
7. **审计预算**：如果只是想要「QQ 机器人 + 记忆」，市面上有更小、更专注、可替换的现成方案；这套的价值在「B站 + 桌宠 + 记忆 + 主动机制」的整合度，代价是 11.7MB 的攻击面。

### 5.2 如果你是作者 / 维护者

按投入产出排序（前四条都是"少做事、少风险"，不是加功能）：

1. **把 `app.asar` 补丁下掉，改成上游 issue + 本地兜底**：宁可给用户一个「坏会话体检」脚本（`check:sessions` 已有）并引导隔离坏目录，也不要改宿主二进制。
   如果必须保留，至少改为**显式 opt-in + 每次提示 + 一键还原**，并要求用户确认签名风险。
2. **ACL 失败 fail-closed 或换存储**：收紧失败时拒绝启动桥接（或至少拒绝生成/使用控制台令牌），凭据改 DPAPI 加密；`config.json` 里的 SnowLuma token 同样处理。
3. **修命令注入**：`handleBrowse` 的 `title` 不要拼进 `-Command`，改用 `-EncodedCommand` + 参数数组，或干脆用环境变量传参（`harden-state-acl.mjs` 已经示范了正确写法）。
4. **更新链加签名**：release 清单用离线私钥签名（minisign/ed25519），zip 通道给 sha256 并在 UI 显示；`/MIR` 改白名单拷贝或先快照。
5. **降低提示注入面**：`AGENTS.md` 里删掉「需要 danger-full-access」这类话；`setup-dsh.mjs` 的安装说明书改成「由用户执行，AI 只做只读检查」。
6. **补齐工程门禁**：加 CI（至少 `npm run test:audit` + 语法检查），把「版本号唯一来源」收敛到一个文件生成，修 CHANGELOG 空节与重复标题。
7. **拆 `bridge.js`**：作者自己在风险台账里标 🔴，这是长期维护的最大成本项；建议先把纯函数型控制台端点抽出去。
8. **回应 issue**：5 个 bug 报告（其中 #8 是 TDZ 真 bug）零回复，对 155 stars 的项目来说是最便宜的信誉投资。

---

## 6. 事实核查与未验证（方法说明）

**已一手核实**（我自己读的原文）：

- `README.md`、`package.json`、`LICENSE`、`AGENTS.md`、`docs/guides/ARCHITECTURE.md`、`docs/RISK_REGISTER.md`、`docs/guides/SECURITY_BASELINE.md`；
- `scripts/patch-dsh-session-guard.mjs` **全文**（等长替换逻辑、`ASAR_DEFAULT`、`--apply/--apply-in-place`、备份/回滚）；
- `scripts/install-session-guard.mjs` **全文**（`schtasks`、`--watch`、冒烟、回滚、自删任务）；
- `plugins/surfcore/qq-tool-restrict.mjs` **全文**（restrict 名单 + 执行期 guard）；
- 本机环境：DSH `0.2.1-rc.3`（`resources/app.asar/dsh/package.json` 读到），`app.asar` 是**文件**，未发现 `.old-*`/`.guarded` 残留，未发现 SurfCore 安装痕迹（`~/.dsh/surfcore/config.json` 不存在）。

**未验证 / 置信度边界**（不要当成"没问题"）：

1. `src/bridge.js` 第 2479 行之后（834KB 的绝大部分）：控制台鉴权实现、全部 `/api/*`、审批流、`onebotSend`、更新逻辑——只有文档 + `test-console.mjs` 等间接证据。
2. `plugins/surfcore/lib/client.js`（343KB）中段/尾部未读，理论上仍可能藏远端地址。
3. `plugins/surfcore/lib/updates.js`（33KB）未逐行读；`notify`（用户可配 webhook/cmd 出网通道）的实际地址未核实。
4. `dist/installer/1-安装说明.txt`、`2-安装.bat`、`3-自检.bat` 读不到（raw 返回 `application/octet-stream`），只能从 `installer.mjs` 推断。
5. 全部文件经 `gh-proxy.com` 代理读取，**未做 sha256 校验**；理论上代理可篡改内容——本报告"无恶意代码"的结论因此是**条件性**的。
6. **无法全仓 grep**：shell 在本次会话不可用（`0xC0000142`），"未发现上报端点"的置信度限于所读文件。
7. 未审计信任链上游：SnowLuma 本体、`Mieksido/surfpet` 桌宠、语音包、DSH 本体。
8. B站 侧 `scheduler.js`（70KB）、`bili-memory.js`、`token-ledger.js` 未逐行读；读私信/直播监听的落盘位置未核实。
9. GitHub Releases / tag 是否真发过未确认（API 403 限流），`has_downloads:false` 不足以判断。
10. 155 stars 的来源无法证实（B站/知乎无 "SurfCore" 内容；可能是前身 `qq-bridge` 的老用户迁移）。

**复核方式建议**（在有 shell 的机器上）：

```bash
git clone --depth 1 https://github.com/Mieksido/surfcore.git
cd surfcore
grep -RIn --exclude-dir=node_modules -E "https?://[a-z0-9.-]+" src plugins scripts | grep -vE "127\.0\.0\.1|localhost|bilibili|deepseek|github|npmmirror"   # 找非白名单外链
grep -RIn --exclude-dir=node_modules -E "child_process|execFile|spawn|eval\(|new Function|schtasks|icacls" src plugins scripts
npm run test:audit        # 看门禁真实通过数
schtasks /Query /TN DshSessionGuard   # 若已装，确认是否留下计划任务
```

---

## 7. 结论

- **技术上**：这是一个超出个人项目平均水平的作品——安全判据的工程质量、文档诚实度、SSRF/工具面防护都在平均线之上；但结构失衡（巨型单文件）、工程纪律靠自觉（零 CI、版本漂移）、维护是单点。
- **安全上**：**没有后门**，但**信任模型很重**。它要求你信任：作者每次推 `main`、GitHub raw 不被劫持、npm 包与 electron 镜像、SnowLuma 非商业运行时、以及「让 AI 全权限操作本机」这套工作流。任何一环出问题，你得到的是「以你的身份执行任意代码」。
- **最适合它的场景**：一台可随时重装的备用 Windows 机器 / 独立账户，小号 QQ + 小号 B站，你愿意跟着它的文档一起冒险，并且能自己看代码。
- **最不适合的场景**：主力工作机、公司电脑、多人共用的 Windows、有任何真实凭据价值的 QQ/B站 主账号、以及「把链接丢给 AI 让它自己装」。

> 本报告是静态审计 + 文档核验，不构成对代码安全性的担保。所有引用以 2026-10-28 的 `main` 为准，作者可能随时改动。