# -*- coding: utf-8 -*-
"""P7-a：零依赖桌面启动器（Windows / 纯 Python 标准库）。

思路（参照 AstrBot Desktop 的"桌面壳 + 本机 WebUI"，但先不做 Tauri）：
后端已经有两个本机 HTTP 服务——微信网关 ``801`` 与控制台 ``802``。桌面版 =
「确保两个服务在跑 → 用 Edge/Chrome 的 ``--app`` 模式把 802 打开成独立窗口」。
不需要 pip 依赖；Tauri 打包留到后续（见 README 的迁移说明）。

启动：``python tools/desktop/launcher.py``（或双击仓库根的 ``启动桌面版.bat``）

生命周期：
- 默认 ``--persist``：窗口关了也保留服务（方便再次打开；服务本来也是守护进程语义）；
- ``--exit-with-window``：等窗口关闭后，只停掉**本启动器自己拉起的**服务
  （state 文件 ``agi/data/desktop_state.json`` 里记录的 PID），别人的 801/802 不动；
- ``--no-open``：只起服务不开窗；``--stop``：停掉自己拉起的服务；
- ``--status``：打印 JSON 状态；
- 单实例：state 里的 launcher/服务仍活着时，第二次启动只重新打开窗口，不重复起服务。

注意：本文件不 import 任何第三方库；所有会产生副作用的地方（进程/窗口/端口探测）
都做了小函数包装，方便测试全部 monkeypatch（测试不真起进程、不弹窗口）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

def _find_repo_root() -> Path:
    """源码运行 / PyInstaller exe 运行都能找到仓库根（含 agi/ 与 tools/desktop/）。"""
    env = str(os.environ.get("SOULAGENT_DESKTOP_ROOT") or "").strip()
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    candidates.extend([here.parent, *here.parents])
    exe = Path(sys.executable).resolve()
    candidates.extend([exe.parent, *exe.parents])
    candidates.append(Path.cwd().resolve())
    seen: set[str] = set()
    for cand in candidates:
        key = str(cand).lower()
        if key in seen:
            continue
        seen.add(key)
        if (cand / "agi" / "wechat_gateway").is_dir() and (cand / "tools" / "desktop").is_dir():
            return cand
    return here.parents[2]


ROOT = _find_repo_root()
if str(ROOT) not in sys.path:            # 以脚本方式启动时也能 import agi.*
    sys.path.insert(0, str(ROOT))

#: 单实例/归属状态：只记录"本启动器拉起"的 PID，--stop 只杀这些
STATE_PATH = ROOT / "agi" / "data" / "desktop_state.json"
#: Edge/Chrome 的 app 模式独立 user-data-dir（不污染用户日常浏览数据）
PROFILE_DIR = ROOT / "agi" / "data" / "desktop_profile"

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 801
CONSOLE_PORT = 802
WORKBENCH_PORT = 803
CONSOLE_URL = f"http://{GATEWAY_HOST}:{CONSOLE_PORT}/"
WORKBENCH_URL = f"http://{GATEWAY_HOST}:{WORKBENCH_PORT}/"
GATEWAY_HEALTH = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/health"
CONSOLE_HEALTH = f"http://{GATEWAY_HOST}:{CONSOLE_PORT}/api/health"
WORKBENCH_HEALTH = f"http://{GATEWAY_HOST}:{WORKBENCH_PORT}/api/wb/health"
WINDOW_SIZE = "1440,900"

#: <AGENT_HOME>\logs 是约定位置（和训练/评测日志放一起）；不可写时回退仓库内目录
from agi.paths import logs_dir
DEFAULT_LOG_DIR = Path(os.environ.get("WXBOX_DESKTOP_LOG_DIR") or str(logs_dir()))
FALLBACK_LOG_DIR = ROOT / "agi" / "data" / "desktop_logs"

EDGE_PATHS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CHROME_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

START_TIMEOUT_S = 60.0      # 单个服务启动 + 健康检查的总超时
HEALTH_TIMEOUT_S = 5.0      # 端口已开（别人已跑）时，等健康检查的超时
POLL_S = 0.2
WINDOW_STARTUP_GRACE_S = 15.0
WINDOW_POLL_S = 1.0


class LauncherError(RuntimeError):
    """启动失败（服务超时/端口被占/健康检查失败等）。"""


# --------------------------------------------------------------------------- #
# 探测：端口 / HTTP 健康
# --------------------------------------------------------------------------- #
def port_open(port: int, host: str = GATEWAY_HOST, timeout: float = 0.3) -> bool:
    """TCP 探测端口是否有人监听（不读不写业务数据）。"""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def http_ok(url: str, timeout: float = 1.0) -> bool:
    """HTTP GET 健康检查：2xx 即健康（控制台/网关都只返回小 JSON）。"""
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - 本机回环地址
            return 200 <= int(getattr(response, "status", 200)) < 300
    except (URLError, OSError, ValueError):
        return False


def wait_for(predicate, timeout: float, *, interval: float = POLL_S) -> bool:
    """轮询 predicate 直到 True 或超时。"""
    deadline = time.time() + max(0.0, float(timeout))
    while True:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 探测异常按未就绪处理
            pass
        if time.time() >= deadline:
            return False
        time.sleep(interval)


# --------------------------------------------------------------------------- #
# 日志目录 / 子进程
# --------------------------------------------------------------------------- #
def resolve_log_dir(explicit: str | None = None) -> Path:
    """日志目录：显式参数 > 环境变量 > E:\\agent\\logs > 仓库内回退。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(DEFAULT_LOG_DIR)
    candidates.append(FALLBACK_LOG_DIR)
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return directory
        except OSError:
            continue
    return FALLBACK_LOG_DIR


def _spawn_service(cmd: list[str], log_path: Path,
                   cwd: Path | str | None = None) -> Any:
    """拉起后台服务：stdout/stderr 追加到日志，不弹新控制台窗口。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab", buffering=0)  # noqa: SIM115 - 交给子进程继承后关闭
    kwargs: dict[str, Any] = {
        "cwd": str(cwd) if cwd else str(ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        # 不弹控制台；独立进程组，退出 launcher 不影响服务
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603 - 命令由本文件构造
    finally:
        log_file.close()
    return proc


def _popen_browser(argv: list[str]) -> Any:
    """打开浏览器窗口（app 模式）；窗口进程不需要日志重定向。

    ⚠️ 2026-10-29 实测：PyInstaller 冻结后直接 Popen(msedge) 会打开一个**空白灰窗**
    （Edge 能起、页面不加载；源码运行时同参数正常）——改用 `cmd /c start` 让 Edge
    走正常的 shell 启动上下文。窗口 PID 不再直接可见（返回的 Popen 是 cmd），
    复用/关闭逻辑本来就不依赖这个 PID。
    """
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt" and getattr(sys, "frozen", False):
        return subprocess.Popen(["cmd", "/c", "start", "", *argv], **kwargs)  # noqa: S603
    return subprocess.Popen(argv, **kwargs)  # noqa: S603 - argv 由本文件构造


def _open_default_browser(url: str) -> bool:
    """兜底：没有 Edge/Chrome 时用系统默认浏览器（普通标签页，不是 app 窗口）。"""
    try:
        if os.name == "nt":
            os.startfile(url)  # noqa: S606 - 本机 URL
            return True
        return bool(webbrowser.open(url))
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# 进程工具（测试全部 monkeypatch 这里，不碰真实进程）
# --------------------------------------------------------------------------- #
def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFO
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return int(code.value) == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        os.kill(int(pid), 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def _terminate_pid(pid: int | None, *, timeout: float = 8.0) -> bool:
    """结束一个 PID（Windows 用 taskkill /T /F，连子进程一起）。只用于自有 PID。"""
    if not _pid_alive(pid):
        return False
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           capture_output=True, timeout=timeout)
        else:
            os.kill(int(pid), 15)
    except Exception:  # noqa: BLE001
        return False
    return wait_for(lambda: not _pid_alive(pid), timeout=timeout)


# --------------------------------------------------------------------------- #
# 服务管理
# --------------------------------------------------------------------------- #
def _service_python() -> str:
    """主服务用哪个 Python：env > 源码解释器 > 打包 exe 时的默认 C:/Python314。"""
    env = str(os.environ.get("SOULAGENT_PYTHON") or "").strip()
    if env:
        return env
    if not getattr(sys, "frozen", False):
        return sys.executable
    default = Path(r"C:\Python314\python.exe")
    if default.is_file():
        return str(default)
    return "python"


def _workbench_python() -> str:
    """803 工作台用 idea/.venv（缺省回落到主 Python）。"""
    env = str(os.environ.get("SOULAGENT_WORKBENCH_PYTHON") or "").strip()
    if env:
        return env
    candidate = ROOT / "idea" / ".venv" / "Scripts" / "python.exe"
    if candidate.is_file():
        return str(candidate)
    return _service_python()


def service_commands() -> dict[str, dict[str, Any]]:
    """三个后台服务：801 网关 / 802 控制台 / 803 工作台（最后一个是 idea/.venv）。"""
    return {
        "gateway": {
            "port": GATEWAY_PORT,
            "health": GATEWAY_HEALTH,
            "log": "desktop_gateway.log",
            "cmd": [_service_python(), "-m", "agi.wechat_gateway.server"],
        },
        "console": {
            "port": CONSOLE_PORT,
            "health": CONSOLE_HEALTH,
            "log": "desktop_console.log",
            "cmd": [_service_python(), str(ROOT / "tools" / "console" / "server.py"),
                    "--port", str(CONSOLE_PORT)],
        },
        "workbench": {
            "port": WORKBENCH_PORT,
            "health": WORKBENCH_HEALTH,
            "log": "desktop_workbench.log",
            "cwd": str(ROOT / "idea"),
            "cmd": [_workbench_python(), "-m", "uvicorn", "app.main:app",
                    "--host", GATEWAY_HOST, "--port", str(WORKBENCH_PORT)],
        },
    }


def ensure_service(name: str, spec: dict[str, Any], log_dir: Path, *,
                   start_timeout: float = START_TIMEOUT_S) -> dict[str, Any]:
    """确保一个服务可用；返回 ``{name, port, started, pid, reused, healthy}``。

    - 端口已在监听：复用（最多等 ``HEALTH_TIMEOUT_S`` 健康），**不重复起**；
    - 没在跑：起子进程并等端口+健康；超时则杀掉刚起的 PID 并抛 LauncherError。
    """
    port = int(spec["port"])
    health = str(spec["health"])
    if port_open(port):
        healthy = wait_for(lambda: http_ok(health), timeout=min(HEALTH_TIMEOUT_S, start_timeout))
        if not healthy:
            raise LauncherError(
                f"{name} 端口 {port} 已被占用但健康检查失败（{health}）——"
                "可能是别的进程占用了端口，请先排查")
        return {"name": name, "port": port, "started": False, "pid": None,
                "reused": True, "healthy": True}

    log_path = Path(log_dir) / str(spec["log"])
    proc = _spawn_service(list(spec["cmd"]), log_path, cwd=spec.get("cwd"))
    pid = int(getattr(proc, "pid", 0) or 0)
    ok = wait_for(lambda: port_open(port) and http_ok(health), timeout=start_timeout)
    if not ok:
        _terminate_pid(pid)
        raise LauncherError(
            f"{name} 启动超时（{start_timeout:.0f}s，端口 {port} / 健康 {health}），"
            f"请查看日志：{log_path}")
    return {"name": name, "port": port, "started": True, "pid": pid,
            "reused": False, "healthy": True}


# --------------------------------------------------------------------------- #
# 浏览器窗口
# --------------------------------------------------------------------------- #
def _candidate_browsers() -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    for raw in EDGE_PATHS:
        candidates.append((Path(raw), "edge"))
    for raw in CHROME_PATHS:
        candidates.append((Path(raw), "chrome"))
    return candidates


def find_browser() -> tuple[Path | None, str]:
    """优先 Edge，其次 Chrome，最后 PATH 里的 msedge/chrome。

    返回 ``(可执行文件, 模式)``；都找不到返回 ``(None, "default")``。
    """
    for path, mode in _candidate_browsers():
        try:
            if path.is_file():
                return path, mode
        except OSError:
            continue
    for name, mode in (("msedge", "edge"), ("chrome", "chrome")):
        found = shutil.which(name)
        if found:
            return Path(found), mode
    return None, "default"


def build_browser_argv(browser: Path | str, url: str, profile_dir: Path | str) -> list[str]:
    """Edge/Chrome app 模式参数（固定契约，测试锁住）。"""
    # --disable-gpu / --no-first-run：远程桌面/无 GPU 会话里不加会渲染成空白灰窗
    # （2026-10-29 实测：不加 --disable-gpu 时 Edge app 窗口只显示灰底，页面不渲染）。
    return [str(browser), f"--app={url}", f"--window-size={WINDOW_SIZE}",
            "--disable-gpu", "--disable-features=CalculateNativeWinOcclusion",
            "--no-first-run", f"--user-data-dir={Path(profile_dir)}"]


def is_elevated() -> bool:
    """当前进程是否以管理员（提升）权限运行。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def _open_url_via_explorer(url: str) -> bool:
    """用 explorer.exe（普通权限）打开 URL，避免 Edge「现有实例以提升权限运行」弹窗。"""
    try:
        subprocess.Popen(["explorer.exe", url], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


def _hidden_powershell(script: str, *, timeout: float = 8.0) -> str:
    """执行 PowerShell 并**完全隐藏窗口**（否则 exe 每次查询都会闪一个黑色/管理员窗口）。"""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "timeout": max(2.0, float(timeout)),
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                               "-WindowStyle", "Hidden", "-Command", script], **kwargs)
        raw = proc.stdout or b""
    except Exception:  # noqa: BLE001
        return ""
    text = raw.decode("utf-8", "replace")
    if "�" in text:
        text = raw.decode("gbk", "replace")
    return text


def _edge_app_pids(profile: Path) -> tuple[list[int], list[int]]:
    """返回 (带 --app 的窗口 PID, 只挂着该 profile 的后台 PID)。仅 Windows 用。"""
    if os.name != "nt":
        return [], []
    marker = profile.name  # desktop_profile；用目录名匹配，避开路径转义
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*" + marker + "*' } | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"
    )
    text = _hidden_powershell(script, timeout=10)
    apps: list[int] = []
    bg: list[int] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if "|" not in line:
            continue
        pid_s, cmd = line.split("|", 1)
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if "--app=" in cmd:
            apps.append(pid)
        else:
            bg.append(pid)
    return apps, bg


def _window_titles_for_pids(pids: set[int]) -> dict[int, str]:
    """用 Win32 EnumWindows 取这些 PID 的可见顶层窗口标题（无外部进程、无弹窗）。"""
    if os.name != "nt" or not pids:
        return {}
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    titles: dict[int, str] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum(hwnd, _lparam):  # noqa: ANN001
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if int(pid.value) not in pids:
                return True
            length = int(user32.GetWindowTextLengthW(hwnd))
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            titles[int(pid.value)] = str(buf.value or "")
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        user32.EnumWindows(_enum, 0)
    except Exception:  # noqa: BLE001
        return {}
    return titles


def _edge_window_title(pid: int) -> str:
    return _window_titles_for_pids({int(pid)}).get(int(pid), "")


def _kill_pids(pids: list[int]) -> None:
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                           capture_output=True, timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _prepare_edge_profile(profile: Path, *, expect_title: str = "Soul Console") -> bool:
    """已有加载完成的 app 窗口 → 复用（True）。

    标题里没有 expect_title（典型是空白窗，标题只有 127.0.0.1）→ 关掉重开，
    否则会一直复用那个空白窗（2026-10-29 用户截图反馈）。
    """
    apps, bg = _edge_app_pids(profile)
    loaded: list[int] = []
    stale: list[int] = []
    for pid in apps:
        title = _edge_window_title(pid)
        if expect_title and expect_title in title:
            loaded.append(pid)
        else:
            stale.append(pid)
    if loaded:
        return True
    _kill_pids(stale + bg)
    return False


def _wait_edge_loaded(profile: Path, timeout: float = 25.0,
                      *, expect_title: str = "Soul Console") -> bool:
    """开窗后等标题出现；返回是否加载成功。"""
    deadline = time.time() + max(3.0, float(timeout))
    while time.time() < deadline:
        apps, _bg = _edge_app_pids(profile)
        for pid in apps:
            if expect_title in _edge_window_title(pid):
                return True
        time.sleep(0.8)
    return False


def launch_window(*, url: str = CONSOLE_URL, profile_dir: Path | str | None = None,
                  browser: Path | str | None = None) -> dict[str, Any]:
    """打开控制台的独立窗口；返回窗口信息（含内部 ``_proc`` 供等待用，落盘时剔除）。"""
    if is_elevated() and not os.environ.get("SOULAGENT_DESKTOP_ALLOW_ELEVATED_BROWSER"):
        # 管理员启动会带出「Edge 未响应：现有实例正在以提升的权限运行」的弹窗，
        # 这里直接用普通权限的 explorer 打开默认浏览器（用户请用桌面快捷方式正常启动）。
        if _open_url_via_explorer(url):
            print("[desktop] 当前以管理员运行：已用普通权限浏览器打开（桌面窗口模式请正常双击，不要用『以管理员身份运行』）",
                  file=sys.stderr)
            return {"mode": "default", "pid": None, "url": url, "argv": [],
                    "profile_dir": "", "_proc": None}
    profile = Path(profile_dir or PROFILE_DIR)
    profile.mkdir(parents=True, exist_ok=True)
    # 已开着且加载完成的 app 窗口 → 复用；空白/残留窗口先关掉
    if _prepare_edge_profile(profile):
        print("[desktop] 检测到已打开的控制台窗口，直接复用（不重复开）")
        return {"pid": 0, "mode": "edge", "reused": True, "url": url}
    if browser is not None:
        path, mode = Path(browser), "custom"
    else:
        path, mode = find_browser()
    if path is not None:
        argv = build_browser_argv(path, url, profile)
        proc = _popen_browser(argv)
        # 等页面真正加载（标题出现）；空白窗/首次冷启动过长 → 重开一次
        if not _wait_edge_loaded(profile):
            print("[desktop] 窗口未在 25s 内加载，重开一次", file=sys.stderr)
            apps, bg = _edge_app_pids(profile)
            _kill_pids(apps + bg)
            time.sleep(1.0)
            proc = _popen_browser(argv)
            if not _wait_edge_loaded(profile, timeout=30.0):
                print("[desktop] 重开后仍未加载：已回退系统默认浏览器打开", file=sys.stderr)
                _open_default_browser(url)
                return {"mode": "default", "pid": None, "url": url,
                        "argv": [], "profile_dir": str(profile), "_proc": None}
        return {"mode": mode, "pid": int(getattr(proc, "pid", 0) or 0), "url": url,
                "argv": argv, "profile_dir": str(profile), "_proc": proc}
    opened = _open_default_browser(url)
    if not opened:
        raise LauncherError("没找到 Edge/Chrome，也无法用系统默认浏览器打开")
    return {"mode": "default", "pid": None, "url": url, "argv": [],
            "profile_dir": str(profile), "_proc": None}


def _profile_locked(profile_dir: Path | str) -> bool:
    """Edge/Chrome 运行时会占用 profile 锁文件（不同版本文件名略有差异）。"""
    for name in ("SingletonLock", "SingletonCookie", "lockfile"):
        try:
            if (Path(profile_dir) / name).exists():
                return True
        except OSError:
            continue
    return False


def _edge_process_count(profile_dir: Path | str) -> int:
    """数一下命令行里带该 profile 路径的 msedge 进程（Windows PowerShell CIM）。"""
    if os.name != "nt":
        return 0
    marker = str(profile_dir).replace("'", "''")
    script = ("$p = Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              f"Where-Object {{ $_.CommandLine -like '*{marker}*' }}; "
              "if ($p) { @($p).Count } else { 0 }")
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=15)
        return int((proc.stdout or "0").strip().splitlines()[-1] or 0)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return 0


def _window_alive(window: dict[str, Any] | None) -> bool:
    """窗口是否还开着：进程句柄 → profile 锁 → msedge 进程命令行，三层判断。"""
    if not window:
        return False
    proc = window.get("_proc")
    if proc is not None:
        try:
            if proc.poll() is None:
                return True
        except Exception:  # noqa: BLE001
            pass
    profile = window.get("profile_dir") or PROFILE_DIR
    if _profile_locked(profile):
        return True
    return _edge_process_count(profile) > 0


def wait_window_closed(window: dict[str, Any] | None, *, poll: float = WINDOW_POLL_S,
                       startup_grace: float = WINDOW_STARTUP_GRACE_S) -> bool:
    """阻塞到窗口关闭。

    返回 True = 观察到窗口并确认已关闭；False = 观察不到窗口（浏览器把进程交出去
    且查不到 profile 锁/命令行）——调用方应**保留服务**，宁可不收尾也不要误停。
    """
    started = time.time()
    seen = False
    while True:
        alive = _window_alive(window)
        if alive:
            seen = True
        elif seen:
            return True
        now = time.time()
        if not seen and now - started > startup_grace:
            return False
        time.sleep(max(0.05, float(poll)))


# --------------------------------------------------------------------------- #
# P7-d：桌面通知（纯函数，供单测；PowerShell 只负责显示）
# --------------------------------------------------------------------------- #
NOTIFY_LOG_PATH = ROOT / "agi" / "data" / "wechat" / "gateway.log"
NOTIFY_STATE_PATH = ROOT / "agi" / "data" / "desktop_notify_state.json"
GATEWAY_CONFIG_PATH = ROOT / "agi" / "data" / "wechat" / "config.json"
TRAY_SCRIPT = ROOT / "tools" / "desktop" / "tray.ps1"
AUTOSTART_PS1 = ROOT / "tools" / "desktop" / "autostart.ps1"
NOTIFY_TAIL_BYTES = 200_000       # 首次只回放最近 200KB，不通知历史积压
NOTIFY_DEDUPE_S = 60.0            # 相同事件 60s 内去重
NOTIFY_SEEN_TTL_S = 3600.0        # 去重表最多记 1h
DEFAULT_QUIET_HOURS = (23, 8)     # 夜间（与网关默认一致）只报错误

#: 会被通知的事件 → (标题, 是否错误) 的映射在 _map_event 里逐条实现
NOTIFY_KINDS = ("proactive_send", "send_blocked", "send_failed",
                "proactive_discarded", "gateway_stale", "proactive_breaker_open")


def _parse_event(line: Any) -> dict[str, Any] | None:
    """解析一行 gateway.log（也接受已经是 dict 的行，方便测试）。"""
    if isinstance(line, dict):
        return line
    try:
        event = json.loads(str(line or ""))
    except (TypeError, ValueError):
        return None
    return event if isinstance(event, dict) else None


def _event_ts(event: dict[str, Any], default: float) -> float:
    try:
        value = float(event.get("ts"))
        return value if value > 0 else float(default)
    except (TypeError, ValueError):
        return float(default)


def _normalise_quiet(value: Any) -> tuple[int, int] | None:
    """静默区间归一：合法 [start,end]（int 小时，可跨零点）；None/非法 → None。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            return None
    return None


def _quiet_hours_from_config() -> tuple[int, int] | None:
    """从网关 config.json 读静默时段（只读；read 失败时退回默认 [23,8]）。"""
    try:
        raw = json.loads(GATEWAY_CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return DEFAULT_QUIET_HOURS
        quiet = _normalise_quiet(raw.get("quiet_hours"))
        return quiet if quiet is not None else DEFAULT_QUIET_HOURS
    except (OSError, ValueError):
        return DEFAULT_QUIET_HOURS


def _is_night(ts: float, quiet: tuple[int, int] | None) -> bool:
    """ts 所在本地小时是否落在静默区间（支持跨零点；start==end 视为不静默）。"""
    if not quiet:
        return False
    hour = time.localtime(float(ts)).tm_hour
    start, end = int(quiet[0]), int(quiet[1])
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end       # 跨零点，如 23→8


def _map_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """事件 → 通知内容；不关心的事件返回 None。"""
    kind = str(event.get("kind") or "").strip()
    if kind == "proactive_send":
        status = str(event.get("status") or "").strip()
        text = str(event.get("text") or "").strip()
        if status in ("sent", "ok"):
            return {"level": "info", "title": "已发送",
                    "body": f"已发：{text[:30] or '(无文本)'}", "error": False}
        reason = str(event.get("error") or event.get("reason") or status or "unknown")
        return {"level": "error", "title": "发送失败",
                "body": f"未发送：{reason[:80]}", "error": True}
    if kind == "send_blocked":
        reason = str(event.get("reason") or event.get("error") or "blocked")
        return {"level": "error", "title": "未发送（被拦）",
                "body": f"未发送：{reason[:80]}", "error": True}
    if kind == "send_failed":
        reason = str(event.get("error_key") or event.get("error") or "failed")
        return {"level": "error", "title": "发送失败",
                "body": f"未发送：{reason[:80]}", "error": True}
    if kind == "proactive_discarded":
        reason = str(event.get("reason") or event.get("text") or "discarded")
        return {"level": "error", "title": "主动消息已丢弃",
                "body": f"未发送：{reason[:80]}", "error": True}
    if kind == "gateway_stale":
        idle = event.get("idle_s")
        suffix = f"（空闲 {idle}s）" if idle is not None else ""
        return {"level": "warn", "title": "网关可能卡住",
                "body": f"长时间没有活动{suffix}", "error": True}
    if kind == "proactive_breaker_open":
        consecutive = event.get("consecutive")
        key = str(event.get("error_key") or "")[:60]
        return {"level": "error", "title": "熔断：连续失败",
                "body": f"连续 {consecutive or '?'} 次同类失败{('：' + key) if key else ''}",
                "error": True}
    return None


def _notify_key(kind: str, mapped: dict[str, Any]) -> str:
    return f"{kind}|{mapped.get('title')}|{str(mapped.get('body'))[:60]}"


def collect_notifications(state: dict[str, Any] | None,
                          lines: list[Any], *, now: float | None = None,
                          quiet_hours: tuple[int, int] | None = None,
                          ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """P7-d 核心纯函数：把新的 gateway.log 行变成桌面通知（不弹窗、不写盘）。

    - 覆盖 5 类事件：正常发送 / 被拦/失败/丢弃 / 死区 / 熔断；
    - 相同事件（kind+标题+内容前 60 字）``NOTIFY_DEDUPE_S``（60s）内去重；
    - 夜间（``quiet_hours``，默认取 state 里的或 [23,8]）**只报错误**，不报正常发送；
    - 返回 ``(notifications, new_state)``，new_state 带 ``seen`` 去重表（保留 1h）。

    PS 只负责把 notifications 里的 title/body/level 显示成 balloon。
    """
    now = time.time() if now is None else float(now)
    state = dict(state or {})
    quiet = _normalise_quiet(quiet_hours)
    if quiet is None:
        quiet = _normalise_quiet(state.get("quiet_hours")) or DEFAULT_QUIET_HOURS
    seen_raw = state.get("seen")
    seen: dict[str, float] = {}
    if isinstance(seen_raw, dict):
        for key, value in seen_raw.items():
            try:
                seen[str(key)] = float(value)
            except (TypeError, ValueError):
                continue

    notifications: list[dict[str, Any]] = []
    for line in (lines or []):
        event = _parse_event(line)
        if event is None:
            continue
        kind = str(event.get("kind") or "").strip()
        mapped = _map_event(event)
        if mapped is None:
            continue
        ts = _event_ts(event, now)
        if _is_night(ts, quiet) and not mapped["error"]:
            continue                          # 夜间只报错误
        key = _notify_key(kind, mapped)
        last = float(seen.get(key) or 0.0)
        if ts - last < NOTIFY_DEDUPE_S:       # 60s 内相同事件去重
            continue
        seen[key] = ts
        notifications.append({"kind": kind, "ts": ts, "key": key,
                              "title": mapped["title"], "body": mapped["body"],
                              "level": mapped["level"]})

    cutoff = now - NOTIFY_SEEN_TTL_S
    seen = {key: value for key, value in seen.items() if value >= cutoff}
    new_state = dict(state)
    new_state["seen"] = seen
    new_state["updated_at"] = now
    new_state["quiet_hours"] = list(quiet) if quiet else None
    return notifications, new_state


def read_new_log_lines(log_path: Path | str, state: dict[str, Any] | None,
                       *, max_bytes: int = NOTIFY_TAIL_BYTES,
                       ) -> tuple[list[str], dict[str, Any]]:
    """按 state 里的 offset 增量读 gateway.log（文件被截断/轮转时自动从头）。

    首次（没有 offset）只读最近 ``max_bytes``，避免把历史全弹一遍；读不完的下次继续。
    """
    target = Path(log_path)
    new_state = dict(state or {})
    try:
        size = target.stat().st_size
    except OSError:
        return [], new_state
    offset = int(new_state.get("offset") or 0)
    if offset < 0 or offset > size:
        offset = 0
    tail_start = False
    if offset == 0 and size > max_bytes:
        offset = size - max_bytes
        tail_start = True                    # 从中间开始：先丢掉半行
    try:
        with target.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(max_bytes)
    except OSError:
        return [], new_state
    if tail_start and data:
        cut = data.find(b"\n")
        if cut < 0:
            return [], new_state             # 一个完整行都没有，等下次
        offset += cut + 1
        data = data[cut + 1:]
    consumed = len(data)
    if data and not data.endswith(b"\n"):
        cut = data.rfind(b"\n")
        if cut < 0:
            return [], new_state             # 还没读到完整行，等下次
        consumed = cut + 1
        data = data[:cut + 1]
    lines = [line for line in data.decode("utf-8", "replace").splitlines() if line.strip()]
    new_state["offset"] = offset + consumed
    return lines, new_state


def notifications_once(*, log_path: Path | str | None = None,
                       state_path: Path | str | None = None,
                       now: float | None = None) -> dict[str, Any]:
    """托盘内部用：轮询一次日志 → 返回通知 JSON 并推进 offset/去重 state。"""
    log = Path(log_path or NOTIFY_LOG_PATH)
    target = Path(state_path or NOTIFY_STATE_PATH)
    state = load_state(target)
    first_run = not state.get("offset")          # 第一次轮询：只保留近 10 分钟事件
    lines, state = read_new_log_lines(log, state)
    notifications, state = collect_notifications(
        state, lines, now=now, quiet_hours=_quiet_hours_from_config())
    if first_run:
        cutoff = (time.time() if now is None else float(now)) - 600.0
        notifications = [item for item in notifications
                         if float(item.get("ts") or 0) >= cutoff]
    try:
        save_state(state, target)
    except OSError:
        pass
    return {"ok": True, "notifications": notifications, "lines": len(lines),
            "offset": state.get("offset"), "log": str(log), "state": str(target)}


# --------------------------------------------------------------------------- #
# P7-d：托盘进程（托盘本体是 tray.ps1；这里只负责拉起/单实例）
# --------------------------------------------------------------------------- #
def _powershell_exe() -> str:
    return shutil.which("powershell") or "powershell.exe"


def _spawn_tray(*, no_notify: bool = False) -> Any:
    """后台拉起 PowerShell 托盘（隐藏窗口）；返回 Popen。"""
    if not TRAY_SCRIPT.is_file():
        raise LauncherError(f"托盘脚本不存在：{TRAY_SCRIPT}")
    argv = [_powershell_exe(), "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
            "-File", str(TRAY_SCRIPT),
            "-Python", sys.executable, "-RepoRoot", str(ROOT)]
    if no_notify:
        argv.append("-NoNotify")
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | \
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(argv, **kwargs)  # noqa: S603 - argv 由本文件构造


def ensure_tray(state: dict[str, Any], *, no_notify: bool = False) -> dict[str, Any]:
    """托盘单实例：state 里的 tray_pid 还活着就复用，否则拉起新托盘。"""
    pid = int(state.get("tray_pid") or 0)
    if pid and _pid_alive(pid):
        return {"started": False, "pid": pid}
    try:
        proc = _spawn_tray(no_notify=no_notify)
    except LauncherError as exc:
        return {"started": False, "pid": None, "error": str(exc)}
    new_pid = int(getattr(proc, "pid", 0) or 0)
    return {"started": True, "pid": new_pid}


# --------------------------------------------------------------------------- #
# P7-d：开机自启（用户级：启动目录 bat / 可选 HKCU Run；不需要管理员）
# --------------------------------------------------------------------------- #
AUTOSTART_NAME = "SoulCompanion"
AUTOSTART_BAT = "SoulCompanionAutostart.bat"
REG_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _startup_dir(startup_dir: Path | str | None = None) -> Path:
    if startup_dir:
        return Path(startup_dir)
    env = os.environ.get("WXBOX_STARTUP_DIR")
    if env:
        return Path(env)
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _pythonw_exe() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.is_file() else exe


def _autostart_argv() -> list[str]:
    return [str(_pythonw_exe()), str(ROOT / "tools" / "desktop" / "launcher.py"),
            "--tray", "--no-open"]


def _autostart_command() -> str:
    python, launcher = _autostart_argv()[0], _autostart_argv()[1]
    return f'"{python}" "{launcher}" --tray --no-open'


def _autostart_bat_text() -> str:
    python, launcher = _autostart_argv()[0], _autostart_argv()[1]
    return ("@echo off\r\n"
            f'cd /d "{ROOT}"\r\n'
            f'start "" "{python}" "{launcher}" --tray --no-open\r\n')


def _autostart_bat_path(startup_dir: Path | str | None = None) -> Path:
    return _startup_dir(startup_dir) / AUTOSTART_BAT


def _reg_set(command: str) -> None:
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY) as key:
        winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, str(command))


def _reg_get() -> str | None:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, AUTOSTART_NAME)[0])
    except (OSError, ImportError):
        return None


def _reg_delete() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, AUTOSTART_NAME)
        return True
    except (OSError, ImportError):
        return False


def autostart_status(*, startup_dir: Path | str | None = None) -> dict[str, Any]:
    bat = _autostart_bat_path(startup_dir)
    try:
        bat_installed = bat.is_file()
    except OSError:
        bat_installed = False
    registry_value = _reg_get()
    return {"ok": True, "startup_dir": str(_startup_dir(startup_dir)),
            "startup_bat": str(bat), "bat_installed": bat_installed,
            "registry_name": AUTOSTART_NAME,
            "registry_value": registry_value,
            "registry_installed": bool(registry_value),
            "command": _autostart_command()}


def autostart_install(*, use_registry: bool = False,
                      startup_dir: Path | str | None = None) -> dict[str, Any]:
    """安装开机自启：默认写「启动」目录 bat；``use_registry=True`` 写 HKCU Run。"""
    bat = _autostart_bat_path(startup_dir)
    if use_registry:
        try:
            _reg_set(_autostart_command())
        except (OSError, ImportError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "method": "registry"}
        method = "registry"
        target = AUTOSTART_NAME
    else:
        try:
            bat.parent.mkdir(parents=True, exist_ok=True)
            bat.write_text(_autostart_bat_text(), encoding="utf-8")
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                    "method": "startup"}
        method = "startup"
        target = str(bat)
    result = {"ok": True, "method": method, "target": target}
    result.update(autostart_status(startup_dir=startup_dir))
    return result


def autostart_uninstall(*, startup_dir: Path | str | None = None) -> dict[str, Any]:
    """卸载开机自启（两种方式都清）；返回删掉了什么。"""
    bat = _autostart_bat_path(startup_dir)
    removed_bat = False
    try:
        if bat.is_file():
            bat.unlink()
            removed_bat = True
    except OSError:
        removed_bat = False
    removed_registry = _reg_delete()
    result = {"ok": True, "removed_bat": removed_bat,
              "removed_registry": removed_registry}
    result.update(autostart_status(startup_dir=startup_dir))
    return result


# --------------------------------------------------------------------------- #
# state（单实例 / 只杀自有 PID）
# --------------------------------------------------------------------------- #
def load_state(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path or STATE_PATH)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any], path: Path | str | None = None) -> Path:
    target = Path(path or STATE_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in state.items() if not key.startswith("_")}
    window = payload.get("window")
    if isinstance(window, dict):
        payload["window"] = {k: v for k, v in window.items() if not k.startswith("_")}
    tmp = target.with_name(target.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target)
    return target


def clear_state(path: Path | str | None = None) -> None:
    try:
        Path(path or STATE_PATH).unlink(missing_ok=True)
    except OSError:
        pass


def owned_pids(state: dict[str, Any]) -> dict[str, int]:
    """state 里由本启动器拉起的 PID（不含复用别人的服务）。"""
    pids: dict[str, int] = {}
    started = state.get("started_pids") or {}
    if isinstance(started, dict):
        for name, pid in started.items():
            try:
                if pid:
                    pids[str(name)] = int(pid)
            except (TypeError, ValueError):
                continue
    window = state.get("window") or {}
    try:
        if isinstance(window, dict) and window.get("pid"):
            pids["window"] = int(window["pid"])
    except (TypeError, ValueError):
        pass
    # P7-d：托盘也是本启动器拉起的进程，--stop/--exit-with-window 一起收
    try:
        if state.get("tray_pid"):
            pids["tray"] = int(state["tray_pid"])
    except (TypeError, ValueError):
        pass
    return pids


def another_instance_running(state: dict[str, Any]) -> bool:
    """单实例判定：launcher 还活着，或本启动器拉起的服务还有存活 PID。"""
    try:
        launcher = int(state.get("launcher_pid") or 0)
    except (TypeError, ValueError):
        launcher = 0
    if launcher and _pid_alive(launcher):
        return True
    return any(_pid_alive(pid) for pid in owned_pids(state).values())


def stop_owned(state: dict[str, Any], state_path: Path | str | None = None) -> dict[str, Any]:
    """只停 state 里记录的、本启动器拉起的 PID；别人的 801/802 一律不动。"""
    stopped: dict[str, int] = {}
    skipped: dict[str, int] = {}
    for name, pid in owned_pids(state).items():
        if _pid_alive(pid) and _terminate_pid(pid):
            stopped[name] = pid
        else:
            skipped[name] = pid
    clear_state(state_path)
    return {"ok": True, "stopped": stopped, "skipped": skipped}


# --------------------------------------------------------------------------- #
# --status
# --------------------------------------------------------------------------- #
def status_report(state: dict[str, Any] | None = None,
                  state_path: Path | str | None = None) -> dict[str, Any]:
    state = state if state is not None else load_state(state_path)
    owned = owned_pids(state)
    services: dict[str, Any] = {}
    for name, spec in service_commands().items():
        port = int(spec["port"])
        open_ = port_open(port)
        services[name] = {
            "port": port,
            "port_open": bool(open_),
            "healthy": bool(open_ and http_ok(str(spec["health"]))),
            "owned_pid": owned.get(name),
            "owned_alive": bool(owned.get(name) and _pid_alive(owned[name])),
        }
    window = state.get("window") or {}
    try:
        tray_pid = int(state.get("tray_pid") or 0)
    except (TypeError, ValueError):
        tray_pid = 0
    return {"ok": True, "state_path": str(state_path or STATE_PATH),
            "state": state or None, "services": services,
            "window": window or None,
            "tray": {"pid": tray_pid or None,
                     "alive": bool(tray_pid and _pid_alive(tray_pid)),
                     "no_notify": bool(state.get("tray_no_notify"))},
            "notify": {"state_path": str(NOTIFY_STATE_PATH),
                       "log_path": str(NOTIFY_LOG_PATH)},
            "running": any(item["port_open"] for item in services.values())}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python tools/desktop/launcher.py",
        description="零依赖桌面启动器：确保 801/802/803 在跑，并用 Edge app 模式打开控制台/工作台窗口")
    parser.add_argument("--no-open", action="store_true", help="只起服务，不打开窗口")
    parser.add_argument("--open-workbench", action="store_true",
                        help="打开 803 工作台窗口（默认打开 802 控制台）")
    parser.add_argument("--stop", action="store_true", help="停掉本启动器拉起的服务/窗口")
    parser.add_argument("--status", action="store_true", help="打印 JSON 状态后退出")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--persist", dest="persist", action="store_true",
                       help="窗口关闭后保留服务（默认）")
    group.add_argument("--exit-with-window", dest="persist", action="store_false",
                       help="窗口关闭后停掉本启动器拉起的服务")
    parser.set_defaults(persist=True)
    parser.add_argument("--start-timeout", type=float, default=START_TIMEOUT_S,
                        help=f"单个服务启动超时秒数（默认 {START_TIMEOUT_S:.0f}）")
    parser.add_argument("--log-dir", default=None,
                        help=f"服务日志目录（默认 {DEFAULT_LOG_DIR}，不可写时回退 {FALLBACK_LOG_DIR}）")
    parser.add_argument("--state-path", default=None, help="state 文件路径（测试用）")
    parser.add_argument("--profile-dir", default=None, help="浏览器 user-data-dir（默认桌面专用目录）")
    # ---- P7-d 桌面增强 ----
    parser.add_argument("--tray", action="store_true",
                        help="确保 801/802 后拉起系统托盘（tray.ps1；单实例）")
    parser.add_argument("--no-notify", action="store_true",
                        help="关闭桌面通知（托盘不轮询 gateway.log）")
    parser.add_argument("--notify-once", action="store_true",
                        help="轮询一次 gateway.log，输出通知 JSON（托盘内部调用）")
    parser.add_argument("--notify-log", default=None, help="通知轮询的日志路径（测试用）")
    parser.add_argument("--notify-state", default=None, help="通知 offset/去重 state 路径（测试用）")
    parser.add_argument("--autostart", choices=("install", "uninstall", "status"),
                        default=None, help="开机自启：install/uninstall/status")
    parser.add_argument("--autostart-registry", action="store_true",
                        help="自启写 HKCU Run（默认写「启动」目录 bat）")
    return parser.parse_args(argv)


def _start_services(log_dir: Path, start_timeout: float,
                    started_pids: dict[str, int] | None = None) -> dict[str, Any]:
    """起服务并把"本启动器拉起的 PID"记录进 ``started_pids``（出错回滚用）。"""
    if started_pids is None:
        started_pids = {}
    specs = service_commands()
    results: dict[str, Any] = {}
    for name in ("gateway", "console", "workbench"):
        result = ensure_service(name, specs[name], log_dir, start_timeout=start_timeout)
        results[name] = result
        if result.get("started") and result.get("pid"):
            started_pids[name] = int(result["pid"])
        if result.get("started"):
            print(f"[desktop] {name} 已启动 pid={result['pid']} "
                  f"(端口 {result['port']}, 日志 {log_dir / specs[name]['log']})")
        else:
            print(f"[desktop] {name} 已在运行 (端口 {result['port']})，复用")
    return results


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = Path(args.state_path) if args.state_path else STATE_PATH
    profile_dir = Path(args.profile_dir) if args.profile_dir else PROFILE_DIR

    # ---- P7-d：开机自启（纯文件/HKCU，不要求管理员） ----
    if args.autostart:
        if args.autostart == "install":
            result = autostart_install(use_registry=bool(args.autostart_registry))
        elif args.autostart == "uninstall":
            result = autostart_uninstall()
        else:
            result = autostart_status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    # ---- P7-d：通知轮询一次（托盘定时调用；PowerShell 只负责显示） ----
    if args.notify_once:
        result = notifications_once(log_path=args.notify_log,
                                    state_path=args.notify_state)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 0

    if args.status:
        print(json.dumps(status_report(load_state(state_path), state_path),
                         ensure_ascii=False, indent=2))
        return 0

    if args.stop:
        state = load_state(state_path)
        if not state:
            print(json.dumps({"ok": True, "stopped": {},
                              "note": "没有 state 文件（可能没通过桌面启动器起过服务）"},
                             ensure_ascii=False, indent=2))
            return 0
        result = stop_owned(state, state_path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    # 单实例：已有 launcher/自有服务在跑 → 只重新打开窗口（并按需确保托盘）
    state = load_state(state_path)
    if another_instance_running(state):
        print("[desktop] 已有实例在运行：只重新打开窗口，不重复起服务")
        if args.tray:
            tray = ensure_tray(state, no_notify=args.no_notify)
            state["tray_pid"] = tray.get("pid") or state.get("tray_pid")
            state["tray_no_notify"] = bool(args.no_notify)
            if tray.get("started"):
                print(f"[desktop] 托盘已启动 pid={tray.get('pid')}")
            elif tray.get("error"):
                print(f"[desktop] 托盘启动失败：{tray['error']}", file=sys.stderr)
            else:
                print(f"[desktop] 托盘已在运行 pid={tray.get('pid')}")
        if not args.no_open:
            target = WORKBENCH_URL if args.open_workbench else CONSOLE_URL
            window = launch_window(url=target, profile_dir=profile_dir)
            state["window"] = window
            state["launcher_pid"] = os.getpid()
            print(f"[desktop] 窗口已打开（{window['mode']} app 模式）：{target}")
        save_state(state, state_path)
        return 0

    log_dir = resolve_log_dir(args.log_dir)
    print(f"[desktop] 日志目录：{log_dir}")
    started_pids: dict[str, int] = {}
    try:
        services = _start_services(log_dir, args.start_timeout, started_pids)
    except LauncherError as exc:
        print(f"[desktop] 启动失败：{exc}", file=sys.stderr)
        if started_pids:
            stop_owned({"started_pids": started_pids}, state_path)
            print("[desktop] 已回滚本次拉起的服务", file=sys.stderr)
        return 2

    state = {
        "launcher_pid": os.getpid(),
        "started_at": time.time(),
        "started_pids": started_pids,
        "services": services,
        "log_dir": str(log_dir),
        "profile_dir": str(profile_dir),
        "window": None,
        "tray_pid": None,
        "tray_no_notify": bool(args.no_notify),
    }

    # ---- P7-d：托盘（单实例；--no-notify 时托盘不轮询通知） ----
    if args.tray:
        tray = ensure_tray(state, no_notify=args.no_notify)
        if tray.get("pid"):
            state["tray_pid"] = tray["pid"]
        if tray.get("started"):
            print(f"[desktop] 托盘已启动 pid={tray.get('pid')}")
        elif tray.get("error"):
            print(f"[desktop] 托盘启动失败：{tray['error']}", file=sys.stderr)
        else:
            print(f"[desktop] 托盘已在运行 pid={tray.get('pid')}")
    save_state(state, state_path)

    if args.no_open:
        print(f"[desktop] 服务已就绪（--no-open 未开窗）：{CONSOLE_URL}")
        state["launcher_pid"] = None
        save_state(state, state_path)
        return 0

    target = WORKBENCH_URL if args.open_workbench else CONSOLE_URL
    try:
        window = launch_window(url=target, profile_dir=profile_dir)
    except LauncherError as exc:
        print(f"[desktop] 打开窗口失败：{exc}", file=sys.stderr)
        if started_pids:
            stop_owned({"started_pids": started_pids}, state_path)
            print("[desktop] 已回滚本次拉起的服务", file=sys.stderr)
        return 2

    state["window"] = window
    save_state(state, state_path)
    print(f"[desktop] 窗口已打开（{window['mode']} app 模式）：{target}")
    print("[desktop] 关闭窗口后服务默认保留；要一起收尾用 --exit-with-window，"
          "手动停服务用 --stop")

    if args.persist:
        state["launcher_pid"] = None
        save_state(state, state_path)
        return 0

    # --exit-with-window：等窗口关闭，只停自己拉起的服务
    closed = wait_window_closed(window)
    if not closed:
        print("[desktop] 无法确认窗口状态（浏览器进程已交出去且查不到），"
              "为安全起见保留服务；稍后可用 --stop 手动收尾")
        return 0
    result = stop_owned(state, state_path)
    print(f"[desktop] 窗口已关闭，已停止本启动器拉起的服务：{result['stopped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
