# -*- coding: utf-8 -*-
"""等待本机端口就绪（启动脚本用；纯标准库）。"""
from __future__ import annotations

import socket
import sys
import time


def main() -> int:
    port = int(sys.argv[1])
    timeout = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = socket.socket()
        sock.settimeout(0.4)
        try:
            sock.connect(("127.0.0.1", port))
            return 0
        except OSError:
            time.sleep(0.5)
        finally:
            sock.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
