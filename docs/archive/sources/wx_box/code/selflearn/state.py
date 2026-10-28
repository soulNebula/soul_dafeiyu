# -*- coding: utf-8 -*-
"""09 情绪/边界/关系状态。"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

CRISIS = ("不想活", "自杀", "想死", "活不下去", "结束生命")
NEG = ("累", "难过", "烦", "焦虑", "崩溃", "孤独", "委屈", "生气", "害怕")
POS = ("开心", "高兴", "喜欢", "谢谢", "舒服", "期待", "安心")

BOUNDARY_LEVELS = {
    0: "normal",
    1: "uncomfortable",
    2: "stop",
    3: "serious",
    4: "crisis",
}
REJECT = ("别说了", "不想聊", "到此为止", "闭嘴", "停止", "别发了")
MANIP = ("只有你懂我", "别人都不懂我", "你不能离开我", "我只有你了", "你只能陪我")
DEFAULT = {
    "emotion": {"valence": 0.0, "arousal": 0.3, "label": "neutral"},
    "boundary": {"level": 0, "crisis": False},
    "relationship": {"stage": "陌生", "closeness": 0.2},
    # 它自己的心情，不是用户情绪
    "self": {"mood": 0.55, "energy": 0.6, "sharing": 0.45, "battery": 0.7},
}


def load(path: Path | str = DB_PATH) -> dict[str, Any]:
    con = db.connect(path)
    try:
        row = con.execute("SELECT value FROM state WHERE key='self_state'").fetchone()
        data = json.loads(row["value"]) if row else {}
    except (TypeError, ValueError):
        data = {}
    finally:
        con.close()
    out = json.loads(json.dumps(DEFAULT))
    if isinstance(data, dict):
        for key in ("emotion", "boundary", "relationship", "self"):
            if isinstance(data.get(key), dict):
                out[key].update(data[key])
    return out


def feedback(path: Path | str, reaction: str) -> dict[str, Any]:
    """用户反馈影响它自己的心情和分享欲。"""
    data = load(path)
    own = data["self"]
    if reaction == "good":
        own["mood"] = min(1.0, float(own.get("mood", 0.55)) + 0.06)
        own["sharing"] = min(1.0, float(own.get("sharing", 0.45)) + 0.04)
    elif reaction == "bad":
        own["mood"] = max(0.0, float(own.get("mood", 0.55)) - 0.08)
        own["sharing"] = max(0.0, float(own.get("sharing", 0.45)) - 0.04)
    save(data, path)
    return data


def save(data: dict[str, Any], path: Path | str = DB_PATH) -> None:
    con = db.connect(path)
    try:
        con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
                    ("self_state", json.dumps(data, ensure_ascii=False), time.time()))
        con.commit()
    finally:
        con.close()


def update(path: Path | str = DB_PATH, text: str = "", reply: str = "",
           *, update_own: bool = True) -> dict[str, Any]:
    data = load(path)
    emo = data["emotion"]
    valence = float(emo.get("valence") or 0.0)
    crisis = any(w in text for w in CRISIS)
    if crisis:
        valence = -1.0
    elif any(w in text for w in NEG):
        valence = max(-1.0, valence - 0.15)
    elif any(w in text for w in POS):
        valence = min(1.0, valence + 0.1)
    emo["valence"] = round(valence, 3)
    emo["label"] = "crisis" if crisis else ("negative" if valence <= -0.2 else
                                           "positive" if valence >= 0.2 else "neutral")
    emo["arousal"] = round(min(1.0, 0.3 + (0.2 if crisis or any(w in text for w in NEG) else 0)), 2)
    if crisis:
        level = 4
    elif any(w in text for w in REJECT):
        level = 2
    elif any(w in text for w in MANIP):
        level = 1
    elif any(w in text for w in NEG):
        level = 1
    else:
        level = 0
    data["boundary"] = {"level": level, "name": BOUNDARY_LEVELS[level],
                        "crisis": crisis,
                        "manipulation": any(w in text for w in MANIP)}
    own = data["self"]
    if not update_own:
        pass
    elif crisis:
        own["mood"] = max(0.0, float(own.get("mood", 0.55)) - 0.12)
    elif any(w in text for w in NEG):
        own["mood"] = max(0.0, float(own.get("mood", 0.55)) - 0.05)
        own["sharing"] = max(0.0, float(own.get("sharing", 0.45)) - 0.02)
    elif any(w in text for w in POS):
        own["mood"] = min(1.0, float(own.get("mood", 0.55)) + 0.04)
        own["sharing"] = min(1.0, float(own.get("sharing", 0.45)) + 0.03)
    # 关系阶段由信任账本决定；这里只保留 closeness 的缓存
    from . import trust
    t = trust.get(path)
    data["relationship"] = {"stage": trust.stage(t), "closeness": round(t, 3)}
    save(data, path)
    return data
