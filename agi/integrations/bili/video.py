# -*- coding: utf-8 -*-
"""B站展示级评论/弹幕（V1 PR9）；**只服务 802 的展示开关**。

doc 64 §10 明确：弹幕/评论不做数据抓取主链，只在 802「B站」页由用户单独开关
（默认关），开启后只做展示级拉取 + 限频。本模块因此：

- 不写数据库、不进训练语料；
- 每次调用都过 ``rate_limit``，评论/弹幕条数有上限；
- 失败抛 :class:`BiliError`，由 802 转成页面提示（不伪造数据）。
"""
from __future__ import annotations

import re
import zlib

from .common import BiliError, cookie_header, http_bytes, http_json, load_credential

VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
REPLY_URL = "https://api.bilibili.com/x/v2/reply"
DANMAKU_URL = "https://api.bilibili.com/x/v1/dm/list.so"
MAX_COMMENTS = 20
MAX_DANMAKU = 100
_DM_RE = re.compile(r'<d p="([^"]*)">([^<]*)</d>')


def _headers() -> dict:
    credential = load_credential()
    return {"Cookie": cookie_header(credential)} if credential else {}


def video_info(bvid: str, *, timeout: float = 8.0) -> dict:
    """取 aid/cid/标题（评论/弹幕接口都要用）。"""
    text = str(bvid or "").strip()
    if not text.upper().startswith("BV"):
        raise BiliError("bvid_invalid")
    payload = http_json(f"{VIEW_URL}?bvid={text}", headers=_headers(), timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise BiliError(f"video_not_found:{payload.get('code') if isinstance(payload, dict) else '?'}")
    pages = data.get("pages") if isinstance(data.get("pages"), list) else []
    cid = (pages[0].get("cid") if pages and isinstance(pages[0], dict)
           else data.get("cid"))
    return {"bvid": text, "aid": data.get("aid"), "cid": cid,
            "title": str(data.get("title") or ""),
            "owner": str((data.get("owner") or {}).get("name") or "")}


def comments(bvid: str, limit: int = MAX_COMMENTS, *,
             timeout: float = 8.0, info: dict | None = None) -> list[dict]:
    """热门评论（展示级，最多 20 条）。"""
    meta = info or video_info(bvid, timeout=timeout)
    aid = meta.get("aid")
    if not aid:
        raise BiliError("aid_missing")
    count = max(1, min(int(limit or MAX_COMMENTS), MAX_COMMENTS))
    payload = http_json(
        f"{REPLY_URL}?type=1&oid={aid}&sort=2&ps={count}&pn=1",
        headers=_headers(), timeout=timeout)
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = (data or {}).get("replies") or []
    result = []
    for row in rows[:count]:
        if not isinstance(row, dict):
            continue
        member = row.get("member") if isinstance(row.get("member"), dict) else {}
        content = row.get("content") if isinstance(row.get("content"), dict) else {}
        result.append({
            "uname": str(member.get("uname") or ""),
            "text": str(content.get("message") or "")[:500],
            "like": int(row.get("like") or 0),
        })
    return result


def danmaku(bvid: str, limit: int = MAX_DANMAKU, *,
            timeout: float = 8.0, info: dict | None = None) -> list[dict]:
    """弹幕（展示级，最多 100 条；list.so 是 deflate 过的 XML）。"""
    meta = info or video_info(bvid, timeout=timeout)
    cid = meta.get("cid")
    if not cid:
        raise BiliError("cid_missing")
    raw = http_bytes(f"{DANMAKU_URL}?oid={cid}", headers=_headers(), timeout=timeout)
    text = _decode(raw)
    count = max(1, min(int(limit or MAX_DANMAKU), MAX_DANMAKU))
    result = []
    for attrs, body in _DM_RE.findall(text)[:count]:
        parts = attrs.split(",")
        try:
            seconds = float(parts[0]) if parts else 0.0
        except ValueError:
            seconds = 0.0
        result.append({"time": round(seconds, 1), "text": body[:200]})
    return result


def _decode(raw: bytes) -> str:
    data = raw or b""
    for attempt in (data, None):
        if attempt is None:
            break
        try:
            return zlib.decompress(attempt).decode("utf-8", "replace")
        except zlib.error:
            continue
    return data.decode("utf-8", "replace")


def display(bvid: str, *, show_comments: bool = False,
            show_danmaku: bool = False, comment_limit: int = MAX_COMMENTS,
            danmaku_limit: int = MAX_DANMAKU, timeout: float = 8.0) -> dict:
    """按 802 的两个展示开关拉取；开关全关时不发任何请求。"""
    if not show_comments and not show_danmaku:
        return {"ok": False, "error": "display_disabled",
                "hint": "B站页的「显示弹幕」「显示评论」开关都关着"}
    info = video_info(bvid, timeout=timeout)
    result = {"ok": True, "video": info, "comments": [], "danmaku": [],
              "errors": []}
    if show_comments:
        try:
            result["comments"] = comments(bvid, comment_limit, timeout=timeout,
                                          info=info)
        except BiliError as exc:
            result["errors"].append(f"comments:{exc}")
            result["ok"] = False
    if show_danmaku:
        try:
            result["danmaku"] = danmaku(bvid, danmaku_limit, timeout=timeout,
                                        info=info)
        except BiliError as exc:
            result["errors"].append(f"danmaku:{exc}")
            result["ok"] = False
    return result