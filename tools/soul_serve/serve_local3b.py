# -*- coding: utf-8 -*-
"""本地 3B 提取模型服务（P14）：llama-server 端口 8125，OpenAI 兼容。

用途：高量长文本提取（摘要/关键词/情绪），与 8123 灵魂 0.5B 服务**串行**使用。
固定参数：``-m <gguf> -ngl 99 --host 127.0.0.1 --port 8125 -c 4096 -np 1 --jinja``，
日志 ``E:\\agent\\logs\\local3b_8125.log``。

用法::

    # 启动（默认）
    E:\\agent\\venv312\\Scripts\\python.exe tools/soul_serve/serve_local3b.py

    # 状态 / 停止 / 冒烟
    ... serve_local3b.py --status
    ... serve_local3b.py --stop
    ... serve_local3b.py --test

只负责 8125 端口与自己的 pid 文件；**不碰 8123**（灵魂 0.5B 服务）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path


ROOT = Path(__file__).resolve().parents[2]
MODEL = agent_path(r"models\Qwen2.5-3B-Instruct-GGUF\qwen2.5-3b-instruct-q4_k_m.gguf")
LLAMA_BIN = agent_path(r"llama\cuda")
TORCH_LIB = agent_path(r"venv312\Lib\site-packages\torch\lib")
LOG_DIR = agent_path(r"logs")
PID_FILE = LOG_DIR / "local3b_8125.pid"
LOG_FILE = LOG_DIR / "local3b_8125.log"
PORT = 8125
HOST = "127.0.0.1"
CTX = 4096
NGL = 99


def _port_open(port: int = PORT, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((HOST, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_from_file() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="gbk",
                             errors="replace", timeout=5)
    except Exception:  # noqa: BLE001
        return False
    return "llama-server.exe" in out.stdout.lower()


def status() -> dict:
    pid = _pid_from_file()
    running = _port_open()
    healthy = False
    if running:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2) as resp:
                healthy = 200 <= int(resp.status) < 300
        except Exception:  # noqa: BLE001
            healthy = False
    return {"port": PORT, "listening": running, "healthy": healthy,
            "pid": pid, "pid_alive": bool(pid and _pid_alive(pid)),
            "model": str(MODEL), "model_exists": MODEL.exists(),
            "log": str(LOG_FILE)}


def start(timeout: float = 180.0) -> dict:
    if _port_open():
        return {"ok": False, "error": "port_already_in_use", **status()}
    if not MODEL.exists():
        return {"ok": False, "error": "model_missing", "model": str(MODEL)}
    exe = LLAMA_BIN / "llama-server.exe"
    if not exe.exists():
        return {"ok": False, "error": "llama_server_missing", "path": str(exe)}
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if TORCH_LIB.exists():
        env["PATH"] = str(TORCH_LIB) + os.pathsep + env.get("PATH", "")
    command = [str(exe), "-m", str(MODEL), "-ngl", str(NGL), "--host", HOST,
               "--port", str(PORT), "-c", str(CTX), "-np", "1", "--jinja"]
    log_handle = open(LOG_FILE, "ab")  # noqa: SIM115
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    print(f"[local3b] start: {' '.join(command)}", flush=True)
    process = subprocess.Popen(command, env=env, stdout=log_handle,
                               stderr=subprocess.STDOUT, **kwargs)
    PID_FILE.write_text(str(process.pid), encoding="utf-8")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open():
            try:
                with urllib.request.urlopen(f"http://{HOST}:{PORT}/health", timeout=2) as resp:
                    if 200 <= int(resp.status) < 300:
                        return {"ok": True, "pid": process.pid, "port": PORT,
                                "model": str(MODEL), "log": str(LOG_FILE),
                                "command": command}
            except Exception:  # noqa: BLE001
                pass
        if process.poll() is not None:
            return {"ok": False, "error": "server_exited", "returncode": process.returncode,
                    "log": str(LOG_FILE)}
        time.sleep(0.5)
    return {"ok": False, "error": "startup_timeout", "pid": process.pid,
            "log": str(LOG_FILE), **status()}


def stop() -> dict:
    pid = _pid_from_file()
    if not pid:
        return {"ok": False, "error": "no_pid_file", "status": status()}
    if not _pid_alive(pid):
        PID_FILE.unlink(missing_ok=True)
        return {"ok": False, "error": "pid_not_alive", "pid": pid}
    if not _port_open():
        return {"ok": False, "error": "registered_pid_but_port_not_listening",
                "pid": pid}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       text=True, encoding="gbk", errors="replace", timeout=10)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"kill_failed:{exc}", "pid": pid}
    PID_FILE.unlink(missing_ok=True)
    return {"ok": True, "pid": pid, "port": PORT}


def _chat(prompt: str, max_tokens: int = 400) -> dict:
    body = json.dumps({
        "model": "qwen2.5-3b-instruct",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2, "max_tokens": int(max_tokens),
        "stream": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        f"http://{HOST}:{PORT}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.loads(response.read().decode("utf-8"))
    elapsed = round(time.perf_counter() - started, 2)
    text = payload["choices"][0]["message"]["content"]
    return {"text": text.strip(), "elapsed_s": elapsed,
            "usage": payload.get("usage") or {}}


def test() -> dict:
    prompt = ("用 JSON 输出这句话的摘要：{\"summary\":\"≤30字\"}。"
              "文本：这是一条测试消息，用于验证本地 3B 服务能否正常返回中文摘要。")
    try:
        result = _chat(prompt, max_tokens=120)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"request_failed:{exc}", "status": status()}
    return {"ok": True, **result, "status": status()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地 3B 提取服务（8125）")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    if args.stop:
        print(json.dumps(stop(), ensure_ascii=False, indent=1))
    elif args.status:
        print(json.dumps(status(), ensure_ascii=False, indent=1))
    elif args.test:
        print(json.dumps(test(), ensure_ascii=False, indent=1))
    else:
        print(json.dumps(start(timeout=args.timeout), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
