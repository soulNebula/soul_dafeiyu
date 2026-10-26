# -*- coding: utf-8 -*-
"""自学习对话体配置：独立 DB，不使用旧业务数据/固定人格。"""
from __future__ import annotations

import json
import os
import time
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
MEDIA_STICKER_IN_REPLY = True
MEDIA_MATERIAL_LIMIT = 40
MEDIA_MAX_STICKERS = 3
MEDIA_STICKER_COOLDOWN_DAYS = 7
MEDIA_MAX_CONSECUTIVE = 2
#: P19：被动回复媒体配额（≥2/10）；关闭后只保留原有 media 行为
MEDIA_QUOTA_ENABLED = True
MEDIA_TAG_FILE = DATA_DIR.parent / "media_tags.json"
MEDIA_WISH_FILE = DATA_DIR / "media_wishes.jsonl"
#: 预留：素材获取管线默认关闭（本仓库不下载、不联网）
MEDIA_ACQUISITION_ENABLED = False

# P16-A：时间感知（被动回复 + simple 主动消息注入当前时间）
INJECT_TIME_CONTEXT = True
_WECHAT_CONFIG = ROOT / "agi" / "data" / "wechat" / "config.json"
_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _wechat_setting(key: str, default):
    try:
        raw = json.loads(_WECHAT_CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default
    return raw.get(key, default) if isinstance(raw, dict) else default


def time_context_enabled() -> bool:
    env = os.environ.get("INJECT_TIME_CONTEXT")
    if env is not None:
        return str(env).strip().lower() in ("1", "true", "yes", "on")
    value = _wechat_setting("inject_time_context", INJECT_TIME_CONTEXT)
    return bool(value)


def _period(hour: int) -> str:
    if hour < 6:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 23:
        return "晚上"
    return "深夜"


def time_context(now: float | None = None) -> str:
    """格式化当前时间上下文；统一给 chat 和 simple 主动消息复用。"""
    tm = time.localtime(time.time() if now is None else float(now))
    stamp = f"{tm.tm_year:04d}-{tm.tm_mon:02d}-{tm.tm_mday:02d} {tm.tm_hour:02d}:{tm.tm_min:02d}"
    return (f"现在是 {stamp}（{_WEEKDAYS[tm.tm_wday]}）{_period(tm.tm_hour)}。"
            "你知道当前时间，但不要主动报时；被问到就自然回答，"
            "不要说“没有时间感知/查不到/你直接看手机”，也不要加网页来源。")


# P8-b：simple 主动链路话题种子（默认开启；legacy 不受影响）
PROACTIVE_TOPIC_SEED = True

# P10：B站日报话题候选源（默认开启）
PROACTIVE_TOPIC_BILI = True
