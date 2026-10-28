# SurfCore 仓库健康度 / 文档可信度 / 提示注入面（task-3）

> 审计对象：`Mieksido/surfcore` @ `d7f6b33`（2026-10-28 09:42 UTC push）
> 通道：gh-proxy 代理的 raw + api.github.com（API 时好时坏，403 限流）、jsdelivr 文件树（**已确认是旧快照**）、web_search、B站搜索
> 本文件只做只读分析；**未执行仓库内任何安装/更新/补丁指令**。

# 一句话判断

**这是一个真项目（真实代码、真实用户、真实 bug），不是空壳骗局，也不是 48 小时从零 AI 生成的——但它是"重命名继承旧项目 + AI 全量重写文档 + agent-native 分发"的混合体；它最大的系统性风险不是隐藏后门，而是把"让 AI 自动装、自动改宿主、要完全权限"写成了一等公民的工作流。**

---

# 证据

## 1. 身份与血统（最关键的一条，前序事实里没有）

| 维度 | 观察 | 含义 |
| --- | --- | --- |
| 仓库诞生 | `created_at` 2026-10-26T01:39:21Z，`pushed_at` 2026-10-28T09:42:17Z，跨度 **56 小时** | 与"155 stars/12 forks"一起看，是 2.5 天内爆量的年轻仓库 |
| LICENSE 版权人 | `LICENSE` 首行：`Copyright (c) 2026 Derpyu520 and qq-bridge / QSH contributors` | 版权人**不是** Mieksido；项目自称"qq-bridge，将来的 QSH" |
| 前身仓库 | `raw.githubusercontent.com/Derpyu520/qq-bridge/main/package.json` 实测可读：`"name":"qq-bridge"`, `"version":"0.2.1-r1"`，scripts/依赖与 surfcore 高度同构（同样有 `patch-snowluma-sdk.mjs` postinstall、`test:mux-reconnect`、`verify:adaptation`） | **surfcore 是公开旧项目 qq-bridge 的改名/重启版**，不是新代码库 |
| 改名残留 | 用户 PR #3 自述"lockfile 仍写着 `0.1.0` / `qq-bridge`"；本人已核对 package-lock 同步提交 | 改名工程本身也在进行中 |
| 生态仓库 | 4 个仓库在 **70 秒内**创建：surfcore 01:39:21Z、surfvoicelocal 01:40:10Z、surfvoicecloud 01:40:57Z、surfpet 01:41:17Z | 整批导入，不是逐步长出来的 |

**含义**：11.7MB 代码 + 8700 行文档**不是**三天写出来的；是既有工程 + 一次 AI 全量文档梳理。判断"AI 批量生成"要分层：**代码有前史，文档与提交节奏是 AI 驱动**。

## 2. 提交历史与节奏

| 维度 | 观察 | 含义 |
| --- | --- | --- |
| commit 数 | `/contributors`：`Mieksido` **56** + 另一账号 `Surfcore`（GitHub user id 8454421）**1** = **57 commits** | 实质单人；"第二作者"很可能是同一台机器的另一个 git identity |
| 时长密度 | 57 commits / 56 小时 ≈ **每 59 分钟一次提交** | 高频小步 |
| tag 数 | `v0.1.1` → `v0.6.22` 共 **41 个 tag**，全是轻量 tag | 每 ~82 分钟一个"发布" |
| 缺 tag | **没有 `v0.1.0`**；`v0.6.6`、`v0.4.3` 缺失（CHANGELOG 有这三节） | 违反自己写的 tag 纪律（`docs/guides/VERSIONS.md` §1.3） |
| message 风格 | `release: v0.6.22（本地语音装完可用）` / `docs: 强制更新提示词 txt` / `test(pet): 补卸载保留 data 的自动测试` | 严格 conventional-prefix + 中文括注，与 `AGENTS.md` 铁律#9"提交信息写现象+修法、别写 update/fix"完全一致 → **有人先写了 agent 规则，agent 再按规则提交** |
| 可用凭据 | 全部 commit `verification.verified=false`（unsigned） | 无签名，正常 |

**判断**：不是"48 小时内由 AI 一次性生成的一坨"，而是**一条 AI 主导、按自订规范高频迭代的流水线**；且时间轴被"改名重启"掩盖了真实开发史。

## 3. Issues / PR —— 本仓库最有价值的一手信号

`open_issues_count: 9` 但其实 **= 5 个 issue + 4 个 PR**：

| # | 类型 | 标题要点 | 评论数 |
| --- | --- | --- | --- |
| 10 | PR(open) | `fix(diag)`: 诊断拿错对象，桥接正常也恒定报 onebot-rejected | 0 |
| 9 | issue(open) | `sendDesktop()` 生成的 PS 脚本转义错误（`\"` 应为 `""`），附 diff | 0 |
| 8 | issue(open) | `listenTick()` 里 `const L` 重复声明 → TDZ `ReferenceError`，评论/@ 监听**从未真正触发**，附 diff | 0 |
| 7 | issue(open) | "鱼的发现"（用户说"我的鱼说的可以发，我也不管是不是真的"），正文是一个附件链接 | 0 |
| 6 | PR(open) | `web_search` 在 DSH Desktop 静默回退 Bing（凭据布局 `records:` vs `refs:`） | 0 |
| 5 | PR(closed) | 同主题前一版，作者自己关了 | 1（作者自评） |
| 4 | PR(open) | 优先用本地补丁启动器启动 SnowLuma MCP | 0 |
| 3 | PR(open) | 修 DSH Desktop 的 node.exe 解析路径 | 0 |
| 2 | issue(open) | 桥接连上 DSH 事件流但收 0 帧；附完整路由探测表（`/api/events` 200、`/api/remote.mux` 404） | 0 |
| 1 | issue(open) | 关闭"温柔模式"后仍注入"绝不回嘴"；附 VM harness 复现 + 6 处 `文件:行号` | 1（**非维护者**） |

关键观察：
- **维护者对 10 个条目 0 回复、0 合入**（观察窗口 ~24h，尚不能定性"不管事"）；唯一一条评论来自报告者自己，内容是：
  > "非常抱歉刚刚反复开关触发了通知，我原本只是让 AI Agent 在本地帮我查一下问题，**没想到它直接调用工具发了 Issue**，我当时吓了一跳赶紧让它关了。"
  → **这个仓库的用户群本身就是 agent**，issue 是 agent 自动生成的。这解释了为什么 10 个条目的模板、语气、"## Environment / ## Root cause / ## Fix / ## Verification" 结构完全一致。
- 报告质量**很高**（#8 是真 TDZ bug 且给出重命名修法；#9 的 PowerShell 转义分析正确）。**这是真 bug、真用户，不是刷单**。
- #8、#9 引用的 `plugins/surfcore/tools/commands/notify.mjs` 在 jsdelivr 树里**不存在** → 再次证明 jsdelivr 是旧快照。

## 4. License

| 观察 | 含义 |
| --- | --- |
| `LICENSE` 12934 B：**MIT 正文 + 约 11KB 附加的"第三方组件与合规边界"长附录** | GitHub 的 licensee 只认前半段、见到附加条款就判 `other / NOASSERTION`。**NOASSERTION ≠ 有陷阱**，是"MIT + 自定义附录"的正常结果 |
| README 写"许可 MIT"，仓库徽标显示 Other | 表述简化导致的显示不一致，非实质风险 |
| 附录质量意外地高：逐条对照 SnowLuma EULA §2/§4/§5.4，自己承认 `@snowluma/sdk`/`@snowluma/mcp` **声明了 `SEE LICENSE IN LICENSE` 但包里没有 LICENSE**，并据实承认 `npm install` 的 postinstall 会**修改 node_modules 里 `@snowluma/*` 的 dist**（"所以'不修改 SnowLuma 源码'这种说法是不准确的"） | **这是"诚实"信号**：主动暴露对自己不利的事实。同类项目极少这么写 |
| 同时列了"数据出机清单"（LLM/OneBot/web_search/Bing/web_fetch 五个目的地） | 透明度高于同类 |

## 5. 文档规模声称 vs 文件实体

| README 声称 | 核对结果 | 结论 |
| --- | --- | --- |
| 12 篇 / 约 8700 行 | README 自己那张表的行数加总 = **8737**；且 12 个文件实际字节数 ÷ 声称行数 = 63~134 B/行（中文 markdown 合理区间） | **内部自洽、量级可信** |
| 3150 处 `文件:行号` | 无法逐条核对（需全量 grep 800KB 文档）；但 issue #1/#8 的写作风格与之一致 | 未证伪 |
| 75 个测试脚本 | jsdelivr `/scripts/` 下 `test-*.mjs` = **97 个**（另还有 `plugins/surfcore/scripts/test-*.mjs`）；`test:audit` 子集另说 | 数量声明**偏保守**，不是夸大 |
| 39 个 `qq_*` 工具 | README 与 CHANGELOG 0.1.0 一致；工具定义在 `src/mcp-*`/`plugins/.../tools/`（jsdelivr 旧树看不到） | 无法完全核对，未见矛盾 |
| "全仓库逐行梳理 2026-10-26" | 与 `MODULE-CORE-B.md` 118KB / `HANDOFF.md` 200KB / `PITFALLS.md` 187KB 的体量匹配 | 可信 |

**文档本身不是吹的，问题在"版本叙事"和"门禁数字"（见下节）。**

## 6. 生态

| 仓库 | stars | size | 创建时间 | 语言 | license |
| --- | --- | --- | --- | --- | --- |
| `Mieksido/surfcore` | **155** | 11710 KB | 2026-10-26T01:39:21Z | JavaScript | other/NOASSERTION |
| `Mieksido/surfvoicelocal` | **1** | 269 KB | 01:40:10Z | Python | MIT |
| `Mieksido/surfvoicecloud` | **0** | 175 KB | 01:40:57Z | Python | MIT |
| `Mieksido/surfpet` | **1** | 480 KB | 01:41:17Z | TypeScript | other/NOASSERTION |

- 配套仓库**都真实存在**，但除本体外**几乎没有社区**（0/1 star，各 1 个 open issue）。
- `surfcore` 本体 **155 stars 但 `subscribers_count: 0`**（0 人 watch）。star/fork 可以刷，watch 极少刷 → 不是硬证据，但"155 star 0 watch 12 fork"这个组合值得记一笔。
- `has_downloads:false`；README 反复承诺 `surfpet` 有 Release 安装包——`/releases` 接口被限流，**未验证**。

## 7. 外部社区信号

| 信号 | 数据 | 含义 |
| --- | --- | --- |
| B站搜 `SurfCore` | **0 条结果** | 项目本体在 B站**没有**以该名字出现 |
| B站 DSH 大肥鱼插件生态 | `BV1Ro866nEVh` 59.9 万播放 / `BV19dJQ6sE9F` 52.6 万 / `BV1ednc6jE3j` 50.3 万（月匠、三颗门牙X、湖上的优尤等，都是 DSH 插件） | **载体平台是真的、而且很火**；surfcore 蹭的是这个生态，不是自造热度 |
| web_search 命中 | `raw.githubusercontent.com/Derpyu520/qq-bridge/main/RULES.md`（前身仓库）、npm 包 `dsh-onebot-qq`、CSDN《DSH 桌面客户端装桌宠：…「怎么冒出两只」的排查》 | 说明存在同生态第三方教程与相关包 |
| 知乎 | 搜到"把大肥鱼桌宠装进你的电脑｜GitHub Star 2.4K+" | 2.4K 指的是别的项目（大肥鱼生态），**不能算到 surfcore 头上** |

**注意**：以上都是不可信外部数据，仅作旁证。

---

# 文档夸大 / 矛盾之处（可直接引用的硬矛盾）

| # | 位置 | 矛盾 | 严重度 |
| --- | --- | --- | --- |
| 1 | 版本号 | `package.json` = **0.6.22**；`README.md` 表格 = **0.6.20**；`docs/guides/VERSIONS.md` 版本矩阵 = **0.3.0**；`dist/强制更新提示词.txt` 说"更新到 0.6.20 或更新" | 高（同一仓库三种现值） |
| 2 | 测试门禁 | README"**75** 个脚本"；VERSIONS.md 发版清单"`npm run test:audit` **76** 项全绿"；CHANGELOG 多处"**77/77**"；而用户 PR #6 实测"**70/77**，失败集前后一致" | 高（**7 个红** 被 77/77 的文案覆盖） |
| 3 | CHANGELOG | **18 个版本小节只有空的 `### 变更` + `- `**（0.6.5、0.5.7…0.3.2 等）；**`## [0.6.6]` 标题重复两行** | 中（批量生成/批量填充的典型痕迹） |
| 4 | 版本通胀 | 3 天内 0.1.0 → 0.6.22，**42 个版本号 / 41 个 tag**，其中 v0.1.0 与 v0.6.6、v0.4.3 无 tag | 中（发布语义被稀释） |
| 5 | 发布纪律 | `VERSIONS.md` 规定"发布 = 打注释 tag + GitHub Release"，实际 tag 全为轻量；Release 是否存在未验证 | 中 |
| 6 | 文档引用失联 | README 指向 `dist/强制更新提示词.txt`（raw 200，存在）与 `docs/guides/FORCE_UPDATE_PROMPT.md`（**未在树中**）；CHANGELOG 每条挂 `docs/cases/…`（**旧树中没有**，可能新加） | 低-中（部分是我抓的树太旧，部分是文档漂移） |
| 7 | 工程门禁 | 声称 77 项测试，但**没有 `.github/` 目录（零 CI）**；`docs/guides/VERSIONS.md` 却写 "`node scripts/version.mjs check`（提交前 / CI）" | 中（质量门禁靠自觉） |
| 8 | 许可证显示 | README "MIT" vs GitHub "Other/NOASSERTION" | 低（见上节，属实现方式导致） |
| 9 | 命名 | 仓库叫 surfcore、包名曾叫 qq-bridge、版权人 Derpyu520、文档里同时出现 "qq-bridge / QSH / SurfCore" 三套名字 | 低-中（血统不透明） |

**另有一条自我暴露的严重事实（不算夸大，但必须记）**：CHANGELOG 0.6.12 自述"更新会直接把 DSH 宿主搞崩（0xFFFF7003）"、0.6.19"更新助手从未执行"。**这个项目有过把宿主 DSH 搞崩的历史**——这直接决定了下一条的判断权重。

---

# 提示注入面分析

## A. 面清点：仓库里"写给 AI 读"的文件

| 文件 | 大小 | 谁读它 | 关键内容 |
| --- | --- | --- | --- |
| `AGENTS.md` | 4139 B | **DSH 会自动注入本工作区的每个会话**（文件自己这么写） | 十条铁律；环境事实（DSH 装在哪、`~/.dsh`、桥接控制台 3100 带令牌）；**"本机有 ACL 类沙箱报错时：需要 `danger-full-access` 会话，别硬试"**；2026-10-22 事故 + **给 `app.asar` 打了字节级护栏补丁**，且"**DSH 一升级就会覆盖 app.asar … 升级后第一件事 `npm run guard:check`**" |
| `docs/guides/AGENT-INSTALL-PROMPT.md` | 2860 B | 用户复制粘贴给 agent | 明确写"**帮我在这台 Windows 机器上安装 SurfCore**"；`git clone` → `npm install` → `node scripts/setup-dsh.mjs` → 读 `~/.dsh/profiles/<p>/package.json` 验证；末段附**一行版**：`powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/.../bootstrap.ps1 | iex"` |
| `dist/强制更新提示词.txt` | ~4.6 KB（raw 200） | 同上 | 让 agent **下载 zip → 写 `apply.cmd`（含 `robocopy /MIR`）→ `start "" /b cmd /c` 起脱离进程 → 等你退出 DSH 后覆盖安装目录 → 再拉起 DSH**；并明确要求"**受限模式下会拒绝执行，请临时切到完全权限**" |
| `scripts/bootstrap.ps1` | ~6 KB | 上面那条一行命令 | 我通读了：只做 环境检查 → git clone/下 zip → `npm install` → `node scripts/setup-dsh.mjs <profile>` → 复制 `config.example.json`。**未见外发、未见凭据读取**，但它是**未固定版本的 `main` 分支远程执行 + 跳过 ExecutionPolicy** |
| `scripts/patch-dsh-session-guard.mjs` | ~7 KB | **被 AGENTS.md 要求"每次 DSH 升级后"运行** | 真身：打开 DSH 宿主 `resources/app.asar`，解析 asar 头，定位 `@deepseek-ai/dsh-session-persistence-jsonl/lib/index.js`，**把 `throw new Error(...)` 等长替换成 `throw new SessionPersistenceCorruptionError(...)`**，支持 `--apply`（改名原子替换）与 `--apply-in-place`（对运行中的 Electron 做同长度原地写）。`ASAR_DEFAULT = 'D:\deepseek\resources\app.asar'` |
| `README.md` §"让 DSH 自己装" | — | 所有人 | 把"**把这句话发给 DSH：帮我安装 https://github.com/Mieksido/surfcore**"写成推荐安装路径；后面"一键安装（全部）"会自动**发现 SnowLuma、写白名单、起桥接** |

## B. 这构成什么样的风险（按可执行性排序）

1. **最高：`强制更新提示词.txt` 是一条"教 agent 关掉护栏 + 覆盖代码目录"的现成模板。**
   - 它同时给出：(a) 下载源（codeload / gh-proxy / ghproxy，**三者都没有 sha256 校验**）；(b) 覆盖目标（插件安装目录，正是宿主 `require` 的目录）；(c) 执行方式（脱离父进程的 `start /b cmd /c`，agent 之后**看不到结果**，只能读日志）；(d) **权限升级话术**（"请临时切到完全权限"）。
   - 只要用户照做，**任何能改这三条镜像之一的人（或一次 HTTP 劫持），就能拿到"用户亲手让 AI 全权限覆盖安装目录"的执行上下文**。这不需要仓库里有后门——**分发链本身就是攻击面**。
   - 更值得警惕的是它**建立了一个习惯**："把一段包含 `robocopy /MIR` 和权限要求的话粘给 AI"是官方支持的操作。这条习惯一旦建立，下一次粘的那段就没人审了。

2. **高：`AGENTS.md` 是"自动注入"通道，而且里面有两句会主动把 agent 往全权限推。**
   - "本机有 ACL 类沙箱报错时：需要 `danger-full-access` 会话，别硬试" —— 这句话对一个"正在被沙箱拦住"的 agent 来说，等价于**授权它去要更宽的权限**而不是停下问人。
   - "DSH 一升级就会覆盖 `app.asar`，补丁消失 → 升级后第一件事 `npm run guard:check`" —— 把"**每次宿主升级后重新打宿主二进制补丁**"固化成例行动作。动机是修一个真实 DSH 崩溃（坏 session 文件让 workspace 起不来），实现也做了等长校验 + 备份，**不是恶意**；但性质是**第三方项目在改宿主应用的内核代码**，而且要求 agent 自动重复这件事。任何"下次升级后顺手再补一下"的自动化，都是持久化的宿主篡改面。
   - `AGENTS.md` 自己写明"DSH 会把本文件自动注入本工作区的每个会话" → **用户只要把 repo clone 进工作区，这些指令就进了上下文**，无需用户同意。

3. **高：`setup-dsh.mjs` 的方向是"改宿主的 agent 能力面"。**
   按 README 自述，它做三件事：把 `qq-chat`/`qq-chat-v2` 两条 **agent preset** 插进 DSH profile、挂 **3 个 MCP server**、注册 bundle。也就是说：**安装这个"QQ 机器人"的副作用，是往你未来每一个会话的工具面/预设里注入东西**。`AGENT-INSTALL-PROMPT.md` 里还教 agent 去读 `~/.dsh/profiles/<p>/package.json` 自检——一旦 agent 学会这条路径，它就知道"要给自己加能力，改这里"。

4. **中：`bootstrap.ps1` 的一行版是标准 `irm | iex`。**
   内容我通读过，**没有外发/凭据回传**，比很多同类干净；但 `main` 分支可变、无 hash、`ExecutionPolicy Bypass` 三件套齐全，**读者无法在"执行前"审到内容**。而且它会自动跑 `setup-dsh.mjs`——即上面第 3 条的后果在"一行命令"里就被继承了。

5. **中：`AGENT-INSTALL-PROMPT.md` 相对克制，但仍在训练 agent 越界。**
   优点是**有明确停点**（"第 5 步停在这里，剩下必须我自己做"、"不要替我装 SnowLuma、不要代扫码"、"不要改已有 config 字段"、"不要 push"），这在同类"AI 自安装"文档里算最守规矩的一档。但它仍把 `npm install`（**带 postinstall，会改 node_modules 里 `@snowluma/*` 的 dist**）、`setup-dsh.mjs`（改宿主 profile）、读 `~/.dsh/profiles/**/package.json` 授权给了 agent。

## C. 结论：是"植入注入面"，还是"agent-native 分发"？

- **没有发现隐藏的恶意载荷**：bootstrap.ps1、patch-dsh-session-guard.mjs 我都逐行读过，行为与注释一致；LICENSE 附录主动自曝对 `@snowluma/*` 的修改；issue 里用户 agent 自发提交 bug——**这些都不像骗局**。
- **但注入面的结构是真实存在的**，而且比一般仓库宽：
  1. `AGENTS.md` **依赖宿主自动注入**，属于"零点击"进入 agent 上下文；
  2. 仓库里**存在两类"提示词即安装程序"的文本**（安装 / 强制更新），其中后者**直接要求关闭沙箱**；
  3. 安装的终态是**修改宿主 profile 的 agent 能力面**，并可再**字节级修补宿主 `app.asar`**；
  4. 更新链**无完整性校验 + 覆盖代码目录 + 脱离进程执行**。
- 正确的定性：**不是"藏了后门的仓库"，而是"默认假设 agent 拥有完全本机权限"的仓库**。这个假设在单机自用场景成立，在"用户把链接/文件丢给 agent"的场景就是**把最终确认权从人手里拿走了**。对一个"给 AI 用的插件"来说，这既是它的卖点，也正是它应该被降级评估的地方。
- 我的建议（给 lead 汇总用）：可读、可审、可复现 → **可以分析，但不要照做**；尤其不要执行 `dist/强制更新提示词.txt` 的任何步骤、不要为它切 `danger-full-access`、不要在含 `AGENTS.md` 的工作区里让 agent"顺手把 guard 补一下"。

---

# 仍无法确认的

1. **GitHub Releases / tag 是否真发过**：`/releases` 被 403 限流，`has_downloads:false` 不足以判断。`surfpet` 的 `pet-pack.zip` 是否存在也未验证（README 多处依赖它）。
2. **`dist/installer/1-安装说明.txt`（2134 B）内容**：raw 返回 `application/octet-stream` 被 web_fetch 拒，jsdelivr 上 404。未能读到。
3. **HEAD 的完整文件树**：jsdelivr 树确认是旧快照（缺 `dist/强制更新提示词.txt`、`plugins/surfcore/tools/`、`docs/cases/`）。因此本报告中来自该树的**文件数/大小是某一较早提交的值**；`git/trees?recursive=1` 因限流未取。
4. **`setup-dsh.mjs` / `installer.mjs` 的实际写入内容**：我只按 README 描述判定"它改 profile 的 preset/MCP/bundle"；未逐行核验它到底写了哪些白名单/路径（属于 install-audit 的活，建议交叉引用）。
5. **维护者从未回复**：10 个条目 0 条维护者评论、0 次合入。窗口只有 ~24 小时，**不能定性为"不维护"**，但"155 stars 的项目 24 小时里对 5 个高质量 bug 报告零回应"是个客观事实。
6. **155 stars 的来源**：`subscribers_count: 0` + 12 forks + 3 天历史值得记一笔，但我没有任何证据证明是刷的；B站/知乎没有任何以 "SurfCore" 为名的内容，热度来源仍是谜（可能是 qq-bridge 时期的老用户迁移）。
7. **`Mieksido` 与 `Derpyu520`、以及贡献 1 个 commit 的 `Surfcore`(id 8454421) 三个账号的关系**：没有证据，只能说 LICENSE 版权写的是 Derpyu520、仓库属主是 Mieksido、还有一个 2014 年的老账号 Surfcore 提交过一次。
8. **`3150 处 文件:行号`**：未做全量 grep，未证伪也未证实。