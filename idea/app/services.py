# -*- coding: utf-8 -*-
"""服务管理：gateway 801 / console 802 / soul 8123 / local3b 8125。

只用标准库：端口探测用 socket，PID 用 netstat 解析，启停用 subprocess。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from pathlib import Path

from . import paths

PY = os.environ.get("IDEA_PYTHON", r"C:\Python314\python.exe")
_AGENT_ROOT = paths.agent_home()
LLAMA = os.environ.get("LLAMA_SERVER", str(_AGENT_ROOT / "llama" / "cuda" / "llama-server.exe"))
SOUL_GGUF = os.environ.get("SOUL_GGUF",
                           str(_AGENT_ROOT / "output" / "gguf" / "soul_qwen25_05b_v1b-q4_k_m.gguf"))


def _defs() -> dict:
    home = paths.soulagent_home()
    logs = paths.logs_dir()
    return {
        "gateway": {
            "label": "微信网关", "port": 801,
            "cmd": [PY, "-m", "agi.wechat_gateway.server"],
            "cwd": str(home), "log": str(logs / "gateway_run.log"),
        },
        "console": {
            "label": "控制台", "port": 802,
            "cmd": [PY, "tools/console/server.py", "--port", "802"],
            "cwd": str(home), "log": str(logs / "console_802.log"),
        },
        "soul": {
            "label": "灵魂模型 0.5B", "port": 8123,
            "cmd": [LLAMA, "-m", SOUL_GGUF, "--port", "8123", "--host", "127.0.0.1",
                    "-ngl", "99", "-c", "4096"],
            "cwd": str(logs), "log": str(logs / "llama_8123.log"),
        },
        "local3b": {
            "label": "本地 3B", "port": 8125,
            "cmd": [PY, "tools/soul_serve/serve_local3b.py"],
            "cwd": str(home), "log": str(logs / "llama_8125.log"),
        },
    }


SERVICES = _defs()


def list_services() -> dict:
    items = []
    for name, cfg in SERVICES.items():
        running, pid = port_state(cfg["port"])
        items.append({
            "name": name, "label": cfg["label"], "port": cfg["port"],
            "running": running, "pid": pid, "log": cfg["log"],
            "cmd": " ".join(cfg["cmd"]),
        })
    return {"ok": True, "items": items}


def port_state(port: int) -> tuple[bool, int | None]:
    """返回 (是否在监听, PID)。优先 netstat 取 PID，失败则 socket 探测。"""
    pid = _pid_for_port(port)
    if pid is not None:
        return True, pid
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True, None
    except OSError:
        return False, None


def _pid_for_port(port: int) -> int | None:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    pat = re.compile(rf"127\.0\.0\.1:{port}\s+\S+\s+LISTENING\s+(\d+)")
    for line in out.splitlines():
        m = pat.search(line)
        if m:
            return int(m.group(1))
    return None


def start(name: str) -> dict:
    cfg = SERVICES.get(name)
    if not cfg:
        return {"ok": False, "error": "unknown_service", "name": name}
    running, pid = port_state(cfg["port"])
    if running:
        return {"ok": True, "already_running": True, "pid": pid}
    log = Path(cfg["log"])
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "ab")
    try:
        proc = subprocess.Popen(cfg["cmd"], cwd=cfg["cwd"], stdout=fh, stderr=fh,
                                stdin=subprocess.DEVNULL, close_fds=True,
                                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    finally:
        fh.close()
    deadline = time.time() + 25
    while time.time() < deadline:
        running, pid = port_state(cfg["port"])
        if running:
            return {"ok": True, "pid": pid or proc.pid, "started": True}
        time.sleep(0.5)
    return {"ok": False, "error": "start_timeout", "pid": proc.pid, "log": str(log)}


def stop(name: str, *, force: bool = True) -> dict:
    cfg = SERVICES.get(name)
    if not cfg:
        return {"ok": False, "error": "unknown_service", "name": name}
    running, pid = port_state(cfg["port"])
    if not running:
        return {"ok": True, "already_stopped": True}
    if pid is None:
        return {"ok": False, "error": "pid_unknown", "hint": "端口在监听但取不到 PID"}
    args = ["taskkill", "/PID", str(pid)]
    if force:
        args.append("/F")
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    time.sleep(1.0)
    still, _ = port_state(cfg["port"])
    return {"ok": not still, "pid": pid, "detail": (r.stdout or r.stderr).strip()}


def restart(name: str) -> dict:
    stopped = stop(name)
    if not stopped.get("ok"):
        return {"ok": False, "error": "stop_failed", "detail": stopped}
    time.sleep(1.0)
    return start(name)


def tail_log(name: str, lines: int = 200) -> dict:
    cfg = SERVICES.get(name)
    if not cfg:
        return {"ok": False, "error": "unknown_service"}
    log = Path(cfg["log"])
    if not log.is_file():
        return {"ok": True, "text": "", "exists": False, "path": str(log)}
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": str(exc), "path": str(log)}
    tail = "\n".join(text.splitlines()[-int(lines):])
    return {"ok": True, "text": tail, "exists": True, "path": str(log)}
