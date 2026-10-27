# -*- coding: utf-8 -*-
"""Learn Loop 主链：URL → 抓正文 → quote/provenance → 制卡。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import learn_loop, research


def _card(client, text: str, skill_id: str) -> dict[str, Any] | None:
    if client is None or not getattr(client, "available", lambda: False)():
        return None
    prompt = (
        "把下面资料整理成一张可练习的技能卡。只输出 JSON："
        '{"content":"一句话要点","example":"一个最小例子"}。'
        f"\n技能：{skill_id}\n资料：\n{text[:3000]}"
    )
    try:
        r = client.chat([{"role": "user", "content": prompt}],
                        purpose="selflearn_card", max_tokens=800, temperature=0.2, json_mode=True)
    except Exception:  # noqa: BLE001
        return None
    if not r.get("ok"):
        return None
    try:
        obj = json.loads(str(r.get("text") or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, dict) or not str(obj.get("content") or "").strip():
        return None
    return {"content": str(obj["content"]).strip()[:500],
            "example": str(obj.get("example") or "").strip()[:500]}


def learn_from_url(path: Path | str, skill_id: str, url: str, *,
                   keyword: str = "", client=None) -> dict[str, Any]:
    sid = str(skill_id or "").strip()
    if not sid:
        return {"ok": False, "error": "skill_id 不能为空"}
    fetched = research.fetch_text(url)
    if not fetched.get("ok"):
        return {"ok": False, "step": "fetch", "error": fetched.get("error") or "fetch failed",
                "fetched": {k: fetched.get(k) for k in ("url", "error")}}
    quote = research.extract_quote(fetched.get("text", ""), keyword) if keyword else ""
    if not quote:
        quote = str(fetched.get("text") or "")[:180].strip()
    if not quote:
        return {"ok": False, "step": "quote", "error": "没有可用原文引用"}
    task_id = learn_loop.open_gap(path, sid, f"学习 {url}")
    source = learn_loop.record_source(path, sid, fetched.get("url") or url,
                                      fetched.get("title") or url, quote)
    out: dict[str, Any] = {"ok": True, "skill_id": sid, "task_id": task_id,
                           "source": source, "fetched": {k: fetched.get(k) for k in ("url", "title")}}
    card = _card(client, fetched.get("text", ""), sid)
    if card:
        out["card"] = learn_loop.write_card(path, sid, card["content"], card["example"])
    else:
        out["card"] = {"ok": False, "skipped": "no_client_or_bad_json"}
    return out
