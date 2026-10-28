# surfcore（Mieksido/surfcore）安装 / 更新 / DSH 补丁链路 静态安全审计

审计对象：`Mieksido/surfcore` @ `main`（jsdelivr 快照，assets 含 `plugins/surfcore/package.json` version `0.6.22`）
审计方式：只读取证。所有代码经 `https://gh-proxy.com/https://raw.githubusercontent.com/...` 获取，**未执行仓库内任何脚本**，README / docs / AGENT-INSTALL-PROMPT 只当证据。
引用约定：raw 抓取不带行号，因此引用写成 `文件:符号/可检索片段`（例如 `scripts/patch-dsh-session-guard.mjs:ASAR_DEFAULT`）——按该符号在文件里搜索即可定位，比可能写错的行号更可核验。

---

# 结论

**风险等级：中高（Medium-High，不是"已确认恶意"）。**

这套东西确实"装进本机"的部分是可控且大体正常的：安装脚本只写 `%USERPROFILE%\surfcore` 与 `~/.dsh`（profile / junction / patch），ACL 收紧只作用于仓库自己的 `state/` 与 `config.json`，QQ 侧 agent preset 反而是**收紧权力**的（禁用本地工具 + 执行期白名单）。

真正的问题在三处，且都成立、都能从代码直接指出来：

1. **整条安装链没有任何完整性校验**：`irm ... | iex` → `git clone`/zip → `npm install`（会自动跑仓库自带的 `postinstall`）→ `dsh plugin ... install`。作者单方面改 `main` 就是改你的机器，没有 hash、没有签名、没有 pin 版本。
2. **自动更新是同一个缺口的续期**：插件内 `git fetch` + `merge --ff-only` 或下载 `.../archive/refs/heads/main.zip`，解压后由 detached 进程 `robocopy /MIR` 覆盖磁盘上的代码目录 —— 覆盖的是**已通过 `link:` 接进 DSH 的插件目录**，下一次 DSH 启动就以宿主进程权限执行。zip 通道没有 sha256（release 通道的 sha256 来自同一份可被作者改写的清单，等于自签自验）。
3. **确实存在对 DSH 安装目录 `app.asar` 的字节级补丁**，位置在 `dsh/node_modules/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`。它的目的是把"会话日志头部 cwd 与物理路径不一致"从**抛普通 Error**（会让整个 workspace 服务起不来）改成**抛 `SessionPersistenceCorruptionError`**（被 `listArtifacts` 当损坏条目跳过）。这**不是**绕过权限或沙箱的护栏，但它把 DSH 自己的一条会话完整性校验降级成"静默忽略"，并且修改的是应用本体（通常受签名 / asar 完整性保护），还靠一个**计划任务**等在 DSH 退出后换文件。

未发现恶意行为：没有遥测回传、没有隐藏进程/COM/注册表 Run 键、没有 plan 之外的持久化、没有窃取凭据外发。相反，仓库在多处主动收缩了权限（ACL 收紧 `state/`、QQ preset 禁本地工具、ASAR 补丁带 SHA256 指纹与回滚）。

---

# 关键发现

## 1. `irm | iex` 是无校验的远程代码执行入口（事实）

`scripts/bootstrap.ps1:文件头注释` 就是 README 让陌生人复制的那一行：

```
powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/Mieksido/surfcore/main/scripts/bootstrap.ps1 | iex"
```

`bootstrap.ps1` 随后：
- `git clone --depth 1 https://github.com/Mieksido/surfcore.git $Dir`（`bootstrap.ps1:git clone`），失败则 `Invoke-WebRequest https://github.com/Mieksido/surfcore/archive/refs/heads/main.zip` + `Expand-Archive`；
- `& npm install --no-audit --no-fund`（`bootstrap.ps1:npm install`）；
- `node scripts/detect-profile.mjs`、`node scripts/setup-dsh.mjs $profile`。

**影响**：`-ExecutionPolicy Bypass` + `main` 分支 + 无 hash/签名/版本 pin = 仓库作者（或任何能改这个仓库/劫持 `raw.githubusercontent.com` 的人）在任何时刻对执行该命令的机器拥有等价于当前用户的代码执行权。这不是本仓库独有缺陷，但它就是本项目的官方安装方式，必须写进风险面。

## 2. `npm install` 会执行仓库自带的 `postinstall`，改写 vendor 代码（事实）

根 `package.json:scripts.postinstall` = `node scripts/patch-snowluma-sdk.mjs`。
`scripts/patch-snowluma-sdk.mjs` 遍历 `node_modules/@snowluma/sdk` 与 `@snowluma/mcp` 的 `dist/**/*.js`，用正则把 `from './x'` 改成 `from './x.js'` 并**就地覆写文件**。

**影响**：安装即执行仓库代码（意料之中，但 `--no-audit` 又关掉了 npm 的审计提示）；被改的是第三方包内容，`npm install` 重装即可恢复。另外 `dsh plugin ... install`（`setup-dsh.mjs:autoInstallProfileBundles`）同样会跑 pnpm 的依赖 lifecycle 脚本 —— 这是第三方依赖的任意代码执行面，本审计无法穷举（`@deepseek-ai/dsh-host-apiproxy`、`@snowluma/*`、`@modelcontextprotocol/sdk`、`js-yaml`、`zod`）。

## 3. DSH `app.asar` 被真实补丁：会话完整性校验降级（**本次最关键**）

- `scripts/patch-dsh-session-guard.mjs:TARGET` 硬编码目标：
  `/dsh/node_modules/@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`
- `scripts/patch-dsh-session-guard.mjs:OLD` / `NEW_PREFIX` / `NEW_MESSAGE`：
  ```
  OLD: throw new Error(`corrupt session log "${path}": header id "${meta.id}" and cwd identify "${expectedPath}"`)
  NEW: throw new SessionPersistenceCorruptionError(`corrupt session log "${path}": cwd mismatch ${expectedPath}` + 补空格)
  ```
- `buildReplacement()` 强制**等长**替换（长度不符直接抛错、绝不写文件），所以 asar 头/偏移无需重算。
- `readAsarHeader()` / `findEntry()` / `patchBuffer()`：自己解 asar 头部、按路径找 entry、在内存里替换字节。
- `apply()` / `applyInPlace()`：先 `fs.copyFileSync(asar, asar + '.bak-<ts>')`，再要么原子 rename（`asar -> .old-<ts>`，tmp -> asar），要么 `openSync('r+')` 原地写变化区间。

**目的（代码注释自述，可信度较高）**：`~/.dsh/sessions/...` 里一个"头部 cwd 与物理目录不一致"的坏会话会让 `listArtifacts()` 抛普通 Error、只吞两类 corruption 错误，于是整个 `list()` 失败、workspace 服务起不来。补丁让这条错误落进"跳过损坏条目"的分支。

**影响评估（诚实版）**：
- 它**没有**放宽任何权限 / 沙箱 / 工具白名单，也没有关闭鉴权；打开该会话仍会照常报错（仅 `list` 时跳过）。
- 但它**确实削弱了 DSH 的一条完整性判定**：会话文件"物理路径 ≠ 头部 cwd/id"原本是硬错误（可疑篡改 / 移动 / 串目录会暴露），补丁后变成静默忽略。如果有人把会话文件放到错目录来"藏"，现在不会报错。
- 它**修改应用本体**：`resources/app.asar`。Electron 若启用 asar integrity / 代码签名校验，这种改动会失败或破坏签名信任链（`install-session-guard.mjs:smokeTest` 正是为此写的冒烟检查）；`--apply` 路径在运行中会 rename 正在使用的 asar（脚本注释已说明运行中会被拒）。
- **默认路径写死成作者机器**：`ASAR_DEFAULT = 'D:\\deepseek\\resources\\app.asar'`、`EXE = 'D:\\deepseek\\DeepSeek Harness.exe'`（`install-session-guard.mjs:EXE`）。普通安装位置 `%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\app.asar` **默认不会被命中**，需要显式 `--asar "..."`。也就是说：在多数用户机器上，这段代码默认是"跑不起来的"，这降低了实际命中率，但不改变意图。

## 4. 计划任务 + 后台守候 + 替换应用二进制（事实）

`scripts/install-session-guard.mjs`：
- `schedule()`：`schtasks /Create /TN DshSessionGuard /TR "<node>" "<script>" --watch --timeout-s 172800 /SC ONCE /ST <h+1min> /F /RL LIMITED`
- `watch()`：每 1.5s 用 `tasklist /FI "IMAGENAME eq DeepSeek Harness.exe"` 判断 DSH 是否退出，连续两次确认后 `installNow()`。
- `installNow()`：拒绝在 DSH 运行时换入；校验 `sha256(app.asar) == meta.sourceHash`（防止 DSH 升级后套旧补丁）；`rename(app.asar -> .old-<ts>)`、`rename(app.asar.guarded -> app.asar)`；`smokeTest()` 用 `ELECTRON_RUN_AS_NODE=1` 启动 `DeepSeek Harness.exe -e "readFileSync(app.asar/dsh/package.json)"` 验证可读，失败自动回滚。
- `cleanupTask()`：成功后 / 超时后 `schtasks /Delete /TN DshSessionGuard /F`。

**影响**：这是显式的、自清理的持久化（不是隐藏后门）。残留条件：watch 进程在 172800s 内一直等不到 DSH 退出，则任务会挂着直到超时；若用户强杀 watch 进程，`ONCE` 任务仍会在注册时间点启动一次（启动后若无 DSH 退出就继续等）。
**注意**：`--watch` 是一个**独立于 DSH 进程树**的常驻 node 进程（这正是计划任务的用意），会持续读取进程列表。

## 5. 自动更新：`git merge --ff-only` / 下载 zip + `robocopy /MIR` 覆盖=新的代码执行（事实 + 推断）

`plugins/surfcore/lib/updates.js`：
- `runGit()` 包装 git，`GIT_TERMINAL_PROMPT=0`、`GCM_INTERACTIVE=never`（不卡交互，这点做得对）。
- `inspectGitComponent()`：`git fetch --quiet --no-tags origin <branch>` → `rev-parse` → `rev-list --count` → `git status --porcelain`（脏/分叉则 `blocked`）。
- `fastForwardComponent()`：`git merge --ff-only origin/<branch>`（无 `reset --hard`、无 stash，纪律良好）。
- `updateFromZip()`：`fetch(item.archiveUrl ?? 'https://github.com/<repo>/archive/refs/heads/main.zip')` → `Expand-Archive` → `stageDeferredApply()`。
- `downloadReleaseAsset()`：若清单给了 `sha256` 就比对，否则**只比大小**，并在日志里承认"只校验了大小"。

`plugins/surfcore/lib/index.js`（组件清单）：`surfcore` 本体 = `{ dir: QQBOT_ROOT, kind: 'git', branch: 'main', versionRepo: 'Mieksido/surfcore' }` —— 就是被 `~/.dsh/plugins/surfcore` junction 接进 DSH 的那棵树。

`scripts/apply-update.mjs`：
- `waitExit(pid)`：`process.kill(pid, 0)` 轮询，最多 15min，等宿主退出；
- `copyZip()`：`robocopy <staging> <target> /MIR /XD state backups node_modules .git logs staging pet characters voice-packs tmp-update* /XF config.json bili.config.json updates-pending.json`；
- `gitPull()`：`git fetch --prune` + `git merge --ff-only`；
- 写 `.installed-meta.json`。

**影响**：`/MIR` 是**镜像**，会删除目标里"源里没有"的文件；排除表覆盖了用户数据目录与配置，但仍是"整目录删除式覆盖"——排除表漏一个月（或 updater 自己新加目录）就会删用户数据，这是**可用性/数据丢失**风险而非机密性风险。
**供应链影响（推断，逻辑成立）**：单个维护者推 `main` → 客户端 ff-merge / zip 覆盖 → 下次 DSH 启动时 `plugins/surfcore/lib/index.js` 在**宿主进程**里 `import` 执行（该插件 `inject: ['webServer','connection','sessionController']`，用 `fs`、`child_process.spawn`、`execFile`、`powershell`），无签名、无 pin、无不可抵赖的 hash 锚点。等价于"作者永久性地持有你 DSH 进程的执行权"。
`startAutoCheck({ delayMs = 20_000, intervalMs = 6h, enabled = true })` 默认开启；是否由 `index.js` 自动调用，我只读到 index.js 前 ~50KB（截断）未能确认 → 标为**推断/待验证**。

## 6. `setup-dsh.mjs` 具体改了哪些路径（事实，逐条）

以 `DSH_HOME = %USERPROFILE%\.dsh`、profile=`web` 为例（`setup-dsh.mjs:DSH_HOME`）：

| 写入对象 | 操作 | 位置 |
|---|---|---|
| `C:\Users\<u>\.dsh\profiles\web\cordis.patch.yml` | 解析 → 删除由它托管的 3 个 MCP 条目 → 追加托管区块（新条目写成 `- insert:` 形式）；写前 `backupBeforeOverwrite()` 打时间戳快照 `.bak-<ts>`，保留最近 5 份；临时文件 + `rename` 原子替换 | `prepareCordisPatch` / `patchCordis` / `mcpEntries` |
| `C:\Users\<u>\.dsh\profiles\web\package.json` | 注入 `dependencies.surfcore = "link:<repo>\plugins\surfcore"`、`dsh.profile.bundles += "surfcore"`，删除旧 bundle `qq-agent-presets` | `patchProfilePackage` |
| `C:\Users\<u>\.dsh\plugins\surfcore` | 创建 **junction** 指向 `C:\Users\<u>\surfcore\plugins\surfcore`（已存在且指向别处则删除重建） | `ensureBundle` / `ensureDirLink` |
| `C:\Users\<u>\.dsh\profiles\web\node_modules\surfcore` | 同上，第二层 junction（preset 子行按 profile 目录解析裸说明符） | `ensureBundle` |
| `C:\Users\<u>\surfcore\plugins\surfcore\presets\*.patch.yml` 等 | `regeneratePresetPatches()` → `scripts/build-agent-preset-patches.mjs` 从 `dsh/agent-presets/` 重新生成 | `regeneratePresetPatches` |
| `C:\Users\<u>\surfcore\state\mode.json` | 不存在时创建 `{ mode: 'reserved2', closedAgentPreset: '' }` | `ensureLocalModeFile` |
| 子进程 | `cmd /d /v:off /s /c dsh.cmd plugin --profile "<profile>" install`（env 只传 `QQ_BRIDGE_SETUP_PROFILE`，120s 超时，`windowsHide`） | `autoInstallProfileBundles` |

三个注入的 MCP（`setup-dsh.mjs:mcpEntries`）都是 `transport: stdio`、`command: <当前 node 的绝对路径>`、`args: [<repo>/src/...]`：
- `mcp-snowluma` → `src/mcp-snowluma-safe.js`，**`toolCallTimeoutMs: 725000`（≈12 分钟）**
- `mcp-snowluma-host` → `src/mcp-host-server.js`（只读 `snowluma_status`，注释说明已删除 `start/stop_snowluma` 进程控制类工具）
- `mcp-web-search-safe` → `src/mcp-web-search-safe.js`

**不写**：注册表、环境变量（持久）、启动项、服务。旧目录 `~/.dsh/.agent-presets` **只报告不删除**（`reportLegacyPresetDir`），这点是克制的。
`725000ms` 的工具超时值得点一句：它让单次 `qq_wait_for_messages` 能在宿主的工具调用里挂 12 分钟，是"长挂起"而不是越权。

## 7. `icacls` 的真实范围、可逆性、提权（事实）

`scripts/harden-state-acl.mjs` 自己**不含**任何 icacls，全部转调 `src/state-acl.mjs`：
- 目标仅两个：`<repo>\state`（目录，递归）与 `<repo>\config.json`（存在时）。`src/state-acl.mjs:PERMISSIVE_SIDS` = `Authenticated Users(S-1-5-11)`、`BUILTIN\Users(S-1-5-32-545)`、`Everyone(S-1-1-0)`。
- `hardenDir()` 四步：① `icacls <dir> /inheritance:r /C /Q`；② `icacls <dir> /remove:g *S-1-5-11 *S-1-5-32-545 *S-1-1-0 /T /C /Q`；③ `icacls <dir> /T /C /Q /grant <user>:F /grant SYSTEM:F /grant Administrators:F`（**不带** `(OI)(CI)`，所以对 `/T` 展开出的文件也有效）；④ 目录上加一份可继承的 `(OI)(CI)` ACE。之后 `fs.readdirSync(dir)` 自检，不可读则报失败。
- `hardenFile()`：`/inheritance:r /grant:r user:F /grant:r SYSTEM:F /grant:r Administrators:F`。

**误伤/继承**：注释自述历史 bug —— 旧版把 `/inheritance:r` 和 `(OI)(CI)` 用在同一条 `/T` 命令上，导致 `/T` 展开的子**文件** DACL 被清空（连属主都读不了），且回读自检被 `Mandatory Label\Low Mandatory Level:(I)...` 这一行误导，导致**每次启动都重跑那条破坏性命令**。现版本已按上面四步拆开，并显式排除 Mandatory Label 行（`hasInheritedPermissionAce`），有单测 `test-hardening.mjs`。
**范围外**：不碰系统目录、不碰 `C:\Windows`、不碰 DSH 安装目录，只碰仓库树内两个路径 —— 不会"切断系统继承"。
**副作用（有意的）**：`state/` 切断继承后，DSH 沙箱会话（按能力 SID 继承授权）里写 `state/` 会被拒。`scripts/diagnose-acl.mjs:文件注释` 明确写了这一点，并**警告不要** `icacls /inheritance:e` 去"修好"它（会把 Authenticated Users 的可继承 ACE 带回来）。
**可逆性**：**没有**提供恢复脚本；`uninstall.mjs` 也不还原 ACL。`diagnose-acl.mjs` 只打印 `icacls "<dir>" /inheritance:e /C /Q` 作为建议（且只对"非预期收紧"的路径）。
**提权**：`harden-state-acl.mjs:elevateAndRetry()` 在直接收紧失败且非 `--elevated` 时，用 `Start-Process -FilePath $env:DSH_HARDEN_NODE -ArgumentList @($env:DSH_HARDEN_SCRIPT,"--elevated","--quiet") -Verb RunAs -Wait -WindowStyle Hidden` 弹 UAC 重跑自己。路径经**环境变量**传参而不是拼进 `-Command` 字符串（这段注释与写法是对的，避免了提权后的命令注入）。`--elevated` 标记用于防递归。用户点"否"会被吞掉并降级为打印手动命令、`exit 0`。

## 8. 卸载不完整：补丁 / ACL / 计划任务都会残留（事实）

`scripts/uninstall.mjs` 只做四件事：profile `package.json` 去掉 `surfcore`、`cordis.patch.yml` 删掉 `# === qq-bridge MCP BEGIN/END ===` 区块、删两个 junction、`--purge-state` 时删 `~/.dsh/surfcore`。
**不还原**：`app.asar` 补丁（及 `app.asar.old-*` / `app.asar.guarded` / `state/guard-patch.json`）、`state/` 与 `config.json` 的 ACL、计划任务 `DshSessionGuard`、仓库目录本身（它自己说明"自己删"）。

## 9. QQ 侧 preset 是"缩小权力"，不是放大（事实，值得表扬）

`dsh/agent-presets/qq-chat-v2/agent.cordis.yml`：
- 注释即声明「无本地工具」；实际配置 `tool-web` 的 `search: false, fetch: false`（禁用内置 `web_search`/`web_fetch`，改用带 SSRF 防护的 `mcp__web-search-safe__*`），只挂 `tool-ask-user`、`tool-todo`、`compaction`（含 `tool-result-pruner` 50k 阈值）。
- `qq-tool-restrict`（权威实现在 `plugins/surfcore/qq-tool-restrict.mjs`）做了两层：
  1. `ctx.tools.restrict({ deny: [name] })` 逐个隐藏 `pwsh/bash/read/write/edit/glob/grep/load_workspace_dependencies/subagent*/workflow/ralph/dsh_snapshot/dsh_rollback/plugin_manager/schedule_*/dev_*` 等（名单逐条对过 DSH 真实工具名，注释里还写明"假名会静默失效"这个坑）；
  2. `ctx.tools.guard(exec => ...)` 执行期白名单：仅允许前缀 `mcp__snowluma__` / `mcp__snowluma-host__` / `mcp__web-search-safe__` 与精确名 `ask_user_question`、`todo_write`，其余一律返回拒绝原因。
- 失败模式也是 fail-closed 的：`DSH_SETUP.md` 写明"无 preset 会话会回落 DSH 默认 preset（含 bash/文件读写）"，所以桥接**直接拒绝**建无 preset 会话。
这个设计方向是对的：给 QQ 群里陌生人的 agent 反而拿不到本地执行能力。

## 10. 桥接持有 DSH 的"浏览器会话身份"（事实，需知悉）

`docs/guides/DSH_SETUP.md:DSH 侧 401/鉴权失败` 一节写明：桥接 `src/bridge.js` 读 `~/.dsh/.credentials.yaml` 里的 `client-connection/browser-session` 签名密钥，**离线铸造 DSH 会话 Cookie**，以浏览器会话身份调 DSH RPC（`settings/update`、`session/control` 等），不再依赖进程内存 token。
**影响**：一个独立 node 进程（`src/bridge.js`，由插件或 `start.bat` 拉起）拿到等价于"已登录 DSH Web"的身份，其控制台用 `state/console-token` + 127.0.0.1 绑定保护。任何能读 `state/` 或 `config.json` 的本地进程都能借这条路驱动 DSH。这也是它为什么要花力气收紧 `state/` ACL、并把 `config.json`（内含 SnowLuma OneBot accessToken）一起收紧 —— 逻辑自洽，但意味着**`config.json` 里存着一个能收发你 QQ 消息的 token**。

## 11. 桌宠：下载 zip 即运行、且 `--no-sandbox`（事实，默认未安装）

`plugins/surfcore/lib/pet.js`：
- `installElectronRuntime()`：从 `https://npmmirror.com/mirrors/electron/45.0.0/...zip` 或 GitHub 官方下载 Electron，`Expand-Archive` 解压，**无 hash 校验**。
- `download()` + `unzip()`：用户填的 `source`（`http(s)` 或本地路径）下载 zip → `fs.rmSync(appDir, recursive, force)` → `Expand-Archive`。
- `start()`：`spawn(electron, ['.', '--no-sandbox', '--user-data-dir=<appDir>/data/profile'])`，`detached: true`。
- 有一层防误删闸 `checkAppDir()`（禁盘根/禁受保护目录的上级/非空且无标记则拒绝），并有 `looksLikeWrongPackage()` 拦"把 SurfCore 源码包当桌宠装"。
**影响**：`--no-sandbox` 关闭了桌宠进程的 Chromium 沙箱（注释理由是受限环境下 profile 初始化 EPERM），加上"任意 URL 的 zip 直接解压执行"，是默认未安装但一旦启用就降低安全性的功能面。

---

# 逐脚本说明

### `scripts/bootstrap.ps1`（6785 B）
环境检查（node ≥22.13，允许回退到 `~/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/node/bin/node.exe`；`netstat` 探 3000/3001 判断 SnowLuma）→ 取代码（`git clone --depth 1`，或 zip 到 `%TEMP%\surfcore-main.zip` 再 `Move-Item` 到 `$env:USERPROFILE\surfcore`；已存在则 `git pull --ff-only`）→ `npm install`（无 npm 则用 DSH 自带 pnpm）→ `detect-profile.mjs` + `setup-dsh.mjs <profile>` → `Copy-Item config.example.json config.json`（已存在则不覆盖）。
写：`%USERPROFILE%\surfcore\**`、`%TEMP%\surfcore-main.zip`、`~/.dsh`（经 setup-dsh）。**不写**注册表/环境变量/启动项/计划任务。

### `dist/installer/installer.mjs`（8021 B）+ `2-安装.bat` / `3-自检.bat` / `1-安装说明.txt`
`installer.mjs`：`TAG = 'v0.3.2'`，`REPO_ZIP = https://github.com/Mieksido/surfcore/archive/refs/tags/v0.3.2.zip`（**钉了 tag，比 bootstrap 好**，但仍无 hash）。流程与 bootstrap 基本一致：`Invoke-WebRequest` 下载 → `Expand-Archive` 到 `dist/installer/` → 重命名成 `surfcore` → `npm install`（900s 超时）→ `detect-profile.mjs` → `setup-dsh.mjs` → 复制 config。**同样无签名/无 sha256**。
`.bat` / `.txt`：**未能读取**（raw 返回 `application/octet-stream`，代理与 jsdelivr/statically 均拒）。从 `installer.mjs` 的用法注释 `node installer.mjs [--check] [--zip <path>]` 与 `3-自检.bat` 生成 `诊断报告.txt` 的说明推断：`2-安装.bat` = `cd /d %~dp0` + `node installer.mjs`，`3-自检.bat` = `node doctor.mjs`。**标为未验证**。

### `scripts/setup-dsh.mjs`（写 DSH profile / patch / package.json / 3 个 MCP）
见关键发现 #6。额外细节：profile 名做了路径边界校验（拒 `.`/`..`/`node_modules`/含 `\\/ \x00-\x1f<>:"|?*`/以 `.` 或空格结尾）；YAML 解析失败时**不写文件**；只修历史遗留的"根 `[]` 后接块操作"；`ensureDirLink` 遇到"存在但不是符号链接/junction"直接 fatal 而不是删（保守，正确）。`backupBeforeOverwrite` 保留最近 5 份 `.bak-<ts>`。

### `scripts/install-session-guard.mjs` + `scripts/patch-dsh-session-guard.mjs`
见关键发现 #3 / #4。二者是"库 + 执行器"关系：`install-session-guard.mjs` 从 `patch-dsh-session-guard.mjs` 导入 `ASAR_DEFAULT / inspect / inspectBytes / patchBuffer`，自己负责"准备副本 → 守候 → 换入 → 冒烟 → 回滚 → 清理计划任务"。另有 `npm run guard:check|prepare|install|status` 四个入口（根 `package.json:scripts`）。

### `scripts/apply-update.mjs`
见关键发现 #5。细节：启动即写 `<pending>.started` 标记（插件侧 2.5s 后检查，防止 Electron 没带 `ELECTRON_RUN_AS_NODE` 时"静默退出但显示成功"）；`pending.meta` 存在时写 `<target>/.installed-meta.json`；失败写 `<pending>.failed`；成功写 `updates-applied/<ts>-<component>.json` 并把 `pending` 与 `staging` 清掉。`copyZip` 的 robocopy 退出码按 `>=8` 才算失败。

### `plugins/surfcore/lib/updates.js`
见关键发现 #5。三种通道：`git`（fetch/ff-only merge）、`version`（提交级/版本号比对，zip 更新）、`release`（版本清单 + sha256）。"不再提示"以 `dismissed[id] = remoteSha` 记，远端前进会重新提示；`startAutoCheck` 默认 20s 后首次、每 6h 一次（`enabled=true`）。`fetchCommitSha` / `fetchRemoteVersion` 调 `api.github.com`（无 token，60 次/小时配额），`fetchLatestByRedirect` 走 302 省配额。

### `scripts/uninstall.mjs`
见关键发现 #8。默认是**演练**（不加 `--yes` 什么都不改），这点很好。

### `scripts/harden-state-acl.mjs` + `src/state-acl.mjs` + `scripts/diagnose-acl.mjs`
见关键发现 #7。`diagnose-acl.mjs` **只读**（注释与代码一致：只 `icacls <target>` 读，不改），`--strict` 时"发现非预期收紧"以 1 退出可进 CI；它在 DSH 沙箱里因 `EPERM`（不能以管道 stdio 拉子进程）会以 2 退出并解释原因，这个降级处理是对的。

### 其它相关（顺带确认，非本次重点）
- `plugins/surfcore/package.json`：`dsh.bundle.patch = ['./cordis.patch.yml','./presets/qq-chat.patch.yml','./presets/qq-chat-v2.patch.yml']`，`dsh.client.platform = web`；无 `postinstall`。
- `plugins/surfcore/cordis.patch.yml`：只 insert 两行插件（`surfcore` 本体 + `qq-mode`），没有改别人的行、没有 `remove`/`modify` 别的插件。
- `plugins/surfcore/lib/doctor.js`：纯函数体检，只算"缺什么"，不写盘。
- `plugins/surfcore/lib/bridge.js`：桥接启停。`start()` 直接 `spawn(node, ['src/bridge.js'], {detached, stdio:'ignore', windowsHide:true})`（注释解释为何绕过 `start.bat` 以避开 WindowsTerminal 黑框）；`stop()` 走 `scripts/stop-qq-bot.ps1`，失败则按 `bridge.lock` 的 pid `process.kill` 兜底。
- `start.bat`：`node scripts\harden-state-acl.mjs --quiet` 然后 `:loop node src/bridge.js` + 崩溃 5s 重启。**这解释了两件事**：ACL 收紧会在用户手动 start 时自动跑；桥接有崩溃循环。

---

# 未验证 / 存疑

1. **`.bat` 与 `1-安装说明.txt` 未读到正文**：`raw.githubusercontent.com` 对这些文件返回 `application/octet-stream`，`web_fetch` 拒绝；jsdelivr / statically / ungh 通道均失败。结论是"推断为 node 包装"，未坐实。
2. **`scripts/apply-update.mjs` 不在 jsdelivr 目录清单里**：清单输出中段被代理截断（44410 B 被省略），我按 `updates.js` 的 `path.join(REPO_ROOT,'scripts','apply-update.mjs')` 直接取 raw 成功，文件确实存在。**但那份被省略的清单里可能还有我没看到的脚本**；"scripts/ 全清单"这一点未闭环。
3. **`plugins/surfcore/lib/index.js` 只读到前 ~50KB**（原文件 134 KB，`web_fetch` 截断）。未确认：插件是否会在启动 / 某条 API 路由里**自动**调用 `install-session-guard.mjs` / `harden-state-acl.mjs`。已读到开头 `apply(ctx)` 里有 `ensureDirs()`、`resumePendingApply()`、`startSchedulerNow()`、`startMemoryCardTimer()`、角色迁移、`maybeAutoStartBridge()`，**没有**看到 guard/harden 调用；`/pet/electron-install`、`/pet/install` 这类路由是明确由用户点击触发的。
4. **`src/bridge.js`（834 KB）完全未读**，只从 `harden-state-acl.mjs` 与 `lib/bridge.js` 的注释得知"桥接启动时会自己收紧 state/ ACL"。桥接内部是否有别的写盘 / 出网 / 计划任务逻辑，未验证。
5. **`mcp-snowluma-safe.js`（83 KB）与 `mcp-web-search-safe.js` 未读**：`safe-fetch.js`/`http-guard.js` 的存在与 `test-http-guard.mjs` 表明作者做了 SSRF 防护，但我没有逐条验证 `web_fetch` 的目标过滤是否真的挡住了内网/元数据地址。这正是"QQ 群里陌生人能触发的最强能力"，建议补审。
6. **"未发现恶意行为"的能力边界**：我只能对**读过的文件**下此判断。B站侧（`scheduler.js` 70KB、`bili-*.js`、`token-ledger.js`）、`src/bridge.js`、134KB 插件本体、大量 `test-*.mjs` 均未读，不能替它们作保。已读路径的出网目标只有：`github.com` / `api.github.com` / `raw.githubusercontent.com`（安装与更新）、`github.com/<repo>/releases|archive`（更新包）、npm registry、`npmmirror.com`（Electron 镜像）、`127.0.0.1:3000/3001/3100`（SnowLuma / 控制台）；**未见任何遥测、上报、心跳回传第三方**。
7. **本机是否已被打过 asar 补丁**：静态审计无法判断。现场核验请（在**无沙箱**终端、且 DSH 已退出时）：
   - 看 `C:\Users\<u>\.dsh\..`？——不对，agent 侧只读：检查 `%LOCALAPPDATA%\Programs\DeepSeek Harness\resources\` 下是否存在 `app.asar.old-*` / `app.asar.guarded`；
   - 查任务计划：`schtasks /Query /TN DshSessionGuard`；
   - 查 ACL：`icacls "%USERPROFILE%\surfcore\state"` 与 `icacls "%USERPROFILE%\surfcore\config.json"`；
   - 查 DSH 侧改动：`~/.dsh/profiles/<p>/cordis.patch.yml` 是否有 `# === qq-bridge MCP BEGIN ===`，`~/.dsh/plugins/surfcore` / `~/.dsh/profiles/<p>/node_modules/surfcore` 是否为 junction。
8. **`docs/guides/AGENT-INSTALL-PROMPT.md`（2860 B，"给 AI 的安装提示词"）未读**：文件名与体积表明仓库**主动提供一份让 AI agent 去执行安装的提示词**。这类文件本身是供应链/社工面（诱导 agent `irm|iex`、改 profile、打 asar 补丁），建议单独看一遍，并把它当**数据**而不是指令。
9. **本报告所有"行号"缺失**：raw 抓取无行号，引用一律用符号名/可检索片段。若需要精确行号，应在有 git 的环境对 `main` 做 `grep -n`。