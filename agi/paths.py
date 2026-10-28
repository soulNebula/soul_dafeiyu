# -*- coding: utf-8 -*-
"""P0-3：可配置的路径根（agent home）——不再把 ``E:\\agent`` 写死在代码里。

解析优先级（``agent_home()``）：
1. 环境变量 ``AGENT_HOME``
2. 环境变量 ``SOULAGENT_AGENT_HOME``
3. 配置文件 ``agi/data/paths.json`` 的 ``agent_home`` 字段
4. 默认 ``E:\\agent``（保持老机器零配置行为）

``agi/data/paths.json`` 还可以覆盖各子目录（``models_dir`` / ``gguf_dir`` /
``adapters_dir`` / ``logs_dir`` / ``venv312_python`` / ``llama_bin`` /
``convert_dir`` / ``tmp_dir`` / ``qq_export_dir`` / ``backups_dir``），
环境变量同名大写（如 ``AGENT_MODELS_DIR``）优先于文件。

用法::

    from agi.paths import agent_home, agent_path, models_dir, gguf_dir

    agent_home()                       # Path("D:/agent") 之类
    agent_path("output/gguf")          # <agent_home>/output/gguf
    models_dir() / "Qwen2.5-0.5B-Instruct"

迁移：把 ``E:\\agent`` 挪到 ``D:\\agent``，只改 ``agi/data/paths.json`` 或设
``AGENT_HOME=D:\\agent`` 即可；详见 ``docs/PATHS.md``。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: 仓库根目录（agi/ 的上一级）
REPO_ROOT = Path(__file__).resolve().parents[1]
#: 可选的本地配置文件（不入库；参考 docs/PATHS.md）
PATHS_JSON = REPO_ROOT / "agi" / "data" / "paths.json"
DEFAULT_AGENT_HOME = r"E:\agent"


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _read_config() -> dict:
    try:
        raw = json.loads(PATHS_JSON.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001 - 没配置/坏 JSON 用默认
        return {}


def _resolve(value: Any) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def agent_home() -> Path:
    """agent 数据根：AGENT_HOME → SOULAGENT_AGENT_HOME → paths.json → E:\\agent。"""
    for name in ("AGENT_HOME", "SOULAGENT_AGENT_HOME"):
        value = _env(name)
        if value:
            resolved = _resolve(value)
            if resolved is not None:
                return resolved
    configured = _resolve(_read_config().get("agent_home"))
    return configured if configured is not None else Path(DEFAULT_AGENT_HOME)


#: 数据根 / 日志根 / 根配置文件的环境变量（测试与多实例隔离用；生产不设置即保持历史路径）
DATA_DIR_ENV = "SOULAGENT_DATA_DIR"
LOG_DIR_ENV = "SOULAGENT_LOG_DIR"
CONFIG_ENV = "SOULAGENT_CONFIG_PATH"
WEB_CONFIG_ENV = "SOULAGENT_WEB_CONFIG"


def data_dir() -> Path:
    """仓库内数据根：``SOULAGENT_DATA_DIR`` → 默认 ``<repo>/agi/data``。

    为什么单独抽出来：``agi/wechat_gateway/config.py``（网关配置/草稿/日志）、
    ``agi/selflearn/config.py``（自学习库/日记/冲浪状态）、``agi/soul/dataset.py``
    （消息日志）都把路径写成 import 时求值的常量。测试一旦先 import 这些模块，
    任何 fixture 都改不动它们——只能在 import 之前用环境变量把整个数据根搬走。
    默认值必须与历史路径逐字节一致（``REPO_ROOT/agi/data``），保证生产零变化。
    """
    value = _env(DATA_DIR_ENV)
    if value:
        resolved = _resolve(value)
        if resolved is not None:
            return resolved
    return REPO_ROOT / "agi" / "data"


def log_dir() -> Path:
    """仓库内日志根：``SOULAGENT_LOG_DIR`` → 默认 ``<repo>/log``（默认与历史一致）。"""
    value = _env(LOG_DIR_ENV)
    if value:
        resolved = _resolve(value)
        if resolved is not None:
            return resolved
    return REPO_ROOT / "log"


def config_path() -> Path:
    """根 ``config.json``（含 db_keys 等敏感项）：``SOULAGENT_CONFIG_PATH`` → 默认 ``<repo>/config.json``。"""
    value = _env(CONFIG_ENV)
    if value:
        resolved = _resolve(value)
        if resolved is not None:
            return resolved
    return REPO_ROOT / "config.json"


def web_config_path() -> Path:
    """根 ``web_config.json``（含 LLM API key）：``SOULAGENT_WEB_CONFIG`` → 默认 ``<repo>/web_config.json``。"""
    value = _env(WEB_CONFIG_ENV)
    if value:
        resolved = _resolve(value)
        if resolved is not None:
            return resolved
    return REPO_ROOT / "web_config.json"


def agent_path(*parts: str) -> Path:
    """``agent_home()/parts...``；parts 可以带 ``/`` 或 ``\\``。"""
    path = agent_home()
    for part in parts:
        text = str(part or "").strip().strip("/\\")
        if not text:
            continue
        for piece in text.replace("\\", "/").split("/"):
            if piece:
                path = path / piece
    return path


def _dir(name: str, default_parts: tuple[str, ...]) -> Path:
    env = _env("AGENT_" + name.upper())
    if env:
        resolved = _resolve(env)
        if resolved is not None:
            return resolved
    configured = _resolve(_read_config().get(name))
    if configured is not None:
        return configured
    return agent_path(*default_parts)


def models_dir() -> Path:
    """模型目录（HF/GGUF 原始权重），默认 <home>/models。"""
    return _dir("models_dir", ("models",))


def gguf_dir() -> Path:
    """导出 GGUF 目录，默认 <home>/output/gguf。"""
    return _dir("gguf_dir", ("output", "gguf"))


def adapters_dir() -> Path:
    """LoRA/adapter 训练产物根（各次实验在其中建子目录），默认 <home>/output。"""
    return _dir("adapters_dir", ("output",))


def output_dir() -> Path:
    """训练/导出产物根，默认 <home>/output。"""
    return _dir("output_dir", ("output",))


def logs_dir() -> Path:
    """训练/评测/桌面日志目录，默认 <home>/logs。"""
    return _dir("logs_dir", ("logs",))


def venv312_python() -> Path:
    """Python 3.12 训练/服务 venv 解释器，默认 <home>/venv312/Scripts/python.exe。"""
    env = _env("AGENT_VENV312_PYTHON")
    if env:
        resolved = _resolve(env)
        if resolved is not None:
            return resolved
    configured = _resolve(_read_config().get("venv312_python"))
    if configured is not None:
        return configured
    return agent_path("venv312", "Scripts", "python.exe")


def llama_bin() -> Path:
    """llama.cpp 二进制目录，默认 <home>/llama/cuda。"""
    return _dir("llama_bin", ("llama", "cuda"))


def convert_dir() -> Path:
    """llama.cpp convert 脚本目录，默认 <home>/llama/convert。"""
    return _dir("convert_dir", ("llama", "convert"))


def torch_lib_dir() -> Path:
    """venv312 的 torch/lib（Windows DLL 搜索用），默认 <home>/venv312/Lib/site-packages/torch/lib。"""
    return agent_path("venv312", "Lib", "site-packages", "torch", "lib")


def tmp_dir() -> Path:
    """临时中间产物，默认 <home>/tmp。"""
    return _dir("tmp_dir", ("tmp",))


def qq_export_dir() -> Path:
    """QQ 导出语料目录，默认 <home>/qq_export/c2c。"""
    return _dir("qq_export_dir", ("qq_export", "c2c"))


def backups_dir() -> Path:
    """控制台/数据库备份目录，默认 <home>/backups。"""
    return _dir("backups_dir", ("backups",))


def describe() -> dict:
    """当前解析结果（诊断/控制台用）。"""
    return {
        "data_dir": str(data_dir()),
        "log_dir": str(log_dir()),
        "repo_config": str(config_path()),
        "web_config_path": str(web_config_path()),
        "agent_home": str(agent_home()),
        "models_dir": str(models_dir()),
        "gguf_dir": str(gguf_dir()),
        "adapters_dir": str(adapters_dir()),
        "logs_dir": str(logs_dir()),
        "venv312_python": str(venv312_python()),
        "llama_bin": str(llama_bin()),
        "convert_dir": str(convert_dir()),
        "config_path": str(PATHS_JSON),
        "source": _source(),
    }


def _source() -> str:
    if _env("AGENT_HOME"):
        return "env:AGENT_HOME"
    if _env("SOULAGENT_AGENT_HOME"):
        return "env:SOULAGENT_AGENT_HOME"
    if _resolve(_read_config().get("agent_home")) is not None:
        return "paths.json"
    return "default"
