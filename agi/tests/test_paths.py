# -*- coding: utf-8 -*-
"""P0-3：路径可配置化（agi.paths）测试。全部 tmp_path，不联网。

覆盖：env 优先级（AGENT_HOME → SOULAGENT_AGENT_HOME → paths.json → 默认）、
相对路径解析、坏 JSON 兜底、派生目录、单项覆盖、agent_path 拼接、
以及「代码里没有 E:\\agent 硬编码」的可移植性约束。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agi import paths

REPO = Path(__file__).resolve().parents[2]
DEFAULT_HOME = Path("E:" + chr(92) + "agent")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in ("AGENT_HOME", "SOULAGENT_AGENT_HOME", "AGENT_MODELS_DIR",
                 "AGENT_GGUF_DIR", "AGENT_LOGS_DIR", "AGENT_VENV312_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, "PATHS_JSON", tmp_path / "paths.json")
    yield


def _write_config(config_path: Path, payload: dict) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload), encoding="utf-8")


# --------------------------------------------------------------------------- #
# 1. 优先级
# --------------------------------------------------------------------------- #
def test_default_is_e_agent():
    assert paths.agent_home() == DEFAULT_HOME
    assert paths.describe()["source"] == "default"


def test_agent_home_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "home_a"))
    monkeypatch.setenv("SOULAGENT_AGENT_HOME", str(tmp_path / "home_b"))
    assert paths.agent_home() == tmp_path / "home_a"
    assert paths.describe()["source"] == "env:AGENT_HOME"


def test_soulagent_env_is_second(monkeypatch, tmp_path):
    monkeypatch.setenv("SOULAGENT_AGENT_HOME", str(tmp_path / "home_b"))
    assert paths.agent_home() == tmp_path / "home_b"
    assert paths.describe()["source"] == "env:SOULAGENT_AGENT_HOME"


def test_paths_json_is_third(monkeypatch, tmp_path):
    _write_config(paths.PATHS_JSON, {"agent_home": str(tmp_path / "home_json")})
    assert paths.agent_home() == tmp_path / "home_json"
    assert paths.describe()["source"] == "paths.json"


def test_env_beats_paths_json(monkeypatch, tmp_path):
    _write_config(paths.PATHS_JSON, {"agent_home": str(tmp_path / "home_json")})
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "home_env"))
    assert paths.agent_home() == tmp_path / "home_env"


def test_relative_path_resolved_against_repo(tmp_path):
    _write_config(paths.PATHS_JSON, {"agent_home": "my_agent_home"})
    assert paths.agent_home() == REPO / "my_agent_home"


def test_bad_json_falls_back_to_default():
    paths.PATHS_JSON.parent.mkdir(parents=True, exist_ok=True)
    paths.PATHS_JSON.write_text("{bad json", encoding="utf-8")
    assert paths.agent_home() == DEFAULT_HOME


# --------------------------------------------------------------------------- #
# 2. 派生目录 / 单项覆盖
# --------------------------------------------------------------------------- #
def test_derived_dirs(monkeypatch, tmp_path):
    home = tmp_path / "agent_home"
    monkeypatch.setenv("AGENT_HOME", str(home))
    assert paths.models_dir() == home / "models"
    assert paths.gguf_dir() == home / "output" / "gguf"
    assert paths.adapters_dir() == home / "output"
    assert paths.output_dir() == home / "output"
    assert paths.logs_dir() == home / "logs"
    assert paths.llama_bin() == home / "llama" / "cuda"
    assert paths.convert_dir() == home / "llama" / "convert"
    assert paths.tmp_dir() == home / "tmp"
    assert paths.qq_export_dir() == home / "qq_export" / "c2c"
    assert paths.backups_dir() == home / "backups"
    assert paths.torch_lib_dir() == home / "venv312" / "Lib" / "site-packages" / "torch" / "lib"
    assert paths.venv312_python() == home / "venv312" / "Scripts" / "python.exe"


def test_env_single_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("AGENT_MODELS_DIR", str(tmp_path / "models_x"))
    assert paths.models_dir() == tmp_path / "models_x"
    assert paths.gguf_dir() == tmp_path / "home" / "output" / "gguf"


def test_paths_json_single_dir_override(tmp_path):
    config = tmp_path / "paths.json"
    _write_config(config, {
        "agent_home": str(tmp_path / "home"),
        "gguf_dir": str(tmp_path / "gguf_x"),
        "venv312_python": str(tmp_path / "py312.exe"),
    })
    paths.PATHS_JSON = config
    assert paths.gguf_dir() == tmp_path / "gguf_x"
    assert paths.venv312_python() == tmp_path / "py312.exe"


def test_agent_path_join():
    home = paths.agent_home()
    assert paths.agent_path("output", "gguf") == home / "output" / "gguf"
    assert paths.agent_path("output/gguf") == home / "output" / "gguf"
    assert paths.agent_path("output" + chr(92) + "gguf") == home / "output" / "gguf"
    assert paths.agent_path("", "logs", "") == home / "logs"


# --------------------------------------------------------------------------- #
# 3. 可移植性约束
# --------------------------------------------------------------------------- #
def test_no_hardcoded_agent_path_in_code():
    """代码里除 agi/paths.py 的默认值外，不应再出现 E:\\agent。"""
    needle = "E:" + chr(92) + "agent"
    offenders: list[str] = []
    for path in REPO.rglob("*.py"):
        parts = set(path.parts)
        if "__pycache__" in parts or ".venv" in parts or "venv312" in parts \
                or "scratch" in parts or "node_modules" in parts \
                or "archive" in parts:
            continue
        if path == REPO / "agi" / "paths.py":
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle in text:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, offenders


def test_check_portability_script_passes():
    script = REPO / "tools" / "check_portability.py"
    assert script.is_file()
    env = dict(os.environ)
    for name in ("AGENT_HOME", "SOULAGENT_AGENT_HOME"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8",
        errors="replace", env=env, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS" in result.stdout
