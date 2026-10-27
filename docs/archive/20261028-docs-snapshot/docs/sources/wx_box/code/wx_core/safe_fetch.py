"""SSRF 防护的网页抓取（纯标准库、fail-closed）。

设计目标（与 qq-bridge 的 ``src/safe-fetch.js`` 对齐）：

- 仅允许 http/https，拒绝 URL 内嵌凭据（``user:pass@``）与空主机名；
- 解析主机名的**全部** A/AAAA 记录（``socket.getaddrinfo``），任何一条落在
  环回 / 私网 / 链路本地 / CGNAT / 组播 / 保留 / ULA / NAT64 / 6to4 /
  IPv4-mapped IPv6 等范围就整体拒绝——不允许「DNS 给了一堆地址，挑一条能连的」；
- 连接目标固定为**已校验的那个 IP**，Host 头与 TLS SNI 仍用原域名
  （``SSLContext.wrap_socket(server_hostname=原域名)``），
  避免「校验完 DNS 再重新解析」被 DNS rebinding 绕过；
- 每一跳重定向（301/302/303/307/308）都重新做一次完整校验，最多 5 跳；
- 响应体流式读取，超过 ``max_bytes`` 立即停止并置 ``truncated``；正文按
  ``max_chars`` 截断；
- 任何异常（DNS 解析、连接、TLS、超时、HTTP 状态码）都转成
  ``{"ok": False, ...}``，不向上抛异常；
- 本模块**不读取**任何 ``*_proxy`` 环境变量，也不会经由系统代理发请求。

对外只有一个入口 :func:`fetch_text`；:func:`html_to_text` 与
:func:`extract_title` 供其它模块（如 web_search）复用。
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

# ---------------------------------------------------------------- 常量

DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_CHARS = 50_000
DEFAULT_MAX_REDIRECTS = 5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)
ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.5"

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# 单次 socket.read 的块大小；同时决定「超过 max_bytes 时最多多读多少」。
_READ_CHUNK = 64 * 1024
# 一次抓取最多尝试几个已校验地址（IPv4 优先，坏地址不拖垮整个请求）。
_MAX_ADDRESS_ATTEMPTS = 4

# http/https 的默认端口。
_DEFAULT_PORTS = {"http": 80, "https": 443}

# 禁止访问的 IPv4 网段（内网/环回/链路本地/CGNAT/组播/保留/文档段）。
_BLOCKED_V4_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)

# 禁止访问的 IPv6 网段。
#   ::/96        未指定 + 环回 + 已废弃的 IPv4-compatible（::127.0.0.1）
#   ::ffff:0:0/96  IPv4-mapped（::ffff:a.b.c.d）
#   ::ffff:0:0:0/96 IPv4-translated
#   64:ff9b::/96   NAT64（内嵌 IPv4）
#   64:ff9b:1::/48 本地 NAT64
#   100::/64       丢弃前缀
#   2001::/32      Teredo
#   2002::/16      6to4（内嵌 IPv4）
#   fc00::/7       ULA
#   fe80::/10      链路本地
#   ff00::/8       组播
_BLOCKED_V6_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "::/96",
        "::ffff:0:0/96",
        "::ffff:0:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",
        "2001:2::/48",
        "2001:10::/28",
        "2001:20::/28",
        "2001:db8::/32",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)

_RE_HOSTNAME = re.compile(r"^[A-Za-z0-9._~-]+$")
_RE_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)
_RE_META_CONTENT = re.compile(
    rb"""<meta[^>]+http-equiv\s*=\s*["']?content-type["']?[^>]+content\s*=\s*["'][^"']*charset\s*=\s*([A-Za-z0-9._:-]+)""",
    re.I,
)
_RE_HEADER_CHARSET = re.compile(r"""charset\s*=\s*["']?\s*([A-Za-z0-9._:-]+)""", re.I)

# 整块丢弃的标签（含内容）。
_RE_DROP_BLOCKS = re.compile(r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
# 块级标签转成换行，保住段落结构。
_RE_BLOCK_TAGS = re.compile(
    r"</?(?:address|article|aside|blockquote|br|dd|div|dl|dt|fieldset|figcaption|figure"
    r"|footer|form|h[1-6]|header|hr|li|main|nav|ol|p|pre|section|table|tbody|td|tfoot"
    r"|th|thead|tr|ul)\b[^>]*>",
    re.I,
)
_RE_TAGS = re.compile(r"<[^>]+>", re.S)
_RE_SPACES = re.compile(r"[ \t\u00a0\u3000]+")
_RE_NEWLINES = re.compile(r"\n{3,}")
_RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title\s*>", re.I | re.S)


class _FetchError(Exception):
    """内部错误：统一由 :func:`fetch_text` 转成 ``ok=False`` 的返回值。"""


# ---------------------------------------------------------------- IP 判定


def is_blocked_ip(ip: str) -> bool:
    """判断一个 IP 是否落在禁止访问的范围（内网/环回/组播/保留等）。

    解析失败或带 zone id（``fe80::1%eth0``）的地址按**危险**处理（fail-closed）。
    """
    text = str(ip or "").strip()
    if not text:
        return True
    if "%" in text:  # 带 zone id 的链路本地地址一律拒绝
        return True
    text = text.strip("[]")
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return True

    if isinstance(address, ipaddress.IPv6Address):
        # IPv4-mapped（::ffff:a.b.c.d）与 IPv4-translated 一律拒绝，不区分内嵌是否公网。
        if address.ipv4_mapped is not None:
            return True
        for network in _BLOCKED_V6_NETWORKS:
            if address in network:
                return True
        return _generic_blocked(address)

    for network in _BLOCKED_V4_NETWORKS:
        if address in network:
            return True
    return _generic_blocked(address)


def _generic_blocked(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """用 ipaddress 自带属性再兜一层（版本间网段表有差异，这里双保险）。"""
    return bool(
        address.is_private
        or address.is_reserved
        or address.is_multicast
        or address.is_loopback
        or address.is_link_local
        or address.is_unspecified
        or getattr(address, "is_site_local", False)
    )


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


# ---------------------------------------------------------------- URL 解析


def _idna_host(host: str) -> str:
    """把主机名转成 ASCII（IDN → punycode）；非法名称抛 ``_FetchError``。"""
    if not host:
        raise _FetchError("URL 缺少主机名")
    if _is_ip_literal(host):
        return host
    if any(ch in host for ch in " \t\r\n/\\?#"):
        raise _FetchError(f"主机名包含非法字符：{host!r}")
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise _FetchError(f"主机名无法解析：{host}（{exc}）") from None
    if not _RE_HOSTNAME.fullmatch(ascii_host):
        raise _FetchError(f"主机名包含非法字符：{host!r}")
    return ascii_host


def _parse_target(raw: str) -> dict:
    """校验 URL 并拆成抓取所需的目标描述；不合规时抛 ``_FetchError``。"""
    text = str(raw or "").strip()
    if not text:
        raise _FetchError("URL 为空")
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise _FetchError(f"URL 无效：{exc}") from None

    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        shown = scheme or "空协议"
        raise _FetchError(f"仅允许 http/https，拒绝 {shown}")

    if parts.username is not None or parts.password is not None:
        raise _FetchError("URL 不能包含凭据（user:pass@）")

    try:
        host = (parts.hostname or "").strip()
    except ValueError as exc:
        raise _FetchError(f"URL 主机名无效：{exc}") from None
    if not host:
        raise _FetchError("URL 缺少主机名")

    try:
        port = parts.port
    except ValueError as exc:
        raise _FetchError(f"URL 端口无效：{exc}") from None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    if not 0 < int(port) < 65536:
        raise _FetchError(f"URL 端口无效：{port}")

    host = _idna_host(host.rstrip("."))
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    return {
        "url": urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, "")),
        "scheme": scheme,
        "host": host,
        "port": int(port),
        "path": path,
    }


def _resolve_addresses(host: str, port: int) -> list[str]:
    """解析主机名的全部地址并逐个校验，返回「IPv4 优先、两族交替」的尝试顺序。"""
    if _is_ip_literal(host):
        if is_blocked_ip(host):
            raise _FetchError(f"禁止访问内网/本机地址：{host}")
        return [host]

    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost") or lowered.endswith(".local"):
        raise _FetchError("禁止访问内网/本机地址（localhost/.local）")

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise _FetchError(f"域名解析失败：{host}（{exc}）") from None

    seen: set[str] = set()
    addresses: list[str] = []
    for info in infos or ():
        try:
            address = str(info[4][0]).strip()
        except (IndexError, TypeError):
            continue
        if not address or address in seen:
            continue
        seen.add(address)
        if is_blocked_ip(address):
            raise _FetchError(f"域名解析到内网/本机地址，已阻止：{address}")
        addresses.append(address)

    if not addresses:
        raise _FetchError(f"域名没有解析结果：{host}")

    ipv4 = [item for item in addresses if ":" not in item]
    ipv6 = [item for item in addresses if ":" in item]
    ordered: list[str] = []
    for index in range(max(len(ipv4), len(ipv6))):
        if index < len(ipv4):
            ordered.append(ipv4[index])
        if index < len(ipv6):
            ordered.append(ipv6[index])
    return ordered


# ---------------------------------------------------------------- 连接层


def _host_header(host: str, port: int, scheme: str) -> str:
    """拼 Host 头：IPv6 字面量要加方括号，非默认端口要带端口。"""
    shown = f"[{host}]" if ":" in host else host
    if port != _DEFAULT_PORTS[scheme]:
        shown = f"{shown}:{port}"
    return shown


def _connect_pinned(address: str, port: int, timeout: float | None) -> socket.socket:
    """直接连到**已校验的 IP**（不再走 getaddrinfo，杜绝二次解析）。"""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        if family == socket.AF_INET6:
            sock.connect((address, port, 0, 0))
        else:
            sock.connect((address, port))
        return sock
    except BaseException:
        try:
            sock.close()
        except OSError:
            pass
        raise


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连接固定到已校验 IP 的 HTTP 连接（Host 头由调用方显式给出）。"""

    def __init__(self, host: str, port: int, *, pinned_ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # noqa: D401 - 覆盖父类实现
        self.sock = _connect_pinned(self._pinned_ip, self.port, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连接固定到已校验 IP 的 HTTPS 连接。

    TLS 的 SNI 与证书校验仍然针对**原域名**（``server_hostname=self.host``），
    只有 TCP 目标被钉在已校验的 IP 上。
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_ip: str,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:  # noqa: D401 - 覆盖父类实现
        sock = _connect_pinned(self._pinned_ip, self.port, self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise


def _open_connection(
    scheme: str,
    host: str,
    port: int,
    address: str,
    *,
    timeout: float,
) -> http.client.HTTPConnection:
    """创建「钉住 IP」的连接对象。

    单独抽成函数是给测试留的接缝：离线测试替换本函数即可注入假连接，
    不会真的建 socket。
    """
    if scheme == "https":
        context = ssl.create_default_context()
        return _PinnedHTTPSConnection(
            host, port, pinned_ip=address, timeout=timeout, context=context
        )
    return _PinnedHTTPConnection(host, port, pinned_ip=address, timeout=timeout)


# ---------------------------------------------------------------- 响应读取


def _header(response: object, name: str) -> str:
    """尽量兼容地取响应头（真 HTTPResponse 用 getheader，假对象用 headers）。"""
    getter = getattr(response, "getheader", None)
    if callable(getter):
        try:
            value = getter(name)
        except Exception:  # noqa: BLE001 - 假对象实现千奇百怪，取不到就当空
            value = None
        if value:
            return str(value)
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            value = headers.get(name)
        except Exception:  # noqa: BLE001
            value = None
        if value:
            return str(value)
    return ""


def _read_limited(response: object, limit: int) -> tuple[bytes, bool]:
    """流式读响应体，最多读 ``limit`` 字节；返回 (正文, 是否被截断)。"""
    chunks: list[bytes] = []
    total = 0
    truncated = False
    while True:
        room = limit - total
        if room <= 0:
            break
        chunk = response.read(min(_READ_CHUNK, room))
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray)):
            chunk = bytes(chunk)
        chunk = bytes(chunk)
        if len(chunk) > room:
            chunk = chunk[:room]
            truncated = True
        chunks.append(chunk)
        total += len(chunk)
        if truncated:
            break
    if not truncated and total >= limit:
        # 恰好读满上限：再探 1 字节判断是否还有后续内容。
        try:
            extra = response.read(1)
        except Exception:  # noqa: BLE001 - 探测失败不影响已读内容
            extra = b""
        if extra:
            truncated = True
    return b"".join(chunks), truncated


def _request_once(
    target: dict,
    address: str,
    *,
    timeout: float,
    max_bytes: int,
) -> dict:
    """向**一个**已校验地址发起 GET，返回状态/头/正文（重定向只回状态与 Location）。"""
    conn = _open_connection(
        target["scheme"], target["host"], target["port"], address, timeout=timeout
    )
    try:
        headers = {
            "Host": _host_header(target["host"], target["port"], target["scheme"]),
            "User-Agent": USER_AGENT,
            "Accept": ACCEPT,
            "Accept-Language": ACCEPT_LANGUAGE,
            "Connection": "close",
        }
        conn.request("GET", target["path"], headers=headers)
        response = conn.getresponse()
        status = int(getattr(response, "status", 0) or 0)
        reason = str(getattr(response, "reason", "") or "")
        if status in REDIRECT_STATUSES:
            # 不下载重定向正文：攻击者可以用无限正文拖住连接。
            return {
                "status": status,
                "reason": reason,
                "location": _header(response, "location"),
                "content_type": "",
                "body": b"",
                "truncated": False,
            }
        body, truncated = _read_limited(response, max_bytes)
        return {
            "status": status,
            "reason": reason,
            "location": "",
            "content_type": _header(response, "content-type"),
            "body": body,
            "truncated": truncated,
        }
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - 关闭失败不应影响结果
            pass


# ---------------------------------------------------------------- 解码与正文处理


def _normalize_charset(label: str) -> str:
    """规整编码名（``utf8`` → ``utf-8``、``GB2312`` → ``gb2312`` 等）。"""
    text = str(label or "").strip().strip("\"'").lower()
    aliases = {
        "utf8": "utf-8",
        "utf-8": "utf-8",
        "gb2312": "gbk",  # gb2312 页面实际常混入 gbk 字符
        "gb_2312-80": "gbk",
        "x-gbk": "gbk",
        "cp936": "gbk",
        "ms936": "gbk",
    }
    return aliases.get(text, text)


def _charset_from_meta(raw_head: bytes) -> str:
    """从 ``<meta charset>`` / ``<meta http-equiv>`` 里取编码。"""
    for pattern in (_RE_META_CHARSET, _RE_META_CONTENT):
        match = pattern.search(raw_head or b"")
        if match:
            try:
                return match.group(1).decode("ascii", "ignore")
            except Exception:  # noqa: BLE001
                continue
    return ""


def _decode_body(raw: bytes, content_type: str) -> str:
    """解码正文：响应头 charset → ``<meta charset>`` → utf-8 → gbk 兜底，错误字符替换。"""
    charset = ""
    match = _RE_HEADER_CHARSET.search(str(content_type or ""))
    if match:
        charset = match.group(1)
    if not charset:
        charset = _charset_from_meta(raw[:4096])

    if charset:
        encoding = _normalize_charset(charset)
        try:
            return raw.decode(encoding, "replace")
        except (LookupError, ValueError):
            pass  # 未知编码名：继续走下面的探测

    for encoding in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(encoding)
        except LookupError:
            continue
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def html_to_text(html: str) -> str:
    """HTML → 可读纯文本：去脚本/样式、去标签、解实体、折叠空白。"""
    text = str(html or "")
    if not text:
        return ""
    text = _RE_COMMENT.sub(" ", text)
    text = _RE_DROP_BLOCKS.sub(" ", text)
    text = _RE_BLOCK_TAGS.sub("\n", text)
    text = _RE_TAGS.sub(" ", text)
    text = unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _RE_SPACES.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _RE_NEWLINES.sub("\n\n", text)
    return text.strip()


def extract_title(html: str) -> str:
    """取 ``<title>`` 的纯文本；没有标题时返回空串。"""
    match = _RE_TITLE.search(str(html or ""))
    if not match:
        return ""
    return html_to_text(match.group(1)).replace("\n", " ").strip()


def _collapse_plain(text: str) -> str:
    """纯文本正文的空白折叠（与 html_to_text 的收尾规则一致）。"""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _RE_SPACES.sub(" ", text)
    text = _RE_NEWLINES.sub("\n\n", text)
    return text.strip()


def _looks_like_html(content_type: str, raw_head: bytes) -> bool:
    """按 Content-Type 判断正文是不是 HTML（头部缺失时看开头字节）。"""
    lowered = str(content_type or "").lower()
    if "html" in lowered or "xml" in lowered:
        return True
    if "json" in lowered or "plain" in lowered or "javascript" in lowered:
        return False
    head = (raw_head or b"")[:512].lstrip().lower()
    return head.startswith(b"<!doctype html") or head.startswith(b"<html") or b"<html" in head


# ---------------------------------------------------------------- 对外入口


def _validate_limit(name: str, value: object, *, allow_zero: bool = False) -> int:
    """把一个数量参数校验成正整数（或非负整数）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _FetchError(f"{name} 必须是数字")
    number = int(value)
    if number != value:
        raise _FetchError(f"{name} 必须是整数")
    if number < 0 or (number == 0 and not allow_zero):
        raise _FetchError(f"{name} 必须为{'非负' if allow_zero else '正'}整数")
    return number


def _empty_result(url: str) -> dict:
    """统一的结果骨架（失败时也保持字段齐全，调用方无需判空）。"""
    return {
        "ok": False,
        "url": _display_url(url),
        "status": 0,
        "title": "",
        "text": "",
        "chars": 0,
        "error": "",
        "truncated": False,
    }


def _display_url(raw: str) -> str:
    """回显用的 URL：抹掉可能存在的 ``user:pass``，失败信息里不泄露凭据。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if parts.username is None and parts.password is None:
        return text
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def fetch_text(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
) -> dict:
    """抓取网页并转成可读纯文本（带 SSRF 防护，fail-closed）。

    :param url: 目标 URL，仅允许 http/https。
    :param timeout: 单跳总超时（秒，连接 + 读取）。
    :param max_bytes: 最多读取多少字节响应体，超出即停止并标记 truncated。
    :param max_chars: 正文最多保留多少个字符。
    :param max_redirects: 最多跟随几跳重定向（默认 5）。
    :return: ``{"ok": bool, "url": 最终URL, "status": int, "title": str,
        "text": str, "chars": int, "error": str, "truncated": bool}``；
        任何失败都是 ``ok=False`` + 可读 ``error``，不抛异常。
    """
    result = _empty_result(url)
    try:
        timeout_value = float(timeout)
        if timeout_value <= 0:
            raise _FetchError("timeout 必须为正数")
        byte_limit = _validate_limit("max_bytes", max_bytes)
        char_limit = _validate_limit("max_chars", max_chars)
        redirect_limit = _validate_limit("max_redirects", max_redirects, allow_zero=True)

        target = _parse_target(url)
        result["url"] = target["url"]

        for hop in range(redirect_limit + 1):
            addresses = _resolve_addresses(target["host"], target["port"])
            response = _request_with_fallback(
                target, addresses, timeout=timeout_value, max_bytes=byte_limit
            )

            if response["status"] in REDIRECT_STATUSES:
                location = str(response.get("location") or "").strip()
                if not location:
                    raise _FetchError(f"重定向缺少 Location（HTTP {response['status']}）")
                if hop >= redirect_limit:
                    raise _FetchError(f"重定向次数过多（超过 {redirect_limit} 跳）")
                next_url = urljoin(target["url"], location)
                target = _parse_target(next_url)  # 下一跳重新做完整校验
                result["url"] = target["url"]
                continue

            status = int(response["status"] or 0)
            result["status"] = status
            if not 200 <= status < 300:
                reason = str(response.get("reason") or "").strip()
                suffix = f" {reason}" if reason else ""
                raise _FetchError(f"HTTP {status}{suffix}")

            raw = response["body"]
            content_type = str(response.get("content_type") or "")
            if _looks_like_html(content_type, raw):
                decoded = _decode_body(raw, content_type)
                title = extract_title(decoded)
                text = html_to_text(decoded)
            else:
                decoded = _decode_body(raw, content_type)
                title = ""
                text = _collapse_plain(decoded)

            clipped = text[:char_limit]
            result.update(
                ok=True,
                status=status,
                title=title[:500],
                text=clipped,
                chars=len(clipped),
                error="",
                truncated=bool(response.get("truncated")),
            )
            return result

        raise _FetchError("重定向次数过多，已停止")
    except _FetchError as exc:
        result["ok"] = False
        result["error"] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - 兜底：任何意外都 fail-closed
        result["ok"] = False
        result["error"] = f"抓取失败：{type(exc).__name__}: {exc}"
        return result


def _request_with_fallback(
    target: dict,
    addresses: list[str],
    *,
    timeout: float,
    max_bytes: int,
) -> dict:
    """依次尝试已校验的地址；全部失败时抛最后一个错误（文案保留原始原因）。"""
    if not addresses:
        raise _FetchError(f"没有可用地址：{target['host']}")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    attempts = addresses[:_MAX_ADDRESS_ATTEMPTS]
    for index, address in enumerate(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            break
        try:
            return _request_once(
                target, address, timeout=remaining, max_bytes=max_bytes
            )
        except _FetchError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络层失败换下一个地址
            last_error = exc
            if index == len(attempts) - 1:
                break
    if last_error is not None:
        detail = f"；已尝试 {len(attempts)} 个地址" if len(attempts) > 1 else ""
        raise _FetchError(f"请求失败：{target['host']}（{last_error}{detail}）")
    raise _FetchError(f"请求超时：{target['host']}")
