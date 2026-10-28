# -*- coding: utf-8 -*-
"""自学习对话体配置：独立 DB，不使用旧业务数据/固定人格。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agi.paths import data_dir as _default_data_dir
from agi.paths import web_config_path as _default_web_config

ROOT = Path(__file__).resolve().parents[2]
# 数据根/根配置统一由 agi.paths 解析（SOULAGENT_DATA_DIR / SOULAGENT_WEB_CONFIG 可重定向）；
# 不设环境变量时与历史路径完全一致（<repo>/agi/data/selflearn、<repo>/web_config.json）。
DATA_DIR = _default_data_dir() / "selflearn"
DB_PATH = DATA_DIR / "selflearn.sqlite3"
WEB_CONFIG = _default_web_config()
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

# P27：网络腔/比赛比喻守卫（默认开启）
SLANG_GUARD_ENABLED = True

# P28：金句腔/主动收尾守卫（默认开启）
CANNED_GUARD_ENABLED = True

# P16-A：时间感知（被动回复 + simple 主动消息注入当前时间）
INJECT_TIME_CONTEXT = True
_WECHAT_CONFIG = DATA_DIR.parent / "wechat" / "config.json"
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

# P24：联网实时信息（天气/公众号文章/少量新闻/网页/搜索，规则触发 + 免费数据源）
#: 默认开启；关闭后 chat 完全不做 web_lookup（prompt 逐字节不变）。
WEB_LOOKUP_ENABLED = True
#: 单次联网请求超时（秒）；公众号文章单独用 8s
WEB_LOOKUP_TIMEOUT_S = 6.0
#: 天气默认城市；空 = wttr.in IP 定位
WEATHER_CITY = ""
#: 新闻开关（读不到就安静，不注入）
NEWS_ENABLED = True
#: 10 分钟查询缓存文件
WEB_LOOKUP_CACHE = DATA_DIR.parent / "web_cache.json"

# P25：网上冲浪引擎（小鱼自己上网看东西；默认不主动发消息）
#: 总开关（env SURF_ENABLED 可覆盖）
SURF_ENABLED = True
#: 冲浪窗口（本地小时，左闭右开；10:00–22:00）
SURF_WINDOW = (10, 22)
#: 基准投骰间隔（秒）；实际 gap 由因子微调，见 surf.gap_minutes
SURF_ROLL_INTERVAL_S = 1800
#: 单轮最多看几条
SURF_MAX_ITEMS = 20
#: 状态文件（factor / last_roll_ts / next_roll_ts / 今日计数）
SURF_STATE_PATH = DATA_DIR / "surf_state.json"

# P31：小鱼的日记（每晚 23:00 自动写今天的情感与经过；只存本机）
#: 总开关（gateway config.json 的 diary_enabled 优先，env DIARY_ENABLED 可覆盖）
DIARY_ENABLED = True
#: 每天几点后允许写（默认 23:00）
DIARY_HOUR = 23
#: 日记目录（用户要的「本机文本文档」）
DIARY_DIR = DATA_DIR.parent / "diary"
#: 索引一行一篇：date/path/chars/mood_avg/generated_at/fallback
DIARY_INDEX = DIARY_DIR / "index.jsonl"
