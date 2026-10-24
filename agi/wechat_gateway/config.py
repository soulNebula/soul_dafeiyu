# -*- coding: utf-8 -*-
"""801 微信网关配置。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "agi" / "data" / "wechat"
CONFIG_PATH = DATA_DIR / "config.json"
CONTACT_DB_DIR = ROOT / "agi" / "data" / "selflearn" / "wechat_contacts"

DEFAULTS = {
    "enabled": False,
    "port": 801,
    "auto_reply": False,
    "dry_run": True,
    "monitored_contacts": [],
    "allowed_contacts": [],
    "max_chars": 500,
    "reply_delay": 0.3,
    "stop": False,
    # 23:00–08:00 夜间静音；int 小时语义不变，start > end 即跨零点
    "quiet_hours": [23, 8],
    "rate_per_minute": 3,
    "reactive_rate_per_minute": 8,
    "daily_limit": 200,
    "send_mode": "auto",
    "admin_token": "",
    "complaint_stop": True,
    "min_interval_s": 1.5,
    "hourly_limit": 40,
    "fallback_enabled": False,
    "fallback_mode": "manual",
    "proactive_enabled": False,
    "proactive_interval_s": 900,
    "proactive_contacts": [],
    # 主动检查间隔：60–7200 秒（1–120 分钟）随机（2026-10-25 用户拍板）
    "proactive_interval_min_s": 60,
    "proactive_interval_max_s": 7200,
    # 每轮检查的发送概率：在 [min, max] 随机采（旧键 proactive_probability 作为固定值兼容）
    "proactive_probability_min": 0.3,
    "proactive_probability_max": 0.9,
    "proactive_until_ts": 0,
    "reply_debounce_ms": 2000,
    "proactive_idle_s": 600,
    # P0-2：同内容去重窗口（秒）——窗口内发过相同/高相似文本就跳过
    "proactive_dedupe_window_s": 1800,
    # P0-2：同类错误连续 N 次熔断，冷却 cooldown 秒
    "proactive_breaker_threshold": 3,
    "proactive_breaker_cooldown_s": 1800,
    # P0-2：死区哨兵——超过该秒数没有任何事件就写 gateway_stale
    "gateway_stale_alert_s": 1800,
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            cfg.update(raw)
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def contact_db(contact: str) -> Path:
    safe = hashlib.sha1(str(contact).encode("utf-8")).hexdigest()[:14]
    CONTACT_DB_DIR.mkdir(parents=True, exist_ok=True)
    return CONTACT_DB_DIR / f"{safe}.sqlite3"
