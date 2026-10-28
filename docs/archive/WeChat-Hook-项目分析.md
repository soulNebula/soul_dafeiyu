# aixed/WeChat-Hook 项目分析报告

> 分析对象：<https://github.com/aixed/WeChat-Hook>
> 快照：`main` 分支 commit `a9d3f27e4afbe4af85ba72a0a9d93ef8f16e594f`，数据抓取时间 2026-10-22
> 说明：本机网络无法直连 GitHub（DNS 被劫持），源码经 `ghproxy.net` 代理逐文件抓取后阅读；下文所有结论均标注了证据来源文件。

**目录**：0 结论先行 · 1 项目概况 · 2 技术架构 · 3 接口面 · 4 工程质量 · 5 安全分析 · 6 同类对比 · 7 可持续性 · 8 结论与建议 · 附 数据来源

---

## 0. 结论先行（TL;DR）

| 维度 | 判断 |
| --- | --- |
| **是什么** | PC 微信 **4.1.10.27**（x64）的**内存 Hook 框架**：用一个伪装成 `version.dll` 的 DLL 注入微信主进程，在进程内起 HTTP 服务（默认 `0.0.0.0:30001`），对外提供发消息、转发 XML、解码图片、读个人资料、**执行任意 SQL 查微信本地数据库**等接口。 |
| **技术含量** | 中上。加载方式（DLL 代理劫持）、PEB 隐藏、RVA 硬编码 + 内存 patch、进程内复用微信的 SQLCipher 句柄，都是成熟的逆向工程手法；但工程实现粗糙（易语言直译、无错误处理、无测试）。 |
| **开源属性** | **不是真正的开源项目**：仓库根目录**没有 LICENSE 文件**，法律上默认"保留所有权利"；README 明说 main 分支"已移除 VMP 保护、远程授权校验、PB/NetSceneSendPB、CDN、WcProbe/CCD/NtQuery 相关 Hook"，完整功能（OCR、视频号、公众号、好友管理、WebSocket 远程）只以 showdoc 文档 + 私域群的形式存在（**是否收费未能验证**，但"远程授权校验"被移除、`GetDeviceUUID` 机器指纹代码残留，说明原版是按机器授权分发的商业软件）—— 典型的 **open-core（开源引流 + 商业版收费）**。更关键的是：**main 分支是"半成品"**，收消息链路缺失、登录态未赋值，连"开箱可用"都做不到。 |
| **最大安全问题** | HTTP 服务**无任何鉴权**（无 token/IP 白名单/CORS 限制）却绑定 `0.0.0.0`。同一局域网内**任何人都能**：查全部聊天记录与联系人、冒充账号发消息、按任意路径读写文件。这是比"封号"严重一个量级的问题。 |
| **适合谁** | 逆向工程 / Windows 系统编程**学习样本**（在合法前提下读代码）；个人小号自用自动化（隔离环境）。 |
| **不适合谁** | 生产业务、对客户交付、多账号运营 —— 授权缺失、封号风险、微信一升级就失效、合规风险高。 |

---

## 1. 项目概况

| 项 | 值 |
| --- | --- |
| 仓库 | aixed/WeChat-Hook |
| Stars / Forks / Watchers | **2156 / 594 / 33** |
| 创建时间 | 2022-04-25 |
| 最近 push | **2026-10-22**（即分析当天，仍在活跃维护） |
| 默认分支 | `main` |
| 语言 | C++（MSVC / MSBuild，x64） |
| 目标客户端 | PC 微信 **4.1.10.27**（历史支持 3.9.10.16） |
| 产物 | `version.dll`（替换微信安装目录下的同名系统 DLL） |
| 许可证 | **无 LICENSE 文件** |
| 贡献者 | **仅 1 人**（`aixed`，586 次提交）—— 单人项目 |
| Release | 5 个（2022×2、2023×1、2026×2），最新 `v411027_`（2026-06-29） |
| 仓库体积 | 约 **177 MB**（不含 Release 里另挂的 239 MB 微信安装包） |
| Issue / PR | 35 条 Issue（未关闭 24 条）/ 4 条 PR（约 2/3 长期挂起） |
| 配套项目 | <https://github.com/aixed/wechat_control>（微信进程启动/终止控制器，`0.0.0.0:29999`） |

### 1.1 版本与发布记录（说明这个仓库的"前世今生"）

| 时间 | Tag | 适配微信 | 内容 |
| --- | --- | --- | --- |
| 2022-04-25 | `微信Hook` | 3.6.0.18 | **易语言时代**：`version.dll` + 30 多个独立 EXE 示例（`SendTextMsg.exe`、`ForwardMsg.exe`、`GetDBHandle.exe`、`SendAtMsg.exe`…），HTTP 通信 30001/回调 30000 |
| 2022-12-15 | `v3.8.0.41` | 3.8.0.41 | `version.dll` + `HPSocket4C.dll` + `recmsg_demo.exe` |
| 2023-07-26 | `v3.9.2.23` | 3.9.2.23 | `IntelEnc.rar`（加密封装） |
| 2026-06-18 | `v4.1.10.27` | 4.1.10.27 | **重写为 C++（version.dll 代理）**，`version.dll` 483,840 字节，给出 SHA256；**附带 239 MB 的 `WeChatWin_4.1.10.27.exe`**（对应版本微信安装包，下载 2330 次） |
| 2026-06-24 | `v411027_` | 4.1.10.27 | `version.dll` 606 KB（下载 2012 次）+ Postman 集合 |

两个关键读法：
- **2023-07 → 2026-06 有近 3 年没有发布**，但 `update.log` 显示 2026 年每月都在改代码 —— 说明 GitHub 主要起**展示/引流**作用，真正交付与迭代在私域（群、showdoc、商业版）。
- 2022 年的 Release 是**一堆易语言编译的独立 EXE**，这解释了为什么今天的 C++ 源码里还有大量 `字节集_字节到十六进制`、`时间_取现行时间戳` 这类中文函数名：**它是易语言工程的 C++ 重写版**。

**仓库里有几处值得注意的"反常规"内容**（来自 `main` 分支文件清单）：

- `运行库/VC_redist.x64.exe`（**18.8 MB**）、`VC_redist.x86.exe`（7.0 MB）、`vcredist_x86.exe`（6.5 MB）—— 把微软运行库二进制直接提交进仓库；
- `防止微信自动更新/禁止微信更新2.0.exe`（25 KB，**闭源二进制**）—— 用于阻止微信升级，从而"锁死"版本、维持 Hook 可用；
- `xdb/xwechat_offsets.h` 内容只有一行 `#pragma once` —— 关键偏移头文件是**空壳**，说明核心适配数据被剥离。

这三条合起来指向同一个事实：**仓库是"可读的壳"，真正的商业能力（以及维持其可用的关键数据）不在里面。**

---

## 2. 技术架构

### 2.1 加载链路（`dllmain.cpp` → `src/inline_weixin_dll_load.cpp`）

```
WeChat.exe（4.x 客户端进程名为 Weixin.exe）
  └─ 静态导入 version.dll（GetFileVersionInfo* / VerQueryValue* 等 17 个导出）
       └─ 宿主的 version.dll 被替换成项目产物 → DllMain(DLL_PROCESS_ATTACH)
            ├─ InitRealDll()            先 LoadLibrary 真正的系统 version.dll（转发导出，避免微信自身报错）
            ├─ IsMainWeixinProcess()    命令行含 --type= 或 --crashpad-handler → 判定为子进程
            │     └─ 子进程：起一个线程 FreeLibraryAndExitThread 自我卸载，直接退出
            ├─ HideModuleFromPEB(hModule)   把自己从 PEB 的 3 条模块链表里摘掉
            └─ CustomInit(hModule)      主进程才继续：GetModuleHandle("weixin.exe") → MH_Initialize → Evt_WeixinLoad
                 └─ Evt_WeixinLoad()    取 Weixin.dll 基址（**取不到就直接 return，没有等待/重试**）
                      ├─ Patch_Revoke()           2 字节热补丁实现"防撤回"
                      ├─ HttpServer::Start("0.0.0.0", g_StartPort)
                      └─ GetWxRecvUrl()           读 wx.ini（回调配置，见 §2.3）
```

注意：**以上初始化全程同步跑在 `DllMain` 里**，会持着 loader lock 起线程、起 HTTP 服务、改内存 —— 这是明确的风险写法（加载器锁期间做重活容易死锁）。

**为什么选 `version.dll`**：Windows 加载器在"应用目录 → 系统目录"的顺序里先找应用目录，微信安装目录下放一个同名 DLL 就会被优先加载；而 `version.dll` 是几乎所有 EXE 都会静态导入的"低风险"系统 DLL，替换它的兼容性成本最低、也最容易被当作无害文件。`version.def` 把 **17 个导出**全部转发到 `My_*`（`GetFileVersionInfoW = My_GetFileVersionInfoW` 等），保证宿主程序调用不崩 —— 这是教科书式的 **DLL 搜索顺序劫持（T1574.001）**。

### 2.2 核心机制

| 机制 | 实现 | 证据 |
| --- | --- | --- |
| **偏移定位** | 消息/图片/XML 层**硬编码 RVA**（`基址 + 常量`）；**数据库层**用特征码扫描 `ScanPattern` 找 `sqlite3_vfs` 再枚举库 | `include/global.h`：`offset::send_message = 0x1677A30`、`XWECHAT_MAIN_CLAZZ_OFFSET = 0xA83AB20`、`Offsets::FORWARD_XML_CALL = 0x1CF3D20`；`xdb/db_mgr.cpp:ScanPattern` |
| **功能篡改** | `VirtualProtect` 改内存保护后直接写机器码 | `inline_weixin_dll_load.cpp:Patch_Revoke()`（写 `0x90 0xE9`）、`Patch_Low_Version()`（改版本字符串字节） |
| **模块隐藏** | `NtQueryInformationProcess` 取 PEB，遍历 `InLoadOrder / InMemoryOrder / InInitializationOrder` 三条链表并摘除 | `src/hideself.cpp:HideModuleFromPEB()` |
| **Hook 引擎** | **自研 inline hook**（不是 MinHook）：偷 5~16 字节 → 在 RWX 内存写桩（`pushfq` + 保存 16 个寄存器 → `rcx = &CALL_CONTEXT` → `call handler` → 回放原字节 → `jmp` 回原函数）→ `AllocNear` 在 ±2GB 内写 `E9 rel32`；MinHook 只调了 `MH_Initialize`，`Hook_SysApi` 封装无调用点 | `src/Hook_Method.cpp:Hook_Call / Hook_Inline`、`include/Hook_Method.h:CALL_CONTEXT`、`src/HookManager.cpp` |
| **HTTP 服务** | **cpp-httplib**，8 线程池，`listen(host, port)`，`0.0.0.0:30001` | `src/http_server.cpp:Run()`、`inline_weixin_dll_load.cpp:Evt_WeixinLoad()` |
| **数据库** | 不自己打开 db 文件：先扫内存找到微信进程内的 `sqlite3_vfs`，再用 `PRAGMA database_list` 建立「库名 → 已解密句柄」映射，SQL **原样 `sqlite3_prepare` 直拼执行** —— 结果是**明文**，完全绕过 SQLCipher 加密；同时对 `sqlite3_backup_init+0xAD` 处 14 字节做 `WriteProcessMemory` NOP 补丁（持久化修改微信自身代码，让备份/加密路径失效） | `xdb/db_mgr.cpp`、`global.h` 的 `XWECHAT_SQLITE3_VFS_OFFSET / SQLCIPHER_API_ROUTINES_OFFSET` |
| **图片解码** | `lz4` + 微信 `dec_pic_call` 偏移，把 `.dat` 缓存图还原成 jpg | `3rdparty/lz4`、`global.h:dec_pic_call` |

### 2.3 收发消息模型：**只发不收**（关键事实）

- **发**：HTTP 请求 → 构造微信内部消息对象（文本走 `txt_message_ctr` + `txt_message_vtbl`，图片走 `img_msg_vtbl`）→ 调用 `send_message` 偏移处的内部函数。本质是"在微信进程里替用户点发送"。
- **收：开源版没有实现**。审计结果：
  - 没有安装任何"收消息"Hook（Release 构建里唯一的运行时修改是 `Patch_Revoke` 的两字节补丁；`Hook_Call(0x108678, …)` 只在 `_DEBUG` 下编译）；
  - `CallBackURL=` / `RecvType=` 只被 `ParseCmdLine` 解析存进 `g_CallBack_Url` 就结束了，`src/http_post.cpp` 里的 WinHttp POST 实现**没有任何调用点**；
  - 连登录态全局量 `g_IsLogin` 也从未被赋值 —— 而数据库层用它做前置判断，导致 `/QueryDB/*` 常年返回空/失败。
- 结论：**README 的"微信收发消息"只做到了"发"，且"发"还是假成功**；"收"链路（回调 POST、登录 Hook、`WcProbe`）在 main 分支是残缺的，推测被裁剪进了商业版。再叠加 `xdb/xwechat_offsets.h` 是空文件，**这个开源源码更接近"可读的框架/教学版"，而不是开箱能用的产品**。
- 外部佐证：仓库 **Issue #132 反馈"源码编译后没有 CallBackURL"**；**PR #130（修 QueryDB 登录态）从 2026-08-25 挂到今天未合并** —— 说明"半成品"不是抓取误差，而是公开版的长期状态。

---

## 3. 对外接口面（8 个）

| 方法 | 路径 | 请求字段 | 实际行为 | 问题 |
| --- | --- | --- | --- | --- |
| POST | `/SendTextMsg` | `wxidorgid`, `msg` | 调微信内部发消息函数 | **无条件返回 `ret:0 success`**，不校验发送结果（`SendTextMsg.cpp`） |
| POST | `/SendImgMsg` | `wxidorgid`, `path` | 读本地文件并按图片消息发送 | 路径不校验 |
| POST | `/ForwardXMLMsg` | `to_wxid`, `content` | 从 XML 抽属性，进程内伪造消息对象后投递 | 只支持 `<img>/<videomsg>/<emoji>`，见 §3.1 |
| POST | `/Decode_Pic` | `src_path`, `dst_path` | 解码微信 `.dat` 图片缓存并写盘 | **src/dst 均为任意绝对路径，无任何校验** |
| POST | `/GetSelfProfile` | `{}` | 序列化全局 `SelfInfo` 返回 | 填充逻辑被删，实际**恒为空**（见 §3.1） |
| POST | `/QueryDB/execute` | `optDbName`, `SQL` | **把 SQL 原样交给数据库执行** | 任意 SQL，无白名单、无只读限制 |
| POST | `/QueryDB/GetAllDBName` | `{}` | 枚举可访问的数据库名 + 句柄 | 暴露内部句柄地址 |
| GET | `/QueryDB/status` | - | 返回登录状态与 `Weixin.dll` 基址 | 内存地址泄露 |

接口定义与 README、`postman/WeChat-Hook.postman_collection.json` 完全一致，路由注册在 `src/http_routes.cpp`；**没有发现隐藏接口或后门路由**。字段名大小写敏感（`SQL`、`optDbName`、`wxidorgid`、`to_wxid`）。

### 3.1 能力边界（二次开发前必须知道）

- **`/ForwardXMLMsg` 名不副实**：它并"不发 XML 文本"，而是从你给的 XML 里抽出 `cdnbigimgurl / cdnthumburl / aeskey / md5 / length` 等属性，在进程内**伪造一个 C++ 消息对象**再投递（`wx_send_xml.cpp:ExtractXmlFields / BuildImageStructure`）。只认 `<img>`(0x3)、`<videomsg>`(0x2B)、`<emoji>`(0x2F) 三种；**appmsg（链接/小程序/文件）、引用、@At、群公告全部不支持**（`atlist` 字段结构里留着但从未赋值）。
- 任一数值属性缺失会让 `std::stoi` 抛异常且无人捕获 → **直接崩掉微信进程**。
- 所有发送接口都是**假的成功**：`SendText/SendImage` 返回 `void`，路由无条件回 `{"ret":0,"retmsg":"success"}`；被拉黑、已被踢出群、消息过长，照样返回成功。要确认投递只能去查数据库。
- `/GetSelfProfile` 不解析 body、**不校验登录态**；而且 `GetSelfProfile.cpp` 里"核心：获取个人资料"那一节**是空的**（填充逻辑被删掉了），HTTP 处理函数只把空结构体序列化出去 —— 实际恒返回空字段。
- `/QueryDB/execute` 的 `sqlite3_prepare(..., tail=0)` 只编译第一条语句，所以**堆叠注入不可行**，但 `SELECT / UPDATE / DELETE / PRAGMA / ATTACH` 全都能跑 —— 既能读，也能**改和删**你的聊天记录。
- `/GetAllDBName` 返回原始 `dbHandle` 指针、`/QueryDB/status` 返回 `Weixin.dll` 基址 —— 地址泄露，可用于绕过 ASLR。

---

## 4. 工程质量评估

**优点**
- 目标明确、分层清晰：注入（`dllmain`）/ 初始化（`inline_weixin_dll_load`）/ 业务（`src/Send*`、`QueryDB`）/ 基础设施（`http_server`、`db_mgr`）职责分离。
- 依赖干净：MinHook、cpp-httplib、nlohmann/json、lz4、md5 全部内嵌 `3rdparty`，无包管理器依赖，可直接 MSBuild 编译。
- 主/子进程判定、自卸载、导出转发这些细节处理得比较认真（说明作者踩过坑）。
- 有 `update.log` 维护版本适配记录，更新节奏密集。

**缺点（都是实打实影响可用性的）**
1. **易语言直译痕迹严重**：`include/tools.h` 里成片的 `字节集_字节集到十六进制`、`时间_取现行时间戳`、`取文本左边`、`编码_UTF8编码` —— 这是把易语言（中文编程语言）代码逐行翻成 C++ 的产物。命名混乱、职责不清、大量胶水函数。
2. **无错误处理**：发消息接口不检查内部返回值，HTTP 层永远 `success`；`listen()` 的返回值也被丢弃，端口被占用时静默失败。
3. **并发不安全（会崩）**：`QueryDB.cpp` 里的临界区保护被**整段注释掉**；`db_mgr.cpp:getDatabaseInfo` 在锁外 `m_dbs.clear()`，与 `execute` 取句柄形成竞态 —— 悬空句柄 / UAF。
4. **未定义行为**：查询结果里 NULL 列走 `std::string(nullptr, 0)` 构造，属 UB，可崩溃；`ScanPattern` 在 `VirtualQueryEx` 失败（`RegionSize=0`）时**死循环**。
5. **确定性内存泄漏**：每条消息 `HeapAlloc` 的对象都不释放（作者自己注释"清理会崩溃"），`RWX VirtualAlloc` 内存永久泄漏 —— 长时间运行必然膨胀。
6. **偏移硬编码 = 版本一升级就废**：微信每次小版本更新 RVA 就变，必须重新逆向定位、重新编译。`update.log` 显示 2026 年 1 月到 10 月几乎每月一次适配，`other_version_fix.md` 还单独记录图片解密失效。
7. **无测试、无 CI、无 LICENSE**；`.vcxproj`/`CppProperties.json` 等 IDE 文件直接提交。
8. **关键数据被剥离**：`xdb/xwechat_offsets.h` 是空的、收消息链路缺失、`g_IsLogin` 无人赋值 —— **开源源码大概率不能直接编译出可用产物**，必须自己补偏移和逻辑。
9. **卸载/清理缺失**：DLL 常驻，`FreeLibraryAndExitThread` 只用于子进程；Hook 的 `UninitializeHookEngine` 基本没被调用。
10. **危险写法随处可见**：`SetWeixinString` 分配失败后仍 `Memory::Free(buf)` 继续用；`ReadWxString` 只检查 `len<=0x400` + 首块可读就 `memcpy`；`Patch_Low_Version` 不判空指针就解引用再写；`Patch_Low_Version_m2` 声明 12 个补丁却只循环 `i<3`（半成品 bug，且已被注释）；改代码时不挂起其他线程，运行中 patch 与正在执行的指令竞争。
11. **授权指纹残留**：`tools.cpp:GetDeviceUUID` 用 `cpuid.asm` 取 CPU 特征 + MAC 做 MD5，配合已删除的"远程授权校验"，说明原版是按机器绑定授权的商业软件；main 里这段成了死代码。
12. **启动不校验微信版本**：直接按硬编码偏移调用，版本不匹配时首次发消息即崩溃（而不是给出可读错误）。

---

## 5. 安全分析（本文最重要的部分）

### 5.1 高：未鉴权的局域网"微信后门"

`inline_weixin_dll_load.cpp` 中：

```cpp
g_httpServer->Start("0.0.0.0", g_StartPort);   // 默认 30001
```

`http_server.cpp` 里没有任何鉴权中间件，`http_routes.cpp` 注册的 8 个路由全部裸奔。攻击者在**同一局域网**（公司内网、酒店 WiFi、咖啡厅、云桌面同网段）扫到 30001 端口后可以：

| 攻击 | 后果 |
| --- | --- |
| `POST /QueryDB/execute` + `SELECT * FROM ...` | 拖走**全部联系人与聊天记录**（微信进程内句柄，结果是明文），包括图片/文件路径 |
| `POST /QueryDB/execute` + `UPDATE / DELETE` | **篡改、删除**聊天记录（销毁证据/伪造对话），或用 `ATTACH` 建库把数据写到磁盘任意位置 |
| `POST /SendTextMsg` / `ForwardXMLMsg` | **冒充本人**给任意好友/群发消息，钓鱼、诈骗、发广告 |
| `POST /Decode_Pic`（任意 `src_path`/`dst_path`） | 在用户权限下**任意路径读写文件**（解码写入），可写启动目录、写脚本、覆盖配置 |
| `POST /GetSelfProfile` | 资料填充逻辑被补回后，将泄露手机号、邮箱、QQ、地区等实名信息（当前版本恒返回空，见 §3.1） |
| `GET /QueryDB/status` / `POST /QueryDB/GetAllDBName` | 拿到 `Weixin.dll` 基址与数据库句柄，为进一步内存攻击提供 ASLR 绕过信息 |
| **网页 CSRF** | 无 CORS/Origin 校验，但 `Content-Type: text/plain` 的 POST 属"简单请求"免预检，用户浏览恶意网页时可能在后台被触发发消息（副作用已执行，只是读不到响应） |

**修复方式**（如果不放弃这个方案）：绑定 `127.0.0.1`、加 Bearer Token、加 IP 白名单、Windows 防火墙入站阻断 30001。开源版**一条都没做**。

### 5.2 高：技术手法本身就是杀软重点特征

进程注入 + PEB 模块摘除 + 内存热补丁 + 系统 DLL 劫持，对应 MITRE ATT&CK 的 T1055 / T1574.001 / T1562，与恶意软件行为高度重合。实际上这类 DLL 在国内主流杀软上**基本必被查杀**（无论是否真有恶意），使用体验会很差；反过来，如果你的电脑装了 EDR，这类注入行为也会被记录告警。

### 5.3 中高：供应链与二进制可信度

- 仓库直接提交了 3 个微软运行库 exe（约 32 MB）和一个**闭源的 `禁止微信更新2.0.exe`**。这些二进制无法从源码验证。
- Release `v4.1.10.27` 里附带 **239 MB 的 `WeChatWin_4.1.10.27.exe`**（下载 2330 次）—— 这是把对应版本的微信安装包直接二次分发，既不合规，也让"从哪装的微信"变得不可控。
- Release 提供编译好的 `version.dll`（483 KB / 606 KB 两个版本，**无代码签名**），无法自证与源码一致；来源不明的 `version.dll` 放进微信目录等于把整个账号交给它（它能读你所有聊天记录，也能外传）。
- 唯一相对安全的做法：**自己编译**，并核对源码（但你还需要自己补偏移，见 §4.8）。

### 5.4 低-中：回调外发与数据边界（当前为残留）

`CallBackURL=` 启动参数目前只被解析存进 `g_CallBack_Url`，`http_post.cpp` 的 POST 实现**没有调用点**（见 §2.3）。但一旦按设计补全（商业版大概率是完整的），消息内容就会以**明文 HTTP** POST 到任意指定 URL —— 指向第三方就等于把聊天内容外包出去。这也是为什么二次开发时必须把这条链路当作"外部数据出口"来设计。

### 5.5 法律与平台规则

- 《微信软件许可及服务协议》《微信个人账号使用规范》明确禁止使用外挂、非官方客户端、自动化工具；微信安全中心 [2024-03 公告](https://m.gmw.cn/2024-03/28/content_1303698258.htm) 点名"微信机器人、群控"并实施阶梯处罚。违反可**永久封号**。
- 抓取**他人**聊天记录/通讯录，可能触犯《刑法》第 253 条之一（侵犯公民个人信息）、《个人信息保护法》《网络安全法》。
- 对微信客户端注入并修改其运行逻辑，可能被评价为《刑法》第 285 条第 2 款"采用其他技术手段获取计算机信息系统数据 / 非法控制计算机信息系统"（情节严重 3 年以下，特别严重 3–7 年）；**对外售卖此类工具牟利还可能触第 3 款"提供侵入、非法控制计算机信息系统的程序、工具罪"**（[条文](https://qh.12348.gov.cn/pub/qhpfw/rdpf/flcs/202302/t20230221_100555.html)）。
- 《网络安全法》第 27 条禁止非法侵入、干扰网络正常功能；批量群发、营销引流还涉及《反不正当竞争法》与平台规则。
- **明确越界**：批量爬通讯录/群成员、导出他人聊天记录、代挂养号、群发营销、对外售卖工具。本仓库**没有 LICENSE**，"商用/再分发"连民事授权都没有依据。

> 合规边界：**自己的账号 + 自己的数据 + 本地自用**风险最低（但仍违反微信协议，只是不构成刑事问题）；一旦涉及他人数据、对外提供服务、商业牟利，性质就变了。

### 5.6 中高：账号封禁的现实概率

微信侧的检测面：**PEB 模块枚举**（本项目自带 `hideself.cpp` 就是为了对抗它）、注入痕迹（`version.dll` 代理、无签名）、行为特征（频率/节奏）、协议异常、客户端完整性校验、服务端风控。

现实数据（第三方调查，非官方）：WeChatFerry 作者 2025-04 的统计显示，Hook 框架用户中**仅约 15% 未被封号**，有人累计被封 20 次（[报道](https://cloud.tencent.com.cn/developer/article/2516190)）；国内论坛也长期有"已经封了一个"之类的反馈（[精易](https://bbs.ijingyi.com/forum.php?mod=viewthread&action=printable&tid=14861436)）。

**结论：请按"随时会封"来设计** —— 小号、限频、绝不上主号/工作号。

---

## 6. 同类方案对比

| 方案 | 技术路线 | 稳定性 | 上手 | 说明 |
| --- | --- | --- | --- | --- |
| **本项目 WeChat-Hook** | `version.dll` 代理 + 自研 inline hook + 进程内 HTTP | 低（版本绑定） | 中 | 纯 C++，进程内 HTTP，适合做底座；无鉴权、无授权、无收消息 |
| **wechatferry / wcferry** | 注入 + RPC（Python 客户端成熟） | 低（版本绑定） | **高** | 生态最大，Python 直接调；同样绑定特定微信版本，同样封号风险 |
| **ntchat** | 注入 + Python SDK | 低 | 高 | 文档友好，已停更多时 |
| **wxauto / pywinauto** | UI 自动化（不注入） | **中高** | 高 | 不碰内存、不易被判定外挂，但慢、只能操作前台窗口、功能受限 |
| **wechaty（协议/pad 版）** | 协议模拟 / 云端 pad | 中 | 高 | 网页协议已死，现多为付费 pad 服务，合规灰区 |
| **安卓 Xposed 模块** | 手机端 Hook | 中 | 中 | 与 PC 场景不同，需 Root/框架 |
| **官方能力** | 公众号/客服/企业微信 API | **高** | 中 | 唯一合规路线，但功能与个人号场景不重合 |

**选型建议**：要长期稳定 → 官方 API / 企业微信；要个人号自动化且能接受风险 → wechatferry（生态与文档更好）；只想读屏操作 → wxauto；想学逆向 → 读本项目源码。

---

## 7. 可持续性

- **活跃**：2022 年建仓，持续更新到 2026-10-22；`update.log` 显示 2026 年 1/3/4/5/6/7/8/9/10 月均有提交，跟微信 4.1.8.67 → 4.1.9.23 → 4.1.9.30 → 4.1.10.27 的升级同步适配，跟进延迟约 **2 天 ~ 2 周**。对一个逆向项目来说，这个节奏相当勤奋。
- **但"活跃"要打折**：最近 30 次提交集中在 2026-06-22 ~ 10-22，且**几乎全是 `update.log` / `README.md` 文档提交**，代码提交很少。
- **单人维护**：贡献者只有 `aixed` 一人（586 次提交），**0 个外部贡献者**。Bus factor = 1。
- **社区反馈积压**：Issue 35 条、PR 仅 4 条，**未关闭 24 条**（约 2/3 长期挂起）。PR #130（修 QueryDB 登录态）2026-08-25 提交至今未合并；Issue #133 询问"商业分发许可"2026-10-06 起无任何回应。
- **发布节奏与开发节奏脱节**：5 个 Release 里，2023-07 到 2026-06 空了近 3 年，而同期代码一直在改。说明 GitHub 更像是展示橱窗，二进制与文档交付走私域渠道。
- **脆弱**：偏移硬编码。作者一旦停更，或微信做一次结构性重构（4.x 已经是重构过的客户端），项目立刻归零。
- **商业闭环**：开源仓库引流 → showdoc 文档 / 交流群（TG、QQ 群）→ 多形态版本（E语言 / C++ / go 协议 / C++ 协议）。这种模式决定开源版**永远会比商业版慢、功能少**。
- **热度边际**：2026-10-09 → 10-21 的 12 天里 stars 从 2.1k 到 2.2k（+30），对一个 2156★ 的仓库来说属于"慢速长尾"，不是爆发式项目。

---

## 8. 结论与建议

**一句话**：这是一个**技术扎实、工程粗糙、法律与安全风险都偏高**的"开源引流版"微信 Hook 框架；它的价值在于**学习一套完整的 Windows 注入 + 逆向 + 进程内服务化范式**，而不在于直接拿来跑业务。

| 场景 | 建议 | 理由 |
| --- | --- | --- |
| 学习 Windows 逆向 / 注入 / Hook | ✅ **推荐读代码** | 注入链路、PEB 隐藏、自研 inline hook、进程内 SQLCipher 复用，是很好的教学素材 |
| 个人小号做自动化 | ⚠️ 谨慎，隔离环境 | 必须小号 + 虚拟机/备用机；先改 `127.0.0.1` + 加 token |
| 公司内部工具（客户数据） | ❌ 不建议 | 无 LICENSE（无授权）、无鉴权、封号即业务中断 |
| 对外交付 / 商业产品 | ❌ **明确不建议** | 授权、合规、数据安全三重风险，且随时可能因微信升级失效 |
| 多账号营销 / 群发 | ❌ **违法风险** | 违反平台协议 + 可能触及刑法条款 |

**如果一定要试，最低安全动作清单**：
1. 只用自己的小号，绝不登录工作号/主号；
2. 在隔离虚拟机里运行，不要给杀软加白名单（这是风险提示，不是省事技巧）；
3. 把 `0.0.0.0` 改成 `127.0.0.1`，并加一层 token 校验（改 `inline_weixin_dll_load.cpp` + `http_routes.cpp`）；
4. 自己编译，不要用 Release 里现成的 `version.dll`；
5. 关掉 `CallBackURL`，不要把聊天内容外发；
6. 备份聊天记录，做好随时封号的心理准备。

### 8.1 如果要基于它二次开发：改造清单（按优先级）

| # | 改造点 | 具体做法 | 对应缺陷 |
| --- | --- | --- | --- |
| 1 | **网络收敛** | `Start("127.0.0.1", port)`；httplib 加 `set_pre_routing_handler` 校验 `Authorization: Bearer <token>`；只允许回环 | §5.1 |
| 2 | **SQL 收敛** | 关键字白名单（只允许 `SELECT`）、强制 `LIMIT`、禁止 `ATTACH/PRAGMA`、独立只读连接 | §3.1 |
| 3 | **路径收敛** | `Decode_Pic` / `SendImgMsg` 的路径做 `GetFullPathName` + 白名单目录校验 | §3.1 |
| 4 | **版本门禁** | 启动时读微信版本号，与偏移表比对，不匹配就拒绝初始化并明确报错 | §4.12 |
| 5 | **收消息链路** | 自己补：Hook 收消息回调，或定时轮询 `MSG`/`ChatRoom` 表并用游标去重 | §2.3 |
| 6 | **并发与内存** | 恢复 `g_dbMgrCriticalSection`；消息对象统一 RAII 释放；去掉 RWX 分配（改 `PAGE_EXECUTE_READ`） | §4.3/§4.5 |
| 7 | **返回值真实化** | 发送后查回执/查询消息表确认，HTTP 返回真实结果而非恒 `success` | §3.1 |
| 8 | **偏移可维护** | 用特征码/字符串引用扫描替代硬编码 RVA，做一张"版本 → 偏移"表 | §4.6 |


---

## 附：数据来源与未验证事项

**主要来源**
- 仓库元数据 / Release / 贡献者：GitHub API（经 `gh-proxy.com` 转发）与 [ungh.cc](https://ungh.cc/repos/aixed/WeChat-Hook)；
- 源码原文：`main@a9d3f27`，经 `ghproxy.net` 转发 `raw.githubusercontent.com` 逐文件抓取（`global.h`、`dllmain.cpp`、`inline_weixin_dll_load.cpp`、`hideself.cpp`、`http_server.cpp`、`http_routes.cpp`、`QueryDB.cpp`、`SendTextMsg.cpp`、`GetSelfProfile.cpp`、`tools.h`、`version.def`、Postman 集合等）；
- 同类项目与封号数据：腾讯云开发者社区、LINUX DO、精易论坛等公开讨论。

**未能验证**
- **代码签名**：本机命令行不可用，无法对 Release 的 `version.dll` 做 Authenticode 校验；Release 仅给出作者自述的 SHA256（`5ABB5002…`），无签名链与可复现构建说明。
- **showdoc 文档是否收费/需卡密**：站点为 JS 渲染且反爬，未取到价目信息。"商业版收费"属于**强推测**（依据：`license: null`、`GetDeviceUUID` 机器指纹、已移除的"远程授权校验"、多形态文档分发、Issue #133 询问"商业分发许可"无人回应）。
- **Issue/PR 的具体内容**：仅拿到数量与两条关键 issue 的标题级信息，正文未逐条阅读。
- 本报告基于 **2026-10-22 的快照**；该仓库更新频繁，偏移常量、文件结构随时可能变化。
