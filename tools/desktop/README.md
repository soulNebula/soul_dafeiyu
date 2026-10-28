# 桌面版（P7-a 启动器 + P7-d 托盘/通知/自启，零依赖）

把已有的三个本机 HTTP 服务开成"桌面应用"：

- `801` = 微信网关（`python -m agi.wechat_gateway.server`）
- `802` = Soul Console 调试控制台（`tools/console/server.py`）
- `803` = IDEA 工作台（`idea/.venv\Scripts\python.exe -m uvicorn app.main:app`）

桌面版 = **确保三个服务在跑 → 用 Edge/Chrome 的 `--app` 模式把 802（或 803）打开成独立窗口**，
外加**系统托盘**（打开控制台 / 主动消息开关 / STOP / 状态 / 日志 / 退出）、
**桌面通知**（主动发送、被拦、死区、熔断等）和**开机自启**。
纯 Python 标准库 + 系统自带 PowerShell/.NET，**不需要 pip 安装任何东西**；
Tauri 打包留到后续（见文末迁移说明）。

## 快速开始

双击仓库根目录的 **`启动桌面版.bat`**（或命令行 `python tools/desktop/launcher.py`）。

带托盘 / 通知（推荐日常使用）：

```bash
启动桌面版.bat --tray              # 起服务 + 托盘 + 窗口
启动桌面版.bat --tray --no-open    # 只起服务 + 托盘（不开窗口）
启动桌面版.bat --tray --no-notify  # 托盘但不要桌面通知
```

首次启动会自动：

1. 探测 801/802/803；没跑就用对应 Python 起后台进程（不弹控制台窗口；803 用 `idea/.venv`），
   日志追加到 `E:\agent\logs\desktop_gateway.log` / `desktop_console.log`
   （E 盘不可写时回退 `agi\data\desktop_logs\`）；
2. 等两个服务健康（`/health`、`/api/health`，默认每个最多 60s）；
3. `--tray` 时后台拉起 `tray.ps1`（PowerShell + WinForms NotifyIcon，隐藏窗口、单实例）；
4. 用 Edge app 模式打开控制台：

```text
msedge.exe --app=http://127.0.0.1:802/ --window-size=1440,900 \
           --user-data-dir=<repo>\agi\data\desktop_profile
```

没有 Edge 时依次尝试 Chrome → PATH 里的 `msedge`/`chrome` → 系统默认浏览器（普通标签页兜底）。

## 参数

| 参数 | 作用 |
| --- | --- |
| `--no-open` | 只保证服务在跑，不打开窗口 |
| `--open-workbench` | 打开 803 工作台窗口（默认打开 802 控制台） |
| `--tray` | 确保服务后拉起系统托盘（单实例；PID 记进 state，`--stop` 会一起收） |
| `--no-notify` | 关闭桌面通知（托盘不轮询 gateway.log；自启场景常用） |
| `--persist`（默认） | 窗口关闭后**保留**服务（下次打开更快） |
| `--exit-with-window` | 等窗口关闭后，停掉**本启动器拉起的**服务 |
| `--stop` | 停掉本启动器拉起的服务/窗口/托盘，并清理 state |
| `--status` | 打印 JSON 状态（端口/健康/归属 PID/窗口/托盘/通知路径） |
| `--notify-once` | 轮询一次 gateway.log，输出通知 JSON（托盘内部用，可手动排查） |
| `--autostart install\|uninstall\|status` | 开机自启（用户级；默认写「启动」目录 bat） |
| `--autostart-registry` | 自启改写 HKCU Run（不写启动目录） |
| `--start-timeout N` | 单个服务启动+健康检查超时秒数（默认 60） |
| `--log-dir DIR` | 服务日志目录（默认 `E:\agent\logs`） |
| `--state-path PATH` | state 文件路径（测试/多实例用） |
| `--profile-dir DIR` | 浏览器 user-data-dir（默认 `agi\data\desktop_profile`） |

```bash
python tools/desktop/launcher.py --status
python tools/desktop/launcher.py --exit-with-window
python tools/desktop/launcher.py --stop
python tools/desktop/launcher.py --notify-once          # 手动看一次通知 JSON
```

## 托盘（`tools/desktop/tray.ps1`，P7-d）

- 图标：`tools/desktop/icon.ico`（没有则用系统图标）；双击图标 = 打开控制台；
- 右键菜单：
  - **打开控制台** → 默认浏览器打开 `http://127.0.0.1:802/`
  - **启动主动消息 / 停止主动消息** → `POST /api/config {proactive_enabled:true|false}`
  - **紧急停止 STOP / 恢复** → `POST /api/proactive/stop|resume`
  - **查看状态** → `GET /api/status`，气泡显示 主动消息开关 / STOP / 内状态 mode / 已发送数
  - **打开日志目录** → 优先 `E:\agent\logs`，回退 `agi\data\desktop_logs`
  - **退出托盘** → 只退出托盘进程，不动 801/802
- 写操作带 `X-Admin-Token`，token 从 `agi/data/wechat/config.json` 的 `admin_token` 读；
- 单实例：命名 Mutex `Local\SoulCompanionTray`，重复启动直接退出；
- launcher 记录 `tray_pid` 到 `agi/data/desktop_state.json`，`--stop` 只杀这个自有 PID。

## 桌面通知（规则在 Python 纯函数里，PS 只显示 balloon）

托盘每 20s 调用一次 `python tools/desktop/launcher.py --notify-once`：

- 轮询 `agi/data/wechat/gateway.log`，offset/去重表存在 `agi/data/desktop_notify_state.json`
  （首次只回放最近 200KB，不通知历史积压）；
- 通知事件与文案：

| 事件 | 通知 | 级别 |
| --- | --- | --- |
| `proactive_send`（status=sent） | 已发送：<文本前 30 字> | info |
| `send_blocked` | 未发送：<reason> | error |
| `send_failed` | 未发送：<error_key> | error |
| `proactive_discarded` | 主动消息已丢弃：<reason> | error |
| `gateway_stale` | 网关可能卡住（空闲 Ns） | warn |
| `proactive_breaker_open` | 熔断：连续失败 N 次 | error |

- 相同事件 60s 内去重；
- **夜间静默时段（`config.json` 的 quiet_hours，默认 [23,8]）只报错误**，不报正常发送；
- `--no-notify` 完全关闭（托盘不轮询）；
- 通知逻辑可用 `python tools/desktop/launcher.py --notify-once` 手动验证（输出 JSON，不弹窗）。

## 开机自启（`tools/desktop/autostart.ps1`，P7-d）

只动用户级，**不需要管理员**：

```bash
# 默认：在「启动」目录放 SoulCompanionAutostart.bat（调 launcher --tray --no-open）
powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 install
powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 status
powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 uninstall

# 可选：写 HKCU\Software\Microsoft\Windows\CurrentVersion\Run（不写启动目录）
powershell -ExecutionPolicy Bypass -File tools\desktop\autostart.ps1 install -UseRegistry

# 等价的 launcher 命令（PS 只是薄封装）
python tools/desktop/launcher.py --autostart install
python tools/desktop/launcher.py --autostart install --autostart-registry
python tools/desktop/launcher.py --autostart status
python tools/desktop/launcher.py --autostart uninstall
```

- 自启命令：`pythonw.exe tools\desktop\launcher.py --tray --no-open`
  （pythonw 不存在时用 python；不会弹黑框）；
- 卸载会同时清理「启动」目录 bat 和 HKCU Run 值（删不存在的项是幂等的）。

## 生命周期与单实例

- **只停自己启动的 PID**：state 文件 `agi/data/desktop_state.json` 里记录
  `started_pids`（gateway/console）、窗口 PID、`tray_pid`；`--stop` / `--exit-with-window`
  只动这些 PID。**本来就在跑的 801/802（比如你自己开的网关）绝不会被停**。
- **单实例**：state 里的 launcher 或自有服务仍存活时，再启动一次只会
  **重新打开窗口 / 确保托盘**，不会重复起服务；托盘自身另有 Mutex 单实例。
- `--persist`（默认）启动器打开窗口后即退出，服务/托盘在后台继续；
  `--exit-with-window` 会留在前台，用「进程句柄 → profile 锁 → PowerShell 查
  msedge 命令行」三层判断窗口是否关闭，关闭后收尾。
- 启动器**不会改任何配置、不会开启主动消息**；是否主动发消息完全由
  `agi/data/wechat/config.json` 决定。桌面壳只是把页面/托盘打开。

## 目录 / 产物

```text
tools/desktop/
  launcher.py        # 主逻辑 + 通知纯函数 + 自启实现（标准库；副作用拆成小函数便于测试）
  tray.ps1           # 托盘（PowerShell WinForms NotifyIcon；单实例 Mutex）
  autostart.ps1      # 自启薄封装（install/uninstall/status → launcher --autostart）
  icon.ico           # 托盘图标（纯标准库生成）
  README.md          # 本文件
启动桌面版.bat        # 双击入口（GBK/CRLF；支持 --tray/--no-notify 等参数）
agi/data/desktop_state.json     # 单实例/归属 PID（git 忽略的数据目录）
agi/data/desktop_notify_state.json  # 通知 offset + 60s 去重表
agi/data/desktop_profile/       # Edge app 模式专用 user-data-dir（不污染日常浏览）
E:\agent\logs\desktop_*.log     # 两个后台服务的日志
```

## 测试

```bash
python -m pytest agi/tests/test_desktop_launcher.py agi/tests/test_desktop_enhance.py -q
```

全部 **monkeypatch**：不真起进程、不弹窗口/通知、不占 801/802、不动真实注册表/启动目录；
托盘/自启 PS 只做静态契约检查（另可用 PowerShell Parser 做语法校验）。
覆盖：CLI 解析、浏览器参数、服务复用/启动/超时回滚、`--no-open`、`--status` JSON、
`--stop` 只杀 state PID、单实例只重开窗口、`--exit-with-window` 收尾、窗口存活判定；
通知 5 类事件/60s 去重/夜间只报错误/日志增量与轮转；托盘单实例与 `--no-notify`；
自启安装/状态/卸载（临时启动目录 + 假注册表）。

## 常见问题

- **启动超时**：看 `E:\agent\logs\desktop_*.log` 末尾；801 需要微信已登录，
  802 需要 `tools/console/server.py` 可运行。
- **端口被占但健康检查失败**：说明 801/802 被别的程序占了（不是我们的服务），
  启动器会明确报错并退出，不会盲杀。
- **托盘图标没出现**：`--status` 看 `tray.pid/alive`；手动跑
  `powershell -ExecutionPolicy Bypass -File tools\desktop\tray.ps1` 看报错。
- **不想被通知打扰**：`--no-notify`；或改 `agi/data/wechat/config.json` 的 `quiet_hours`。
- **想换窗口大小/浏览器**：改 `launcher.py` 顶部的 `WINDOW_SIZE`/`EDGE_PATHS`。
- **自启没生效**：`python tools/desktop/launcher.py --autostart status` 看 `bat_installed` /
  `registry_installed`；确认 Python 路径在开机环境也可用（自启 bat 里写的是绝对路径）。

## 后续换 Tauri（迁移说明，本任务不做）

参考 `AstrBot-desktop`：Tauri 2 外壳 + 打包 Python 后端 + WebUI。迁移时：

1. Tauri 窗口直接 `WebviewUrl::External("http://127.0.0.1:802/")` 或内嵌前端；
2. Python 后端用 Tauri sidecar（PyInstaller 打包 801/802 两个入口）或随安装包带
   Python 运行时，由 Tauri 生命周期管理进程（替代本文件的 `_spawn_service`/state）；
3. 托盘 → Tauri TrayIcon 菜单（复用本文件的 HTTP 菜单动作与 token 读取逻辑）；
4. 通知 → Tauri Notification 插件（复用 `collect_notifications` 纯函数：事件映射/
   60s 去重/夜间规则的逻辑与壳无关）；
5. 开机自启 → Tauri autostart 插件（等价于本文件的用户级启动目录/HKCU Run）；
6. `--app` Edge 窗口 → Tauri WebView 窗口，`desktop_profile` 不再需要。

在 Tauri 版本就绪前，这个启动器 + 托盘 + 通知 + 自启就是可用的桌面入口。


## 打包成 exe（Windows 单目录，PyInstaller）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\desktopuild_exe.ps1
# 产物：tools/desktop/dist/SoulAgentDesktop/SoulAgentDesktop.exe
```

- 打包后入口与 `launcher.py` 完全一致（参数透传）：双击=起服务+开 802 窗口；
  `--tray` 加托盘，`--open-workbench` 开 803 工作台，`--status/--stop/--autostart` 照常可用。
- 仓库根的 `启动桌面版.bat` 会**优先调用 exe**（存在时），没有再回落到 `python launcher.py`。
- exe 运行时会自动找到仓库根（向上查找 `agi/` + `tools/desktop/`），可放在仓库任意子目录；
  换机器/换路径可用环境变量 `SOULAGENT_DESKTOP_ROOT` 指定。
- `dist/`、`build/`、`*.spec` 已加入 `.gitignore`，不入库。
