# 桌面版启动器（P7-a，零依赖）

把已有的两个本机 HTTP 服务开成"桌面应用"：

- `801` = 微信网关（`python -m agi.wechat_gateway.server`）
- `802` = Soul Console 调试控制台（`tools/console/server.py`）

桌面版 = **确保这两个服务在跑 → 用 Edge/Chrome 的 `--app` 模式把 802 打开成独立窗口**。
纯 Python 标准库，**不需要 pip 安装任何东西**；Tauri 打包留到后续（见文末迁移说明）。

## 快速开始

双击仓库根目录的 **`启动桌面版.bat`**（或命令行 `python tools/desktop/launcher.py`）。

首次启动会自动：

1. 探测 801/802；没跑就用当前 Python 起后台进程（不弹控制台窗口），
   日志追加到 `E:\agent\logs\desktop_gateway.log` / `desktop_console.log`
   （E 盘不可写时回退 `agi\data\desktop_logs\`）；
2. 等两个服务健康（`/health`、`/api/health`，默认每个最多 60s）；
3. 用 Edge app 模式打开控制台：

```text
msedge.exe --app=http://127.0.0.1:802/ --window-size=1440,900 \
           --user-data-dir=<repo>\agi\data\desktop_profile
```

没有 Edge 时依次尝试 Chrome → PATH 里的 `msedge`/`chrome` → 系统默认浏览器（普通标签页兜底）。

## 参数

| 参数 | 作用 |
| --- | --- |
| `--no-open` | 只保证服务在跑，不打开窗口 |
| `--persist`（默认） | 窗口关闭后**保留**服务（下次打开更快） |
| `--exit-with-window` | 等窗口关闭后，停掉**本启动器拉起的**服务 |
| `--stop` | 停掉本启动器拉起的服务/窗口，并清理 state |
| `--status` | 打印 JSON 状态（端口/健康/归属 PID/窗口信息） |
| `--start-timeout N` | 单个服务启动+健康检查超时秒数（默认 60） |
| `--log-dir DIR` | 服务日志目录（默认 `E:\agent\logs`） |
| `--state-path PATH` | state 文件路径（测试/多实例用） |
| `--profile-dir DIR` | 浏览器 user-data-dir（默认 `agi\data\desktop_profile`） |

```bash
python tools/desktop/launcher.py --status
python tools/desktop/launcher.py --exit-with-window
python tools/desktop/launcher.py --stop
```

## 生命周期与单实例

- **只停自己启动的 PID**：state 文件 `agi/data/desktop_state.json` 里记录
  `started_pids`（gateway/console）与窗口 PID；`--stop` / `--exit-with-window`
  只动这些 PID。**本来就在跑的 801/802（比如你自己开的网关）绝不会被停**。
- **单实例**：state 里的 launcher 或自有服务仍存活时，再启动一次只会
  **重新打开窗口**，不会重复起服务。
- `--persist`（默认）启动器打开窗口后即退出，服务在后台继续（守护语义）；
  `--exit-with-window` 会留在前台，用「进程句柄 → profile 锁 → PowerShell 查
  msedge 命令行」三层判断窗口是否关闭，关闭后收尾。
- 启动器**不会改任何配置、不会开启主动消息**；是否主动发消息完全由
  `agi/data/wechat/config.json` 决定。桌面壳只是把页面打开。

## 目录 / 产物

```text
tools/desktop/
  launcher.py     # 全部逻辑（标准库；副作用都拆成小函数方便测试）
  README.md       # 本文件
启动桌面版.bat     # 双击入口（UTF-8 + chcp 65001）
agi/data/desktop_state.json     # 单实例/归属 PID（git 忽略的数据目录）
agi/data/desktop_profile/       # Edge app 模式专用 user-data-dir（不污染日常浏览）
E:\agent\logs\desktop_*.log     # 两个后台服务的日志
```

## 测试

```bash
python -m pytest agi/tests/test_desktop_launcher.py -q
```

全部 **monkeypatch**：不真起进程、不弹窗口、不占 801/802；覆盖 CLI 解析、
浏览器参数、服务复用/启动/超时回滚、`--no-open`、`--status` JSON、`--stop`
只杀 state PID、单实例只重开窗口、`--exit-with-window` 收尾、窗口存活判定。

## 常见问题

- **启动超时**：看 `E:\agent\logs\desktop_*.log` 末尾；801 需要微信已登录，
  802 需要 `tools/console/server.py` 可运行。
- **端口被占但健康检查失败**：说明 801/802 被别的程序占了（不是我们的服务），
  启动器会明确报错并退出，不会盲杀。
- **想换窗口大小/浏览器**：改 `launcher.py` 顶部的 `WINDOW_SIZE`/`EDGE_PATHS`，
  或直接 `--profile-dir` 指定独立 profile。
- **窗口没弹出但服务已经起来**：`--status` 看端口；也可以直接浏览器打开
  `http://127.0.0.1:802/`。

## 后续换 Tauri（迁移说明，本任务不做）

参考 `AstrBot-desktop`：Tauri 2 外壳 + 打包 Python 后端 + WebUI。迁移时：

1. Tauri 窗口直接 `WebviewUrl::External("http://127.0.0.1:802/")` 或内嵌前端；
2. Python 后端用 Tauri sidecar（PyInstaller 打包 801/802 两个入口）或随安装包带
   Python 运行时，由 Tauri 生命周期管理进程（替代本文件的 `_spawn_service`/state）；
3. 本文件里的**探测/健康检查/单实例/只停自有 PID** 语义直接复用；
4. `--app` Edge 窗口 → Tauri WebView 窗口，`desktop_profile` 不再需要；
5. 测试仍可保留（逻辑层与壳解耦）。

在 Tauri 版本就绪前，这个启动器就是可用的桌面入口。
