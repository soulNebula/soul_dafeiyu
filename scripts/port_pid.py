# -*- coding: utf-8 -*-
"""端口就绪/PID 辅助（启动/停止脚本用，纯标准库）。

用法：
    python scripts/port_pid.py 803      # 端口在监听则打印 PID（可能为空），exit 0
    python scripts/port_pid.py 803 --check  # 只检查，不打印
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys


def _listening_pid(port: int) -> int | None:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    pattern = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)", re.I)
    for line in out.splitlines():
        match = pattern.search(line)
        if match:
            return int(match.group(1))
    return None


def _connected(port: int) -> bool:
    sock = socket.socket()
    sock.settimeout(0.4)
    try:
        sock.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def main() -> int:
    port = int(sys.argv[1])
    args = sys.argv[2:]
    check_only = "--check" in args
    kill = "--kill" in args
    dry = "--dry-run" in args
    if not _connected(port):
        if kill:
            print(f"[{port}] 未在监听，跳过")
            return 0
        return 1
    pid = _listening_pid(port)
    if kill:
        if pid is None:
            print(f"[{port}] 端口在监听但取不到 PID")
            return 1
        if dry:
            print(f"[{port}] [dry-run] 将停止 PID {pid}")
            return 0
        print(f"[{port}] PID {pid} 停止...")
        result = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        if result.returncode != 0:
            print(f"[{port}] 停止失败：{(result.stdout or result.stderr).strip()[:160]}")
            return 1
        print(f"[{port}] 已停止")
        return 0
    if pid and not check_only:
        print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
