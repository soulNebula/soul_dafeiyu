# -*- coding: utf-8 -*-
"""激活一个 Bot，并按 Bot 的数据根重启 801/802。

由 802 的「加载 Bot」按钮以独立进程调用（`--restart`）：先准备 `<bot>/data`、写活跃指针，
再结束旧 801/802，然后用 Bot 环境变量启动新进程；802 页面轮询健康检查。

用法：
    python tools/bot_activate.py --bot E:/bots/xiaoyu --restart
    python tools/bot_activate.py --bot ... --activate-only   # 只准备/写指针，不起服务（测试）
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agi import bot_runtime  # noqa: E402
from tools import bot_profile  # noqa: E402

GATEWAY_PORT = 801
CONSOLE_PORT = 802


def _is_windows() -> bool:
    return os.name == "nt"


def _port_pids(port: int) -> list[int]:
    """当前监听该端口的 PID（Windows netstat 解析，避免额外依赖）。"""
    pids: set[int] = set()
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="replace").stdout
    except Exception:  # noqa: BLE001
        return []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() not in ("TCP",):
            continue
        local, state, pid = parts[1], parts[-2], parts[-1]
        if state.upper() != "LISTENING":
            continue
        if local.endswith(f":{port}"):
            try:
                pids.add(int(pid))
            except ValueError:
                pass
    return sorted(pids)


def _kill_port(port: int) -> list[int]:
    killed = []
    for pid in _port_pids(port):
        if pid == os.getpid():
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=15)
            killed.append(pid)
        except Exception:  # noqa: BLE001
            pass
    return killed


def _python() -> str:
    env = str(os.environ.get("SOULAGENT_PYTHON") or "").strip()
    if env:
        return env
    if not getattr(sys, "frozen", False):
        return sys.executable
    default = Path(r"C:\Python314\python.exe")
    return str(default) if default.is_file() else "python"


def _wait_health(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + max(3.0, timeout)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= int(getattr(response, "status", 200)) < 300:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


BOT_ENV_KEYS = ("SOULAGENT_DATA_DIR", "SOULAGENT_LOG_DIR", "SOULAGENT_WEB_CONFIG",
                "SOULAGENT_CONFIG_PATH", "SOULAGENT_SECRETS")


def restart_services(runtime: dict, *, python: str | None = None,
                     timeout: float = 30.0) -> dict:
    """结束旧 801/802 并以 Bot 环境变量重启；返回结果（不抛）。"""
    py = python or _python()
    env = bot_runtime.apply_env(runtime["env"])
    log_dir = Path(runtime["logs"])
    log_dir.mkdir(parents=True, exist_ok=True)
    killed = {"gateway": _kill_port(GATEWAY_PORT), "console": _kill_port(CONSOLE_PORT)}
    time.sleep(1.0)
    flags = 0
    if _is_windows():
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    def spawn(cmd: list[str], name: str) -> subprocess.Popen:
        log = log_dir / f"bot_{name}.log"
        handle = log.open("ab")
        try:
            return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                    stdout=handle, stderr=subprocess.STDOUT,
                                    creationflags=flags)
        finally:
            handle.close()

    gateway = spawn([py, "-m", "agi.wechat_gateway.server"], "gateway")
    console = spawn([py, str(ROOT / "tools" / "console" / "server.py"),
                     "--port", str(CONSOLE_PORT)], "console")
    ok_gateway = _wait_health(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout)
    ok_console = _wait_health(f"http://127.0.0.1:{CONSOLE_PORT}/api/health", timeout)
    return {"ok": ok_gateway and ok_console, "gateway": ok_gateway, "console": ok_console,
            "killed": killed, "gateway_pid": gateway.pid, "console_pid": console.pid}


def clear_active(*, restart: bool = True, python: str | None = None,
                 timeout: float = 30.0, delay: float = 0.0) -> dict:
    """取消激活，回到仓库默认数据根（默认 agi/data），重启 801/802。"""
    bot_runtime.clear_active()
    bot_runtime.write_switch_status({"state": "restarting", "bot": None})
    result: dict = {"ok": True, "active": None}
    if not restart:
        return result
    if delay > 0:
        time.sleep(float(delay))
    py = python or _python()
    env = dict(os.environ)
    for key in BOT_ENV_KEYS:
        env.pop(key, None)
    log_dir = bot_runtime.GLOBAL_DIR / "desktop_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    killed = {"gateway": _kill_port(GATEWAY_PORT), "console": _kill_port(CONSOLE_PORT)}
    time.sleep(1.0)
    flags = 0
    if _is_windows():
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))

    def spawn(cmd: list[str], name: str) -> subprocess.Popen:
        handle = (log_dir / f"default_{name}.log").open("ab")
        try:
            return subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                    stdout=handle, stderr=subprocess.STDOUT,
                                    creationflags=flags)
        finally:
            handle.close()

    gateway = spawn([py, "-m", "agi.wechat_gateway.server"], "gateway")
    console = spawn([py, str(ROOT / "tools" / "console" / "server.py"),
                     "--port", str(CONSOLE_PORT)], "console")
    ok_gateway = _wait_health(f"http://127.0.0.1:{GATEWAY_PORT}/health", timeout)
    ok_console = _wait_health(f"http://127.0.0.1:{CONSOLE_PORT}/api/health", timeout)
    result.update({"gateway": ok_gateway, "console": ok_console, "killed": killed,
                   "gateway_pid": gateway.pid, "console_pid": console.pid,
                   "ok": ok_gateway and ok_console})
    bot_runtime.write_switch_status({"state": "ready" if result["ok"] else "failed",
                                     "bot": None, "detail": result})
    return result


def activate(bot_path: str, *, restart: bool = True, python: str | None = None,
             timeout: float = 30.0, delay: float = 0.0) -> dict:
    validation = bot_profile.validate_bot(bot_path)
    if not validation.get("ok"):
        return {"ok": False, "error": "invalid_bot", "validation": validation}
    bot = bot_profile.load_bot(bot_path)
    runtime = bot_runtime.prepare_runtime(bot_path, bot=bot)
    active = bot_runtime.write_active(bot_path, bot=bot)
    result: dict = {"ok": True, "active": active, "runtime": runtime}
    if restart:
        if delay > 0:
            time.sleep(float(delay))
        bot_runtime.write_switch_status({"state": "restarting", "bot": active})
        switched = restart_services(runtime, python=python, timeout=timeout)
        result.update(switched)
        bot_runtime.write_switch_status({
            "state": "ready" if switched.get("ok") else "failed",
            "bot": active, "detail": switched,
        })
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="激活 Bot 并重启 801/802")
    parser.add_argument("--bot", default="", help="Bot 目录（--clear 时可省略）")
    parser.add_argument("--clear", action="store_true", help="取消 Bot，回到默认数据")
    parser.add_argument("--restart", action="store_true", help="重启 801/802（默认只准备）")
    parser.add_argument("--activate-only", action="store_true", help="只准备并写活跃指针")
    parser.add_argument("--python", default="", help="Python 解释器（默认当前/ C:\\Python314）")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--delay", type=float, default=0.0,
                        help="重启前等待秒数（给 802 返回响应/页面轮询留时间）")
    args = parser.parse_args(argv)
    do_restart = bool(args.restart and not args.activate_only)
    if args.clear:
        result = clear_active(restart=do_restart, python=args.python or None,
                              timeout=args.timeout, delay=max(0.0, float(args.delay)))
    else:
        if not args.bot:
            parser.error("--bot 必填（除非 --clear）")
        result = activate(args.bot, restart=do_restart,
                          python=args.python or None, timeout=args.timeout,
                          delay=max(0.0, float(args.delay)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
