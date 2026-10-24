# -*- coding: utf-8 -*-
"""llama-server 的 Python 启动封装（评测/CI 里可编程地起停 server）。

命令行用法（阻塞式，Ctrl-C 停）::

    E:\\agent\\venv312\\Scripts\\python.exe tools/soul_serve/server.py ^
        --model E:\\agent\\output\\gguf\\soul_qwen25_05b_v1-q4_k_m.gguf --port 8123

编程用法::

    from tools.soul_serve.server import start_server, wait_healthy, stop_server
    proc = start_server(gguf_path, port=8123)
    assert wait_healthy(8123)
    ...
    stop_server(proc)

关键点：
- 子进程环境里 ``PATH`` 前面加 ``E:/agent/venv312/Lib/site-packages/torch/lib``，
  否则 ggml-cuda.dll 加载失败（Windows 踩坑）；
- 默认只带 ``--jinja``；JSON schema 约束走**请求级** response_format（b11857 的
  ``--json-schema-file`` 常驻模式在 Qwen jinja template 下会 400，见 README 踩坑）；
  如需服务端常驻约束，传 ``server_json_schema=True``（修复后可切）。
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

ROOT = Path(__file__).resolve().parents[2]
LLAMA_BIN = Path(r"E:\agent\llama\cuda")
TORCH_LIB = Path(r"E:\agent\venv312\Lib\site-packages\torch\lib")
SCHEMA_PATH = ROOT / "agi" / "soul" / "inner_state_schema.json"


def server_env() -> dict:
    env = dict(os.environ)
    env["PATH"] = str(TORCH_LIB) + os.pathsep + env.get("PATH", "")
    return env


def build_command(model_path: Path | str, *, port: int = 8123, ctx: int = 2048,
                  ngl: int = 99, host: str = "127.0.0.1",
                  schema_path: Path | str | None = None,
                  server_json_schema: bool = False,
                  extra_args: list[str] | None = None) -> list[str]:
    exe = LLAMA_BIN / "llama-server.exe"
    schema = Path(schema_path) if schema_path else SCHEMA_PATH
    cmd = [
        str(exe), "-m", str(model_path), "-ngl", str(ngl), "--host", host,
        "--port", str(port), "-c", str(ctx), "-np", "1", "--jinja",
    ]
    if server_json_schema:
        cmd.extend(["--json-schema-file", str(schema)])
    if extra_args:
        cmd.extend(str(item) for item in extra_args)
    return cmd


def start_server(model_path: Path | str, *, port: int = 8123, ctx: int = 2048,
                 ngl: int = 99, host: str = "127.0.0.1",
                 schema_path: Path | str | None = None,
                 server_json_schema: bool = False,
                 log_path: Path | str | None = None,
                 extra_args: list[str] | None = None) -> subprocess.Popen:
    """启动 llama-server 子进程；日志写 log_path（不传则丢弃）。"""
    model = Path(model_path)
    if not model.exists():
        raise FileNotFoundError(f"GGUF 不存在: {model}")
    cmd = build_command(model, port=port, ctx=ctx, ngl=ngl, host=host,
                        schema_path=schema_path,
                        server_json_schema=server_json_schema, extra_args=extra_args)
    log_file = open(log_path, "ab") if log_path else subprocess.DEVNULL
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT,
                            env=server_env(), creationflags=creationflags)
    return proc


def wait_healthy(port: int = 8123, host: str = "127.0.0.1", timeout_s: float = 180.0,
                 poll_s: float = 1.0) -> bool:
    url = f"http://{host}:{port}/health"
    deadline = time.time() + float(timeout_s)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(payload, dict) and str(payload.get("status") or "ok") == "ok":
                return True
        except Exception:  # noqa: BLE001 - 还没起来
            pass
        time.sleep(poll_s)
    return False


def stop_server(proc: subprocess.Popen, timeout_s: float = 15.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout_s)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="start llama-server with InnerState JSON schema")
    parser.add_argument("--model", required=True, help="GGUF 路径")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--ctx", type=int, default=2048)
    parser.add_argument("--ngl", type=int, default=99, help="GPU 层数，0=CPU")
    parser.add_argument("--schema", default=str(SCHEMA_PATH))
    parser.add_argument("--log", default="", help="server 日志路径")
    parser.add_argument("--server-json-schema", action="store_true",
                        help="用服务端 -jf 常驻约束（b11857 有 bug，默认关）")
    parser.add_argument("--health-timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    proc = start_server(args.model, port=args.port, ctx=args.ctx, ngl=args.ngl,
                        schema_path=args.schema,
                        server_json_schema=args.server_json_schema,
                        log_path=args.log or None)
    print(f"[soul_serve] pid={proc.pid} waiting for health on {args.port} ...", flush=True)
    if not wait_healthy(args.port, timeout_s=args.health_timeout):
        print("[soul_serve] server 未能 healthy，见日志", file=sys.stderr)
        stop_server(proc)
        return 1
    print(f"[soul_serve] healthy: http://127.0.0.1:{args.port}", flush=True)
    try:
        proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        stop_server(proc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
