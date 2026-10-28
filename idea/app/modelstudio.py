# -*- coding: utf-8 -*-
"""M3-B 模型中心后端：模型/适配器清单、本地模型启停、合并 adapter、转 GGUF、磁盘用量。

只提供纯函数（路由由 Lead 统一接 main.py；本任务不改 main.py / web）。

安全边界：
- 所有可操作路径必须在 ``AGENT_HOME`` 下（软链解析后校验），拒绝路径越权；
- llama-server 启停端口只允许 ``8100-8199``；
- 启停日志写工作台 ``.data/modellogs/<port>.log``；
- 合并 adapter / 转 GGUF 调用 SoulAgent 仓库里的真实 CLI（参数按其 argparse 定义）；
- 子进程一律参数列表（不经过 shell），带超时；失败返回 ``ok:false + error``。

模型根解析：``AGENT_HOME`` → ``SOULAGENT_AGENT_HOME`` → SoulAgent 的
``agi/data/paths.json`` → ``E:\\agent``（与 agi.paths 的优先级一致）。
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
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from . import audit, paths, services

#: 允许启停 llama-server 的端口范围
PORT_MIN = 8100
PORT_MAX = 8199
#: 启停参数边界
NGL_MIN, NGL_MAX = 0, 999
CTX_MIN, CTX_MAX = 256, 65536
#: 模型日志目录（工作台自身数据目录，gitignore）
MODEL_LOG_DIR = paths.DATA_DIR / "modellogs"
#: 合并/转换超时（秒）；merge CPU 慢、convert 也慢
MERGE_TIMEOUT_S = 3600
CONVERT_TIMEOUT_S = 3600
START_TIMEOUT_S = 25.0
_QUANT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DEFAULT_PORTS = (8123, 8125)

#: 已由本模块启动的进程（stop 时优先用端口 PID，取不到时兜底）
_RUNNING: dict[int, Any] = {}
#: 本进程管理的端口 → {pid, model_path, log}（/running 用，stop 后移除）
_MANAGED: dict[int, dict] = {}


# ------------------------------------------------------------------ 基础
def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def agent_home() -> Path:
    """AGENT_HOME → SOULAGENT_AGENT_HOME → SoulAgent paths.json → idea 默认。"""
    for name in ("AGENT_HOME", "SOULAGENT_AGENT_HOME"):
        raw = _env(name)
        if raw:
            return Path(os.path.expanduser(os.path.expandvars(raw)))
    try:
        config = paths.soulagent_home() / "agi" / "data" / "paths.json"
        payload = json.loads(config.read_text(encoding="utf-8"))
        raw = str((payload or {}).get("agent_home") or "").strip()
        if raw:
            return Path(os.path.expanduser(os.path.expandvars(raw)))
    except Exception:  # noqa: BLE001 - 没有配置用默认
        pass
    return paths.agent_home()


def soulagent_home() -> Path:
    return paths.soulagent_home()


def llama_server_path() -> Path:
    raw = _env("LLAMA_SERVER")
    if raw:
        return Path(raw)
    return agent_home() / "llama" / "cuda" / "llama-server.exe"


def _resolve(path: Path | str, home: Path | None = None) -> Path:
    base = Path(home) if home is not None else agent_home()
    raw = Path(str(path or "")).expanduser()
    if not raw.is_absolute():
        raw = base / raw
    try:
        return raw.resolve()
    except OSError:
        return raw.absolute()


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _allowed(path: Path | str, home: Path | None = None) -> tuple[Path | None, str]:
    """返回 (resolved, error)；路径必须在 AGENT_HOME 下。"""
    home = Path(home) if home is not None else agent_home()
    resolved = _resolve(path, home)
    if not _is_under(resolved, home):
        return None, "path_outside_agent_home"
    return resolved, ""


def _size_of(path: Path) -> tuple[int, int]:
    """(字节数, 文件数)；目录递归统计；不存在返回 0。"""
    try:
        if path.is_file():
            return int(path.stat().st_size), 1
        if not path.is_dir():
            return 0, 0
        total = 0
        files = 0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += int(child.stat().st_size)
                    files += 1
            except OSError:
                continue
        return total, files
    except OSError:
        return 0, 0


def _mtime_of(path: Path) -> float:
    try:
        if path.is_file():
            return round(path.stat().st_mtime, 3)
        latest = 0.0
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    latest = max(latest, child.stat().st_mtime)
            except OSError:
                continue
        return round(latest, 3)
    except OSError:
        return 0.0


def _item(path: Path, kind: str, meta: dict | None = None) -> dict:
    size, files = _size_of(path)
    data = {
        "name": path.name,
        "path": str(path),
        "size": size,
        "files": files,
        "kind": kind,
        "mtime": _mtime_of(path),
    }
    if meta:
        data.update(meta)
    return data


def _record(action: str, target: str, **meta: Any) -> None:
    try:
        audit.record(action, target, actor="workbench", meta=meta)
    except Exception:  # noqa: BLE001 - 审计失败不影响主链
        pass


# ------------------------------------------------------------------ 清单
def _hf_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "config.json").is_file() or any(
                child.glob("*.safetensors")):
            out.append(child)
    return out


def _adapter_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return [child for child in sorted(root.iterdir())
            if child.is_dir() and (child / "adapter_config.json").is_file()]


def _adapter_meta(path: Path) -> dict:
    meta: dict = {"adapter_config": str(path / "adapter_config.json")}
    try:
        raw = json.loads((path / "adapter_config.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            meta.update({
                "base_model": str(raw.get("base_model_name_or_path") or ""),
                "r": raw.get("r"),
                "lora_alpha": raw.get("lora_alpha"),
                "target_modules": raw.get("target_modules") or [],
            })
    except Exception as exc:  # noqa: BLE001 - 坏配置也列出来，附错误
        meta["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return meta


def list_models(home: Path | str | None = None) -> dict:
    """扫 <agent_home> 的 models / output/gguf / output/adapters / llama。"""
    base = Path(home) if home is not None else agent_home()
    items: list[dict] = []
    for path in _hf_dirs(base / "models"):
        items.append(_item(path, "base"))
    gguf_dir = base / "output" / "gguf"
    if gguf_dir.is_dir():
        for path in sorted(gguf_dir.glob("*.gguf")):
            items.append(_item(path, "gguf"))
    adapters = _adapter_dirs(base / "output" / "adapters")
    # 兼容历史布局：adapter 目录也可能直接放在 output/ 下
    seen = {str(path.resolve()) for path in adapters}
    for path in _adapter_dirs(base / "output"):
        key = str(path.resolve())
        if key not in seen:
            adapters.append(path)
            seen.add(key)
    for path in adapters:
        items.append(_item(path, "adapter", _adapter_meta(path)))
    items.sort(key=lambda row: (row["kind"], row["name"].lower()))
    counts = {kind: sum(1 for item in items if item["kind"] == kind)
              for kind in ("base", "gguf", "adapter")}
    return {"ok": True, "agent_home": str(base), "items": items,
            "count": len(items), "counts": counts, "runtime": runtime_info(base)}


def runtime_info(home: Path | str | None = None) -> dict:
    base = Path(home) if home is not None else agent_home()
    llama_dir = base / "llama"
    server = llama_server_path()
    quantize = base / "llama" / "cuda" / "llama-quantize.exe"
    convert_dir = base / "llama" / "convert"
    return {
        "llama_dir": str(llama_dir),
        "llama_dir_exists": llama_dir.is_dir(),
        "llama_server": str(server),
        "llama_server_exists": server.is_file(),
        "llama_quantize": str(quantize),
        "llama_quantize_exists": quantize.is_file(),
        "convert_dir": str(convert_dir),
        "convert_dir_exists": convert_dir.is_dir(),
    }


def disk_usage(home: Path | str | None = None) -> dict:
    base = Path(home) if home is not None else agent_home()
    # adapter 实际布局有两种：output/adapters/* 与历史 output/<adapter> 直放；
    # 磁盘统计按“adapter_config.json 的目录”聚合，避免 adapters 显示 0。
    adapter_dirs = _adapter_dirs(base / "output" / "adapters")
    seen = {str(path.resolve()) for path in adapter_dirs}
    for path in _adapter_dirs(base / "output"):
        key = str(path.resolve())
        if key not in seen:
            adapter_dirs.append(path)
            seen.add(key)
    adapter_size = sum(_size_of(path)[0] for path in adapter_dirs)
    adapter_files = sum(_size_of(path)[1] for path in adapter_dirs)
    items = []
    total = 0
    for kind, path, size, files in (
            ("models", base / "models", *_size_of(base / "models")),
            ("adapters", base / "output", adapter_size, adapter_files),
            ("gguf", base / "output" / "gguf", *_size_of(base / "output" / "gguf")),
    ):
        items.append({"kind": kind, "dir": str(path), "size": int(size),
                      "files": int(files),
                      "exists": path.exists() if kind != "adapters"
                      else bool(adapter_dirs)})
        total += int(size)
    return {"ok": True, "items": items, "total": total,
            "agent_home": str(base)}


# ------------------------------------------------------------------ 适配器
def list_adapters(home: Path | str | None = None) -> dict:
    base = Path(home) if home is not None else agent_home()
    items = []
    for path in _adapter_dirs(base / "output" / "adapters"):
        items.append(_item(path, "adapter", _adapter_meta(path)))
    seen = {item["path"] for item in items}
    for path in _adapter_dirs(base / "output"):
        item = _item(path, "adapter", _adapter_meta(path))
        if item["path"] not in seen:
            items.append(item)
            seen.add(item["path"])
    items.sort(key=lambda row: row["name"].lower())
    return {"ok": True, "items": items, "count": len(items),
            "agent_home": str(base)}


# ------------------------------------------------------------------ 运行中模型
def _ports_of_interest() -> list[int]:
    ports: list[int] = list(_DEFAULT_PORTS)
    raw = _env("MODELSTUDIO_PORTS")
    if raw:
        for piece in raw.replace(";", ",").split(","):
            piece = piece.strip()
            if piece.isdigit():
                ports.append(int(piece))
    for name, cfg in getattr(services, "SERVICES", {}).items():
        cmd = " ".join(str(part) for part in (cfg.get("cmd") or []))
        if "llama-server" in cmd or name in ("soul", "local3b"):
            try:
                ports.append(int(cfg.get("port")))
            except (TypeError, ValueError):
                continue
    for port in list(_MANAGED) + list(_RUNNING):
        if PORT_MIN <= port <= PORT_MAX and port not in ports:
            ports.append(port)
    out: list[int] = []
    for port in ports:
        if PORT_MIN <= port <= PORT_MAX and port not in out:
            out.append(port)
    return out


def _model_path_from_cmd(cmd: list[str]) -> str:
    parts = [str(part) for part in cmd]
    for flag in ("-m", "--model"):
        if flag in parts:
            index = parts.index(flag)
            if index + 1 < len(parts):
                return parts[index + 1]
    return ""


def running_models(home: Path | str | None = None) -> dict:
    """8123/8125/其它已知 llama-server 端口的运行状态（复用 services.port_state）。"""
    items = []
    for port in _ports_of_interest():
        try:
            running, pid = services.port_state(port)
        except Exception:  # noqa: BLE001 - 探测失败视为未运行
            running, pid = False, None
        managed = _MANAGED.get(port) or {}
        model_path = str(managed.get("model_path") or "")
        for name, cfg in getattr(services, "SERVICES", {}).items():
            try:
                if int(cfg.get("port")) == port:
                    model_path = _model_path_from_cmd(cfg.get("cmd") or [])
                    break
            except (TypeError, ValueError):
                continue
        items.append({"port": port, "pid": pid, "running": bool(running),
                      "model_path": model_path,
                      "log": str(MODEL_LOG_DIR / f"{port}.log")})
    return {"ok": True, "items": items,
            "running": sum(1 for item in items if item["running"])}


# ------------------------------------------------------------------ 启停
def _validate_port(port: Any) -> tuple[int | None, str]:
    try:
        value = int(port)
    except (TypeError, ValueError):
        return None, "invalid_port"
    if not (PORT_MIN <= value <= PORT_MAX):
        return None, f"port_out_of_range（只允许 {PORT_MIN}-{PORT_MAX}）"
    return value, ""


def _popen_factory():
    return subprocess.Popen


def start_model(model_path: str | Path, port: int, ngl: int = 99,
                ctx: int = 4096, *, home: Path | str | None = None,
                wait_s: float = START_TIMEOUT_S,
                popen: Callable | None = None,
                port_state: Callable | None = None) -> dict:
    """后台启一个 llama-server（只允许 8100-8199），日志 .data/modellogs/<port>.log。"""
    base = Path(home) if home is not None else agent_home()
    port_value, error = _validate_port(port)
    if port_value is None:
        return {"ok": False, "error": error}
    resolved, error = _allowed(model_path, base)
    if error:
        return {"ok": False, "error": error}
    if resolved is None or not resolved.is_file() or resolved.suffix.lower() != ".gguf":
        return {"ok": False, "error": "model_path_must_be_gguf_file"}
    try:
        ngl_value = int(ngl)
        ctx_value = int(ctx)
    except (TypeError, ValueError):
        return {"ok": False, "error": "invalid_ngl_or_ctx"}
    if not (NGL_MIN <= ngl_value <= NGL_MAX):
        return {"ok": False, "error": f"ngl_out_of_range（{NGL_MIN}-{NGL_MAX}）"}
    if not (CTX_MIN <= ctx_value <= CTX_MAX):
        return {"ok": False, "error": f"ctx_out_of_range（{CTX_MIN}-{CTX_MAX}）"}
    server = llama_server_path()
    if not server.is_file():
        return {"ok": False, "error": "llama_server_missing", "path": str(server)}
    probe = port_state or services.port_state
    running, pid = False, None
    try:
        running, pid = probe(port_value)
    except Exception:  # noqa: BLE001
        running, pid = False, None
    if running:
        return {"ok": False, "error": "port_in_use", "port": port_value,
                "pid": pid}
    log_path = MODEL_LOG_DIR / f"{port_value}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "ab")
    except OSError as exc:
        return {"ok": False, "error": f"log_open_failed: {exc}"}
    cmd = [str(server), "-m", str(resolved), "--port", str(port_value),
           "--host", "127.0.0.1", "-ngl", str(ngl_value), "-c", str(ctx_value)]
    try:
        factory = popen or _popen_factory()
        proc = factory(cmd, cwd=str(base), stdout=log_handle, stderr=log_handle,
                       stdin=subprocess.DEVNULL, close_fds=True,
                       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except Exception as exc:  # noqa: BLE001
        try:
            log_handle.close()
        except OSError:
            pass
        return {"ok": False, "error": f"spawn_failed: {type(exc).__name__}: {exc}"}
    finally:
        try:
            log_handle.close()
        except OSError:
            pass
    deadline = time.time() + max(0.0, float(wait_s))
    while True:
        try:
            up, live_pid = probe(port_value)
        except Exception:  # noqa: BLE001
            up, live_pid = False, None
        if up:
            _RUNNING[port_value] = proc
            _MANAGED[port_value] = {
                "pid": live_pid or getattr(proc, "pid", None),
                "model_path": str(resolved), "log": str(log_path)}
            _record("model.start", str(resolved),
                    port=port_value, pid=live_pid or getattr(proc, "pid", None))
            return {"ok": True, "port": port_value,
                    "pid": live_pid or getattr(proc, "pid", None),
                    "model_path": str(resolved), "log": str(log_path),
                    "cmd": cmd, "started": True}
        if time.time() >= deadline:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False, "error": "start_timeout",
                    "port": port_value, "pid": getattr(proc, "pid", None),
                    "log": str(log_path), "cmd": cmd}
        time.sleep(0.5)


def stop_model(port: int, *, force: bool = True,
               port_state: Callable | None = None,
               taskkill: Callable | None = None) -> dict:
    """按端口 PID 杀 llama-server；只允许 8100-8199。"""
    port_value, error = _validate_port(port)
    if port_value is None:
        return {"ok": False, "error": error}
    probe = port_state or services.port_state
    try:
        running, pid = probe(port_value)
    except Exception:  # noqa: BLE001
        running, pid = False, None
    if not running:
        _RUNNING.pop(port_value, None)
        _MANAGED.pop(port_value, None)
        return {"ok": True, "already_stopped": True, "port": port_value}
    if pid is None:
        proc = _RUNNING.get(port_value)
        pid = getattr(proc, "pid", None)
    if pid is None:
        return {"ok": False, "error": "pid_unknown", "port": port_value,
                "hint": "端口在监听但取不到 PID"}
    args = ["taskkill", "/PID", str(pid)]
    if force:
        args.append("/F")
    runner = taskkill or subprocess.run
    try:
        result = runner(args, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=20)
        detail = (getattr(result, "stdout", "") or getattr(result, "stderr", "") or "").strip()
        code = int(getattr(result, "returncode", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"taskkill_failed: {type(exc).__name__}: {exc}",
                "port": port_value, "pid": pid}
    try:
        still, _ = probe(port_value)
    except Exception:  # noqa: BLE001
        still = False
    _RUNNING.pop(port_value, None)
    _MANAGED.pop(port_value, None)
    _record("model.stop", str(port_value), pid=pid, force=force)
    return {"ok": not still, "port": port_value, "pid": pid,
            "detail": detail, "returncode": code}


# ------------------------------------------------------------------ 子进程工具
def _run_tool(args: list[str], *, cwd: Path, timeout: int,
              run: Callable | None = None) -> dict:
    runner = run or subprocess.run
    started = time.time()
    try:
        proc = runner([str(part) for part in args], cwd=str(cwd),
                      capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "timeout_s": timeout}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    code = int(getattr(proc, "returncode", 0) or 0)
    stdout = str(getattr(proc, "stdout", "") or "")
    stderr = str(getattr(proc, "stderr", "") or "")
    return {"ok": code == 0, "returncode": code,
            "elapsed_s": round(time.time() - started, 2),
            "stdout_tail": stdout[-4000:], "stderr_tail": stderr[-4000:]}


def _tool_script(name: str) -> tuple[Path | None, str]:
    path = soulagent_home() / "tools" / "soul_serve" / name
    if not path.is_file():
        return None, "tool_script_missing"
    return path, ""


def _venv_python() -> Path:
    raw = _env("SOULAGENT_PY312")
    if raw:
        return Path(raw)
    path = agent_home() / "venv312" / "Scripts" / "python.exe"
    return path if path.is_file() else Path(sys.executable)


def _tail_payload(result: dict, **extra: Any) -> dict:
    payload = {"ok": bool(result.get("ok")), "returncode": result.get("returncode"),
               "elapsed_s": result.get("elapsed_s"),
               "stdout_tail": result.get("stdout_tail", ""),
               "stderr_tail": result.get("stderr_tail", "")}
    if not payload["ok"]:
        payload["error"] = result.get("error") or f"exit_{result.get('returncode')}"
    payload.update(extra)
    return payload


# ------------------------------------------------------------------ 合并 adapter
def merge_adapter(adapter: str | Path, out: str | Path | None = None, *,
                  base: str | Path | None = None,
                  device: str = "cpu", dtype: str = "float16",
                  home: Path | str | None = None,
                  run: Callable | None = None,
                  timeout: int = MERGE_TIMEOUT_S) -> dict:
    """调 SoulAgent ``tools/soul_serve/merge_adapter.py``（--base/--adapter/--out）。"""
    root = Path(home) if home is not None else agent_home()
    adapter_dir, error = _allowed(adapter, root)
    if error:
        return {"ok": False, "error": error}
    if adapter_dir is None or not adapter_dir.is_dir():
        return {"ok": False, "error": "adapter_not_found"}
    config_path = adapter_dir / "adapter_config.json"
    if not config_path.is_file():
        return {"ok": False, "error": "adapter_config_missing"}
    meta = _adapter_meta(adapter_dir)
    if base is None:
        base = meta.get("base_model") or ""
    if not str(base or "").strip():
        return {"ok": False, "error": "base_model_missing",
                "adapter": str(adapter_dir)}
    base_dir, error = _allowed(base, root)
    if error:
        return {"ok": False, "error": "base_" + error}
    if base_dir is None or not base_dir.is_dir():
        # adapter_config 里可能写的是 HF id（如 Qwen/Qwen2.5-0.5B-Instruct）：
        # 再按 <models>/<basename> 找一次本地目录。
        fallback = root / "models" / Path(str(base or "")).name
        if fallback.is_dir():
            base_dir = fallback
        else:
            return {"ok": False, "error": "base_model_not_found",
                    "base": str(base or "")}
    out_dir = _resolve(out, root) if out else root / "output" / f"merged_{adapter_dir.name}"
    if not _is_under(out_dir, root):
        return {"ok": False, "error": "out_outside_agent_home"}
    if out_dir.exists() and any(out_dir.iterdir()):
        return {"ok": False, "error": "out_exists_not_empty", "out": str(out_dir)}
    if device not in ("cpu", "cuda"):
        return {"ok": False, "error": "invalid_device"}
    if dtype not in ("float16", "bfloat16", "float32"):
        return {"ok": False, "error": "invalid_dtype"}
    script, error = _tool_script("merge_adapter.py")
    if error:
        return {"ok": False, "error": error}
    python = _venv_python()
    cmd = [str(python), str(script), "--base", str(base_dir),
           "--adapter", str(adapter_dir), "--out", str(out_dir),
           "--device", device, "--dtype", dtype]
    result = _run_tool(cmd, cwd=soulagent_home(), timeout=timeout, run=run)
    _record("model.merge", str(adapter_dir), out=str(out_dir), ok=result.get("ok"))
    return _tail_payload(result, adapter=str(adapter_dir), base=str(base_dir),
                         out=str(out_dir), cmd=cmd)


# ------------------------------------------------------------------ 转 GGUF
def convert_gguf(merged_dir: str | Path, quant: str = "Q4_K_M", *,
                 name: str | None = None,
                 out_dir: str | Path | None = None,
                 home: Path | str | None = None,
                 run: Callable | None = None,
                 timeout: int = CONVERT_TIMEOUT_S) -> dict:
    """调 SoulAgent ``tools/soul_serve/convert.py``（--hf-dir/--name/--out-dir/--quant）。"""
    root = Path(home) if home is not None else agent_home()
    hf_dir, error = _allowed(merged_dir, root)
    if error:
        return {"ok": False, "error": error}
    if hf_dir is None or not hf_dir.is_dir():
        return {"ok": False, "error": "merged_dir_not_found"}
    quant_value = str(quant or "").strip()
    if not quant_value or not _QUANT_RE.match(quant_value):
        return {"ok": False, "error": "invalid_quant"}
    display_name = str(name or "").strip()
    if display_name:
        if not _QUANT_RE.match(display_name):
            return {"ok": False, "error": "invalid_name"}
    else:
        display_name = hf_dir.name
        if display_name.startswith("merged_"):
            display_name = display_name[len("merged_"):]
    target_dir = _resolve(out_dir, root) if out_dir else root / "output" / "gguf"
    if not _is_under(target_dir, root):
        return {"ok": False, "error": "out_outside_agent_home"}
    script, error = _tool_script("convert.py")
    if error:
        return {"ok": False, "error": error}
    python = _venv_python()
    cmd = [str(python), str(script), "--hf-dir", str(hf_dir),
           "--name", display_name, "--out-dir", str(target_dir),
           "--quant", quant_value,
           "--llama-bin", str(root / "llama" / "cuda"),
           "--convert-dir", str(root / "llama" / "convert")]
    result = _run_tool(cmd, cwd=soulagent_home(), timeout=timeout, run=run)
    _record("model.convert", str(hf_dir), out=str(target_dir), quant=quant_value,
            ok=result.get("ok"))
    return _tail_payload(result, merged_dir=str(hf_dir), name=display_name,
                         out_dir=str(target_dir), quant=quant_value, cmd=cmd)


def status(home: Path | str | None = None) -> dict:
    """控制台模型中心总览（清单 + 运行中 + 磁盘 + runtime）。"""
    base = Path(home) if home is not None else agent_home()
    return {"ok": True, "agent_home": str(base),
            "catalog": list_models(base), "running": running_models(base),
            "disk": disk_usage(base), "adapters": list_adapters(base)}


# ================================================================== #
# 在线下载 / 离线导入 / 删除 / 格式校验 / 清单增强（task-147）
# ================================================================== #
MODELS_DIRNAME = "models"
DOWNLOADS_DIRNAME = "downloads"
IMPORTED_DIRNAME = "imported"
TRASH_DIRNAME = ".trash"
#: 下载进度状态文件（工作台数据目录，gitignore）
DOWNLOAD_STATE = paths.DATA_DIR / "downloads.json"
_DL_LOCK = threading.Lock()
_DL_CANCEL: set[str] = set()
_DL_ACTIVE: dict[str, dict] = {}


class _DownloadCancelled(Exception):
    pass


def _models_root(home: Path | str | None = None) -> Path:
    base = Path(home) if home is not None else agent_home()
    return base / MODELS_DIRNAME


def _safe_name(name: Any) -> str | None:
    """模型/文件名校验：不含路径分隔符、不以点开头、长度 1–120。"""
    value = str(name or "").strip()
    if not value or len(value) > 120:
        return None
    if value.startswith(".") or value in (".", ".."):
        return None
    for char in value:
        if char in ("/", "\\", ":", "*", "?", '"', "<", ">", "|"):
            return None
        if ord(char) < 32:
            return None
    return value


def _safe_file_name(name: Any) -> str | None:
    value = _safe_name(Path(str(name or "")).name)
    return value


def _now_name() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


# ------------------------------------------------------------------ 状态文件
def _state_path(state_path: Path | str | None = None) -> Path:
    return Path(state_path) if state_path is not None else DOWNLOAD_STATE


def _read_state(state_path: Path | str | None = None) -> dict:
    path = _state_path(state_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_state(state: dict, state_path: Path | str | None = None) -> None:
    path = _state_path(state_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        state["updated_at"] = time.time()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def _job_update(job_id: str, state_path: Path | str | None = None, **fields: Any) -> None:
    with _DL_LOCK:
        state = _read_state(state_path)
        jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
        job = dict(jobs.get(job_id) or {})
        if not job:
            return
        job.update(fields)
        job["updated_at"] = time.time()
        jobs[job_id] = job
        state["jobs"] = jobs
        _write_state(state, state_path)
        if job_id in _DL_ACTIVE:
            _DL_ACTIVE[job_id].update(fields)


def download_status(job_id: str | None = None,
                    state_path: Path | str | None = None) -> dict:
    """读下载进度：不传 job_id 返回全部（最近的在前），传则单条。"""
    state = _read_state(state_path)
    jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
    items = list(jobs.values())
    items.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    if job_id:
        job = jobs.get(str(job_id))
        if not job:
            return {"ok": False, "error": "not_found", "job_id": str(job_id)}
        return {"ok": True, "job": job}
    return {"ok": True, "items": items, "count": len(items)}


def cancel_download(job_id: str,
                    state_path: Path | str | None = None) -> dict:
    """请求取消后台下载；线程在下个 chunk 检查到后标 cancelled。"""
    state = _read_state(state_path)
    jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
    job = jobs.get(str(job_id))
    if not job:
        return {"ok": False, "error": "not_found", "job_id": str(job_id)}
    if job.get("status") in ("done", "failed", "cancelled"):
        return {"ok": True, "job": job, "already_finished": True}
    _DL_CANCEL.add(str(job_id))
    _job_update(str(job_id), state_path, status="cancelling")
    return {"ok": True, "job_id": str(job_id), "status": "cancelling"}


# ------------------------------------------------------------------ 格式校验
def _read_head(path: Path, size: int = 512) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(size)
    except OSError:
        return b""


def _looks_like_html(head: bytes) -> bool:
    value = head[:512].lstrip().lower()
    return (value.startswith(b"<!doctype") or value.startswith(b"<html")
            or b"<title>404" in value or b"404 not found" in value
            or b"access denied" in value)


def validate_model_path(path: Path | str,
                        home: Path | str | None = None) -> dict:
    """校验一个模型路径的格式：gguf / hf（目录）/ adapter（目录）。"""
    target = _resolve(path, home)
    reasons: list[str] = []
    if not target.exists():
        return {"ok": False, "format": "", "path": str(target),
                "reasons": ["path_not_found"], "size": 0, "files": 0}
    size, files = _size_of(target)
    if target.is_file():
        name = target.name.lower()
        if name.endswith(".gguf"):
            head = _read_head(target, 4)
            if head != b"GGUF":
                reasons.append("bad_gguf_magic")
            if size <= 0:
                reasons.append("empty_file")
            return {"ok": not reasons, "format": "gguf", "path": str(target),
                    "reasons": reasons, "size": size, "files": files}
        if name.endswith(".safetensors"):
            if size <= 0:
                reasons.append("empty_file")
            if _looks_like_html(_read_head(target, 512)):
                reasons.append("looks_like_html_error_page")
            reasons.append("safetensors_requires_hf_dir")
            return {"ok": False, "format": "safetensors", "path": str(target),
                    "reasons": reasons, "size": size, "files": files}
        return {"ok": False, "format": "", "path": str(target),
                "reasons": ["unsupported_file"], "size": size, "files": files}
    # 目录
    adapter_config = target / "adapter_config.json"
    if adapter_config.is_file():
        try:
            payload = json.loads(adapter_config.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                reasons.append("adapter_config_not_object")
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"adapter_config_invalid:{type(exc).__name__}")
        return {"ok": not reasons, "format": "adapter", "path": str(target),
                "reasons": reasons, "size": size, "files": files}
    config = target / "config.json"
    if config.is_file():
        try:
            payload = json.loads(config.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            payload = None
            reasons.append(f"config_json_invalid:{type(exc).__name__}")
        if isinstance(payload, dict):
            if not str(payload.get("model_type") or "").strip():
                reasons.append("config_missing_model_type")
        if not any(target.glob("*.safetensors")):
            reasons.append("no_safetensors_weights")
        return {"ok": not reasons, "format": "hf", "path": str(target),
                "reasons": reasons, "size": size, "files": files}
    gguf_files = list(target.glob("*.gguf"))
    if gguf_files:
        if _read_head(gguf_files[0], 4) != b"GGUF":
            reasons.append("bad_gguf_magic")
        return {"ok": not reasons, "format": "gguf", "path": str(target),
                "reasons": reasons, "size": size, "files": files}
    return {"ok": False, "format": "", "path": str(target),
            "reasons": ["unsupported_dir"], "size": size, "files": files}


# ------------------------------------------------------------------ 在线下载
def _download_url_for(spec: dict) -> tuple[str, str]:
    """返回 (url, 建议文件名)。支持直链与 HF（hf-mirror 直链兜底）。"""
    url = str(spec.get("url") or "").strip()
    if url:
        base = Path(unquote(urlsplit(url).path)).name or "model.gguf"
        return url, str(spec.get("filename") or base)
    repo = str(spec.get("repo") or "").strip().strip("/")
    filename = str(spec.get("file") or "model.safetensors").strip()
    mirror = str(spec.get("mirror") or "hf-mirror.com").strip().strip("/")
    return (f"https://{mirror}/{repo}/resolve/main/{filename}",
            Path(filename).name)


def _validate_downloaded(path: Path) -> dict:
    if not path.is_file() or path.stat().st_size <= 0:
        return {"ok": False, "error": "empty_download", "format": ""}
    size = int(path.stat().st_size)
    suffix = path.suffix.lower()
    if suffix == ".gguf":
        if _read_head(path, 4) != b"GGUF":
            return {"ok": False, "error": "bad_gguf_magic", "format": "gguf",
                    "size": size}
        return {"ok": True, "format": "gguf", "size": size}
    head = _read_head(path, 512)
    if _looks_like_html(head):
        return {"ok": False, "error": "downloaded_html_error_page",
                "format": "", "size": size}
    if suffix == ".safetensors":
        return {"ok": True, "format": "hf", "size": size}
    return {"ok": True, "format": "unknown", "size": size}


def _stream_download(url: str, target: Path, job_id: str,
                     state_path: Path | str | None = None) -> int:
    """默认实现：httpx 流式下载并写进度；测试可 monkeypatch 本函数。"""
    import httpx

    done = 0
    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=60.0) as response:
        if response.status_code >= 400:
            raise RuntimeError(f"http_{response.status_code}")
        try:
            total = int(response.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            total = 0
        with target.open("wb") as handle:
            for chunk in response.iter_bytes(65536):
                if job_id in _DL_CANCEL:
                    raise _DownloadCancelled()
                if not chunk:
                    continue
                handle.write(chunk)
                done += len(chunk)
                _job_update(job_id, state_path, bytes_done=done, total=total)
    return done


def _hf_hub_download(repo: str, filename: str, target_dir: Path) -> Path | None:
    """优先 huggingface_hub；未安装返回 None（调用方走 hf-mirror 直链）。"""
    try:
        from huggingface_hub import hf_hub_download  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        try:
            path = hf_hub_download(repo_id=repo, filename=filename,
                                   local_dir=str(target_dir),
                                   local_dir_use_symlinks=False)
        except TypeError:  # 新版去掉了 local_dir_use_symlinks
            path = hf_hub_download(repo_id=repo, filename=filename,
                                   local_dir=str(target_dir))
        return Path(path)
    except Exception:  # noqa: BLE001 - 失败交给直链兜底
        return None


def _download_worker(job_id: str, spec: dict, target: Path, state_path: Path) -> None:
    try:
        _job_update(job_id, state_path, status="downloading",
                    started_at=time.time())
        url, _suggested = _download_url_for(spec)
        done = 0
        file_name = target.name
        if spec.get("repo"):
            hf_path = _hf_hub_download(str(spec.get("repo")), file_name,
                                       target.parent)
            if hf_path is not None:
                if hf_path.resolve() != target.resolve():
                    if target.exists():
                        target.unlink()
                    try:
                        os.link(hf_path, target)
                    except OSError:
                        shutil.copy2(hf_path, target)
                done = int(target.stat().st_size)
                _job_update(job_id, state_path, bytes_done=done, total=done)
            else:
                done = _stream_download(url, target, job_id, state_path)
        else:
            done = _stream_download(url, target, job_id, state_path)
        if job_id in _DL_CANCEL:
            raise _DownloadCancelled()
        checked = _validate_downloaded(target)
        if not checked.get("ok"):
            raise RuntimeError(str(checked.get("error") or "invalid_download"))
        _job_update(job_id, state_path, status="done", bytes_done=done,
                    total=done, finished_at=time.time(),
                    format=checked.get("format") or "",
                    target=str(target), source="downloaded", error="")
    except _DownloadCancelled:
        _safe_remove(target)
        _job_update(job_id, state_path, status="cancelled",
                    finished_at=time.time(), error="cancelled")
    except Exception as exc:  # noqa: BLE001
        _safe_remove(target)
        _job_update(job_id, state_path, status="failed",
                    finished_at=time.time(),
                    error=f"{type(exc).__name__}: {exc}")
    finally:
        _DL_CANCEL.discard(str(job_id))
        _DL_ACTIVE.pop(str(job_id), None)


def _safe_remove(path: Path) -> None:
    try:
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def download_model(spec: dict, home: Path | str | None = None,
                   state_path: Path | str | None = None) -> dict:
    """在线下载（直链或 HF 仓库）；后台线程执行，立即返回 job_id。

    spec:
      - 直链：{"name":"...", "url":"https://.../model.gguf", "filename"?: "model.gguf"}
      - HF：{"name":"...", "repo":"Qwen/Qwen2.5-0.5B-Instruct",
             "file":"model.safetensors"|None, "mirror":"hf-mirror.com"|None}
    """
    if not isinstance(spec, dict):
        return {"ok": False, "error": "invalid_spec"}
    name = _safe_name(spec.get("name"))
    if not name:
        return {"ok": False, "error": "invalid_name"}
    if not str(spec.get("url") or "").strip() and not str(spec.get("repo") or "").strip():
        return {"ok": False, "error": "url_or_repo_required"}
    if spec.get("url") and not str(spec.get("url")).startswith(("http://", "https://")):
        return {"ok": False, "error": "invalid_url"}
    url, suggested = _download_url_for(spec)
    file_name = _safe_file_name(spec.get("filename") or suggested)
    if not file_name:
        return {"ok": False, "error": "invalid_filename"}
    target_dir = _models_root(home) / DOWNLOADS_DIRNAME / name
    target = target_dir / file_name
    if target_dir.exists() and any(target_dir.iterdir()) and not spec.get("overwrite"):
        return {"ok": False, "error": "target_exists",
                "hint": "传 overwrite=true 覆盖", "target": str(target_dir)}
    target_dir.mkdir(parents=True, exist_ok=True)
    job_id = "DL-" + uuid.uuid4().hex[:12]
    suffix = Path(file_name).suffix.lower()
    fmt = "gguf" if suffix == ".gguf" else ("hf" if suffix == ".safetensors" else "unknown")
    state_path_obj = _state_path(state_path)
    with _DL_LOCK:
        state = _read_state(state_path_obj)
        jobs = state.get("jobs") if isinstance(state.get("jobs"), dict) else {}
        jobs[job_id] = {
            "job_id": job_id, "name": name, "status": "queued",
            "bytes_done": 0, "total": 0, "target": str(target),
            "format": fmt, "source": "downloaded", "url": url,
            "repo": str(spec.get("repo") or ""), "file": file_name,
            "created_at": time.time(), "updated_at": time.time(), "error": "",
        }
        state["jobs"] = jobs
        _write_state(state, state_path_obj)
    _DL_ACTIVE[job_id] = {"target": str(target)}
    worker = threading.Thread(target=_download_worker,
                              args=(job_id, dict(spec), target, state_path_obj),
                              name="model-download-" + job_id, daemon=True)
    worker.start()
    return {"ok": True, "job_id": job_id, "name": name, "status": "queued",
            "target": str(target), "format": fmt, "source": "downloaded"}


def wait_download(job_id: str, *, timeout: float = 30.0,
                  interval: float = 0.05,
                  state_path: Path | str | None = None) -> dict:
    """轮询等待后台下载结束（测试/CLI 用；失败/取消也返回）。"""
    import time as _time
    deadline = _time.time() + max(0.0, float(timeout))
    while True:
        result = download_status(job_id, state_path)
        job = result.get("job") if result.get("ok") else None
        if job and job.get("status") in ("done", "failed", "cancelled"):
            return {"ok": job.get("status") == "done", "job": job,
                    "status": job.get("status"), "error": job.get("error") or ""}
        if _time.time() >= deadline:
            return {"ok": False, "error": "timeout", "job": job,
                    "status": (job or {}).get("status") or "unknown"}
        _time.sleep(max(0.01, float(interval)))


# ------------------------------------------------------------------ 离线导入
def _copy_or_link(source: Path, target: Path) -> None:
    """同盘优先硬链接；跨盘/失败回退复制。"""
    try:
        os.link(source, target)
        return
    except OSError:
        pass
    shutil.copy2(source, target)


def _copy_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        rel = item.relative_to(source)
        destination = target / rel
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            destination.parent.mkdir(parents=True, exist_ok=True)
            _copy_or_link(item, destination)


def import_model(spec: dict, home: Path | str | None = None) -> dict:
    """离线导入本地 gguf / HF 目录 / adapter 目录到 ``models/imported/<name>/``。

    spec: {"name": "...", "path": "本地路径", "format": "gguf|hf|adapter" (可省略自动识别),
           "overwrite": false}
    """
    if not isinstance(spec, dict):
        return {"ok": False, "error": "invalid_spec"}
    name = _safe_name(spec.get("name"))
    if not name:
        return {"ok": False, "error": "invalid_name"}
    source_raw = str(spec.get("path") or "").strip()
    if not source_raw:
        return {"ok": False, "error": "path_required"}
    source = Path(os.path.expanduser(os.path.expandvars(source_raw)))
    if not source.exists():
        return {"ok": False, "error": "source_not_found", "path": str(source)}
    checked = validate_model_path(source)
    detected = str(checked.get("format") or "")
    requested = str(spec.get("format") or "").strip().lower()
    if requested and requested not in ("gguf", "hf", "adapter", "auto"):
        return {"ok": False, "error": "invalid_format", "format": requested}
    if not checked.get("ok"):
        return {"ok": False, "error": "invalid_model", "format": detected,
                "reasons": checked.get("reasons") or [], "path": str(source)}
    if requested and requested != "auto" and requested != detected:
        return {"ok": False, "error": "format_mismatch",
                "expected": requested, "detected": detected}
    target_root = _models_root(home) / IMPORTED_DIRNAME
    target = target_root / name
    if source.resolve() == target.resolve() or _is_under(source, target):
        return {"ok": False, "error": "source_inside_target",
                "source": str(source), "target": str(target)}
    if target.exists():
        if not spec.get("overwrite"):
            return {"ok": False, "error": "target_exists",
                    "hint": "传 overwrite=true 覆盖", "target": str(target)}
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            return {"ok": False, "error": f"overwrite_failed: {exc}"}
    try:
        target_root.mkdir(parents=True, exist_ok=True)
        source_size, source_files = _size_of(source)
        if source.is_dir():
            _copy_tree(source, target)
        else:
            target.mkdir(parents=True, exist_ok=True)
            _copy_or_link(source, target / source.name)
        target_size, target_files = _size_of(target)
    except Exception as exc:  # noqa: BLE001
        _safe_remove(target)
        return {"ok": False, "error": f"import_failed: {type(exc).__name__}: {exc}"}
    if target_size != source_size:
        _safe_remove(target)
        return {"ok": False, "error": "size_mismatch",
                "source_size": source_size, "target_size": target_size}
    _record("model.import", str(target), source=str(source), format=detected,
            size=target_size)
    return {"ok": True, "name": name, "path": str(target), "format": detected,
            "source": "imported", "size": target_size, "files": target_files,
            "imported_from": str(source)}


# ------------------------------------------------------------------ 删除（进回收站）
def _find_deletable(name: str, home: Path | str | None) -> Path | None:
    root = _models_root(home)
    candidates = [root / name, root / DOWNLOADS_DIRNAME / name,
                  root / IMPORTED_DIRNAME / name]
    for candidate in candidates:
        try:
            if candidate.exists() and candidate.resolve() != root.resolve():
                return candidate
        except OSError:
            continue
    return None


def delete_model(spec: dict, home: Path | str | None = None) -> dict:
    """删除模型条目：默认移动到 ``models/.trash/<时间戳>/``（可恢复）。

    spec: {"name": "...", "path"?: "...", "kind"?: "gguf|hf|adapter"}
    """
    if not isinstance(spec, dict):
        return {"ok": False, "error": "invalid_spec"}
    root = _models_root(home)
    candidate: Path | None = None
    raw_path = str(spec.get("path") or "").strip()
    if raw_path:
        candidate = _resolve(raw_path, home)
        if not _is_under(candidate, root) or candidate.resolve() == root.resolve():
            return {"ok": False, "error": "path_outside_models"}
        if candidate == root / TRASH_DIRNAME or _is_under(candidate, root / TRASH_DIRNAME):
            return {"ok": False, "error": "cannot_delete_trash"}
    else:
        name = _safe_name(spec.get("name"))
        if not name:
            return {"ok": False, "error": "invalid_name"}
        candidate = _find_deletable(name, home)
        if candidate is None:
            return {"ok": False, "error": "not_found", "name": name}
    kind = str(spec.get("kind") or "").strip().lower()
    if kind:
        checked = validate_model_path(candidate)
        if checked.get("format") != kind:
            return {"ok": False, "error": "kind_mismatch",
                    "expected": kind, "detected": checked.get("format") or ""}
    trash_parent = root / TRASH_DIRNAME / _now_name()
    target = trash_parent / candidate.name
    suffix = 1
    while target.exists():
        target = trash_parent / f"{candidate.name}-{suffix}"
        suffix += 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(candidate), str(target))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"move_failed: {type(exc).__name__}: {exc}"}
    _record("model.delete", str(candidate), moved_to=str(target))
    return {"ok": True, "name": candidate.name, "path": str(candidate),
            "moved_to": str(target)}


# ------------------------------------------------------------------ 清单/详情增强
def _entry_format(path: Path) -> str:
    checked = validate_model_path(path)
    return str(checked.get("format") or "")


def _inventory_entry(path: Path, *, source: str, fmt: str | None = None,
                     kind: str | None = None) -> dict:
    size, files = _size_of(path)
    format_value = fmt if fmt is not None else _entry_format(path)
    kind_value = kind or ("adapter" if format_value == "adapter"
                          else "gguf" if format_value == "gguf" else "base")
    return {
        "name": path.name,
        "path": str(path),
        "format": format_value,
        "kind": kind_value,
        "size": size,
        "files": files,
        "source": source,
        "mtime": _mtime_of(path),
        "usable_for_train": format_value == "hf",
        "usable_for_serve": format_value == "gguf",
    }


def model_inventory(home: Path | str | None = None) -> dict:
    """扩展清单：builtin/downloaded/imported + output gguf/adapter，带格式与用途。"""
    base = Path(home) if home is not None else agent_home()
    root = base / MODELS_DIRNAME
    items: list[dict] = []
    seen: set[str] = set()

    def add(entry: dict) -> None:
        key = entry["path"]
        if key in seen:
            return
        seen.add(key)
        items.append(entry)

    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.name in (DOWNLOADS_DIRNAME, IMPORTED_DIRNAME, TRASH_DIRNAME):
                continue
            if child.is_dir() and (child / "adapter_config.json").is_file():
                add(_inventory_entry(child, source="builtin", fmt="adapter"))
            elif child.is_dir() and (child / "config.json").is_file():
                add(_inventory_entry(child, source="builtin", fmt="hf"))
            elif child.is_file() and child.suffix.lower() == ".gguf":
                add(_inventory_entry(child, source="builtin", fmt="gguf"))
    for sub, source in ((DOWNLOADS_DIRNAME, "downloaded"),
                        (IMPORTED_DIRNAME, "imported")):
        directory = root / sub
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if child.is_dir():
                fmt = _entry_format(child)
                if fmt:
                    add(_inventory_entry(child, source=source, fmt=fmt))
            elif child.is_file() and child.suffix.lower() == ".gguf":
                add(_inventory_entry(child, source=source, fmt="gguf"))
    output = base / "output"
    if (output / "gguf").is_dir():
        for path in sorted((output / "gguf").glob("*.gguf")):
            add(_inventory_entry(path, source="builtin", fmt="gguf"))
    adapter_dirs = _adapter_dirs(output / "adapters")
    existing = {str(path.resolve()) for path in adapter_dirs}
    for path in _adapter_dirs(output):
        key = str(path.resolve())
        if key not in existing:
            adapter_dirs.append(path)
            existing.add(key)
    for path in adapter_dirs:
        add(_inventory_entry(path, source="builtin", fmt="adapter"))
    items.sort(key=lambda row: (row["source"], row["format"], row["name"].lower()))
    counts: dict[str, int] = {}
    for item in items:
        counts[item["format"] or "unknown"] = counts.get(item["format"] or "unknown", 0) + 1
    return {"ok": True, "agent_home": str(base), "items": items,
            "count": len(items), "counts": counts}
