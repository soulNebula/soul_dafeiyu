# -*- coding: utf-8 -*-
"""本地模型注册表（P3-c）：预留接口、健康检查、启停、缺文件校验。

设计目标：
- 用户要求 **1.5B 模型不要删，后面可能做别的事**：注册表把模型登记成 ``reserved`` 状态；
  预留端口 8124、GGUF 指向已有文件，但默认不启动、不合并；
- 以后任何新适配器（0.5B/1.5B/2B…）只需要改 ``agi/data/local_models.json`` 或在
  ``tools/soul_serve/prepare_model.py`` 生成后登记，就能被统一发现/启动/健康检查；
- ``start/stop`` 只操作注册表里登记的模型与端口，并且只 kill 自己启动的 PID，
  绝不碰用户其他进程。

配置：``agi/data/local_models.json``（gitignore），结构：
::

    {
      "llama_bin": "E:/agent/llama/cuda",
      "torch_lib": "E:/agent/venv312/Lib/site-packages/torch/lib",
      "models": [
        {"name":"soul-05b","base":"...","adapter":"...","gguf":"...",
         "port":8123,"role":"soul","status":"ready","purpose":"..."},
        {"name":"qwen15","base":"...","adapter":null,
         "gguf":"E:/agent/output/gguf/soul_qwen25_15b_v1-q4_k_m.gguf",
         "port":8124,"role":"reserved","status":"reserved","purpose":"..."}
      ],
      "extra_adapters": [
        {"name":"tone_qwen15_v1","path":"...","base":"...","purpose":"..."}
      ]
    }

CLI::

    python -m agi.selflearn.model_registry --list
    python -m agi.selflearn.model_registry --get soul-05b
    python -m agi.selflearn.model_registry --resolve reserved
    python -m agi.selflearn.model_registry --health qwen15
    python -m agi.selflearn.model_registry --missing
    python -m agi.selflearn.model_registry --start soul-05b
    python -m agi.selflearn.model_registry --stop soul-05b
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "agi" / "data" / "local_models.json"
STATE_PATH = ROOT / "agi" / "data" / "local_models_state.json"
DEFAULT_LLAMA_BIN = Path(r"E:\agent\llama\cuda")
DEFAULT_TORCH_LIB = Path(r"E:\agent\venv312\Lib\site-packages\torch\lib")

_HEALTH_CACHE: dict[tuple[str, float], tuple[float, bool]] = {}
_HEALTH_TTL = 5.0


# --------------------------------------------------------------------------- #
# 配置读取
# --------------------------------------------------------------------------- #
def _empty_config() -> dict:
    return {"llama_bin": str(DEFAULT_LLAMA_BIN), "torch_lib": str(DEFAULT_TORCH_LIB),
            "models": [], "extra_adapters": []}


def load_config(path: Path | None = None) -> dict:
    """读注册表；文件缺失/损坏时返回空表（不抛异常）。"""
    config_path = Path(path) if path else CONFIG_PATH
    if not config_path.exists():
        return _empty_config()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_config()
    if not isinstance(payload, dict):
        return _empty_config()
    config = _empty_config()
    config.update({key: value for key, value in payload.items() if key in config})
    if not isinstance(config.get("models"), list):
        config["models"] = []
    if not isinstance(config.get("extra_adapters"), list):
        config["extra_adapters"] = []
    return config


def list_models(path: Path | None = None, role: str | None = None) -> list[dict]:
    """列出所有已登记模型（可过滤 role）。"""
    models = [copy.deepcopy(item) for item in load_config(path).get("models", [])
              if isinstance(item, dict)]
    if role:
        models = [item for item in models if item.get("role") == role]
    return models


def get(name: str, path: Path | None = None) -> dict | None:
    """按 name 取模型条目；不存在返回 None。"""
    for item in list_models(path):
        if item.get("name") == name:
            return item
    return None


def resolve_by_role(role: str, path: Path | None = None) -> list[dict]:
    """按 role 找模型（返回列表，便于同角色多模型）。"""
    return list_models(path, role=role)


def list_extra_adapters(path: Path | None = None) -> list[dict]:
    return [copy.deepcopy(item) for item in load_config(path).get("extra_adapters", [])
            if isinstance(item, dict)]


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
def _probe_health(port: int, timeout: float = 0.5) -> bool:
    """GET http://127.0.0.1:<port>/health；服务未启动返回 False（不抛异常）。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{int(port)}/health",
                                    timeout=timeout) as response:
            return 200 <= int(response.status) < 300
    except Exception:  # noqa: BLE001 - 端口未开/超时/HTTP 错误都视为不健康
        return False


def health(name: str, *, path: Path | None = None, ttl: float = _HEALTH_TTL) -> bool:
    """模型健康探测（/health，带 TTL 缓存）；未知模型/无端口返回 False，不抛异常。"""
    model = get(name, path)
    if not model or not model.get("port"):
        return False
    port = int(model["port"])
    key = (str(name), port)
    cached = _HEALTH_CACHE.get(key)
    now = time.monotonic()
    if cached and now - cached[0] <= ttl:
        return cached[1]
    value = _probe_health(port)
    _HEALTH_CACHE[key] = (now, value)
    return value


# --------------------------------------------------------------------------- #
# 缺文件校验
# --------------------------------------------------------------------------- #
def _entry_paths(entry: dict) -> list[tuple[str, str]]:
    paths = []
    for kind in ("base", "adapter", "gguf", "path"):
        value = entry.get(kind)
        if value:
            paths.append((kind, str(value)))
    return paths


def missing_files(name: str | None = None, path: Path | None = None) -> list[dict]:
    """校验登记的文件是否存在；返回 [{"name","kind","path"}]，不存在才列出。"""
    config = load_config(path)
    entries: list[dict] = []
    if name:
        model = get(name, path)
        if model:
            entries = [model]
        else:
            entries = [item for item in config.get("extra_adapters", [])
                       if isinstance(item, dict) and item.get("name") == name]
    else:
        entries = [item for item in config.get("models", []) if isinstance(item, dict)]
        entries += [item for item in config.get("extra_adapters", []) if isinstance(item, dict)]
    problems = []
    for entry in entries:
        for kind, value in _entry_paths(entry):
            if not Path(value).exists():
                problems.append({"name": entry.get("name"), "kind": kind, "path": value})
    return problems


def validate_registry(path: Path | None = None) -> dict:
    """汇总：缺文件 + 重名 + 端口冲突。"""
    config = load_config(path)
    names = [item.get("name") for item in config.get("models", [])]
    ports = [item.get("port") for item in config.get("models", []) if item.get("port")]
    duplicate_names = sorted({name for name in names if names.count(name) > 1})
    duplicate_ports = sorted({port for port in ports if ports.count(port) > 1})
    return {"models": len(config.get("models", [])),
            "extra_adapters": len(config.get("extra_adapters", [])),
            "duplicate_names": duplicate_names, "duplicate_ports": duplicate_ports,
            "missing_files": missing_files(path=path)}


# --------------------------------------------------------------------------- #
# 启动 / 停止（只操作注册表里登记且由本模块启动的进程）
# --------------------------------------------------------------------------- #
def _state_path(path: Path | None = None) -> Path:
    return STATE_PATH if path is None else Path(path).with_name("local_models_state.json")


def _load_state(path: Path | None = None) -> dict:
    state_path = _state_path(path)
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(state: dict, path: Path | None = None) -> None:
    state_path = _state_path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n",
                          encoding="utf-8")


def _port_open(port: int, timeout: float = 0.4) -> bool:
    """端口是否在监听（只连本地回环，不发送业务请求）。"""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _port_pid(port: int) -> int | None:
    """用 netstat 反查监听该端口的 PID；失败返回 None。"""
    try:
        result = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True,
                                text=True, encoding="gbk", errors="replace", timeout=5)
    except Exception:  # noqa: BLE001
        return None
    pattern = re.compile(rf"TCP\s+\S+:{int(port)}\s+\S+\s+LISTENING\s+(\d+)", re.I)
    for line in result.stdout.splitlines():
        match = pattern.search(line)
        if match:
            return int(match.group(1))
    return None


#: 兼容旧名字（之前叫 `_port_owner`）
_port_owner = _port_pid


def _process_cmdline(pid: int) -> str:
    """取进程命令行；优先 psutil，失败回退 PowerShell（只读查询）。"""
    try:
        import psutil  # type: ignore

        return " ".join(psutil.Process(int(pid)).cmdline())
    except Exception:  # noqa: BLE001
        pass
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8)
        return (result.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _process_identity(pid: int | None) -> dict:
    """安全识别：命令行必须同时含 llama-server 和 .gguf 才认为可操作。"""
    if not pid:
        return {"pid": None, "command": "", "is_llama": False, "has_gguf": False,
                "safe": False}
    command = _process_cmdline(int(pid))
    lower = command.lower()
    is_llama = ("llama-server" in lower) or ("llama_server" in lower)
    has_gguf = ".gguf" in lower
    return {"pid": int(pid), "command": command, "is_llama": is_llama,
            "has_gguf": has_gguf, "safe": bool(is_llama and has_gguf)}


def _pid_alive(pid: int) -> bool:
    try:
        result = subprocess.run(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                                capture_output=True, text=True, encoding="gbk",
                                errors="replace", timeout=5)
    except Exception:  # noqa: BLE001
        return False
    return "llama-server.exe" in result.stdout.lower()


def _kill_pid(pid: int, timeout: float = 8.0) -> dict:
    """只 kill 已通过安全识别的 PID；返回 {ok, error?}。"""
    try:
        result = subprocess.run(["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                                capture_output=True, text=True, encoding="gbk",
                                errors="replace", timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"kill_failed:{exc}"}
    if int(getattr(result, "returncode", 0) or 0) != 0:
        detail = ((result.stderr or result.stdout or "")[-300:]).strip()
        return {"ok": False, "error": f"kill_failed:{detail or 'taskkill_nonzero'}"}
    return {"ok": True}


def start(name: str, *, path: Path | None = None, extra_args: list[str] | None = None) -> dict:
    """启动已登记模型；端口已在监听时返回真实 PID（外部启动也报出来）。"""
    model = get(name, path)
    if not model:
        return {"ok": False, "error": f"unknown_model:{name}",
                "hint": f"注册表里没有名为 {name} 的模型"}
    if model.get("role") == "reserved" or model.get("status") == "reserved":
        return {"ok": False, "error": "reserved_model_not_started",
                "hint": "qwen15 为预留模型；如确需启动请先改配置 status/role"}
    port = model.get("port")
    if not port:
        return {"ok": False, "error": "model_has_no_port",
                "hint": "该模型没有登记端口，无法启动"}
    gguf = model.get("gguf")
    state = _load_state(path)
    record = state.get(name) or {}
    if _port_open(int(port)):
        pid = _port_pid(int(port))
        identity = _process_identity(pid)
        external = not (record and int(record.get("pid") or 0) == pid
                        and int(record.get("port") or 0) == int(port))
        if not identity["safe"]:
            return {"ok": False, "error": "port_in_use_by_other_process",
                    "port": int(port), "pid": pid, "external": False,
                    "hint": f"端口 {port} 已被非 llama-server 进程占用（PID {pid}），"
                            f"为安全起见不启动；请先检查。"}
        return {"ok": True, "already_running": True, "pid": pid, "port": int(port),
                "external": bool(external), "healthy": health(name, path=path),
                "hint": f"端口 {port} 已有 llama-server 在运行（PID {pid}，"
                        + ("外部启动" if external else "注册表启动")
                        + "），未重复启动。"}
    if not gguf or not Path(str(gguf)).exists():
        return {"ok": False, "error": "gguf_not_found", "path": str(gguf),
                "hint": f"GGUF 文件不存在：{gguf}"}
    config = load_config(path)
    llama_bin = Path(str(config.get("llama_bin") or DEFAULT_LLAMA_BIN))
    torch_lib = Path(str(config.get("torch_lib") or DEFAULT_TORCH_LIB))
    exe = llama_bin / "llama-server.exe"
    if not exe.exists():
        return {"ok": False, "error": "llama_server_missing", "path": str(exe),
                "hint": f"找不到 llama-server.exe：{exe}"}
    env = dict(os.environ)
    if torch_lib.exists():
        env["PATH"] = str(torch_lib) + os.pathsep + env.get("PATH", "")
    command = [str(exe), "-m", str(gguf), "-ngl", "99", "--host", "127.0.0.1",
               "--port", str(int(port)), "-c", "2048", "-np", "1", "--jinja"]
    command.extend(extra_args or [])
    log_dir = ROOT / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"model_server_{name}.log"
    log_handle = open(log_path, "ab")  # noqa: SIM115 - 交给子进程写
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(command, env=env, stdout=log_handle,
                                   stderr=subprocess.STDOUT, **kwargs)
    except OSError as exc:
        log_handle.close()
        return {"ok": False, "error": f"spawn_failed:{exc}",
                "hint": f"启动 llama-server 失败：{exc}"}
    state = _load_state(path)
    state[name] = {"pid": process.pid, "port": int(port), "gguf": str(gguf),
                   "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                   "log": str(log_path)}
    _save_state(state, path)
    return {"ok": True, "pid": process.pid, "port": int(port), "external": False,
            "command": command, "log": str(log_path),
            "hint": f"已启动 {name}（PID {process.pid}，端口 {port}）"}


def stop(name: str, *, path: Path | None = None, timeout: float = 8.0,
         force_external: bool = False) -> dict:
    """停止模型；无注册表记录时按端口反查，外部进程需 force_external 二次确认。"""
    model = get(name, path)
    if not model:
        return {"ok": False, "error": f"unknown_model:{name}",
                "hint": f"注册表里没有名为 {name} 的模型"}
    port = int(model.get("port") or 0)
    if not port:
        return {"ok": False, "error": "model_has_no_port",
                "hint": "该模型没有登记端口，无法停止"}
    state = _load_state(path)
    record = state.get(name)
    # 1) 注册表记录有效：只 kill 端口 owner 一致且命令行安全的进程
    if record:
        pid = int(record.get("pid") or 0)
        record_port = int(record.get("port") or 0)
        owner = _port_pid(port)
        identity = _process_identity(pid)
        if (record_port == port and pid and _pid_alive(pid)
                and owner == pid and identity["safe"]):
            killed = _kill_pid(pid, timeout=timeout)
            if not killed.get("ok"):
                return {"ok": False, "error": killed.get("error") or "kill_failed",
                        "pid": pid, "port": port,
                        "hint": f"停止 PID {pid} 失败，请手动检查。"}
            state.pop(name, None)
            _save_state(state, path)
            return {"ok": True, "pid": pid, "port": port, "external": False,
                    "hint": f"已停止 {name}（PID {pid}，端口 {port}）"}
        # 记录失效/进程不符：清掉脏记录，转入外部进程识别，绝不误杀
        state.pop(name, None)
        _save_state(state, path)
    # 2) 无有效记录：端口反查 + 命令行安全校验
    if not _port_open(port):
        return {"ok": False, "status": "not_running",
                "error": "not_started_by_registry", "port": port, "pid": None,
                "hint": f"端口 {port} 当前没有在监听，模型可能已经停止。"}
    pid = _port_pid(port)
    identity = _process_identity(pid)
    if not pid:
        return {"ok": False, "status": "pid_unknown", "error": "pid_unknown",
                "port": port,
                "hint": f"端口 {port} 在监听，但查不到 PID；为安全起见不停止。"}
    if not identity["safe"]:
        return {"ok": False, "status": "refused", "error": "unsafe_process",
                "pid": pid, "port": port, "command": identity["command"],
                "hint": f"端口 {port} 被非 llama-server/.gguf 进程占用（PID {pid}），"
                        f"为安全起见拒绝停止，请手动检查。"}
    if not force_external:
        return {"ok": False, "status": "external", "external": True,
                "needs_confirm": True, "pid": pid, "port": port,
                "command": identity["command"],
                "hint": f"检测到 {name} 是外部启动的 llama-server（PID {pid}，"
                        f"端口 {port}，注册表无记录）。停止前请二次确认；"
                        f"确认后带 force_external=true 再调用。"}
    killed = _kill_pid(pid, timeout=timeout)
    if not killed.get("ok"):
        return {"ok": False, "status": "failed", "error": killed.get("error") or "kill_failed",
                "pid": pid, "port": port,
                "hint": f"停止外部 PID {pid} 失败，请手动检查。"}
    return {"ok": True, "pid": pid, "port": port, "external": True,
            "hint": f"已停止外部启动的 {name}（PID {pid}，端口 {port}）"}


def runtime_info(name: str, *, path: Path | None = None) -> dict:
    """运行态摘要（控制台卡片用）：running/pid/external/healthy。

    external=true 表示端口上的 llama-server 不是注册表 state 记录的进程。
    """
    model = get(name, path)
    if not model:
        return {"ok": False, "error": f"unknown_model:{name}", "running": False,
                "pid": None, "external": False, "healthy": False}
    port = model.get("port")
    if not port:
        return {"ok": True, "running": bool(health(name, path=path)), "pid": None,
                "external": False, "healthy": health(name, path=path), "port": None}
    port = int(port)
    running = _port_open(port)
    pid = _port_pid(port) if running else None
    healthy = bool(health(name, path=path)) if running else False
    external = False
    if running:
        record = (_load_state(path).get(name) or {})
        external = not (record and int(record.get("pid") or 0) == pid
                        and int(record.get("port") or 0) == port)
    return {"ok": True, "running": running, "pid": pid, "port": port,
            "external": external, "healthy": healthy}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本地模型注册表")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--get", metavar="NAME")
    parser.add_argument("--resolve", metavar="ROLE")
    parser.add_argument("--health", metavar="NAME")
    parser.add_argument("--health-all", action="store_true")
    parser.add_argument("--missing", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--start", metavar="NAME")
    parser.add_argument("--stop", metavar="NAME")
    parser.add_argument("--force-external", action="store_true",
                        help="确认停止外部启动的 llama-server（二次确认后使用）")
    args = parser.parse_args(argv)
    config = Path(args.config)

    if args.list:
        print(json.dumps(list_models(config), ensure_ascii=False, indent=1))
    elif args.get:
        print(json.dumps(get(args.get, config), ensure_ascii=False, indent=1))
    elif args.resolve:
        print(json.dumps(resolve_by_role(args.resolve, config), ensure_ascii=False, indent=1))
    elif args.health:
        print(json.dumps({"name": args.health, "healthy": health(args.health, path=config)},
                         ensure_ascii=False, indent=1))
    elif args.health_all:
        print(json.dumps({model["name"]: health(model["name"], path=config)
                          for model in list_models(config)},
                         ensure_ascii=False, indent=1))
    elif args.missing:
        print(json.dumps(missing_files(path=config), ensure_ascii=False, indent=1))
    elif args.validate:
        print(json.dumps(validate_registry(config), ensure_ascii=False, indent=1))
    elif args.start:
        print(json.dumps(start(args.start, path=config), ensure_ascii=False, indent=1))
    elif args.stop:
        print(json.dumps(stop(args.stop, path=config,
                              force_external=bool(args.force_external)),
                         ensure_ascii=False, indent=1))
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
