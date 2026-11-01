# -*- coding: utf-8 -*-
"""B站推荐流 / 关键词搜索（V1 PR9）。

- 登录后走首页推荐 ``rcmd``；未登录或接口不给数据时降级到「热门」；
- 关键词搜索走 ``search/type``（视频）；
- 所有调用过 :func:`agi.integrations.bili.common.rate_limit`，失败抛 BiliError；
- 返回统一结构::

      {"bvid": "...", "title": "...", "owner": "...", "duration": 123,
       "view": 4567, "url": "https://www.bilibili.com/video/BV...",
       "source": "recommend|popular|search", "desc": "..."}
"""
from __future__ import annotations

from .common import BiliError, cookie_header, http_json, load_credential, rate_limit

RCmd_URL = "https://api.bilibili.com/x/web-interface/wbi/index/top/feed/rcmd"
POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/search/type"
DEFAULT_LIMIT = 20


def _item(raw: dict, source: str) -> dict:
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    bvid = str(raw.get("bvid") or "").strip()
    return {
        "bvid": bvid,
        "title": str(raw.get("title") or "").strip(),
        "owner": str(owner.get("name") or raw.get("author") or ""),
        "duration": int(raw.get("duration") or 0),
        "view": int(raw.get("view") or raw.get("play") or 0),
        "desc": str(raw.get("desc") or "")[:300],
        "url": f"https://www.bilibili.com/video/{bvid}" if bvid else "",
        "source": source,
    }


def _ok_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise BiliError("bad_payload")
    code = payload.get("code")
    if code not in (0, None):
        raise BiliError(f"bili_code:{code}")
    return payload


def popular(limit: int = DEFAULT_LIMIT, *, timeout: float = 8.0) -> list[dict]:
    """热门（匿名可用；推荐流失败时的降级源）。"""
    count = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    payload = _ok_payload(http_json(f"{POPULAR_URL}?ps={count}&pn=1", timeout=timeout))
    rows = ((payload.get("data") or {}).get("list")) or []
    return [_item(row, "popular") for row in rows if isinstance(row, dict)]


def search(keyword: str, limit: int = DEFAULT_LIMIT, *,
           timeout: float = 8.0) -> list[dict]:
    """关键词搜索（视频）。空关键词直接返回空列表，不浪费请求。"""
    text = str(keyword or "").strip()
    if not text:
        return []
    count = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    credential = load_credential()
    headers = {"Cookie": cookie_header(credential)} if credential else {}
    query = (f"{SEARCH_URL}?search_type=video&keyword={_quote(text)}"
             f"&page=1&page_size={count}")
    payload = _ok_payload(http_json(query, headers=headers, timeout=timeout))
    data = payload.get("data") if isinstance(payload, dict) else {}
    rows = (data or {}).get("result") or []
    items = [_item(row, "search") for row in rows if isinstance(row, dict)]
    # 搜索接口的标题带 <em class="keyword"> 高亮标签
    for item in items:
        item["title"] = _strip_tags(item["title"])
    return items


def recommend(limit: int = DEFAULT_LIMIT, *, timeout: float = 8.0) -> dict:
    """首页推荐；失败/未登录降级热门（返回 ``fallback=True`` 说明来源）。"""
    count = max(1, min(int(limit or DEFAULT_LIMIT), 50))
    credential = load_credential()
    headers = {"Cookie": cookie_header(credential)} if credential else {}
    try:
        payload = _ok_payload(http_json(
            f"{RCmd_URL}?ps={count}&fresh_type=4&feed_version=V8",
            headers=headers, timeout=timeout))
        rows = ((payload.get("data") or {}).get("item")) or []
        items = [_item(row, "recommend") for row in rows
                 if isinstance(row, dict) and row.get("bvid")]
        if items:
            return {"ok": True, "items": items[:count], "fallback": False,
                    "source": "recommend"}
    except BiliError:
        pass
    items = popular(count, timeout=timeout)
    return {"ok": True, "items": items, "fallback": True, "source": "popular"}


def _quote(text: str) -> str:
    import urllib.parse
    return urllib.parse.quote(str(text))


def _strip_tags(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()