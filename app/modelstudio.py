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
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

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
    targets = (
        ("models", base / "models"),
        ("adapters", base / "output" / "adapters"),
        ("gguf", base / "output" / "gguf"),
    )
    items = []
    total = 0
    for kind, path in targets:
        size, files = _size_of(path)
        items.append({"kind": kind, "dir": str(path), "size": size,
                      "files": files, "exists": path.exists()})
        total += size
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
        model_path = ""
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
