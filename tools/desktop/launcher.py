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

ROOT = Path(__file__).resolve().parents[2]

#: 单实例/归属状态：只记录"本启动器拉起"的 PID，--stop 只杀这些
STATE_PATH = ROOT / "agi" / "data" / "desktop_state.json"
#: Edge/Chrome 的 app 模式独立 user-data-dir（不污染用户日常浏览数据）
PROFILE_DIR = ROOT / "agi" / "data" / "desktop_profile"

GATEWAY_HOST = "127.0.0.1"
GATEWAY_PORT = 801
CONSOLE_PORT = 802
CONSOLE_URL = f"http://{GATEWAY_HOST}:{CONSOLE_PORT}/"
GATEWAY_HEALTH = f"http://{GATEWAY_HOST}:{GATEWAY_PORT}/health"
CONSOLE_HEALTH = f"http://{GATEWAY_HOST}:{CONSOLE_PORT}/api/health"
WINDOW_SIZE = "1440,900"

#: E:\agent\logs 是约定位置（和训练/评测日志放一起）；不可写时回退仓库内目录
DEFAULT_LOG_DIR = Path(os.environ.get("WXBOX_DESKTOP_LOG_DIR") or r"E:\agent\logs")
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


def _spawn_service(cmd: list[str], log_path: Path) -> Any:
    """拉起后台服务：stdout/stderr 追加到日志，不弹新控制台窗口。"""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "ab", buffering=0)  # noqa: SIM115 - 交给子进程继承后关闭
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
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
    """打开浏览器窗口（app 模式）；窗口进程不需要日志重定向。"""
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
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
def service_commands() -> dict[str, dict[str, Any]]:
    """两个后台服务的启动方式（都用当前 Python 解释器，零额外依赖）。"""
    return {
        "gateway": {
            "port": GATEWAY_PORT,
            "health": GATEWAY_HEALTH,
            "log": "desktop_gateway.log",
            "cmd": [sys.executable, "-m", "agi.wechat_gateway.server"],
        },
        "console": {
            "port": CONSOLE_PORT,
            "health": CONSOLE_HEALTH,
            "log": "desktop_console.log",
            "cmd": [sys.executable, str(ROOT / "tools" / "console" / "server.py"),
                    "--port", str(CONSOLE_PORT)],
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
    proc = _spawn_service(list(spec["cmd"]), log_path)
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
    return [str(browser), f"--app={url}", f"--window-size={WINDOW_SIZE}",
            f"--user-data-dir={Path(profile_dir)}"]


def launch_window(*, url: str = CONSOLE_URL, profile_dir: Path | str | None = None,
                  browser: Path | str | None = None) -> dict[str, Any]:
    """打开控制台的独立窗口；返回窗口信息（含内部 ``_proc`` 供等待用，落盘时剔除）。"""
    profile = Path(profile_dir or PROFILE_DIR)
    profile.mkdir(parents=True, exist_ok=True)
    if browser is not None:
        path, mode = Path(browser), "custom"
    else:
        path, mode = find_browser()
    if path is not None:
        argv = build_browser_argv(path, url, profile)
        proc = _popen_browser(argv)
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
    return {"ok": True, "state_path": str(state_path or STATE_PATH),
            "state": state or None, "services": services,
            "window": window or None,
            "running": any(item["port_open"] for item in services.values())}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python tools/desktop/launcher.py",
        description="零依赖桌面启动器：确保 801/802 在跑，并用 Edge app 模式打开控制台窗口")
    parser.add_argument("--no-open", action="store_true", help="只起服务，不打开窗口")
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
    return parser.parse_args(argv)


def _start_services(log_dir: Path, start_timeout: float,
                    started_pids: dict[str, int] | None = None) -> dict[str, Any]:
    """起服务并把"本启动器拉起的 PID"记录进 ``started_pids``（出错回滚用）。"""
    if started_pids is None:
        started_pids = {}
    specs = service_commands()
    results: dict[str, Any] = {}
    for name in ("gateway", "console"):
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

    # 单实例：已有 launcher/自有服务在跑 → 只重新打开窗口
    state = load_state(state_path)
    if another_instance_running(state):
        print("[desktop] 已有实例在运行：只重新打开窗口，不重复起服务")
        if not args.no_open:
            window = launch_window(url=CONSOLE_URL, profile_dir=profile_dir)
            state["window"] = window
            state["launcher_pid"] = os.getpid()
            save_state(state, state_path)
            print(f"[desktop] 窗口已打开（{window['mode']} app 模式）：{CONSOLE_URL}")
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
    }
    save_state(state, state_path)

    if args.no_open:
        print(f"[desktop] 服务已就绪（--no-open 未开窗）：{CONSOLE_URL}")
        state["launcher_pid"] = None
        save_state(state, state_path)
        return 0

    try:
        window = launch_window(url=CONSOLE_URL, profile_dir=profile_dir)
    except LauncherError as exc:
        print(f"[desktop] 打开窗口失败：{exc}", file=sys.stderr)
        if started_pids:
            stop_owned({"started_pids": started_pids}, state_path)
            print("[desktop] 已回滚本次拉起的服务", file=sys.stderr)
        return 2

    state["window"] = window
    save_state(state, state_path)
    print(f"[desktop] 窗口已打开（{window['mode']} app 模式）：{CONSOLE_URL}")
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
