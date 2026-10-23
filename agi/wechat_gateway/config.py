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
