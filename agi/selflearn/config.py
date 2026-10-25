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

# ---------------------------------------------------------------- P5-b 媒体决策
#: 默认关闭；只有用户显式开启才会产生 emoji/sticker/image 计划。
MEDIA_ENABLED = False
MEDIA_STICKER_IN_REPLY = False
MEDIA_MATERIAL_LIMIT = 40
MEDIA_MAX_STICKERS = 3
MEDIA_STICKER_COOLDOWN_DAYS = 7
MEDIA_MAX_CONSECUTIVE = 2
MEDIA_TAG_FILE = DATA_DIR.parent / "media_tags.json"
MEDIA_WISH_FILE = DATA_DIR / "media_wishes.jsonl"
#: 预留：素材获取管线默认关闭（本仓库不下载、不联网）
MEDIA_ACQUISITION_ENABLED = False
