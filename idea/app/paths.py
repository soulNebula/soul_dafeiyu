# -*- coding: utf-8 -*-
"""路径与环境：工作台自己 vs 被管理的 SoulAgent 项目 vs 模型目录。"""
from __future__ import annotations

import os
from pathlib import Path

IDEA_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = IDEA_ROOT / "web"
DATA_DIR = IDEA_ROOT / ".data"          # 工作台自身运行数据（gitignore）
AUDIT_DB = DATA_DIR / "audit.sqlite3"


def soulagent_home() -> Path:
    raw = os.environ.get("SOULAGENT_HOME", "")
    if raw:
        return Path(raw)
    default = Path.home() / "Desktop" / "SoulAgent"
    return default


def agent_home() -> Path:
    """模型/数据根目录：环境变量 AGENT_HOME > 本机默认（可用 env 覆盖）。"""
    raw = os.environ.get("AGENT_HOME", "")
    if raw:
        return Path(raw)
    return Path("E:/agent")


def wechat_config_path() -> Path:
    return soulagent_home() / "agi" / "data" / "wechat" / "config.json"


def prompts_path() -> Path:
    return soulagent_home() / "agi" / "data" / "prompts.json"


def logs_dir() -> Path:
    return agent_home() / "logs"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
