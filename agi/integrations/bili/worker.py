# -*- coding: utf-8 -*-
"""B站刷一轮（V1 PR9）：推荐流 → 关键词过滤 → 状态落盘。

V1 只做「挑几条 + 记住看过」，不自动评论/不投递私信、不写训练语料（V2 再接）。
调用方（802/801）负责开关与频次；本模块保证：

- ``enabled=False`` 直接返回 skipped，不联网；
- 关键词留空 → LLM 自选（见 :mod:`keywords`），再按标题过滤；
- 状态文件（last_run / seen / keywords）落在 bot 包或 ``agi/data/bili/state.json``，
  重启不重复推荐同一条。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from agi.paths import data_dir
from .feed import recommend
from .keywords import resolve_keywords

MAX_SEEN = 500


def state_path() -> Path:
    try:
        from agi import bot_runtime
        bot = bot_runtime.bot_dir()
        if bot is not None:
            return bot / "logs" / "bili_state.json"
    except Exception:  # noqa: BLE001 - 旧部署
        pass
    return data_dir() / "bili" / "state.json"


def load_state(path: Path | None = None) -> dict:
    target = Path(path) if path is not None else state_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"seen": [], "last_run": 0, "keywords": []}
    if not isinstance(payload, dict):
        return {"seen": [], "last_run": 0, "keywords": []}
    payload.setdefault("seen", [])
    payload.setdefault("last_run", 0)
    payload.setdefault("keywords", [])
    return payload


def _save_state(state: dict, path: Path | None = None) -> Path:
    target = Path(path) if path is not None else state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    import os
    os.replace(tmp, target)
    return target


def _matches(item: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    text = (str(item.get("title") or "") + " " + str(item.get("desc") or "")).lower()
    return any(str(keyword).lower() in text for keyword in keywords)


def run_once(*, enabled: bool = True, keywords=None, limit: int = 5,
             now: float | None = None, path: Path | None = None,
             llm_chat=None) -> dict:
    """刷一轮；返回 ``{ok, items, keywords, source, fetched, state_path}``。"""
    if not enabled:
        return {"ok": True, "skipped": "disabled", "items": [], "fetched": 0}
    count = max(1, min(int(limit or 5), 20))
    feed = recommend(max(count * 3, 10))
    items = list(feed.get("items") or [])
    titles = [item.get("title") or "" for item in items]
    resolved = resolve_keywords(keywords, titles, llm_chat=llm_chat)
    picked = [item for item in items if _matches(item, resolved["keywords"])]
    if len(picked) < count:
        for item in items:
            if item not in picked:
                picked.append(item)
            if len(picked) >= count:
                break
    picked = picked[:count]
    state = load_state(path)
    seen = [str(bvid) for bvid in (state.get("seen") or [])]
    fresh = [item for item in picked if item.get("bvid") not in seen]
    seen.extend(str(item.get("bvid")) for item in picked if item.get("bvid"))
    state.update({
        "seen": seen[-MAX_SEEN:],
        "last_run": float(now if now is not None else time.time()),
        "keywords": resolved["keywords"],
        "source": resolved["source"],
        "fallback_feed": bool(feed.get("fallback")),
    })
    _save_state(state, path)
    return {
        "ok": True,
        "items": picked,
        "fresh": len(fresh),
        "fetched": len(items),
        "keywords": resolved["keywords"],
        "keywords_source": resolved["source"],
        "feed_source": feed.get("source") or "",
        "feed_fallback": bool(feed.get("fallback")),
        "state_path": str(path or state_path()),
    }