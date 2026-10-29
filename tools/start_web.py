# -*- coding: utf-8 -*-
"""Web 端启动器：由 启动.bat 调用，启动 803 + 802 并打开 803。

不用 PowerShell / start 嵌套引号；所有子进程用 Python 的 Popen 分离启动。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0)
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False


def wait_health(url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                if 200 <= int(getattr(response, "status", 200)) < 300:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
    return False


def spawn(cmd: list[str], cwd: Path, log: Path) -> subprocess.Popen:
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = None
    for attempt in range(5):
        try:
            handle = log.open("ab")
            break
        except PermissionError:
            time.sleep(0.4)
    if handle is None:
        print(f"[web] 日志不可写，改用 DEVNULL：{log}")
        handle = open(os.devnull, "ab")
    try:
        return subprocess.Popen(
            cmd, cwd=str(cwd), env=os.environ.copy(),
            stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        )
    finally:
        handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--agent-home", default=os.environ.get("AGENT_HOME", r"E:\agent"))
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    home = Path(args.agent_home).resolve()
    env = os.environ.copy()
    env["AGENT_HOME"] = str(home)
    os.environ["AGENT_HOME"] = str(home)
    logs = home / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    main_py = root / ".venv" / "Scripts" / "python.exe"
    idea_py = root / "idea" / ".venv" / "Scripts" / "python.exe"
    if not main_py.is_file() or not idea_py.is_file():
        print(f"[web] 缺少 venv：{main_py} / {idea_py}")
        return 1

    stamp = int(time.time())
    log803 = logs / f"workbench_boot_{stamp}.log"
    log802 = logs / f"console_boot_{stamp}.log"
    log801 = logs / f"gateway_boot_{stamp}.log"
    started = []
    if port_open(803):
        print("[web] 803 已在运行")
    else:
        print("[web] 启动 803...")
        spawn([str(idea_py), "-m", "uvicorn", "app.main:app",
               "--host", "127.0.0.1", "--port", "803"],
              root / "idea", log803)
        started.append("803")
    if port_open(801):
        print("[web] 801 已在运行")
    else:
        print("[web] 启动 801 网关...")
        spawn([str(main_py), "-m", "agi.wechat_gateway.server"], root, log801)
    if port_open(802):
        print("[web] 802 已在运行")
    else:
        print("[web] 启动 802...")
        spawn([str(main_py), str(root / "tools" / "console" / "server.py"),
               "--port", "802"], root, log802)
        started.append("802")

    ok803 = wait_health("http://127.0.0.1:803/api/wb/health", 45.0)
    ok801 = wait_health("http://127.0.0.1:801/health", 30.0)
    ok802 = wait_health("http://127.0.0.1:802/api/health", 30.0)
    print(f"[web] 803 health: {'OK' if ok803 else 'FAIL'}")
    print(f"[web] 801 health: {'OK' if ok801 else 'FAIL'}")
    print(f"[web] 802 health: {'OK' if ok802 else 'FAIL'}")
    if not ok803:
        _tail(log803)
    if not ok801:
        _tail(log801)
    if not ok802:
        _tail(log802)
    if ok803 and not args.no_open:
        try:
            webbrowser.open("http://127.0.0.1:803/")
        except Exception:  # noqa: BLE001
            pass
    return 0 if (ok803 and ok801 and ok802) else 1


def _tail(path: Path, lines: int = 20) -> None:
    try:
        data = path.read_text(encoding="utf-8", errors="replace").splitlines()
        print(f"[web] --- {path} ---")
        print("\n".join(data[-lines:]))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
