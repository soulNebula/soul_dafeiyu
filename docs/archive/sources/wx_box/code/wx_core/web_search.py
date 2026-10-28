"""联网搜索：DeepSeek 原生搜索 + Bing RSS 回退（纯标准库、fail-closed）。

后端一（主）：DeepSeek 的 Anthropic 兼容 Messages 端点，带服务端 ``web_search``
工具（``web_search_20250305``）。凭据解析顺序：

1. ``web_config.json`` 的 ``llm.api_key`` / ``llm.base_url``（路径相对仓库根）；
2. 环境变量 ``DEEPSEEK_API_KEY``。

后端二（回退）：Bing 的 RSS 搜索结果（``?format=rss``，注意只有 ``format=rss``
才返回 XML，普通 HTML 页面不解析）。两个后端都失败时返回 ``provider="none"``，
不会抛异常。

请求一律**显式禁用代理**（``ProxyHandler({})``），不读 ``*_proxy`` 环境变量；
DeepSeek 请求拒绝跟随重定向，避免把 API key 带到别的站点。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from .safe_fetch import html_to_text

# ---------------------------------------------------------------- 常量

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "web_config.json"

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 20.0
DEFAULT_COUNT = 5
MAX_COUNT = 20

ANTHROPIC_VERSION = "2023-06-01"
SEARCH_TOOL_TYPE = "web_search_20250305"
SEARCH_TOOL_NAME = "web_search"
SEARCH_MAX_USES = 3
MAX_TOKENS = 2048

DEEPSEEK_MAX_BYTES = 2 * 1024 * 1024
BING_MAX_BYTES = 512 * 1024
BING_ENDPOINT = "https://www.bing.com/search"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

_RE_URL_START = re.compile(r"^https?://", re.I)
_RE_URL = re.compile(r"https?://[^\s<>\"'\]\[()（）【】]+", re.I)
_RE_TRAILING = re.compile(r"[)\].,;:!?，。；：！？、】》」』]+$")
_RE_SPACE = re.compile(r"\s+")


class _SearchError(Exception):
    """内部错误：由 :func:`search` 统一转成 ``ok=False`` 的返回值。"""


# ---------------------------------------------------------------- 配置与凭据


def _read_config() -> dict:
    """读 ``web_config.json``；文件缺失或损坏时返回空字典（不抛）。"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_credentials() -> tuple[str, str]:
    """解析 (api_key, base_url)：配置文件优先，其次环境变量，最后默认值。"""
    llm = _read_config().get("llm")
    if not isinstance(llm, dict):
        llm = {}
    key = str(llm.get("api_key") or "").strip()
    base_url = str(llm.get("base_url") or "").strip()
    if not key:
        key = str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not base_url:
        base_url = DEFAULT_BASE_URL
    return key, base_url.rstrip("/") or DEFAULT_BASE_URL


def _api_endpoint(base_url: str) -> str:
    """把 base_url 拼成 Messages 端点（兼容已带 ``/anthropic/v1`` 的写法）。"""
    base = str(base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
    if base.endswith("/messages"):
        return base
    if base.endswith("/anthropic/v1"):
        return f"{base}/messages"
    if base.endswith("/anthropic"):
        return f"{base}/v1/messages"
    return f"{base}/anthropic/v1/messages"


# ---------------------------------------------------------------- HTTP 层


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝一切重定向（POST 带 key，跟随重定向可能把 key 发到别的站点）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        raise _SearchError(f"HTTP {code} 重定向已拒绝：{str(newurl)[:160]}")


def _build_opener(*, follow_redirects: bool) -> urllib.request.OpenerDirector:
    """构造**不读代理环境变量**的 opener。"""
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if follow_redirects:
        handlers.append(urllib.request.HTTPRedirectHandler())
    else:
        handlers.append(_NoRedirectHandler())
    return urllib.request.build_opener(*handlers)


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict | None = None,
    body: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEEPSEEK_MAX_BYTES,
    follow_redirects: bool = False,
) -> tuple[int, bytes]:
    """发一次 HTTP(S) 请求，返回 ``(状态码, 原始响应体)``。

    HTTP 4xx/5xx 也照常返回状态与正文（由调用方决定文案），网络层异常则抛
    :class:`_SearchError`。响应体超过 ``max_bytes`` 直接中止。
    """
    opener = _build_opener(follow_redirects=follow_redirects)
    request = urllib.request.Request(
        url, data=body, headers=dict(headers or {}), method=method
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:  # 4xx/5xx：当成「有正文的响应」返回
        status = int(getattr(exc, "code", 0) or 0)
        try:
            raw = exc.read(max_bytes + 1)
        except Exception:  # noqa: BLE001 - 读错误正文失败就当作空
            raw = b""
        return status, raw
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise _SearchError(f"请求失败：{reason}") from None
    if len(raw) > max_bytes:
        raise _SearchError("响应体超过大小限制，已中止")
    return status, raw


def _error_detail(raw: bytes) -> str:
    """从错误响应体里提取一句可读信息（不拼接请求头，避免泄露 key）。"""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        message = data.get("message")
    if not isinstance(message, str):
        return ""
    return _RE_SPACE.sub(" ", message).strip()[:200]


# ---------------------------------------------------------------- 结果规整


def _clean_inline(value: object) -> str:
    """把一段可能带 HTML 的文本压成单行纯文本。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return _RE_SPACE.sub(" ", html_to_text(text)).strip()


def _normalize_url(raw: object) -> str:
    """规整搜索结果 URL：只留 http(s)、去碎片/句末标点、拒绝内嵌凭据。"""
    text = _RE_SPACE.sub("", str(raw or "").strip().strip("\"'"))
    if not text:
        return ""
    text = _RE_TRAILING.sub("", text)
    text = re.sub(r"#\d+$", "", text)  # DeepSeek 的「第几条」锚点，不是真实片段
    if not _RE_URL_START.match(text):
        return ""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not parts.hostname:
        return ""
    if parts.username is not None or parts.password is not None:
        return ""
    # 主机名统一小写，避免同一个站点因大小写被当成两条结果。
    return urllib.parse.urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


def _extract_urls_from_text(text: str, limit: int) -> list[dict]:
    """从文本里抽 URL，并把「相邻上下文」当作 snippet（DeepSeek 纯文本兜底）。"""
    results: list[dict] = []
    seen: set[str] = set()
    if limit <= 0:
        return results
    source = str(text or "")
    for match in _RE_URL.finditer(source):
        url = _normalize_url(match.group(0))
        if not url or url in seen:
            continue
        start = max(0, match.start() - 120)
        end = min(len(source), match.end() + 120)
        context = _clean_inline(source[start:end])
        if not context:
            continue
        seen.add(url)
        results.append({"title": context[:200], "url": url, "snippet": context[:500]})
        if len(results) >= limit:
            break
    return results


def _map_deepseek(payload: object, count: int) -> list[dict]:
    """把 Anthropic Messages 响应映射成统一结果列表。

    优先取结构化的 ``web_search_tool_result``；没有结构化结果时退而解析文本块里
    的 URL 与相邻上下文。摘要优先用 ``citations[].cited_text``。
    """
    if not isinstance(payload, dict):
        raise _SearchError("DeepSeek 返回的不是 JSON 对象")
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise _SearchError("DeepSeek 响应缺少 content 字段")

    snippets: dict[str, str] = {}
    text_parts: list[str] = []
    structured: list[dict] = []

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            raw_text = block.get("text")
            if isinstance(raw_text, str) and raw_text.strip():
                text_parts.append(raw_text)
            citations = block.get("citations")
            if not isinstance(citations, list):
                continue
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                url = _normalize_url(citation.get("url"))
                snippet = _clean_inline(citation.get("cited_text"))
                if url and snippet and url not in snippets:
                    snippets[url] = snippet[:500]
        elif kind == "web_search_tool_result":
            content = block.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict) and item.get("type") == "web_search_result":
                    structured.append(item)

    results: list[dict] = []
    seen: set[str] = set()
    for item in structured:
        url = _normalize_url(item.get("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        title = _clean_inline(item.get("title"))[:200] or url
        results.append({"title": title, "url": url, "snippet": snippets.get(url, "")})
        if len(results) >= count:
            return results

    if len(results) < count:
        for item in _extract_urls_from_text("\n".join(text_parts), count - len(results)):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            results.append(item)
            if len(results) >= count:
                break
    return results[:count]


# ---------------------------------------------------------------- DeepSeek


def _deepseek_search(query: str, count: int, timeout: float) -> list[dict]:
    """DeepSeek 原生联网搜索；没有可用 key 或接口失败时抛 :class:`_SearchError`。"""
    key, base_url = _resolve_credentials()
    if not key:
        raise _SearchError("未配置 DeepSeek API key")

    endpoint = _api_endpoint(base_url)
    payload = {
        "model": DEFAULT_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Perform a web search for the query: {query}"}
                ],
            }
        ],
        "tools": [
            {
                "type": SEARCH_TOOL_TYPE,
                "name": SEARCH_TOOL_NAME,
                "max_uses": SEARCH_MAX_USES,
            }
        ],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "x-api-key": key,
        "authorization": f"Bearer {key}",
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": USER_AGENT,
    }

    status, raw = _http_request(
        endpoint,
        method="POST",
        headers=headers,
        body=body,
        timeout=timeout,
        max_bytes=DEEPSEEK_MAX_BYTES,
        follow_redirects=False,
    )
    if not 200 <= status < 300:
        detail = _error_detail(raw)
        suffix = f"：{detail}" if detail else ""
        raise _SearchError(f"DeepSeek 搜索接口 HTTP {status}{suffix}")

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError) as exc:
        raise _SearchError(f"DeepSeek 搜索接口返回了无法解析的响应（{exc}）") from None

    results = _map_deepseek(data, count)
    if not results:
        raise _SearchError("DeepSeek 没有返回可用的搜索结果")
    return results


# ---------------------------------------------------------------- Bing RSS


def _local_name(tag: object) -> str:
    """去掉 XML 命名空间，返回小写标签名。"""
    name = str(tag or "")
    if "}" in name:
        name = name.rsplit("}", 1)[-1]
    return name.lower()


def _parse_bing_rss(xml_data: bytes | str, count: int) -> list[dict]:
    """解析 Bing RSS：取 ``item`` 的 title/link/description，description 去标签。"""
    try:
        root = ET.fromstring(xml_data)
    except Exception as exc:  # noqa: BLE001 - ParseError 之外也可能有编码错误
        raise _SearchError(f"Bing RSS 解析失败：{exc}") from None

    results: list[dict] = []
    seen: set[str] = set()
    for item in root.iter():
        if _local_name(item.tag) != "item":
            continue
        title = ""
        link = ""
        description = ""
        for child in list(item):
            name = _local_name(child.tag)
            value = "".join(child.itertext())
            if name == "title":
                title = value
            elif name == "link":
                link = value
            elif name == "description":
                description = value
        url = _normalize_url(link)
        if not url or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": _clean_inline(title)[:200] or url,
                "url": url,
                "snippet": html_to_text(description)[:500],
            }
        )
        if len(results) >= count:
            break
    return results


def _bing_rss_search(query: str, count: int, timeout: float) -> list[dict]:
    """Bing RSS 搜索；``format=rss`` 不生效或解析失败时抛 :class:`_SearchError`。"""
    query_string = urllib.parse.urlencode(
        {"format": "rss", "q": query, "count": str(count)}
    )
    url = f"{BING_ENDPOINT}?{query_string}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
    }
    status, raw = _http_request(
        url,
        method="GET",
        headers=headers,
        body=None,
        timeout=timeout,
        max_bytes=BING_MAX_BYTES,
        follow_redirects=True,
    )
    if not 200 <= status < 300:
        raise _SearchError(f"Bing RSS HTTP {status}")
    return _parse_bing_rss(raw, count)


# ---------------------------------------------------------------- 对外入口


def _normalize_count(count: object) -> int:
    """把结果条数限制在 [1, 20]。"""
    try:
        value = int(count)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        value = DEFAULT_COUNT
    return max(1, min(value, MAX_COUNT))


def _error_text(exc: BaseException) -> str:
    """异常 → 单行可读文案。"""
    text = _RE_SPACE.sub(" ", str(exc)).strip()
    return text or exc.__class__.__name__


def search(
    query: str,
    *,
    count: int = DEFAULT_COUNT,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict:
    """联网搜索（先 DeepSeek，失败回退 Bing RSS），fail-closed。

    :param query: 搜索词；为空直接返回 ``ok=False``。
    :param count: 最多返回几条（1~20）。
    :param timeout: 单个后端的请求超时（秒）。
    :return: ``{"ok": bool, "query": str, "provider": "deepseek"|"bing-rss"|"none",
        "results": [{"title": str, "url": str, "snippet": str}], "error": str}``；
        两个后端都失败时 ``provider="none"``，错误同时带上两边的原因。
    """
    text = _RE_SPACE.sub(" ", str(query or "")).strip()
    result: dict = {
        "ok": False,
        "query": text,
        "provider": "none",
        "results": [],
        "error": "",
    }
    if not text:
        result["error"] = "查询词为空"
        return result

    limit = _normalize_count(count)
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError):
        timeout_value = DEFAULT_TIMEOUT
    if timeout_value <= 0:
        timeout_value = DEFAULT_TIMEOUT

    errors: list[str] = []

    try:
        results = _deepseek_search(text, limit, timeout_value)
        return {
            "ok": True,
            "query": text,
            "provider": "deepseek",
            "results": results[:limit],
            "error": "",
        }
    except Exception as exc:  # noqa: BLE001 - 主后端任何失败都回退
        errors.append(f"DeepSeek 搜索失败：{_error_text(exc)}")

    try:
        results = _bing_rss_search(text, limit, timeout_value)
        if not results:
            errors.append("Bing RSS 没有返回结果")
        else:
            return {
                "ok": True,
                "query": text,
                "provider": "bing-rss",
                "results": results[:limit],
                "error": "",
            }
    except Exception as exc:  # noqa: BLE001 - 回退也失败时保留两边的错误
        errors.append(f"Bing RSS 搜索失败：{_error_text(exc)}")

    result["error"] = "；".join(errors) or "没有可用的搜索后端"
    return result
