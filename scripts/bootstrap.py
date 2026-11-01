#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""V1 一键启动：环境检测 → 国内源装依赖 → 初始化 bots/default/ → 启动 803。

用法::

    启动.bat                  # 双击：检测/安装/初始化/启动 803
    python scripts/bootstrap.py --check     # 只检测，不写文件、不安装
    python scripts/bootstrap.py --no-install  # 不装依赖（已装好时）
    python scripts/bootstrap.py --no-start    # 只做检查与初始化

设计约束：
- 只用标准库（全新 clone、依赖还没装时也能跑）；
- 默认不覆盖用户已有配置；bot 只在缺失时初始化；
- 安装/启动失败都打印“下一步怎么做”，日志写 logs/bootstrap.log；
- Windows 优先；其它平台仅用于 CI 检查（启动 803 需 idea/.venv）。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDEA_DIR = ROOT / "idea"
LOG_DIR = ROOT / "logs"
LOG_PATH = LOG_DIR / "bootstrap.log"
DEFAULT_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
MIN_DISK_GB = 10
MIN_PY = (3, 12)


def log(message: str) -> None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def python_info() -> dict:
    version = sys.version_info
    ok = version[:2] >= MIN_PY
    return {"ok": ok, "version": platform.python_version(),
            "executable": sys.executable,
            "hint": "" if ok else f"需要 Python {MIN_PY[0]}.{MIN_PY[1]}+"}


def disk_info(path: Path | str = ROOT) -> dict:
    try:
        usage = shutil.disk_usage(path)
        free_gb = round(usage.free / (1024 ** 3), 1)
        return {"ok": free_gb >= MIN_DISK_GB, "free_gb": free_gb,
                "hint": "" if free_gb >= MIN_DISK_GB else f"磁盘剩余不足 {MIN_DISK_GB}GB"}
    except OSError as exc:
        return {"ok": False, "free_gb": 0, "hint": f"无法读取磁盘：{exc}"}


def wechat_info() -> dict:
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tencent" / "WeChat" / "WeChat.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tencent" / "WeChat" / "WeChat.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tencent" / "Weixin" / "Weixin.exe",
    ]
    found = next((str(path) for path in candidates if path.is_file()), "")
    return {"ok": bool(found), "path": found,
            "hint": "" if found else "未找到微信安装路径（可稍后在 803 里配置；不影响启动）"}


def gpu_info() -> dict:
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total",
                                 "--format=csv,noheader"], capture_output=True,
                                text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "gpus": [], "hint": "未检测到 NVIDIA GPU（V1 可用云端模型）"}
    if result.returncode != 0:
        return {"ok": False, "gpus": [], "hint": "nvidia-smi 不可用（V1 可用云端模型）"}
    gpus = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return {"ok": bool(gpus), "gpus": gpus,
            "hint": "" if gpus else "未检测到 NVIDIA GPU（V1 可用云端模型）"}


def _venv_python(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def build_report() -> dict:
    """只读检测报告（不写文件、不安装）。"""
    report = {
        "python": python_info(),
        "disk": disk_info(),
        "wechat": wechat_info(),
        "gpu": gpu_info(),
        "main_venv": {"ok": _venv_python(ROOT / ".venv").is_file(),
                      "path": str(_venv_python(ROOT / ".venv"))},
        "idea_venv": {"ok": _venv_python(IDEA_DIR / ".venv").is_file(),
                      "path": str(_venv_python(IDEA_DIR / ".venv"))},
        "port_803": {"listening": _port_listening(803)},
    }
    return report


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.3):
            return True
    except OSError:
        return False


def ensure_bot(target: Path | str = ROOT / "bots" / "default") -> dict:
    """初始化 bot 包骨架；已存在的文件绝不覆盖。"""
    bot = Path(target)
    created: list[str] = []
    for sub in ("config", "db/selflearn/wechat_contacts", "models", "prompts",
                "media", "logs", "secrets"):
        path = bot / sub
        if not path.is_dir():
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path))
    weight = bot / "config" / "wechat.json"
    if not weight.exists():
        weight.write_text(json.dumps({
            "enabled": False, "engine_enabled": False, "auto_reply": False,
            "dry_run": True, "send_mode": "off", "stop": True,
            "allowed_contacts": [], "monitored_contacts": [],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        created.append(str(weight))
    app = bot / "config" / "app.json"
    if not app.exists():
        app.write_text("{}\n", encoding="utf-8")
        created.append(str(app))
    meta = bot / "bot.json"
    if not meta.exists():
        meta.write_text(json.dumps({
            "id": bot.name, "created_at": time.time(), "version": 1,
            "note": "bootstrap 初始化；方案/密钥请到 803 配置",
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        created.append(str(meta))
    return {"ok": True, "bot": str(bot), "created": created}


def _run(command: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    log("run: " + " ".join(command))
    try:
        result = subprocess.run(command, cwd=str(cwd or ROOT), capture_output=True,
                                text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    output = (result.stdout or "") + (result.stderr or "")
    for line in output.splitlines()[-5:]:
        log("  " + line)
    return result.returncode, output


def ensure_venv(venv: Path, requirements: Path, *, install: bool,
                mirror: str) -> dict:
    """创建/检查 venv；install=True 时用国内源装依赖。"""
    python = _venv_python(venv)
    created = False
    if not python.is_file():
        rc, out = _run([sys.executable, "-m", "venv", str(venv)])
        if rc != 0:
            return {"ok": False, "error": "创建 venv 失败", "detail": out[-500:]}
        created = True
    if install and requirements.is_file():
        rc, out = _run([str(_venv_python(venv)), "-m", "pip", "install",
                        "--upgrade", "pip", "-i", mirror], cwd=ROOT)
        if rc != 0:
            return {"ok": False, "error": "pip 升级失败", "detail": out[-500:]}
        rc, out = _run([str(_venv_python(venv)), "-m", "pip", "install",
                        "-r", str(requirements), "-i", mirror], cwd=ROOT)
        if rc != 0:
            return {"ok": False, "error": f"安装依赖失败：{requirements.name}",
                    "detail": out[-500:]}
    return {"ok": True, "created": created, "python": str(python)}


def start_803(port: int = 803, timeout: float = 40.0) -> dict:
    """启动 803（uvicorn，cwd=idea/），等待 /health；已监听则复用。"""
    if _port_listening(port):
        return {"ok": True, "started": False, "url": f"http://127.0.0.1:{port}/",
                "note": "803 已在运行"}
    python = _venv_python(IDEA_DIR / ".venv")
    if not python.is_file():
        return {"ok": False, "error": "idea/.venv 不存在",
                "hint": "重跑 bootstrap（默认会安装 idea 依赖），或手动按 idea/README.md 建 venv"}
    log_path = LOG_DIR / "idea-803.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    try:
        process = subprocess.Popen(
            [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port)], cwd=str(IDEA_DIR), stdout=handle,
            stderr=subprocess.STDOUT)
    finally:
        handle.close()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_listening(port):
            return {"ok": True, "started": True, "pid": process.pid,
                    "url": _url_with_token(port), "log": str(log_path)}
        if process.poll() is not None:
            return {"ok": False, "error": f"803 退出码 {process.returncode}",
                    "log": str(log_path)}
        time.sleep(0.5)
    return {"ok": False, "error": "803 启动超时", "log": str(log_path)}


def _url_with_token(port: int) -> str:
    token = ""
    try:
        cfg = json.loads((IDEA_DIR / "config.json").read_text(encoding="utf-8"))
        token = str(cfg.get("admin_token") or "")
    except (OSError, ValueError):
        pass
    base = f"http://127.0.0.1:{port}/"
    return f"{base}?token={token}" if token else base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SoulAgent V1 一键启动")
    parser.add_argument("--check", action="store_true", help="只检测，不写文件/不安装")
    parser.add_argument("--no-install", action="store_true", help="跳过 pip 安装")
    parser.add_argument("--no-start", action="store_true", help="不启动 803")
    parser.add_argument("--port", type=int, default=803)
    parser.add_argument("--mirror", default=os.environ.get("PIP_INDEX_URL") or DEFAULT_MIRROR)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = build_report()
    result: dict = {"ok": True, "report": report, "steps": []}
    if args.check:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("[check]")
            for key, value in report.items():
                print(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
        return 0 if report["python"]["ok"] else 2

    if not report["python"]["ok"]:
        result["ok"] = False
        result["error"] = f"Python 版本过低：需要 {MIN_PY[0]}.{MIN_PY[1]}+"
        print(result["error"])
        return 2

    bot = ensure_bot()
    result["steps"].append({"bot": bot})
    if not args.no_install:
        main_env = ensure_venv(ROOT / ".venv", ROOT / "requirements.txt",
                               install=True, mirror=args.mirror)
        result["steps"].append({"main_venv": main_env})
        idea_env = ensure_venv(IDEA_DIR / ".venv", IDEA_DIR / "requirements.txt",
                               install=True, mirror=args.mirror)
        result["steps"].append({"idea_venv": idea_env})
        if not all(step.get("ok", False) for step in result["steps"][1:]):
            result["ok"] = False
    if result["ok"] and not args.no_start:
        started = start_803(args.port)
        result["steps"].append({"start_803": started})
        result["ok"] = bool(started.get("ok"))
        result["url"] = started.get("url") or f"http://127.0.0.1:{args.port}/"

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("环境检测：")
        for key in ("python", "disk", "wechat", "gpu"):
            item = report[key]
            print(f"  {key}: {'OK' if item.get('ok') else 'WARN'} {item.get('hint', '')}")
        print(f"bot 初始化：{bot['bot']}（新增 {len(bot['created'])} 项）")
        for step in result["steps"]:
            for name, value in step.items():
                if isinstance(value, dict) and "ok" in value:
                    print(f"  {name}: {'OK' if value.get('ok') else 'FAIL'}")
        if result.get("url"):
            print(f"打开 803：{result['url']}")
        if not result["ok"]:
            print("有步骤失败，详见 logs/bootstrap.log")
    log("bootstrap done ok=%s" % result["ok"])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
