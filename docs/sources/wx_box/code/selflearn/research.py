# -*- coding: utf-8 -*-
"""Batch 2 / Agent B：抓正文与摘句（Learn Loop 第 ③ 步「检索来源」）。

公开接口只有两个：

- ``fetch_text(url, timeout=10)``：urllib 抓 HTML，去掉 ``script``/``style``/标签，
  只留正文，返回 ``{ok, url, title, text, error}``；
- ``extract_quote(text, keyword, max_len=200)``：在正文里找含关键词的一句，作为
  provenance 的原文片段；未命中返回 ``""``。

约束：
- fail-closed：超时、HTTP 错误、空正文、非文本类型一律 ``ok=False`` 且 ``text=""``，
  ``error`` 写明原因；两个函数都不抛异常；
- 只认 http/https，``file://`` 之类本地协议直接拒绝；
- 不做搜索、不猜 URL：给什么抓什么，抓不到就返回失败。
"""
from __future__ import annotations

import html as _html
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

__all__ = ["fetch_text", "extract_quote"]

MAX_BYTES = 2 * 1024 * 1024
MAX_TEXT = 200_000
USER_AGENT = "Mozilla/5.0 (compatible; SelfLearnResearch/1.0)"

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_MARKUP_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "text/xml", "application/xml",
})

# 这些标签整段丢弃：脚本、样式、模板，以及不会当正文读的嵌入内容。
_SKIP_TAGS = frozenset({
    "script", "style", "noscript", "template", "svg", "math",
    "iframe", "object", "embed", "canvas", "select", "textarea",
})

# 这些标签两侧断块，避免相邻标题/段落/列表黏成一行。
_BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "br", "dd", "details", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
    "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "ul",
})

_WS_RE = re.compile(r"\s+")
_META_CHARSET_RE = re.compile(
    rb"""charset\s*=\s*["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE)
_MARKUP_HINT_RE = re.compile(
    r"<(?:!doctype|html|head|body|div|p|article|section|h[1-6])\b", re.IGNORECASE)

_CODEC_ALIASES = {
    "gb2312": "gb18030",  # gb18030 是超集，gb2312/gbk 页面用它解码更稳
    "gbk": "gb18030",
    "gb18030": "gb18030",
    "utf8": "utf-8",
    "utf-8": "utf-8",
    "big5": "big5",
    "latin1": "latin-1",
    "iso-8859-1": "latin-1",
}


def _result(url, ok=False, title="", text="", error=""):
    """统一返回结构；``text`` 只在成功时非空。"""
    return {
        "ok": bool(ok),
        "url": str(url or ""),
        "title": str(title or ""),
        "text": str(text or ""),
        "error": str(error or ""),
    }


def _clean_ws(value) -> str:
    """把任意空白（含换行、制表、全角空格）压成单个空格并去掉首尾空白。"""
    return _WS_RE.sub(" ", str(value or "")).strip()


def _normalize_text(value) -> str:
    """按行清理正文：逐行归一空白，丢掉空行，行间保留换行。"""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [_clean_ws(line) for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


class _TextExtractor(HTMLParser):
    """HTML → 块文本。

    - ``script``/``style`` 等标签内的内容整段丢弃；
    - ``title`` 单独收集，不混进正文；
    - 块级标签两侧断块，块内空白归一，块间用换行连接。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.blocks: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    # -- HTMLParser 回调 ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        tag = str(tag).lower()
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = str(tag).lower()
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "title":
            self._in_title = False
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self._in_title:
            self.title_parts.append(str(data))
            return
        if self._skip_depth:
            return
        self._buf.append(str(data))

    # -- 结果 -------------------------------------------------------------
    def _flush(self) -> None:
        text = _clean_ws("".join(self._buf))
        self._buf.clear()
        if text:
            self.blocks.append(text)

    @property
    def title(self) -> str:
        return _clean_ws("".join(self.title_parts))

    @property
    def text(self) -> str:
        self._flush()
        return "\n".join(self.blocks)


def _normalize_codec(name) -> str:
    value = str(name or "").strip().strip("\"'").lower()
    if not value:
        return ""
    return _CODEC_ALIASES.get(value, value)


def _sniff_meta_charset(raw: bytes) -> str:
    match = _META_CHARSET_RE.search(raw[:4096])
    if not match:
        return ""
    return match.group(1).decode("ascii", "ignore")


def _decode(raw: bytes, charset: str = "") -> str:
    """按 头部 charset → meta charset → utf-8 → gb18030 逐级解码。"""
    candidates: list[str] = []
    for name in (charset, _sniff_meta_charset(raw)):
        codec = _normalize_codec(name)
        if codec and codec not in candidates:
            candidates.append(codec)
    for codec in ("utf-8", "gb18030"):
        if codec not in candidates:
            candidates.append(codec)
    for codec in candidates:
        try:
            return raw.decode(codec)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _looks_like_markup(decoded: str, ctype: str) -> bool:
    if ctype in _MARKUP_TYPES or ctype.startswith("text/htm"):
        return True
    return bool(_MARKUP_HINT_RE.search(decoded[:4096]))


def _fallback_html_text(decoded: str) -> tuple[str, str]:
    """HTMLParser 意外失败时的兜底：正则去脚本/注释/标签。"""
    without = re.sub(r"<!--.*?-->", " ", decoded, flags=re.DOTALL)
    without = re.sub(
        r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>",
        " ", without, flags=re.IGNORECASE | re.DOTALL)
    title_match = re.search(
        r"<title[^>]*>(.*?)</title\s*>", without, re.IGNORECASE | re.DOTALL)
    title = _clean_ws(_html.unescape(title_match.group(1))) if title_match else ""
    body = _html.unescape(re.sub(r"<[^>]+>", " ", without))
    return _normalize_text(body), title


def fetch_text(url, timeout=10) -> dict:
    """抓网页正文。

    返回 ``{ok, url, title, text, error}``：

    - 成功：``ok=True``、``url`` 为最终地址（跟随重定向后）、``text`` 为去标签正文；
    - 失败：``ok=False``、``text=""``、``error`` 写明原因（``timeout``/``http 404``/
      ``empty content`` 等）。函数不抛异常。

    ``url`` 只接受 http/https；``timeout`` 为秒，必须为正数。
    """
    target = str(url or "").strip()
    if not target:
        return _result(target, error="empty url")

    parts = urllib.parse.urlsplit(target)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.netloc:
        return _result(target, error="unsupported url: only http/https with host")

    try:
        seconds = float(timeout)
    except (TypeError, ValueError):
        return _result(target, error="invalid timeout")
    if not seconds > 0:
        return _result(target, error="invalid timeout")

    request = urllib.request.Request(
        target,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.1",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=seconds) as response:
            final_url = str(response.geturl() or target)
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            if status is not None and not 200 <= int(status) < 300:
                return _result(final_url, error=f"http {int(status)}")

            ctype = (response.headers.get_content_type() or "").lower()
            if ctype and not ctype.startswith("text/") and ctype not in _MARKUP_TYPES:
                return _result(final_url, error=f"unsupported content type: {ctype}")

            charset = response.headers.get_content_charset() or ""
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return _result(target, error=f"http {int(exc.code)} {_clean_ws(exc.reason)}".strip())
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return _result(target, error="timeout")
        return _result(target, error=f"url error: {reason}")
    except (TimeoutError, socket.timeout):
        return _result(target, error="timeout")
    except Exception as exc:  # 兜底：抓取失败绝不把异常抛给调用方
        return _result(target, error=f"{type(exc).__name__}: {exc}")

    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]
    if not raw.strip():
        return _result(final_url, error="empty content")

    try:
        decoded = _decode(raw, charset)
    except Exception as exc:  # pragma: no cover - _decode 自身已兜底
        return _result(final_url, error=f"decode error: {type(exc).__name__}")

    title = ""
    if _looks_like_markup(decoded, ctype):
        try:
            extractor = _TextExtractor()
            extractor.feed(decoded)
            extractor.close()
            text = _normalize_text(extractor.text)
            title = extractor.title
        except Exception:
            text, title = _fallback_html_text(decoded)
    else:
        text = _normalize_text(decoded)

    if not text:
        return _result(final_url, error="empty content")
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]
    return _result(final_url, ok=True, title=title, text=text)


# ---------------------------------------------------------------------------
# 摘句：给 provenance 用
# ---------------------------------------------------------------------------

_SENT_END = "。！？!?；;\n\r"
_STRIP_CHARS = " \t\r\n，,、；;：:“”\"'（）()【】[]《》<>·"


def _sentences(text: str) -> list[str]:
    """按中英文句末标点和换行切句，逐句归一空白并去掉句首尾标点。"""
    pieces: list[str] = []
    buf: list[str] = []
    total = len(text)
    for index, char in enumerate(text):
        buf.append(char)
        if char in _SENT_END:
            pieces.append("".join(buf))
            buf = []
        elif char == "." and (index + 1 == total or text[index + 1].isspace()):
            pieces.append("".join(buf))
            buf = []
    if buf:
        pieces.append("".join(buf))

    cleaned: list[str] = []
    for piece in pieces:
        sentence = _clean_ws(piece).strip(_STRIP_CHARS)
        if sentence:
            cleaned.append(sentence)
    return cleaned


def _clip_quote(text: str, pos: int, keyword_len: int, max_len: int) -> str:
    """把长句裁到 ``max_len`` 以内，窗口尽量让关键词居中，两端用「…」标记。"""
    text = _clean_ws(text)
    if not text:
        return ""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text.strip(_STRIP_CHARS)

    ellipsis = "…"
    # 预留两端省略号的位置；预算太小时不加省略号，优先保证长度不超限。
    room = max_len - 2 if max_len >= 12 else max_len
    width = max(1, min(room, len(text)))
    start = pos - max(0, (width - keyword_len) // 2)
    start = max(0, min(start, len(text) - width))
    end = start + width

    quote = (ellipsis if start > 0 else "") + text[start:end] + (
        ellipsis if end < len(text) else "")
    if len(quote) > max_len:
        quote = quote[:max_len]
    return quote.strip(_STRIP_CHARS)


def extract_quote(text, keyword, max_len=200) -> str:
    """取正文里含 ``keyword`` 的一句话，最长 ``max_len`` 个字符。

    - 命中：返回该句（空格归一、不带换行；长句围绕关键词裁剪并加「…」）；
    - 未命中（含空正文、空关键词、非法 ``max_len``）：返回 ``""``；
    - ASCII 关键词大小写不敏感，返回保留原文大小写；函数不抛异常。
    """
    source = str(text or "")
    needle = _clean_ws(keyword)
    if not source or not needle:
        return ""

    try:
        budget = int(max_len)
    except (TypeError, ValueError):
        return ""
    if budget <= 0:
        return ""

    lowered = needle.lower()
    for sentence in _sentences(source):
        pos = sentence.lower().find(lowered)
        if pos >= 0:
            return _clip_quote(sentence, pos, len(needle), budget)

    # 关键词跨句（或跨换行）时按字符窗口兜底
    flat = _clean_ws(source)
    pos = flat.lower().find(lowered)
    if pos < 0:
        return ""
    return _clip_quote(flat, pos, len(needle), budget)
