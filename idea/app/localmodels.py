# -*- coding: utf-8 -*-
"""803 本地模型一键准备：llama.cpp + 灵魂 0.5B + 识图 VL-3B。

设计：
- 三步资源互相独立：runtime（llama.cpp CPU 包）、soul（Qwen2.5-0.5B GGUF）、
  vision（Qwen2.5-VL-3B GGUF + mmproj）；
- 下载/解压都在后台线程，进度写入 ``idea/.data/local_jobs.json``，前端轮询；
- Bot 创建时选“灵魂本地/识图本地”会调用 ``provision_bot``，完成后直接写回
  Bot 的 providers/settings，并启动 8123/8125 两个 llama-server。
- 不依赖 PowerShell；Windows CPU 版也能跑（没有 GPU 时不选 CUDA 包）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import paths

AGENT_HOME = paths.agent_home()
JOBS_PATH = paths.DATA_DIR / "local_jobs.json"
DOWNLOAD_ROOT = AGENT_HOME / "models" / "local"
LLAMA_CPU = AGENT_HOME / "llama" / "cpu"
LLAMA_CUDA = AGENT_HOME / "llama" / "cuda"

SOUL_REPO = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
SOUL_FILE = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
VISION_REPO = "ggml-org/Qwen2.5-VL-3B-Instruct-GGUF"
VISION_FILE = "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf"
VISION_MMPROJ = "mmproj-Qwen2.5-VL-3B-Instruct-Q8_0.gguf"

SOUL_DEST = DOWNLOAD_ROOT / "soul" / SOUL_FILE
VISION_DEST = DOWNLOAD_ROOT / "vision" / VISION_FILE
MMPROJ_DEST = DOWNLOAD_ROOT / "vision" / VISION_MMPROJ
SOUL_PORT = 8123
VISION_PORT = 8125

_JOB_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
_ACTIVE_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _load_jobs() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_jobs(jobs: dict[str, dict[str, Any]]) -> None:
    try:
        JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = JOBS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(JOBS_PATH)
    except Exception:  # noqa: BLE001
        pass


def _jobs_snapshot() -> dict[str, dict[str, Any]]:
    with _JOB_LOCK:
        merged = _load_jobs()
        merged.update(_JOBS)
        return merged


def _new_job(kind: str, steps: list[dict[str, Any]], **extra: Any) -> str:
    job_id = "LM-" + uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "kind": kind,
        "status": "queued",
        "steps": steps,
        "error": "",
        "created_at": _now(),
        "updated_at": _now(),
        **extra,
    }
    with _JOB_LOCK:
        jobs = _load_jobs()
        jobs[job_id] = job
        _JOBS[job_id] = job
        _save_jobs(jobs)
    return job_id


def _update_job(job_id: str, **fields: Any) -> None:
    with _JOB_LOCK:
        jobs = _load_jobs()
        job = dict(jobs.get(job_id) or _JOBS.get(job_id) or {})
        if not job:
            return
        job.update(fields)
        job["updated_at"] = _now()
        jobs[job_id] = job
        _JOBS[job_id] = job
        _save_jobs(jobs)


def _update_step(job_id: str, key: str, **fields: Any) -> None:
    with _JOB_LOCK:
        jobs = _load_jobs()
        job = dict(jobs.get(job_id) or _JOBS.get(job_id) or {})
        if not job:
            return
        steps = [dict(s) for s in job.get("steps", [])]
        for step in steps:
            if step.get("key") == key:
                step.update(fields)
                step["updated_at"] = _now()
                break
        job["steps"] = steps
        job["updated_at"] = _now()
        jobs[job_id] = job
        _JOBS[job_id] = job
        _save_jobs(jobs)


def list_jobs(job_id: str | None = None) -> dict[str, Any]:
    jobs = _jobs_snapshot()
    if job_id:
        job = jobs.get(job_id)
        return {"ok": bool(job), "job": job, "error": "" if job else "not_found"}
    items = sorted(jobs.values(), key=lambda x: float(x.get("created_at") or 0),
                   reverse=True)
    return {"ok": True, "items": items[:80], "count": len(items)}


def _which_llama_server() -> Path | None:
    for base in (LLAMA_CUDA, LLAMA_CPU):
        exe = base / "llama-server.exe"
        if exe.is_file():
            return exe
    return None


def _http_download(url: str, target: Path, cb: Callable[[int, int], None]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    req = urllib.request.Request(url, headers={
        "User-Agent": "SoulAgent-local-model/1.0",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = 0
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        done = 0
        with tmp.open("wb") as handle:
            while True:
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                cb(done, total)
    if target.exists():
        target.unlink()
    tmp.replace(target)


def _github_asset(pattern: str) -> tuple[str, str]:
    # latest release 可能只有 nightly-tag.txt；b#### 构建在 releases 列表里
    api = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
    req = urllib.request.Request(api, headers={
        "User-Agent": "SoulAgent-local-model/1.0",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode("utf-8"))
    for release in releases if isinstance(releases, list) else []:
        for asset in release.get("assets") or []:
            name = str(asset.get("name") or "")
            if re.match(pattern, name):
                return name, str(asset.get("browser_download_url") or "")
    raise RuntimeError("llama.cpp releases 中没有找到匹配的 Windows 资产")


def _extract_zip(zip_path: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        for member in members:
            # 解压到同一层，llama.cpp zip 内的目录层级不固定
            name = Path(member.filename).name
            if not name:
                continue
            out = target / name
            with archive.open(member) as src, out.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def install_runtime(job_id: str | None = None, *, cpu: bool = True, finalize: bool = True) -> str:
    """安装 llama.cpp CPU 包（不选 CUDA，避免无 GPU 机器失败）。"""
    steps = [{"key": "runtime", "label": "llama.cpp 运行时", "status": "queued",
              "bytes_done": 0, "total": 0, "pct": 0}]
    job_id = job_id or _new_job("runtime", steps)
    _update_job(job_id, status="running")
    try:
        if _which_llama_server():
            _update_step(job_id, "runtime", status="done", pct=100,
                         detail=str(_which_llama_server()))
            if finalize:
                _update_job(job_id, status="done", finished_at=_now())
            return job_id
        pattern = r"^llama-.*bin-win-cpu-x64\.zip$" if cpu else r"^llama-.*bin-win-cuda-.*\.zip$"
        name, url = _github_asset(pattern)
        if not url:
            raise RuntimeError("asset_url_empty")
        target = LLAMA_CPU if cpu else LLAMA_CUDA
        tmp = AGENT_HOME / "tmp" / name
        _update_step(job_id, "runtime", status="downloading", detail=name)
        _http_download(url, tmp, lambda done, total: _update_step(
            job_id, "runtime", bytes_done=done, total=total,
            pct=int(done * 100 / total) if total else 0))
        _update_step(job_id, "runtime", status="extracting", pct=95, detail="解压中")
        _extract_zip(tmp, target)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        _update_step(job_id, "runtime", status="done", pct=100,
                     detail=str(target / "llama-server.exe"))
        if finalize:
            _update_job(job_id, status="done", finished_at=_now())
    except Exception as exc:  # noqa: BLE001
        _update_step(job_id, "runtime", status="failed", error=f"{type(exc).__name__}: {exc}")
        _update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                    finished_at=_now())
    return job_id


def _download_one(job_id: str, key: str, label: str, url: str, target: Path,
                  pct_base: int = 0, pct_span: int = 100) -> None:
    _update_step(job_id, key, status="downloading")
    _http_download(url, target, lambda done, total: _update_step(
        job_id, key, bytes_done=done, total=total,
        pct=(pct_base + int(done * pct_span / total)) if total else pct_base))
    _update_step(job_id, key, status="done", pct=100, detail=str(target))


def install_soul(job_id: str | None = None, *, finalize: bool = True) -> str:
    steps = [{"key": "soul", "label": "灵魂 Qwen2.5-0.5B", "status": "queued",
              "bytes_done": 0, "total": 0, "pct": 0}]
    job_id = job_id or _new_job("soul", steps)
    _update_job(job_id, status="running")
    try:
        if not SOUL_DEST.is_file():
            url = f"https://hf-mirror.com/{SOUL_REPO}/resolve/main/{SOUL_FILE}"
            _download_one(job_id, "soul", "灵魂 Qwen2.5-0.5B", url, SOUL_DEST)
        else:
            _update_step(job_id, "soul", status="done", pct=100, detail=str(SOUL_DEST))
        if finalize:
            _update_job(job_id, status="done", finished_at=_now())
    except Exception as exc:  # noqa: BLE001
        _update_step(job_id, "soul", status="failed", error=f"{type(exc).__name__}: {exc}")
        _update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                    finished_at=_now())
    return job_id


def install_vision(job_id: str | None = None, *, finalize: bool = True) -> str:
    steps = [
        {"key": "vision", "label": "识图 Qwen2.5-VL-3B", "status": "queued",
         "bytes_done": 0, "total": 0, "pct": 0},
        {"key": "mmproj", "label": "识图 mmproj", "status": "queued",
         "bytes_done": 0, "total": 0, "pct": 0},
    ]
    job_id = job_id or _new_job("vision", steps)
    _update_job(job_id, status="running")
    try:
        if not VISION_DEST.is_file():
            _download_one(job_id, "vision", "识图 Qwen2.5-VL-3B",
                          f"https://hf-mirror.com/{VISION_REPO}/resolve/main/{VISION_FILE}",
                          VISION_DEST, 0, 90)
        else:
            _update_step(job_id, "vision", status="done", pct=90, detail=str(VISION_DEST))
        if not MMPROJ_DEST.is_file():
            _download_one(job_id, "mmproj", "识图 mmproj",
                          f"https://hf-mirror.com/{VISION_REPO}/resolve/main/{VISION_MMPROJ}",
                          MMPROJ_DEST, 90, 10)
        else:
            _update_step(job_id, "mmproj", status="done", pct=100, detail=str(MMPROJ_DEST))
        if finalize:
            _update_job(job_id, status="done", finished_at=_now())
    except Exception as exc:  # noqa: BLE001
        _update_step(job_id, "vision", status="failed", error=f"{type(exc).__name__}: {exc}")
        _update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                    finished_at=_now())
    return job_id


def _port_open(port: int) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except OSError:
        return False


def _wait_health(port: int, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                        timeout=2) as resp:
                if 200 <= int(resp.status) < 300:
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def _start_server(model: Path, port: int, mmproj: Path | None = None) -> dict[str, Any]:
    if _port_open(port):
        return {"ok": True, "port": port, "already_running": True}
    server = _which_llama_server()
    if not server:
        return {"ok": False, "error": "llama_server_missing"}
    if not model.is_file():
        return {"ok": False, "error": "model_missing", "path": str(model)}
    LOG = AGENT_HOME / "logs"
    LOG.mkdir(parents=True, exist_ok=True)
    log = LOG / f"local_{port}.log"
    cmd = [str(server), "-m", str(model), "--host", "127.0.0.1",
           "--port", str(port), "-ngl", "0", "-c", "8192", "--jinja"]
    if mmproj is not None and mmproj.is_file():
        cmd += ["--mmproj", str(mmproj)]
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        handle = log.open("ab")
    except PermissionError:
        handle = None
    try:
        proc = subprocess.Popen(cmd, cwd=str(AGENT_HOME),
                                stdout=handle or subprocess.DEVNULL,
                                stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, close_fds=True, **kwargs)
    finally:
        if handle is not None:
            handle.close()
    ok = _wait_health(port, 150.0 if mmproj else 90.0)
    if not ok:
        return {"ok": False, "error": "start_timeout", "pid": proc.pid,
                "log": str(log)}
    return {"ok": True, "port": port, "pid": proc.pid, "log": str(log)}


def _apply_bot(bot_path: Path, want_soul: bool, want_vision: bool) -> dict[str, Any]:
    from tools import bot_profile
    bot = bot_profile.load_bot(bot_path)
    bot.pop("_root", None)
    providers = bot.setdefault("providers", {})
    settings = bot.setdefault("settings", {})
    if want_soul:
        providers["soul"] = {"mode": "local", "base_url": "", "model": str(SOUL_DEST)}
        settings["local_soul_model"] = str(SOUL_DEST)
        settings["local_soul_port"] = SOUL_PORT
        settings["soul_server_url"] = f"http://127.0.0.1:{SOUL_PORT}"
    if want_vision:
        providers["vision"] = {"mode": "local", "base_url": "",
                               "model": str(VISION_DEST),
                               "mmproj": str(MMPROJ_DEST)}
        settings["local_vision_model"] = str(VISION_DEST)
        settings["local_vision_mmproj"] = str(MMPROJ_DEST)
        settings["local_vision_port"] = VISION_PORT
        settings["vision_server_url"] = f"http://127.0.0.1:{VISION_PORT}"
    settings["local_llama_bin"] = str(LLAMA_CPU)
    (bot_path / "bot.json").write_text(
        json.dumps(bot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    models_path = bot_path / "models" / "models.json"
    try:
        models = json.loads(models_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        models = {"schema": 1, "local": {}, "downloaded_at": None}
    models.setdefault("local", {})
    if want_soul:
        models["local"]["soul-05b"] = {"path": str(SOUL_DEST), "port": SOUL_PORT,
                                       "role": "soul", "source": "oneclick"}
    if want_vision:
        models["local"]["vision-vl-3b"] = {"path": str(VISION_DEST),
                                           "mmproj": str(MMPROJ_DEST),
                                           "port": VISION_PORT,
                                           "role": "vision", "source": "oneclick"}
    models_path.parent.mkdir(parents=True, exist_ok=True)
    models_path.write_text(json.dumps(models, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")
    return {"ok": True, "providers": {"soul": providers.get("soul"),
                                      "vision": providers.get("vision")}}


def _step_failed(job_id: str, key: str) -> bool:
    job = _jobs_snapshot().get(job_id) or {}
    for step in job.get("steps") or []:
        if step.get("key") == key:
            return step.get("status") == "failed"
    return False


def provision_bot(bot_path: Path | str, mode: str) -> str:
    """按 Bot 部署模式准备本地资源并写回 Bot；返回 job_id。"""
    want_soul = mode in ("local_soul_local_vision", "local_soul_cloud_vision")
    want_vision = mode == "local_soul_local_vision"
    steps = []
    if want_soul or want_vision:
        steps.append({"key": "runtime", "label": "llama.cpp 运行时", "status": "queued",
                      "bytes_done": 0, "total": 0, "pct": 0})
    if want_soul:
        steps.append({"key": "soul", "label": "灵魂 Qwen2.5-0.5B", "status": "queued",
                      "bytes_done": 0, "total": 0, "pct": 0})
    if want_vision:
        steps.append({"key": "vision", "label": "识图 Qwen2.5-VL-3B", "status": "queued",
                      "bytes_done": 0, "total": 0, "pct": 0})
        steps.append({"key": "mmproj", "label": "识图 mmproj", "status": "queued",
                      "bytes_done": 0, "total": 0, "pct": 0})
    job_id = _new_job("bot", steps, mode=mode, bot_path=str(bot_path))
    _update_job(job_id, status="running")
    def worker() -> None:
        try:
            if not (_which_llama_server()):
                _update_step(job_id, "runtime", status="downloading")
                install_runtime(job_id, finalize=False)
                if _step_failed(job_id, "runtime"):
                    raise RuntimeError("runtime_install_failed")
            else:
                _update_step(job_id, "runtime", status="done", pct=100)
            if want_soul:
                install_soul(job_id, finalize=False)
                if _step_failed(job_id, "soul"):
                    raise RuntimeError("soul_download_failed")
            if want_vision:
                install_vision(job_id, finalize=False)
                if _step_failed(job_id, "vision") or _step_failed(job_id, "mmproj"):
                    raise RuntimeError("vision_download_failed")
            if want_soul:
                _update_step(job_id, "soul", status="starting")
                r = _start_server(SOUL_DEST, SOUL_PORT)
                if not r.get("ok"):
                    raise RuntimeError("soul_start_failed:" + str(r.get("error")))
                _update_step(job_id, "soul", status="done", pct=100,
                             detail=f"8123 {'运行中' if r.get('ok') else ''}")
            if want_vision:
                _update_step(job_id, "vision", status="starting")
                r = _start_server(VISION_DEST, VISION_PORT, MMPROJ_DEST)
                if not r.get("ok"):
                    raise RuntimeError("vision_start_failed:" + str(r.get("error")))
                _update_step(job_id, "vision", status="done", pct=100,
                             detail="8125 运行中")
            _apply_bot(Path(bot_path), want_soul, want_vision)
            _update_job(job_id, status="done", finished_at=_now(), error="")
        except Exception as exc:  # noqa: BLE001
            _update_job(job_id, status="failed", error=f"{type(exc).__name__}: {exc}",
                        finished_at=_now())
    threading.Thread(target=worker, name="bot-local-setup", daemon=True).start()
    return job_id


def install(kind: str) -> dict[str, Any]:
    key = str(kind or "").strip().lower()
    if key == "runtime":
        job = install_runtime()
    elif key == "soul":
        job = install_soul()
    elif key == "vision":
        job = install_vision()
    else:
        return {"ok": False, "error": "unknown_kind"}
    return {"ok": True, "job_id": job, "kind": key}


def presets() -> dict[str, Any]:
    return {"ok": True, "items": [
        {"key": "runtime", "label": "llama.cpp 运行时", "size_hint": "~20 MB",
         "installed": bool(_which_llama_server()),
         "path": str(_which_llama_server() or LLAMA_CPU)},
        {"key": "soul", "label": "灵魂 Qwen2.5-0.5B Q4", "size_hint": "~400 MB",
         "installed": SOUL_DEST.is_file(), "path": str(SOUL_DEST)},
        {"key": "vision", "label": "识图 Qwen2.5-VL-3B Q4", "size_hint": "~2 GB",
         "installed": VISION_DEST.is_file() and MMPROJ_DEST.is_file(),
         "path": str(VISION_DEST)},
    ], "agent_home": str(AGENT_HOME)}
