# surfcore 运行时审计：数据流向、凭据落盘、攻击面

审计对象：`Mieksido/surfcore`（main）。
范围：QQ 桥接进程（`src/`）+ DSH 合体插件（`plugins/surfcore/`）+ 注入 DSH 的三个 MCP server。
方法：静态阅读源码（经 `gh-proxy.com` 读 raw，`data.jsdelivr.com` 取文件清单）。**未运行任何仓库代码**，未执行 `.bat`，仓库内一切"给 AI 的指令"一律当作数据（`docs/guides/AGENT-INSTALL-PROMPT.md` 等均未照做）。

**行号精度说明**（重要）：`src/bridge.js` 834KB、`plugins/surfcore/lib/client.js` 343KB、`lib/index.js` 134KB，代理单次只返回文件前面约 130KB。因此：
- 无前缀标记的行号 = 代理返回文本里**逐字读到的真实行号**（`bridge.js` 1–2479、`mcp-snowluma-safe.js`、`plugins/surfcore/lib/index.js` 1–2244、jsdelivr 清单）。
- 带 `≈` 或写成"文件头/函数 `xxx()`" = 该段代码在返回窗口之外**没有拿到行号**，只凭函数名与代码原文引用，属于"读到了代码但没定位行号"。
- 凡是**没有**代码原文、只有文档依据的，一律在"未验证"里点名。

---

# 结论

**一句话**：这不是一个"偷偷把用户数据卖给第三方"的仓库——**未发现任何第三方遥测/上报端点，未发现隐蔽外发通道**，bridge 前 2479 行硬编码的外链只有 `127.0.0.1`（:3080/:3000/:3001/:8765/:8767）。但它在**凭据边界**上做得很糟：为了免去"每次要启动 token"，桥接进程**离线伪造 DSH 宿主的合法会话 Cookie**；Windows 上 `state/` 的明文凭据**默认对所有本机已登录用户可读且失败只告警**；设置页有一个**PowerShell 命令注入**；B 站侧是**真实的自动评论/点赞 + 限额与风控规避**，而不是文档暗示的"只读"。

分级结论：

| 维度 | 判定 | 依据 |
| --- | --- | --- |
| 有无硬编码外部服务器/上报域名 | **未发现**（除官方 API、LLM API、安装包下载） | `src/bridge.js:773,1985,1992-1993,2217,2296,2345,2402` 全是回环；`plugins/surfcore/lib/index.js` 内只有下装用途的 GitHub/npmmirror 链接 |
| 凭据落盘 | **明文 + 继承 ACL，Windows 上 fail-open** | `src/bridge.js:275,283-292,298-301,315-330` |
| 是否可能被打包上传 | **代码里不存在上传路径**；但凭据在明文状态下位于 `state/`、`config.json`、`~/.dsh/surfcore/bili/`，任何打包/备份/同步工具都会原样带走 | `plugins/surfcore/lib/paths.js` 路径常量 + `store.js` 只做 UI 脱敏 |
| 本地控制台 :3100 鉴权 | **强于平均水平，但默认凭据对本机全用户可读** | 文档 `SECURITY_BASELINE.md` §4 + `test-console.mjs` + `http-guard.js`；`bridge.js:298-301` 自述 |
| 三个 MCP server 工具面 | **无任意命令/任意文件读写/任意 URL 抓取**（`web_fetch` 受限） | `mcp-snowluma-safe.js:67,368-1344`；`mcp-host-server.js` 仅 1 个只读工具；`mcp-web-search-safe.js` 仅 2 个只读工具 |
| guard 是否 fail-closed | **是，两处都是**（执行期白名单 + 空令牌不通过） | `qq-tool-restrict.mjs`（`ctx.tools.guard` 返回拒绝串）；`http-guard.js safeEqual()` 空串直接 false |
| safe-fetch SSRF 防护 | **真实有效**（DNS 全解析 + 每跳重校验 + 连接 pin），**残余**：不限端口、内容进模型上下文 | `src/safe-fetch.js`（`isPrivateIp`/`validateFetchUrl`/`requestOnce`/`safeFetchBuffer`） |
| B 站自动化账号风险 | **高**：真发评论/点赞/动态，且为规避风控做了工程处理；cookie 明文可接管账号 | `plugins/surfcore/tools/commands/comment.mjs` 全文；`~/.dsh/surfcore/bili/credentials.json` |
| 作弊式"主动意识/聚合数据" | **未发现隐蔽行为**；但确有明示的 QQ→B站 跨平台搬运，默认开关状态不一致 | `config.example.json`（`biliBridge.memory.enabled=true`）vs `store.js DEFAULT_UNIFIED.memory.qqToBili.enabled=false`；`privacy.js` |

---

# 数据流向表

"出网"= 离开本机网卡。所有"出网=是"的行，目的地都是**用户自己配置的那家服务**，没有第四方。

| 数据 | 落盘位置 | 出网 | 目的地 / 说明 |
| --- | --- | --- | --- |
| QQ 群/私聊消息原文 | 内存（bridge）+ `state/*.json`（贴纸库、会话策略） | **是** | ① `POST 127.0.0.1:3080` → DSH `session/prompt`；② DSH 再把整段对话发给 **DeepSeek API**（这是本仓库最大的、也是设计意图内的出网点）|
| 消息的**节选**（活动/审计） | `state/qq-activity.log`、`state/bridge.log`、`state/audit.jsonl` | 否 | 每写一行都过 `redactSensitiveText()`：`src/bridge.js:660-670,696` |
| 出站文本（AI 要说的话） | 不落盘 | 是 | SnowLuma → QQ；发前过 `SENSITIVE_RE` + 已知 token 拦截：`src/bridge.js:1906-1914`（该段是语音文件名版，文本版在同文件 `auditAndSend`，未读到行号）|
| 检索词（黑话/梗求证） | 不落盘（5 分钟内存缓存 64 条） | 是 | 主：`https://api.deepseek.com/anthropic/v1`（与 DSH 同一路，`src/web-search.js` 顶部常量 `DEFAULT_BASE_URL`）；兜底：`cn.bing.com` RSS/HTML，走 safe-fetch |
| QQ 登录态（密码/扫码 ticket） | **不在本仓库** | — | 由 SnowLuma 自己持有；本项目只拿 OneBot 的 HTTP/WS accessToken |
| SnowLuma `accessToken` | `<repo>/config.json`（**明文**，可被 `persistSnowLumaEndpoint()` 回写）、以及 SnowLuma 自己的 `config/onebot_<uin>.json` | 是（仅本机） | `Authorization: Bearer` → `127.0.0.1:3000` / WS `127.0.0.1:3001`；token 全量收集逻辑见 `src/snowluma-conn.js readOneBotTokens()` |
| 控制台令牌 `consoleToken` | `config.json` 或 `state/console-token`（`0o600` + `icacls` 收紧，**默认失败**） | 否 | `src/bridge.js:283-292`；权限问题见 `:298-330` |
| 每会话 agent token | 内存 `KNOWN_AGENT_TOKENS`（上限 512，`src/bridge.js:591-637`） | 否 | `x-agent-token` → `127.0.0.1:3100`；agent 只能读自己会话（见 `bridge.js:1896-1898` 注释口径）|
| **DSH 宿主 browser-session 签名密钥** | 不复制，但被读取：`$DSH_HOME/.credentials.yaml` → `client-connection/browser-session` | 否 | `src/dsh-client.js readBrowserSessionSecret()` + `mintBrowserSessionCookie()`：**离线铸造合法 `dsh-auth-*` Cookie** |
| DSH 进程启动 token | 读 `$DSH_HOME/logs/server-*.log` 里的 `?token=` | 否 | `src/dsh-client.js discoverDshLaunchToken()` |
| B 站 Cookie（`SESSDATA`/`bili_jct`/`buvid3`） | `~/.dsh/surfcore/bili/credentials.json`（鱼号）、`owner-credentials.json`（主人号），**明文 JSON** | 是 | `api.bilibili.com`、`passport.bilibili.com`（`src/bili-api.js loadBiliAccount()`；`plugins/surfcore/lib/bili-qr-login.mjs`）|
| B 站评论 / 点赞 / 动态 / 弹幕 | `~/.dsh/surfcore/bili/state/*.json`（去重、限额账本） | 是 | `POST https://api.bilibili.com/x/v2/reply/add`（带 WBI 签名与 `csrf`）——`plugins/surfcore/tools/commands/comment.mjs` |
| TTS 文本 | 音频缓存 `<state>/tts/*.wav`（按内容寻址，上限 400 文件/150MB） | 是（仅本机） | `POST 127.0.0.1:8765/v1/speak/file`（默认）；`8767` 为 cloud 端点，**也可被用户改成远端**（`src/bridge.js:1985,1992-1993`）|
| 语音文件名 | 不落盘（临时音频） | 是 | 发前审计文件名是否含路径/凭据/token：`src/bridge.js:1906-1914` |
| 记忆文件（跨平台搬运产物） | `~/.dsh/surfcore/memory/{qq-for-bili.md,bili-for-qq.md,memory-card*.md/jsonl}` | 是（间接） | 最终随 prompt 进 DeepSeek；生成前过 `privacy.js` 闸门 |
| 通知/报备 | 不落盘 | 是（**仅当用户配置**） | `config.notify = { type:'none'|'webhook'|'cmd', url, cmd, args }`（`store.js BIDI_DEFAULTS.notify` 默认 `none`）|
| 桌宠安装包 / Electron | `<state>/pet/app`、临时 zip | 是 | `github.com/Mieksido/surfpet/releases/...`、`github.com/electron/electron` 或 npmmirror 镜像（`plugins/surfcore/lib/pet.js`，`config.mirror` 可改任意镜像前缀）|
| 版本检查 / 自更新 | `~/.dsh/surfcore/` | 是 | GitHub（git/raw/releases）；`updates.js`（≈33KB）**未逐行读**，见"未验证" |

---

# 攻击面清单

按"谁能碰到"分层，越往下越需要本机权限。

| # | 面 | 入口 | 谁能碰 | 现状 |
| --- | --- | --- | --- | --- |
| A1 | QQ 群友输入 → 模型 prompt | SnowLuma → bridge → DSH | 任意群友 | 提示注入的**唯一大规模入口**；防线是 preset 提示词 + 工具白名单（无本地工具），不是代码隔离 |
| A2 | MCP 工具参数 | 模型自填 | 被注入的模型 | 46 个 `qq_*` 工具全部走桥接 HTTP；发送类强制 `allow.groups/allow.private`（`mcp-snowluma-safe.js:67,465-540`）|
| A3 | 本机 :3100 控制台 | HTTP | 本机任意进程/用户 | 回环绑定 + `x-console-token` + Host/Origin + `application/json` 强约束；**但 token 默认对本机全用户可读**（见 F1）|
| A4 | 本机 `/dsh-surfcore/api/*` 设置页 | HTTP | 本机浏览器/进程 | `isLoopbackReq` + `sameOrigin`（无 Origin 直接放行）+ DSH `connection.requestRejection`：`plugins/surfcore/lib/index.js:617-627`；**`:1881-1899` 有命令注入** |
| A5 | 本机 `/dsh-surfcore/pet-api/*` | HTTP | 同上 | 共用 `http-guard.js`；另有 `x-pet-token`（未读实现）|
| A6 | SnowLuma `127.0.0.1:3000/3001` | HTTP/WS | 本机 | 只要拿到 accessToken 就等于拿到 QQ 账号全部能力（含踢人/禁言/传文件，OneBot 全量 API）；本项目只用了子集，但**网关本身没有子集限制** |
| A7 | TTS `127.0.0.1:8765/8767` | HTTP | 本机 | 无鉴权（`voice-tts.js` 不带任何 header）；本地服务，风险低 |
| B1 | 子进程：PowerShell | `plugins/surfcore/lib/index.js:1899` | 设置页调用者 | **参数可注入**（见 F3）|
| B2 | 子进程：`netstat` / `sh -c 'ss -ltn...'` | `src/bridge.js:1319,1322` | 定时 | 固定参数，无注入 |
| B3 | 子进程：spawn 桥接、SnowLuma、Electron 桌宠、`wscript` 弹窗 | `plugins/surfcore/lib/bridge.js`、`pet.js`、`index.js` | 设置页用户动作 | 路径来自配置，未做签名校验；桌宠目录有 `checkAppDir()` 守卫（防误删别家目录）|
| C1 | 文件写入面 | `config.json`、`state/*`、`~/.dsh/surfcore/*`、`roles/`、音频/桌宠目录 | 桥接/插件 | 写前备份 + 原子写；`state/` ACL 收紧默认失败 |
| D1 | 供应链 | `npm i`（`@snowluma/sdk`、`@modelcontextprotocol/sdk`、`zod`、`js-yaml`）；GitHub Releases 的 pet 包 | 用户 | pet 包**无签名校验**，解压即用（推断：`pet.js` 只校验 zip 内 `package.json`）|
| E1 | 外部网页内容 | `web_fetch` / Bing 兜底 | 被注入的模型 | 内容**直接进模型上下文** → 间接提示注入面；SSRF 本身拦得住 |

---

# 关键发现

## F1【高】Windows 上 `state/` 凭据默认对本机所有用户可读，且失败只告警（fail-open）

- `src/bridge.js:275`：`fs.writeFileSync(..., { mode: 0o600 })`。
- `src/bridge.js:298-301` 自己在注释里写实测结果：

  > **Windows 上 `mode: 0o600` 是空操作**（实测：chmod 之后 ACL 不变），文件继承父目录 ACL ——
  > 本机 `state/console-token` 实测为 `NT AUTHORITY\Authenticated Users:(M)`，也就是任何能登录这台机器的用户都能读走**控制台令牌、Snowluma accessToken、各会话 agentToken 和全部 QQ 聊天记录**。

- `src/bridge.js:315-330`：`hardenStateDirAcl()` 失败时只 `log('⚠️ 未能收紧 state/ 目录权限...')` 然后继续启动；**没有任何 fail-closed**。
- 影响：多用户 Windows（共用机、远程桌面服务器、装了第三个账号的机器）上，一个普通用户即可拿到控制台令牌 → 进而调用 A4 的全部设置页能力 → 见 F3。

## F2【高】桥接进程离线伪造 DSH 宿主会话 Cookie（绕过启动 token 的"后门式"设计）

`src/dsh-client.js`（函数级引用，行号未定位）：

- `readBrowserSessionSecret()` 读 `$DSH_HOME/.credentials.yaml` 的 `client-connection/browser-session`（32 字节，**长期不变**）。
- `mintBrowserSessionCookie()` 用它做 HMAC，按 `dsh-auth-<base64url(sha256(authority))>` 命名、`issuedAt/expiresAt` 自签，**60 分钟有效**，注释明确写"可以**离线铸造**一个合法 Cookie，完全绕开启动 token"。
- 另有 `discoverDshLaunchToken()`：环境变量 → `$DSH_HOME/logs/server-*.log` → `guard/logs`，正则 `/[?&]token=([A-Za-z0-9_-]+)/` 抓 `dsh web` 打印的启动 token。

影响：
1. 桥接进程 = "能对 DSH 宿主行使**完整会话权限**"的第二个进程。它跑在**不可信输入（QQ 群文本）**的正前方。
2. DSH 侧任何以"启动 token 只在内存、不可推导"为依据的威胁模型，对本项目**不成立**。
3. 注释声称"权限没有提升，桥接本来就用启动 token 换到同等会话权限"——**权限确实没提升，但攻击者拿到的等效凭据多了一份**：只要拿到桥接的文件读能力（或 F1 的 `state/` 读取），就能横向进 DSH。这是"设计便利压过最小权限"的典型例子，不是漏洞，但属于必须写进威胁模型的架构性削弱。
4. 顺带：`expiresAt - issuedAt <= cookieMaxAgeDays` 的上限校验能被"60 分钟"轻松满足，铸造永远成功。

## F3【中高】设置页 `handleBrowse` 的 PowerShell 命令注入

`plugins/surfcore/lib/index.js:1881-1899`（真实行号）：

```js
async function handleBrowse(req, res, url) {
  const mode = url.searchParams.get('mode') === 'file' ? 'file' : 'dir';
  const title = url.searchParams.get('title') || (mode === 'file' ? '选择文件' : '选择目录');
  ...
  const ps = mode === 'file'
    ? `Add-Type -AssemblyName System.Windows.Forms;
$d = New-Object System.Windows.Forms.OpenFileDialog;
$d.Title = ${JSON.stringify(title)};        // ← 1891
...`
  const child = execFile('powershell.exe', ['-STA', '-NoProfile', '-Command', ps], ...);  // ← 1899
```

- `title` 完全来自 query，`JSON.stringify()` 只做 **JSON** 转义（`"` → `\"`），**不是 PowerShell 转义**（PowerShell 用反引号，`\` 是普通字符）。
- 构造 `?title=x"; calc; #` → JSON 里得到 `"x\"; calc; #"` → PowerShell 看到字符串 `"x\"` 结束、`;` 分隔、执行 `calc`，`#` 注释掉残尾。**属于可执行任意命令的注入**（推断：需实机验证 PS 解析细节，但转义方式明确是错的）。
- 门槛：需通过 `:617-618` 的 `isLoopbackReq` + `sameOrigin`（无 Origin 视为通过）+ DSH `requestRejection`。即"本机进程"即可，或"设置页里的任何 XSS"。结合 F1，普通本机用户 → 读 `console-token` → 打 `:3100`，是另一条路（:3100 与设置页是两套路由，token 不通用，需分别评估）。
- 同文件 `:1888-1898` 还把 `title` 用作 `$d.Description`，同样问题。

## F4【高·合规】B 站侧是真实的写操作 + 风控规避，不是"只读"

`plugins/surfcore/tools/commands/comment.mjs`（全文可读，文件头自称 "Post ONE bilibili comment on the user's behalf"）：

- 真发评论：`POST https://api.bilibili.com/x/v2/reply/add`，带 `csrf=cred.bili_jct`、WBI 签名（`wbiSign()`）、`plat:'1'`、Chrome UA + `origin: https://www.bilibili.com`。
- 真点赞：`likeFishComments()`（鱼群保护里顺带给别人的评论点赞，有每日上限）。
- **公开 @ 主人**：`--share` 模式强制正文以 `@<owner.name>` 开头，不带就自动补。
- **为规避平台风控而做的工程处理**（原文注释）：
  - 带链接的评论被判 `state=17`（仅自己可见）→ 自动把 URL 换成 BV 号；
  - "刚发完评论就在同一视频里连点，B站 很容易 **-509**"→ `likeAfterCommentMs` 延迟再点赞；
  - "鱼太多 → 别发新楼层，改成回复一条鱼"（防被识别为刷屏号）；
  - 多本账限额：30 条评论/天、50 条回复/天、15 条搭话/天、同 UP 10 分钟 1 条、同视频 60 分钟 1 条、全局 2 分钟冷却。
- 这些限额本身是"少发点"，但它们的**设计目的**（代码注释与 `fish-guard` 命名）是"看起来像真人、不被当刷屏号"，属于平台规则明确禁止的自动化行为。
- **账号风险**：`bili_jct`（CSRF token）+ `SESSDATA` 明文存于 `~/.dsh/surfcore/bili/credentials.json`，**拿到即等于账号控制权**（发评论/删评论/改资料/私信），无二次验证；文件无加密、无非对称封装，与 F1 的 ACL 问题叠加。
- **合规风险**：B 站《用户协议》禁止自动化脚本模拟操作与刷量；此类账号常见处置是限流→评论仅自己可见（项目已实测遇到 `state=17`）→ 最终封禁。此外 `--share` 会把用户 QQ 身份与 B 站身份**公开关联**。
- 好消息：`owner-credentials.json` 与鱼号 cookie **分文件存放**（`paths.js` 注释明确"绝不用于发评论/动态"），身份串号风险被隔离。

## F5【中】SnowLuma token 的"全账号收集 + 明文回写"

`src/snowluma-conn.js`：

- `readOneBotTokens(homeDir)`：遍历 `<SnowLuma>/config/onebot_*.json`，**把每个账号的 HTTP 与 WS accessToken 全部读出来**（注释解释：换号后 token 会变，所以"拿候选 token 真去问一次 `get_login_info`，谁通就用谁"）。
- `persistSnowLumaEndpoint()`：把命中的 `accessToken` **写回 `<repo>/config.json`（明文，UTF-8，保留 BOM）**。
- 影响：① 一个本项目建设性地持有"机器上所有 QQ 账号的网关 token"集中在一处；② `config.json` 在**仓库目录**（不是 `state/`），`state-acl.mjs` 的收紧不覆盖它 → 这是 F1 的加重项；③ 返回值里含 `token.slice(0,4)+'…'` 这类前缀（UI 展示用），属轻度泄漏。
- `.gitignore` 与 `scripts/privacy-scan.mjs` 的 `denyFiles` 含 `config.json`，**只防"提交到 Git"，不防"本机读取/打包上传"**——两件事。

## F6【中】safe-fetch 的 SSRF 防护：真实有效，残余风险有限

`src/safe-fetch.js`（函数级引用）：

做对的部分（每一项都能对应到代码）：
- 只允许 `http/https`；禁止 URL 内嵌凭据（`validateFetchUrl()`）。
- **DNS 先解析**，对**全部**解析结果调 `isPrivateIp()`；`isPrivateIp()` 覆盖 `0/8`、`10/8`、`127/8`、`169.254/16`、`172.16-31`、`192.168`、`100.64/10`（CGNAT）、`198.18/15`、`224+`（组播/保留）；IPv6 覆盖 `::`、`::1`、`fc00::/7`、`fe80::/10`、`fec0::/10`，并把 IPv4-mapped / IPv4-compatible / **NAT64（`64:ff9b::/96` 与本地 `64:ff9b:1::/48`）** 内嵌的 IPv4 抽出来再判一次——`parseEmbeddedIpv4()` / `ipv4FromLast32()` 写得比多数库都细。
- 非法/带 `%` 的地址一律**当成内网拒绝**（fail-closed）。
- **手动跟随重定向**，每一跳都重新 `validateFetchUrl()`，上限 5 跳（`safeFetch()` 与 `safeFetchBuffer()` 都是）。
- **连接 pin 到已校验的 IP**（`requestOnce(url, addresses, ...)`），同时保留原始 `Host` 与 TLS `servername` → 这才是真正挡住 DNS rebinding 的做法；`rejectUnauthorized: url.protocol === 'https:'` 说明 TLS 校验默认开。
- 响应体按字节流限量 + 总时限（`withTimeout`）。

残余风险：
1. **不限制端口**。校验只看 IP 是否公网，所以 `https://<公网IP>:6379/`、`:9200` 这类"公网上的私有服务"可被访问，且 `safeFetch` 会把响应正文返回给模型 → 可被用来打结构化探测/读公网 Redis、ES、Prometheus。不是内网穿越，但比"只抓网页"宽。
2. **内容进上下文**：返回的是网页正文，**直接进模型上下文**，构成间接提示注入面（MCP 的 `web_fetch` 描述里只说"只读",没提示"内容不可信"）。
3. Bing 兜底与主路径都会把**群里提炼出来的词**发给外部（DeepSeek 或 `cn.bing.com`）。`web-search.js` 头部注释把"不再默认发给 Bing"当成隐私改进，这是对的，但仍应知道：**群聊内容衍生出的检索词会出网**。
4. `redirect: error`（web-search 的 key 请求）是好的；但 `safeFetch` 对于 3xx 循环只靠 5 次上限，没有"跨 origin 重定向到不同站"的告警——不算漏洞。

## F7【中】三个 MCP server 的工具面：没有命令执行/文件面，但有两个"宽度"问题

逐个核对（真实行号）：

- **`src/mcp-snowluma-safe.js`**：46 个工具，从 `qq_status`(:368) 到 `qq_get_forward_msg`(:1344 附近)，全部 `agentApi()`/`onebot()` → 桥接 HTTP。**没有** `onebot 任意 action 透传`、没有 `qq_call_api`、没有禁言/踢人/传文件（文件头注释与代码一致）。发送类走 `isAllowed()`(:67) 校验 `allow/deny/allowAllWhenEmpty`。
  - ⚠️ 宽度问题 1：`qq_collect_sticker`(:1168) 会**把别人发的图收进自己 QQ 收藏**（对账号是写操作）；`qq_send_poke`、`qq_send_voice`、`qq_send_burst` 都是账号级动作。
  - ⚠️ 宽度问题 2：`readConsoleToken()`(:116) 让这个 MCP server **也持有控制台令牌**（为了读桥接的会话状态）。也就是说"给 agent 的工具"这一层本身也能打到 :3100 的**操作者**端点——具体哪些端点接受 agent token、哪些只认 console token，取决于 `bridge.js:1896-1898` 那套 `agent=true/false` 分层（该段代码在返回窗口之外，**未逐行验证**）。
- **`src/mcp-host-server.js`**：只有 `snowluma_status`（只读 `get_login_info`），且**显式删除**了 `start_snowluma`/`stop_snowluma`（文件头 40 行注释解释是 SnowLuma EULA §5.4 的许可问题）。这是全仓最干净的一个 MCP server。
- **`src/mcp-web-search-safe.js`**：只有 `web_search` / `web_fetch`，后者走 `safeFetch`；查询词做 CQ 码/控制字符清洗与 120 字截断（`sanitizeQuery()`）。

**guard 是否 fail-closed：是。**
- 执行期：`plugins/surfcore/qq-tool-restrict.mjs` 的 `ctx.tools.guard((exec) => ...)`：`name` 非字符串或空 → 拒绝；不在 `SAFE_EXACT`、不匹配 `SAFE_PREFIXES` → 返回拒绝串（**默认拒绝**）。
- 注册期：对 67 个已知危险工具名逐个 `ctx.tools.restrict({deny:[name]})`，失败被 try/catch **静默吞掉**（代码自己承认这个失败模式，并留了 `npm run scan:tool-names` 对账）。
- `http-guard.js safeEqual()`：空 token **永不通过**（显式处理了 `timingSafeEqual` 两个空 buffer 返回 true 的坑），长度不等先返回 false，再 `timingSafeEqual` → 定长比较是对的。
- ⚠️ 设计性残余：`SAFE_PREFIXES` 用**前缀匹配** `mcp__snowluma__*` / `mcp__snowluma-host__*` / `mcp__web-search-safe__*`，意味着**只要这三个 MCP server 新增一个工具，执行期白名单就自动放行**——闸门跟着 server 漂移，不跟着人审。

## F8【中】控制台 :3100 鉴权：文档写得很硬，但凭据分发是软肋

我能拿到的**代码级**证据：
- `src/bridge.js:283-292`：未配置时 `crypto.randomBytes(24).toString('hex')`（192 bit）生成并落盘到 `state/console-token`——**不是默认无鉴权**，这点真实。
- `plugins/surfcore/lib/bridge.js consoleToken()`：优先 `config.json` 的 `consoleToken`，否则读 `state/console-token`；每次调用都带 `x-console-token`；写操作强制 `content-type: application/json`（注释写明对应桥接的 415 分支 = CSRF 防护）。
- `scripts/test-console.mjs`：**所有**请求（含 `GET /`）都带 `x-console-token`，并断言"发到非白名单群 → 403"。说明"读也要 token"在实现里成立。
- `plugins/surfcore/lib/http-guard.js`：`isLoopbackReq()` 用 **Host 头**判回环（注释解释了 DNS rebinding 下对端地址不可信、Host 才可信；还修了 IPv6 `[::1]` 分支的死代码）；`sameOrigin()` 对 `Origin` 与 `Host` 做同源比较，**无 Origin 视为通过**（服务非浏览器客户端）。

未能代码级确认（只到文档）：`SECURITY_BASELINE.md` §4 声称 `server.listen(port, '127.0.0.1')`（永不 `0.0.0.0`）、所有路由（读+写）都要 token、`crypto.timingSafeEqual` 定长比较、Host 必须是 `127.0.0.1:<port>`/`localhost:<port>`/`[::1]:<port>`。**这几条在 `bridge.js` 2479 行之后，我读不到**，见"未验证"。但 `http-guard.js`（插件侧）与 `test-console.mjs`（行为断言）与之相互印证，倾向"文档属实"。

真正的软肋不是算法而是**分发**：F1（token 文件本机全用户可读）+ F3（拿到 token 后有命令注入）。

## F9【低-中】隐私闸门与"主动意识"的真相

`plugins/surfcore/lib/privacy.js` 质量高于一般项目：
- 私信硬隔离："QQ 侧只读 `group:*`；B 站侧生成器本身不读 `dm-*`"（文件头硬规则 1；`isPublicGroupMessage()` 里 `!groupIds.includes(groupId)` 拒绝）。
- 确定性正则（不信模型判断）：私钥/`sk-`/`gh[pousr]_`/AKIA/Bearer/Cookie 键值/密码/验证码/QQ 号/微信/TG/身份证/银行卡/手机号/邮箱/IP/住址/实名/精确定位。
- 严重命中 → **整行丢弃**，`assertSafeOutput()` 有一条严重命中就拒绝落盘；`explainText()` 只返回标签不返回原文。
- `SAFE_URL_HOSTS` 白名单只放行 bilibili 域。
- 零宽字符/控制字符剥离（`ZERO_WIDTH`/`CONTROL`）。

盲区（推断 + 代码可见）：
- `bare-long-number` 规则 `/(?<!\d)(\d{6,12})(?!\d)/` 会把任何 6–12 位数字（订单号、时间戳、金额区间）当"疑似账号"——**高误伤**；同时它拦不住 `123-4567-8901` 这种带分隔符的号码。
- 中文姓名、无关键词的地址、未成年人信息**没有覆盖**。
- 闸门作用域是"**生成记忆文件**"；`src/bridge.js` 把**原始群消息**直接塞进 prompt 的路径不经过 `privacy.js`（`privacy.js` 只被插件与记忆生成流程引用）。即：隐私闸门保护的是"跨平台复制"，不是"进 LLM"。

"作弊式主动意识/聚合"判定：**未发现隐蔽聚合或偷偷外发**。
- 存在的跨平台搬运是**明示功能且有开关**：QQ 群公开消息 → `~/.dsh/surfcore/memory/qq-for-bili.md` → B 站侧 prompt。
- 但开关默认值在**两处不一致**：`config.example.json` 的 `biliBridge.memory.enabled: true`（桥接侧默认开）vs `store.js` `DEFAULT_UNIFIED.memory.qqToBili.enabled: false`（合体层默认关）。这是个真实的"默认值漂移"风险——用户看到的开关状态可能与实际生效状态不一致（**推断**：取决于哪一侧最终读配置，未逐行验证 `memory.js`）。
- 没有发现"把群聊内容写到别处"的任何第三方接收点；落盘全在 `~/.dsh/surfcore/` 与 `<repo>/state/`。

## F10【低】其它值得记录的点

- `src/web-search.js`：API key 只发往**校验过的 HTTPS 端点**（必须 https + 解析到公网 + `redirect: error`），且"key 永不写日志/不进错误信息/不进返回结果"；解析顺序 `config.apiKey` → `DEEPSEEK_API_KEY` → `$DSH_HOME/.credentials.yaml` 的 `refs.<name>`。**但** `config.json` 明文存 `webSearch.apiKey` 是可选项，存在即明文。
- 日志脱敏：`redactSensitiveText()`（`src/bridge.js:660-670`）在 `appendActivityLog`（:696）前逐行执行，同时把 `KNOWN_AGENT_TOKENS`(:591-637) 里的每个会话 token 也替换掉。这是真做了，不是注释。
- 出站拦截：`SENSITIVE_RE` + 已知 token 命中就**整条不发/抛错**（`src/bridge.js:1906-1914` 语音文件名版；文本版在 `auditAndSend`，未读到行号）。CQ 码注入用 `[CQ:` → `[CQ：` 转义、`atUserId` 只接受正整数禁 `@all`（文档 §3，代码未逐行验证）。
- `safe-fetch` 的 SSRF 白名单里**没有 QQ 的 CDN 域名**，但 `src/bridge.js` 用 `safeFetchBuffer` 抓 QQ 图片 → 说明这些 CDN 必须解析到公网，正常。
- `plugins/surfcore/lib/pet.js`：`installElectronRuntime()` 里 `powershell Expand-Archive -LiteralPath '${zip}' -DestinationPath '${dest}'` **没有做单引号转义**（同文件 `unzip` 那处做了 `''` 转义）。`zip`/`dest` 目前由 `STATE_DIR` 派生、非攻击者可控 → 仅记为潜在模式缺陷。
- 依赖面很小（`package.json` 4.8KB，`package-lock.json` 760KB）：`@snowluma/sdk`、`@modelcontextprotocol/sdk`、`js-yaml`、`zod`。第三方依赖越少越好，这点加分。

---

# 未验证（明确列出，不要当成"没问题"）

1. **`src/bridge.js` 第 2479 行之后**（834KB 的绝大部分）：控制台 server 实现（`listen` 绑定地址、token 比较、Host/Origin 校验、`requestRejection` 接线）、全部 `/api/*` 端点、审批流、`onebotSend()`、`auditAndSend()`、更新逻辑。**所以"控制台鉴权强度"只有文档 + 测试脚本 + 插件侧客户端的间接证据。**
2. **`src/bili-memory.js`(36KB)、`src/bili-dynamics.js`、`src/bili-share-scheduler.js`、`src/mcp-snowluma-safe.js` 中桥接交互的落盘细节**：没读，`qq_memory_*` 写到哪里、是否脱敏未核实。
3. **`plugins/surfcore/lib/client.js`(343KB)**：插件前端 bundle。只读了开头（无内嵌远端 URL 的迹象），**中段/尾部未读**——理论上仍可能藏远端地址。
4. **`plugins/surfcore/lib/updates.js`(≈33KB)** 与 `tools/` 下其余命令（`daily.mjs`、`danmaku.mjs`、`attack.mjs`、`share-dynamic.mjs`、`quota.mjs`、`notify.mjs`）：未读，**自更新与"报备/通知"的实际外发地址未核实**（`notify` 是唯一用户可配的出网通道，值得单独查）。
5. **`state/` 下具体文件名**（会话策略、`qq-public-feed.jsonl`）：来自 `privacy.js` 注释与推断，未在代码中逐一确认。
6. **全仓 grep 不可用**：本会话 shell 全坏（`ops_bash`/`pwsh` 均为 `0xC0000142`），只能逐文件 web_fetch。**"未发现第三方上报端点"这句话的置信度是"所读文件范围内"，不是"全仓"**。若要作最终结论，需在不依赖 shell 的环境里对全仓跑一次域名/URL 提取。
7. **完整性与时效性**：所有文件经 `gh-proxy.com` 代理读取，**未做 sha256 校验**（jsdelivr 清单里有 hash，我没核对）。理论上代理可篡改内容；本报告对"恶意代码"的结论因此是条件性的。
8. **未审计**：桌宠本体 `Mieksido/surfpet`、SnowLuma 本体、`voxd` TTS 服务、DSH 本体。它们都在信任链上游。
9. **`x-pet-token` 的生成/校验**（`http-guard.js` 注释说由桌宠 API 负责）：实现未读。
10. **`bili.config.json` 的 `listen.dm/live`**：默认 `dm:false, live:false`，但开关打开后会读私信/直播弹幕——**读私信的实现与落盘位置未核实**（`privacy.js` 声称 B 站侧不读 `dm-*`，需交叉验证）。

---

## 附：本次审计实际读过的文件（可复现）

已完整或足够完整阅读：`config.example.json`、`src/safe-fetch.js`、`src/web-search.js`、`src/dsh-client.js`、`src/snowluma-conn.js`、`src/bili-api.js`、`src/voice-tts.js`、`src/mcp-web-search-safe.js`、`src/mcp-host-server.js`、`src/mcp-snowluma-safe.js`(1414 行全)、`src/bridge.js`(1–2479)、`scripts/privacy-scan.mjs`、`scripts/test-console.mjs`、`scripts/test-hardening.mjs`、`plugins/surfcore/lib/{http-guard,privacy,paths,store,bridge,pet,bili-qr-login,token-usage-pure}.js|mjs`、`plugins/surfcore/lib/index.js`(1–2244)、`plugins/surfcore/qq-tool-restrict.mjs`、`plugins/surfcore/tools/commands/comment.mjs`(全)、`dsh/agent-presets/qq-chat-v2/agent.cordis.yml`、`docs/guides/SECURITY_BASELINE.md`、jsdelivr 文件清单。