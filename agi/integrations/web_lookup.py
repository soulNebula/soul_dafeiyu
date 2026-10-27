# -*- coding: utf-8 -*-
"""P24：小鱼实时信息（天气 / 少量新闻 / 通用搜索 / **微信公众号文章阅读**）。

范围（用户修正版）：
- 不做股价/指数/汇率/黄金/比特币；
- 新闻少抓（top 3）；
- **重点：用户发公众号链接 → 读完 → 聊**。

规则触发（先规则，不让模型决定）；免费数据源；失败一律 ``ok:False`` + 明确 error，
**不编造**。所有网络请求经 ``_get``，单测 monkeypatch 即可不联网。

数据源：
- 天气：wttr.in ``?format=j1``（无 key，默认 IP 定位）；失败回退腾讯天气；
- 公众号文章：``mp.weixin.qq.com`` 页面（bs4 提取 #activity-name / #js_name /
  #js_content / 发布时间；反爬验证页 → ``blocked``）；
- 公众号搜索：搜狗微信搜索 top5（验证码 → 限流提示）；
- 新闻：公开 RSS（36氪/新浪）优先，失败搜狗新闻；只取 top 3；
- 搜索：搜狗网页 → 必应 HTML，top 3；
- 网页：用户给的普通 http(s) URL（SSRF 防护：禁内网/回环，≤300KB，正文前 3000 字）。
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import requests
from bs4 import BeautifulSoup

from ..selflearn import config as sl_config

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE_PATH = ROOT / "agi" / "data" / "web_cache.json"
CACHE_TTL_S = 600.0
CACHE_MAX_ENTRIES = 200
MAX_URL_BYTES = 300_000
MAX_URL_TEXT = 3000
MAX_ARTICLE_TEXT = 4000
MAX_SUMMARY = 400
DEFAULT_TIMEOUT_S = 6.0
ARTICLE_TIMEOUT_S = 8.0
TOP_NEWS = 3
TOP_SEARCH = 3
TOP_WECHAT = 5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

WEATHER_WORDS = ("天气", "气温", "下雨", "冷不冷", "热不热", "穿什么", "预报")
WECHAT_WORDS = ("公众号", "公众号文章", "看看公众号", "搜一下公众号",
                "找篇公众号", "微信文章", "搜公众号")
NEWS_WORDS = ("新闻", "最近发生", "今天有什么", "热搜", "热点", "大事")
SEARCH_WORDS = ("查一下", "查查", "搜一下", "搜搜", "帮我查", "帮忙查",
                "搜索一下", "百度一下", "帮我搜", "帮忙搜")
TIME_STOP = ("今天", "明天", "昨天", "现在", "目前", "最近", "刚才", "这里",
             "那边", "这个", "那个", "我们", "你们", "他们", "怎么样", "如何",
             "多少", "什么", "一下", "帮忙", "帮我", "看看", "说说")
URL_RE = re.compile(r"https?://[^\s，。！？、；：)）】」]+", re.I)
MP_HOST = "mp.weixin.qq.com"
BLOCKED_HINT = "这篇被微信拦了（反爬验证），你把正文复制发我就行"
TITLE_MARKERS = ("发布", "官宣", "揭秘", "深度", "盘点", "对话", "专访", "独家",
                 "观察", "解读", "报告", "榜单", "重磅", "刷屏", "万字", "一文",
                 "年度", "｜", "|")
CHAT_WORDS = ("我", "你", "吗", "呢", "啊", "累", "开心", "难过", "在吗",
              "吃", "睡", "想", "今天", "明天", "?!", "?", "？", "！")

#: 优先给「新鲜」的公开 RSS；36氪/新浪的 feed 可能格式变化或停更，
#: 统一由 news() 的 _news_fresh() 判断，太旧就退搜狗新闻。
RSS_FEEDS = (
    ("IT之家", "https://www.ithome.com/rss/"),
    ("中国新闻网", "https://www.chinanews.com.cn/rss/scroll-news.xml"),
    ("少数派", "https://sspai.com/feed"),
    ("36氪", "https://36kr.com/feed"),
    ("新浪新闻", "https://rss.sina.com.cn/news/china/focus15.xml"),
)

#: 单测注入点：monkeypatch 本函数即可离线
def _get(url: str, *, timeout: float, headers: dict | None = None,
         params: dict | None = None):
    hdrs = {"User-Agent": UA}
    if headers:
        hdrs.update(headers)
    return requests.get(url, timeout=timeout, headers=hdrs, params=params)


# ------------------------------------------------------------------ 开关/缓存
def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on", "y"):
        return True
    if value in ("0", "false", "no", "off", "n", ""):
        return False
    return None


def enabled() -> bool:
    value = _env_bool("WEB_LOOKUP_ENABLED")
    if value is not None:
        return value
    return bool(getattr(sl_config, "WEB_LOOKUP_ENABLED", True))


def news_enabled() -> bool:
    value = _env_bool("NEWS_ENABLED")
    if value is not None:
        return value
    return bool(getattr(sl_config, "NEWS_ENABLED", True))


def timeout_s() -> float:
    raw = os.environ.get("WEB_LOOKUP_TIMEOUT_S")
    if raw is not None:
        try:
            return max(1.0, float(raw))
        except (TypeError, ValueError):
            pass
    try:
        return max(1.0, float(getattr(sl_config, "WEB_LOOKUP_TIMEOUT_S",
                                      DEFAULT_TIMEOUT_S)))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S


def weather_city() -> str:
    raw = os.environ.get("WEATHER_CITY")
    if raw is not None:
        return str(raw).strip()
    return str(getattr(sl_config, "WEATHER_CITY", "") or "").strip()


def cache_path() -> Path:
    raw = os.environ.get("WEB_LOOKUP_CACHE")
    return Path(raw) if raw else Path(getattr(sl_config, "WEB_LOOKUP_CACHE",
                                              DEFAULT_CACHE_PATH))


def _cache_key(kind: str, payload: Any) -> str:
    text = json.dumps([kind, payload], ensure_ascii=False, default=str,
                      sort_keys=True)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _load_cache() -> dict:
    try:
        raw = json.loads(cache_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(data: dict) -> None:
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def cache_get(key: str, *, now: float | None = None) -> dict | None:
    now = time.time() if now is None else float(now)
    entry = (_load_cache().get("entries") or {}).get(key)
    if not isinstance(entry, dict):
        return None
    try:
        if now - float(entry.get("ts") or 0) > CACHE_TTL_S:
            return None
        value = entry.get("value")
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        return None


def cache_put(key: str, value: dict, *, now: float | None = None) -> None:
    now = time.time() if now is None else float(now)
    data = _load_cache()
    entries = data.get("entries") if isinstance(data.get("entries"), dict) else {}
    entries[key] = {"ts": round(now, 3), "value": value}
    if len(entries) > CACHE_MAX_ENTRIES:
        ordered = sorted(entries.items(), key=lambda kv: float(kv[1].get("ts") or 0))
        entries = dict(ordered[-CACHE_MAX_ENTRIES:])
    data["entries"] = entries
    _save_cache(data)


def clear_cache() -> None:
    _save_cache({"entries": {}})


# ------------------------------------------------------------------ 意图识别
def _clean(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _strip_words(text: str, words) -> str:
    value = text
    for word in sorted(words, key=len, reverse=True):
        value = value.replace(word, "")
    return value.strip("，。！？?、；：:,.! ")


def _clean_topic(text: str) -> str:
    """从「最近有什么新闻」里剥出真正的主题词；只剩「有/的/了」这类语气字就置空。"""
    value = _strip_words(text, NEWS_WORDS + TIME_STOP + ("有什么", "最近", "今天"))
    value = value.strip("，。！？?、；：:,.! 的有")
    return value[:20] if len(value) >= 2 else ""


def _looks_like_article_title(text: str) -> bool:
    """保守判断「像公众号标题」：用户转发卡片时 UIA 可能只给标题。

    只在没有 URL、没有明确的搜索/新闻意图时兜底；宁可少触发，不打扰普通聊天。
    """
    compact = _clean(text)
    if not (6 <= len(compact) <= 60):
        return False
    if URL_RE.search(text):
        return False
    if compact.endswith(("。", "！", "？", "!", "?")):
        return False
    if any(word in compact for word in CHAT_WORDS):
        return False
    return any(marker in compact for marker in TITLE_MARKERS)


def _title_query(text: str) -> str:
    value = str(text or "").strip()
    for word in ("链接", "公众号文章", "公众号", "微信文章", "文章"):
        value = value.replace(word, "")
    return value.strip("，。！？?、；：:,.! ｜|")[:60]


def _extract_city(text: str) -> str:
    patterns = (r"([\u4e00-\u9fa5]{2,6})(?:今天|明天|现在|的)?(?:天气|气温|下雨)",
                r"(?:天气|气温)[^，。?？]*?([\u4e00-\u9fa5]{2,6})")
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(1)
            if not candidate or candidate in TIME_STOP \
                    or candidate in WEATHER_WORDS:
                continue
            candidate = _strip_words(candidate, TIME_STOP + WEATHER_WORDS)
            if 2 <= len(candidate) <= 8:
                return candidate
    return ""


def _is_mp_link(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host == MP_HOST or host.endswith("." + MP_HOST)


def detect_intent(text: str) -> dict | None:
    """规则触发：只保留 天气/公众号文章/公众号搜索/新闻(top3)/通用搜索(top3)/网页。"""
    value = str(text or "").strip()
    if not value:
        return None
    compact = _clean(value)
    match = URL_RE.search(value)
    if match:
        url = match.group(0).rstrip(".,，。")
        if _is_mp_link(url):
            return {"kind": "wechat_article", "query": url,
                    "params": {"url": url}}
        return {"kind": "url", "query": url, "params": {"url": url}}
    if any(word in compact for word in WEATHER_WORDS):
        city = _extract_city(compact)
        return {"kind": "weather", "query": city or "默认城市",
                "params": {"city": city}}
    if any(word in compact for word in WECHAT_WORDS):
        query = compact
        for word in sorted(WECHAT_WORDS, key=len, reverse=True):
            query = query.replace(word, "")
        query = _strip_words(query, TIME_STOP + ("文章", "一篇", "找篇", "搜"))
        return {"kind": "wechat_search", "query": query or "公众号文章",
                "params": {"query": query}}
    if "链接" in compact:
        query = _title_query(compact)
        query = _strip_words(query, TIME_STOP + CHAT_WORDS)
        if len(query) >= 4:
            return {"kind": "wechat_search", "query": query,
                    "params": {"query": query}}
    if news_enabled() and any(word in compact for word in NEWS_WORDS):
        topic = _clean_topic(compact)
        return {"kind": "news", "query": topic or "热点",
                "params": {"topic": topic}}
    for word in SEARCH_WORDS:
        if word in compact:
            query = compact.split(word, 1)[1]
            query = _strip_words(query, TIME_STOP + ("一下", "看看"))
            if query:
                query = _clean_search_query(query)
                if query:
                    return {"kind": "search", "query": query,
                            "params": {"query": query}}
            return None
    # 转发公众号卡片：只有标题、没有 URL（可能带「链接」字样）→ 搜索摘要兜底
    if _looks_like_article_title(value):
        query = _title_query(compact)
        if len(query) >= 2:
            return {"kind": "wechat_title", "query": query,
                    "params": {"query": query}}
    return None


# ------------------------------------------------------------------ 天气
def _weather_wttr(city: str) -> dict:
    url = f"https://wttr.in/{quote(city)}" if city else "https://wttr.in/"
    try:
        resp = _get(url, timeout=timeout_s(), params={"format": "j1"})
        if int(getattr(resp, "status_code", 0)) != 200:
            raise RuntimeError(f"http {getattr(resp, 'status_code', 0)}")
        payload = json.loads(getattr(resp, "text", "") or "{}")
        current = (payload.get("current_condition") or [{}])[0]
        area = ((payload.get("nearest_area") or [{}])[0].get("areaName")
                or [{}])
        area_name = str((area[0] or {}).get("value") or city or "")
        return {
            "ok": True, "temp": int(current.get("temp_C") or 0),
            "feels": int(current.get("FeelsLikeC") or 0),
            "desc": str(((current.get("weatherDesc") or [{}])[0]).get("value")
                        or ""),
            "humidity": int(current.get("humidity") or 0),
            "wind": str(current.get("windspeedKmph") or ""),
            "city": city or area_name or "本地",
            "source": "wttr.in", "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "temp": None, "desc": "", "feels": None,
                "humidity": None, "wind": "", "city": city or "",
                "source": "wttr.in",
                "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


def _weather_tencent(city: str) -> dict:
    if not city:
        return {"ok": False, "temp": None, "desc": "", "feels": None,
                "humidity": None, "wind": "", "city": "", "source": "腾讯天气",
                "error": "腾讯天气回退需要城市名（IP 定位只支持 wttr.in）"}
    try:
        resp = _get("https://wis.qq.com/weather/common", timeout=timeout_s(),
                    params={"source": "pc", "weather_type": "observe",
                            "province": "", "city": city})
        payload = json.loads(getattr(resp, "text", "") or "{}")
        observe = ((payload.get("data") or {}).get("observe") or {})
        desc = str(observe.get("weather") or "")
        temp = observe.get("degree")
        if not desc and temp is None:
            raise RuntimeError("empty observe")
        return {
            "ok": True, "temp": int(float(temp)) if temp is not None else None,
            "feels": None, "desc": desc,
            "humidity": (int(float(observe["humidity"]))
                         if observe.get("humidity") not in (None, "") else None),
            "wind": str(observe.get("windpower") or ""),
            "city": city, "source": "腾讯天气", "error": "",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "temp": None, "desc": "", "feels": None,
                "humidity": None, "wind": "", "city": city,
                "source": "腾讯天气",
                "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


def weather(city: str | None = None) -> dict:
    target = str(city or weather_city() or "").strip()
    result = _weather_wttr(target)
    if result.get("ok"):
        return result
    fallback = _weather_tencent(target)
    if fallback.get("ok"):
        fallback["error"] = ""
        return fallback
    return {"ok": False, "temp": None, "desc": "", "feels": None,
            "humidity": None, "wind": "", "city": target,
            "source": "wttr.in/腾讯天气",
            "error": f"wttr.in: {result.get('error')}; 腾讯: {fallback.get('error')}"}


# ------------------------------------------------------------------ 微信公众号（核心）
_BLOCK_MARKS = ("环境异常", "请在微信客户端打开", "请用微信扫描二维码",
                "验证", "captcha", "访问过于频繁", "链接已过期")


def _text_of(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)) if node else ""


def wechat_article(url: str, fallback_text: str | None = None, *,
                   fallback_title: str = "", fallback_account: str = "",
                   fallback_published_at: str = "") -> dict:
    """抓一篇公众号文章：标题/公众号/发布时间/正文（≤4000 字）。

    - 只允许 mp.weixin.qq.com；反爬验证页返回 ``ok:False, error:"blocked"``
      （同时给 ``hint``：让用户把正文复制发来）；
    - ``fallback_text``：外部已获取的正文（用户粘贴 / PC 微信读窗口）优先返回，
      不再联网；为后续全文读取升级留的钩子。
    """
    raw = str(url or "").strip()
    if not _is_mp_link(raw):
        return {"ok": False, "title": "", "account": "", "published_at": "",
                "text": "", "url": raw, "source": "mp.weixin.qq.com",
                "error": "not_mp_weixin_url"}
    external = re.sub(r"[ 	]+", " ", str(fallback_text or "")).strip()
    if external:
        return {"ok": True, "title": str(fallback_title or "")[:120],
                "account": str(fallback_account or "")[:60],
                "published_at": str(fallback_published_at or "")[:40],
                "text": external[:MAX_ARTICLE_TEXT], "url": raw,
                "source": "external" if not fallback_title else "external/forwarded",
                "error": "", "hint": ""}
    try:
        resp = _get(raw, timeout=ARTICLE_TIMEOUT_S)
        status = int(getattr(resp, "status_code", 0))
        html = str(getattr(resp, "text", "") or "")
        if status >= 400:
            raise RuntimeError(f"http {status}")
        if not html.strip():
            raise RuntimeError("empty html")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "title": "", "account": "", "published_at": "",
                "text": "", "url": raw, "source": "mp.weixin.qq.com",
                "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
    soup = BeautifulSoup(html, "html.parser")
    content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    title_node = soup.select_one("#activity-name") or soup.select_one("h1")
    title = _text_of(title_node)
    account_node = (soup.select_one("#js_name")
                    or soup.select_one(".rich_media_meta_nickname"))
    account = _text_of(account_node)
    published = ""
    for selector in ("#publish_time", ".rich_media_meta_text", "em#publish_time"):
        node = soup.select_one(selector)
        if node:
            published = _text_of(node)
            if published:
                break
    if not content:
        probe = html[:3000]
        if any(mark in probe for mark in _BLOCK_MARKS) or "微信公众平台" in probe:
            return {"ok": False, "title": title, "account": account,
                    "published_at": published, "text": "", "url": raw,
                    "source": "mp.weixin.qq.com", "error": "blocked",
                    "hint": BLOCKED_HINT}
        return {"ok": False, "title": title, "account": account,
                "published_at": published, "text": "", "url": raw,
                "source": "mp.weixin.qq.com", "error": "no_content",
                "hint": BLOCKED_HINT}
    if any(mark in html[:5000] for mark in ("环境异常", "请在微信客户端打开")):
        return {"ok": False, "title": title, "account": account,
                "published_at": published, "text": "", "url": raw,
                "source": "mp.weixin.qq.com", "error": "blocked",
                "hint": BLOCKED_HINT}
    for tag in content(["script", "style", "noscript"]):
        tag.decompose()
    paragraphs = [re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
                  for p in content.find_all(["p", "section"])]
    paragraphs = [p for p in paragraphs if p]
    text = "\n".join(paragraphs) if paragraphs else _text_of(content)
    text = re.sub(r"[ \t]+", " ", text).strip()
    if len(text) < 20:
        return {"ok": False, "title": title, "account": account,
                "published_at": published, "text": text[:200], "url": raw,
                "source": "mp.weixin.qq.com", "error": "content_too_short"}
    return {"ok": True, "title": title[:120], "account": account[:60],
            "published_at": published[:40], "text": text[:MAX_ARTICLE_TEXT],
            "url": raw, "source": "mp.weixin.qq.com", "error": ""}


def wechat_search(query: str) -> dict:
    """搜狗微信搜索 top5；验证码/限流返回 ok:False（提示直接发链接）。"""
    query = str(query or "").strip() or "公众号文章"
    try:
        resp = _get("https://weixin.sogou.com/weixin", timeout=timeout_s(),
                    params={"type": "2", "query": query})
        html = str(getattr(resp, "text", "") or "")
        if int(getattr(resp, "status_code", 0)) >= 400:
            raise RuntimeError(f"http {getattr(resp, 'status_code', 0)}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "count": 0, "source": "搜狗微信",
                "query": query,
                "error": f"{type(exc).__name__}: {str(exc)[:150]}"
                         "（把文章链接直接发我更好）"}
    if any(mark in html for mark in ("验证码", "antispider", "请输入验证码")):
        return {"ok": False, "items": [], "count": 0, "source": "搜狗微信",
                "query": query, "error": "搜索被限流（验证码），把文章链接直接发我更好"}
    soup = BeautifulSoup(html, "html.parser")
    items: list[dict] = []
    for node in soup.select("ul.news-list li"):
        link = node.select_one("h3 a")
        if link is None:
            continue
        title = _text_of(link)
        href = link.get("href") or ""
        if href.startswith("/"):
            href = "https://weixin.sogou.com" + href
        account = _text_of(node.select_one("div.s-p span.all-time-y2, a.account"))
        summary = _text_of(node.select_one("p.txt-info"))
        if title:
            items.append({"title": title[:120], "url": href,
                          "account": account[:60], "summary": summary[:200]})
        if len(items) >= TOP_WECHAT:
            break
    if not items:
        return {"ok": False, "items": [], "count": 0, "source": "搜狗微信",
                "query": query, "error": "没有搜到（把文章链接直接发我更好）"}
    return {"ok": True, "items": items, "count": len(items),
            "source": "搜狗微信", "query": query, "error": ""}


# ------------------------------------------------------------------ 新闻（top3）
def _rss_items(feed_url: str, limit: int = TOP_NEWS) -> list[dict]:
    resp = _get(feed_url, timeout=timeout_s())
    if int(getattr(resp, "status_code", 0)) != 200:
        raise RuntimeError(f"http {getattr(resp, 'status_code', 0)}")
    import xml.etree.ElementTree as ET

    root = ET.fromstring(getattr(resp, "content", b"") or b"<rss/>")
    items: list[dict] = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        summary = (item.findtext("description") or "").strip()
        if title:
            published = (item.findtext("pubDate") or "").strip()
            items.append({"title": title[:120], "url": link,
                          "summary": BeautifulSoup(summary, "html.parser")
                          .get_text(" ", strip=True)[:200],
                          "published_at": published})
        if len(items) >= limit:
            break
    if not items:
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = (entry.findtext("{http://www.w3.org/2005/Atom}title")
                     or "").strip()
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = link_el.get("href") if link_el is not None else ""
            if title:
                items.append({"title": title[:120], "url": str(link or ""),
                              "summary": ""})
            if len(items) >= limit:
                break
    return items


def _sogou_news(topic: str, limit: int = TOP_NEWS) -> list[dict]:
    resp = _get("https://news.sogou.com/news", timeout=timeout_s(),
                params={"query": topic or "热点新闻"})
    soup = BeautifulSoup(getattr(resp, "text", "") or "", "html.parser")
    items: list[dict] = []
    for node in soup.select("h3 a, a.news-title"):
        title = _text_of(node)
        href = node.get("href") or ""
        if not title or not href:
            continue
        if href.startswith("/"):
            href = "https://news.sogou.com" + href
        items.append({"title": title[:120], "url": href, "summary": ""})
        if len(items) >= limit:
            break
    return items


def _news_fresh(items: list[dict], *, max_age_s: float = 3 * 86400) -> bool:
    """RSS 可能是 2018 年的老 feed；有日期且全部太旧 → 视为 stale，改走搜狗。"""
    stamps: list[float] = []
    for item in items:
        raw = str(item.get("published_at") or "").strip()
        if not raw:
            continue
        try:
            from email.utils import parsedate_to_datetime
            stamps.append(parsedate_to_datetime(raw).timestamp())
        except Exception:  # noqa: BLE001
            continue
    if not stamps:
        return True                     # 没有日期信息 → 按可用处理（测试/极简 RSS）
    return (time.time() - max(stamps)) <= max_age_s


def news(topic: str = "") -> dict:
    topic = str(topic or "").strip()
    errors: list[str] = []
    stale: dict | None = None
    for source, feed in RSS_FEEDS:
        try:
            items = _rss_items(feed, limit=TOP_NEWS)
            if topic:
                matched = [item for item in items
                           if topic in (item.get("title") or "")
                           or topic in (item.get("summary") or "")]
                items = matched or items
            if items:
                payload = {"ok": True, "items": items[:TOP_NEWS],
                           "count": len(items[:TOP_NEWS]),
                           "source": f"RSS/{source}", "topic": topic,
                           "fresh": _news_fresh(items[:TOP_NEWS]), "error": ""}
                if payload["fresh"]:
                    return payload
                stale = stale or payload
            errors.append(f"{source}: empty")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{source}: {type(exc).__name__}: {str(exc)[:80]}")
    try:
        items = _sogou_news(topic, limit=TOP_NEWS)
        if items:
            return {"ok": True, "items": items, "count": len(items),
                    "source": "搜狗新闻", "topic": topic, "fresh": True,
                    "error": ""}
        errors.append("搜狗新闻: empty")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"搜狗新闻: {type(exc).__name__}: {str(exc)[:80]}")
    if stale is not None:
        return stale                    # 搜狗也没有：退回 RSS（并标记 fresh=False）
    return {"ok": False, "items": [], "count": 0, "source": "", "topic": topic,
            "error": "; ".join(errors[-3:]) or "no news"}


# ------------------------------------------------------------------ 通用搜索 top3
_SEARCH_PREFIXES = ("请问一下", "请问", "我想知道", "帮我找一下", "帮我找",
                    "帮我查一下", "帮我查", "告诉我", "介绍一下", "科普一下",
                    "什么是", "是什么", "怎么样", "怎么", "如何", "为什么",
                    "查一下", "搜一下")


def _clean_search_query(query: str) -> str:
    """去掉「什么是/请问/帮我查一下」这类口头前缀，让搜索引擎抓关键词。"""
    value = str(query or "").strip().strip("，。！？?、；：:,.! ")
    changed = True
    while changed:
        changed = False
        for prefix in _SEARCH_PREFIXES:
            if value.startswith(prefix) and len(value) > len(prefix) + 1:
                value = value[len(prefix):].strip("，。！？?、；：:,.! ")
                changed = True
                break
    for suffix in ("是什么", "是啥", "怎么样", "怎么", "如何", "是", "的",
                   "了", "吗", "呢", "啊", "呀", "吧"):
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            value = value[:-len(suffix)].strip("，。！？?、；：:,.! ")
    return value or str(query or "").strip()


def _domain_of(url: str) -> str:
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except Exception:  # noqa: BLE001
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _normalize_url(url: str) -> tuple:
    try:
        parts = urlsplit(str(url or ""))
        host = (parts.hostname or "").lower()
        path = parts.path.rstrip("/")
        return (host, path, parts.query)
    except Exception:  # noqa: BLE001
        return (str(url or ""), "", "")


def _dedupe_items(items: list[dict], limit: int) -> list[dict]:
    """URL 去重 + 同域名最多 2 条（避免一个站点刷屏）。"""
    out: list[dict] = []
    seen: set = set()
    per_domain: dict[str, int] = {}
    for item in items:
        url = str((item or {}).get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        key = _normalize_url(url)
        if key in seen:
            continue
        domain = _domain_of(url) or "?"
        if per_domain.get(domain, 0) >= 2:
            continue
        row = dict(item)
        row["domain"] = domain
        row["title"] = str(row.get("title") or "")[:120]
        row["summary"] = str(row.get("summary") or "")[:200]
        out.append(row)
        seen.add(key)
        per_domain[domain] = per_domain.get(domain, 0) + 1
        if len(out) >= limit:
            break
    return out


def _sogou_search(query: str, limit: int = TOP_SEARCH) -> list[dict]:
    resp = _get("https://www.sogou.com/web", timeout=timeout_s(),
                params={"query": query})
    soup = BeautifulSoup(getattr(resp, "text", "") or "", "html.parser")
    items: list[dict] = []
    for block in soup.select("div.vrwrap, div.rb, div.result"):
        link = block.select_one("h3 a")
        if link is None:
            continue
        snippet_el = block.select_one(
            "div.str-info, p.str-text-snippet, div.str_info, p")
        items.append({"title": _text_of(link)[:120],
                      "url": link.get("href") or "",
                      "summary": (_text_of(snippet_el))[:200]})
        if len(items) >= limit:
            break
    if not items:
        for link in soup.select("h3 a"):
            href = link.get("href") or ""
            if href.startswith(("http://", "https://", "//")):
                items.append({"title": _text_of(link)[:120],
                              "url": href, "summary": ""})
            if len(items) >= limit:
                break
    return items


def _bing_search(query: str, limit: int = TOP_SEARCH,
                 endpoint: str = "https://www.bing.com/search") -> list[dict]:
    resp = _get(endpoint, timeout=timeout_s(), params={"q": query})
    soup = BeautifulSoup(getattr(resp, "text", "") or "", "html.parser")
    items: list[dict] = []
    for block in soup.select("li.b_algo"):
        link = block.select_one("h2 a")
        if link is None:
            continue
        snippet_el = block.select_one("p")
        items.append({"title": _text_of(link)[:120],
                      "url": link.get("href") or "",
                      "summary": _text_of(snippet_el)[:200]})
        if len(items) >= limit:
            break
    return items


#: 搜索 provider 顺序：搜狗 → 必应 → 必应中文（失败/结果不足自动切换）
_BING_ENDPOINTS = (("必应", "https://www.bing.com/search"),
                   ("必应中文", "https://cn.bing.com/search"))


def search(query: str, limit: int = 5) -> dict:
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "items": [], "count": 0, "source": "",
                "query": query, "error": "empty query"}
    query = _clean_search_query(query)
    try:
        limit = max(1, min(int(limit or 5), 20))
    except (TypeError, ValueError):
        limit = 5
    providers: list[tuple[str, object]] = [
        ("搜狗", lambda: _sogou_search(query, limit=max(limit * 2, 8)))]
    for name, endpoint in _BING_ENDPOINTS:
        providers.append((name, (lambda ep: (
            lambda: _bing_search(query, limit=max(limit * 2, 8),
                                 endpoint=ep)))(endpoint)))
    errors: list[str] = []
    found: list[dict] = []
    used: list[str] = []
    for name, func in providers:
        try:
            raw = func()  # type: ignore[operator]
            deduped = _dedupe_items(found + raw, limit)
            if deduped:
                found = deduped
                used.append(name)
                if len(found) >= limit:
                    break
            else:
                errors.append(f"{name}: empty")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:80]}")
    if found:
        return {"ok": True, "items": found[:limit], "count": len(found[:limit]),
                "source": "+".join(used), "query": query, "error": ""}
    return {"ok": False, "items": [], "count": 0, "source": "", "query": query,
            "error": "; ".join(errors[-3:]) or "no result"}


# ------------------------------------------------------------------ 网页抓取（SSRF）
def _forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _resolve_ips(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:  # noqa: BLE001
        return []
    ips: list[str] = []
    for info in infos:
        try:
            ips.append(info[4][0])
        except (IndexError, TypeError):
            continue
    return ips


def is_forbidden_host(host: str) -> bool:
    """SSRF：localhost / 内网 / 回环 / link-local / 保留地址一律禁止。"""
    name = str(host or "").strip().lower().rstrip(".")
    if not name:
        return True
    if name in ("localhost", "localhost.localdomain", "ip6-localhost") \
            or name.endswith(".local"):
        return True
    try:
        return _forbidden_ip(ipaddress.ip_address(name))
    except ValueError:
        pass
    ips = _resolve_ips(name)
    if not ips:
        return True
    for raw in ips:
        try:
            if _forbidden_ip(ipaddress.ip_address(raw)):
                return True
        except ValueError:
            return True
    return False


_NOISE_TAGS = ("script", "style", "noscript", "iframe", "form", "nav",
               "footer", "header", "aside", "button", "svg", "template",
               "input", "select", "textarea")
_NOISE_WORDS = ("nav", "menu", "footer", "header", "breadcrumb", "sidebar",
                "comment", "advert", "promo", "share", "cookie", "banner",
                "related", "recommend", "popup", "copyright", "login",
                "subscribe", "toolbar")
_COPYRIGHT_WORDS = ("版权所有", "copyright", "©", "icp备", "免责声明", "扫码",
                    "长按识别", "点击关注", "关注我们", "登录后", "打开app",
                    "下载app", "未经授权", "转载请联系")
_UI_NOISE_WORDS = ("浅色", "深色", "主题色", "夜间", "日间", "无障碍", "返回顶部",
                   "分享到", "收藏", "点赞", "阅读原文", "小程序", "客户端",
                   "登录", "注册", "搜索", "首页", "热榜")


def _looks_noise(tag) -> bool:
    if tag.name in _NOISE_TAGS:
        return True
    attrs = " ".join(str(tag.get(attr) or "") for attr in ("class", "id")).lower()
    if not attrs:
        return False
    return any(word in attrs for word in _NOISE_WORDS)


def _paragraph_text(node) -> str:
    lines: list[str] = []
    for p in node.find_all(["p", "h1", "h2", "h3", "h4", "li",
                            "blockquote", "pre"]):
        text = " ".join(p.get_text(" ", strip=True).split())
        if not text:
            continue
        low = text.lower()
        if any(word in low for word in _COPYRIGHT_WORDS) and len(text) < 120:
            continue
        if len(text) <= 25 and any(word in low for word in _UI_NOISE_WORDS):
            continue
        if len(text) < 8 and low.startswith(("首页", "上一页", "下一页", "登录",
                                             "注册", "更多", "相关阅读")):
            continue
        lines.append(text)
    return chr(10).join(lines)


def _extract_main_text(soup, title: str = "") -> str:
    """正文密度启发式：article/main/段落最多的容器，去导航/广告/版权行。"""
    for tag in list(soup.find_all(True)):
        try:
            if tag.parent is None:
                continue
            if _looks_noise(tag):
                tag.decompose()
        except Exception:  # noqa: BLE001
            continue
    candidates = []
    selectors = ("article", "main", "[role=main]", ".post-content",
                 ".article-content", ".rich_media_content", "#content",
                 ".content", "body")
    for selector in selectors:
        try:
            for node in soup.select(selector)[:3]:
                candidates.append(node)
        except Exception:  # noqa: BLE001
            continue
    best = None
    best_score = -1
    for node in candidates:
        score = len(_paragraph_text(node))
        if score > best_score:
            best_score = score
            best = node
    node = best if best is not None else (soup.body or soup)
    text = _paragraph_text(node)
    if not text:
        text = " ".join((node.get_text(" ", strip=True) if node else "").split())
    lines = [line for line in text.split(chr(10)) if line.strip()]
    if title:
        for index, line in enumerate(lines):
            if title[:12] and title[:12] in line:
                lines = lines[index:]
                break
    return (chr(10).join(lines) if lines else text)[:MAX_URL_TEXT]


def fetch_url(url: str) -> dict:
    raw = str(url or "").strip()
    try:
        parts = urlsplit(raw)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": raw, "title": "", "text": "", "size": 0,
                "domain": "", "source": "", "error": f"bad url: {exc}"}
    if parts.scheme not in ("http", "https"):
        return {"ok": False, "url": raw, "title": "", "text": "", "size": 0,
                "domain": "", "source": "", "error": "只支持 http(s)"}
    host = parts.hostname or ""
    domain = _domain_of(raw)
    if is_forbidden_host(host):
        return {"ok": False, "url": raw, "title": "", "text": "", "size": 0,
                "domain": domain, "source": "", "error": f"SSRF blocked: {host}"}
    try:
        resp = _get(raw, timeout=timeout_s())
        if int(getattr(resp, "status_code", 0)) >= 400:
            raise RuntimeError(f"http {getattr(resp, 'status_code', 0)}")
        content = getattr(resp, "content", None)
        if content is None:
            content = str(getattr(resp, "text", "") or "").encode("utf-8")
        raw_bytes = bytes(content)
        truncated = len(raw_bytes) > MAX_URL_BYTES
        raw_bytes = raw_bytes[:MAX_URL_BYTES]
        encoding = getattr(resp, "encoding", None) or "utf-8"
        try:
            html = raw_bytes.decode(encoding, "ignore")
        except (LookupError, TypeError):
            html = raw_bytes.decode("utf-8", "ignore")
        soup = BeautifulSoup(html, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        return {"ok": True, "url": raw, "title": title[:120],
                "text": _extract_main_text(soup, title=title),
                "size": len(raw_bytes),
                "truncated": bool(truncated), "domain": domain,
                "source": parts.hostname or "", "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": raw, "title": "", "text": "", "size": 0,
                "domain": domain, "source": parts.hostname or "",
                "error": f"{type(exc).__name__}: {str(exc)[:150]}"}


# ------------------------------------------------------------------ 统一入口
def _clip(text: Any, limit: int = MAX_SUMMARY) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value[:limit]


_WEATHER_DESC_ZH = {
    "sunny": "晴", "clear": "晴", "partly cloudy": "多云", "cloudy": "阴",
    "overcast": "阴", "mist": "薄雾", "fog": "雾", "haze": "霾",
    "light rain": "小雨", "moderate rain": "中雨", "heavy rain": "大雨",
    "light drizzle": "毛毛雨", "patchy rain nearby": "局部有雨",
    "thundery outbreaks possible": "可能有雷阵雨", "light snow": "小雪",
    "snow": "雪", "blizzard": "暴雪", "sleet": "雨夹雪",
}


def _desc_zh(desc: str) -> str:
    value = str(desc or "").strip()
    if not value:
        return "未知"
    return _WEATHER_DESC_ZH.get(value.lower(), value)


def _summary_of(kind: str, result: dict) -> str:
    if kind == "wechat_article" and not result.get("ok"):
        hint = str(result.get("hint") or BLOCKED_HINT)
        return _clip(hint + "（只看到反爬验证页，没读到正文；别假装读过）")
    if kind == "wechat_title" and result.get("ok"):
        lines = []
        for item in (result.get("items") or [])[:2]:
            source = f"（{item.get('account')}）" if item.get("account") else ""
            summary = str(item.get("summary") or "")[:80]
            lines.append(f"- {item.get('title')}{source}" +
                         (f"：{summary}" if summary else ""))
        return _clip("只看到检索摘要（没读到全文）：" + chr(10) + chr(10).join(lines))
    if kind == "weather" and result.get("ok"):
        return _clip(f"{result.get('city') or ''}现在{_desc_zh(result.get('desc'))}，"
                     f"气温{result.get('temp')}℃，体感{result.get('feels')}℃，"
                     f"湿度{result.get('humidity')}%，风速{result.get('wind')}km/h")
    if kind == "wechat_article" and result.get("ok"):
        head = f"刚看完公众号「{result.get('account') or '未知'}」的文章《{result.get('title')}》"
        if result.get("published_at"):
            head += f"（{result['published_at']}）"
        return _clip(head + "。正文摘要：" + str(result.get("text") or "")[:260])
    if kind == "wechat_search" and result.get("ok"):
        lines = [f"- {item.get('title')}（{item.get('account') or '公众号'}）"
                 for item in (result.get("items") or [])[:3]]
        return _clip("找到这几篇公众号文章：" + chr(10) + chr(10).join(lines))
    if kind == "search" and result.get("ok"):
        items = result.get("items") or []
        lines = []
        for item in items[:3]:
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("summary") or "").strip()
            if not title:
                continue
            lines.append("- " + title + ("：" + snippet[:80] if snippet else ""))
        text = "搜了下，找到这些：" + chr(10) + chr(10).join(lines)
        domains = [str(item.get("domain") or "") for item in items
                   if item.get("domain")]
        if domains:
            text += chr(10) + "来源：" + domains[0]
        return _clip(text)
    if kind == "news" and result.get("ok"):
        lines = []
        for item in (result.get("items") or [])[:TOP_NEWS]:
            source = f"（{item.get('source')}）" if item.get("source") else ""
            lines.append(f"- {item.get('title')}{source}")
        return _clip("最近相关新闻：" + chr(10) + chr(10).join(lines))
    if kind == "url" and result.get("ok"):
        return _clip(f"网页《{result.get('title') or result.get('url')}》正文摘要："
                     f"{result.get('text')}")
    return ""


def lookup(text: str, *, now: float | None = None) -> dict:
    """规则识别 → 查询 → 统一结果（失败明确 error，不编造）。"""
    now = time.time() if now is None else float(now)
    intent = detect_intent(text)
    if not intent:
        return {"ok": False, "kind": "", "summary": "", "items": [],
                "source": "", "fetched_at": now, "cached": False,
                "error": "no_intent", "title": "", "account": ""}
    kind = intent["kind"]
    params = intent.get("params") or {}
    key = _cache_key(kind, params if params else intent["query"])
    cached = cache_get(key, now=now)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return out
    if kind == "weather":
        result = weather(params.get("city") or None)
    elif kind == "wechat_article":
        result = wechat_article(params.get("url") or intent["query"])
    elif kind == "wechat_search":
        result = wechat_search(params.get("query") or intent["query"])
    elif kind == "wechat_title":
        searched = wechat_search(params.get("query") or intent["query"])
        result = dict(searched)
        result["kind"] = "wechat_title"
        result["title_only"] = True
    elif kind == "news":
        result = news(params.get("topic") or "")
    elif kind == "url":
        result = fetch_url(params.get("url") or intent["query"])
    else:
        result = search(params.get("query") or intent["query"])
    result = dict(result or {})
    result["kind"] = kind
    result["fetched_at"] = now
    result["summary"] = _summary_of(kind, result)
    result.setdefault("items", [])
    result.setdefault("source", "")
    result.setdefault("title", "")
    result.setdefault("account", "")
    result.setdefault("error", "")
    result.setdefault("hint", "")
    result.setdefault("cached", False)
    if result.get("ok"):
        cache_put(key, result, now=now)
    return result
