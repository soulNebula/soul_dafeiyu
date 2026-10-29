# -*- coding: utf-8 -*-
"""旧版 Web 启动器：801 + 802 + 803（不依赖 Bot 功能）。

由根目录 启动Web.bat / 启动小鱼Web.vbs 调用；只启动缺失的服务，然后打开 803。
"""
from __future__ import annotations

import os
import socket
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = Path(os.environ.get("AGENT_HOME") or r"E:\agent")
LOGS = HOME / "logs"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def spawn(cmd: list[str], log_name: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    handle = open(LOGS / log_name, "ab")
    try:
        subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         stdout=handle, stderr=subprocess.STDOUT,
                         creationflags=CREATE_NO_WINDOW)
    finally:
        handle.close()


def wait(url: str, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if int(getattr(resp, "status", 200)) < 400:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.6)
    return False


def main() -> int:
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    idea_py = ROOT / "idea" / ".venv" / "Scripts" / "python.exe"
    if not py.is_file() or not idea_py.is_file():
        print("缺少 venv，先按 docs/guides/新用户上手.md 建环境")
        return 1
    if not port_open(801):
        print("启动 801...")
        spawn([str(py), "-m", "agi.wechat_gateway.server"], "old801.log")
    else:
        print("801 已在运行")
    if not port_open(802):
        print("启动 802...")
        spawn([str(py), str(ROOT / "tools" / "console" / "server.py"), "--port", "802"],
              "old802.log")
    else:
        print("802 已在运行")
    if not port_open(803):
        print("启动 803...")
        spawn([str(idea_py), "-m", "uvicorn", "app.main:app",
               "--host", "127.0.0.1", "--port", "803",
               "--app-dir", str(ROOT / "idea")], "old803.log")
    else:
        print("803 已在运行")
    ok = all(wait(url, 40) for url in (
        "http://127.0.0.1:801/health",
        "http://127.0.0.1:802/api/health",
        "http://127.0.0.1:803/health",
    ))
    print("801/802/803", "OK" if ok else "部分失败")
    if ok:
        webbrowser.open("http://127.0.0.1:803/")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
