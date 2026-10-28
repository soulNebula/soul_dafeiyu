# -*- coding: utf-8 -*-
"""自学习对话体配置：独立 DB，不使用旧业务数据/固定人格。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "agi" / "data" / "selflearn"
DB_PATH = DATA_DIR / "selflearn.sqlite3"
WEB_CONFIG = ROOT / "web_config.json"
DEFAULT_MODEL = "deepseek-flash"
MODEL_LABEL = "DeepSeek V4.1 Flash"
REFLECT_EVERY_TURNS = 6
MEMORY_TOP_K = 6
