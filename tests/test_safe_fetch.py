"""safe_fetch 离线测试（**不联网**）。

做法：monkeypatch 掉 ``socket.getaddrinfo``（DNS）与 ``_open_connection``（连接层），
用假的响应/连接对象验证：

1. 环回 / 私网 / 链路本地 / IPv4-mapped / 非法协议 / 内嵌凭据的 URL 全部被拒；
2. 公网地址的正常 200 响应能解析出 title 与正文，且连接确实钉在校验过的 IP 上；
3. ``max_bytes`` 触发流式截断，``max_chars`` 截断生效；
4. 每一跳重定向都重新校验（跳到内网会被拦）。
"""
from __future__ import annotations

import socket

import pytest

from wx_core import safe_fetch

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "1.1.1.1"


# ---------------------------------------------------------------- 测试替身


class FakeResponse:
    """最小化的 http.client 响应替身（只实现被测代码用到的接口）。"""

    def __init__(
        self,
        body: bytes = b"",
        *,
        status: int = 200,
        headers: dict | None = None,
        reason: str = "",
    ) -> None:
        self.body = body
        self.status = status
        self.reason = reason
        self.headers = dict(headers or {})
        self.offset = 0
        self.served = 0
        self.closed = False

    def getheader(self, name: str, default=None):  # noqa: ANN001 - 兼容真响应接口
        lowered = str(name).lower()
        for key, value in self.headers.items():
            if key.lower() == lowered:
                return value
        return default

    def read(self, amount: int = -1) -> bytes:
        if amount is None or amount < 0:
            amount = len(self.body) - self.offset
        end = min(len(self.body), self.offset + int(amount))
        chunk = self.body[self.offset:end]
        self.offset = end
        self.served += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    """最小化的连接替身：记录请求头，返回预设响应。"""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[dict] = []
        self.closed = False

    def request(self, method: str, url: str, headers: dict | None = None) -> None:
        self.requests.append(
            {"method": method, "url": url, "headers": dict(headers or {})}
        )

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class TimeoutConnection(FakeConnection):
    """连接阶段超时的替身。"""

    def getresponse(self) -> FakeResponse:  # noqa: D102
        raise TimeoutError("timed out")


class FakeTLSContext:
    """只记录 SNI 的 TLS 上下文替身。"""

    check_hostname = True

    def __init__(self) -> None:
        self.server_names: list[str | None] = []

    def wrap_socket(self, sock, server_hostname=None):  # noqa: ANN001
        self.server_names.append(server_hostname)
        return "tls-wrapped"


class FakeSocket:
    """只用于证明 TLS 包装对象被回填到 conn.sock。"""

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """默认禁止任何真实网络访问；需要的用例在函数体内再覆盖。"""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应发起真实网络请求")

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", boom)
    monkeypatch.setattr(safe_fetch, "_open_connection", boom)


def fake_dns(monkeypatch, *addresses: str) -> None:
    """把 DNS 解析替换成固定地址列表。"""

    def getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        infos = []
        for address in addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            infos.append((family, socket.SOCK_STREAM, 6, "", (address, port or 80)))
        return infos

    monkeypatch.setattr(safe_fetch.socket, "getaddrinfo", getaddrinfo)


def install_connection(monkeypatch, *connections: FakeConnection):
    """替换连接层，按顺序返回假连接；返回记录「钉住的 IP」的列表。"""
    opened: list[tuple] = []
    queue = list(connections)

    def fake_open(scheme, host, port, address, *, timeout):  # noqa: ANN001
        opened.append((scheme, host, port, address))
        if not queue:
            raise AssertionError("连接次数超出预期")
        return queue.pop(0)

    monkeypatch.setattr(safe_fetch, "_open_connection", fake_open)
    return opened


# ---------------------------------------------------------------- 拒绝非法目标


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "http://169.254.1.1/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
        "http://user:pass@example.com/",
        # 额外的边界：私网、链路本地、CGNAT、ULA、空主机
        "http://192.168.1.1/",
        "http://172.20.0.1/",
        "http://[fe80::1]/",
        "http://100.64.0.1/",
        "http://[fc00::1]/",
        "http:///no-host",
        "",
    ],
)
def test_rejects_unsafe_urls(url):
    result = safe_fetch.fetch_text(url)

    assert result["ok"] is False
    assert result["error"], f"{url!r} 被拒时必须给出可读原因"
    assert result["text"] == ""
    assert result["chars"] == 0
    assert result["truncated"] is False


def test_all_resolved_addresses_are_validated(monkeypatch):
    """DNS 同时返回公网和内网地址时，整体拒绝（不允许挑一个能连的）。"""
    fake_dns(monkeypatch, PUBLIC_IP, "10.0.0.1")

    result = safe_fetch.fetch_text("http://example.com/")

    assert result["ok"] is False
    assert "内网" in result["error"]


def test_redirect_hop_is_revalidated(monkeypatch):
    """第一跳公网、第二跳 302 到内网：必须被拦。"""
    fake_dns(monkeypatch, PUBLIC_IP)
    redirect = FakeResponse(status=302, headers={"Location": "http://127.0.0.1/admin"})
    install_connection(monkeypatch, FakeConnection(redirect))

    result = safe_fetch.fetch_text("http://example.com/start")

    assert result["ok"] is False
    assert "内网" in result["error"]


def test_too_many_redirects(monkeypatch):
    fake_dns(monkeypatch, PUBLIC_IP)
    redirect = FakeResponse(status=302, headers={"Location": "http://example.com/again"})
    install_connection(monkeypatch, FakeConnection(redirect))

    result = safe_fetch.fetch_text("http://example.com/start", max_redirects=0)

    assert result["ok"] is False
    assert "重定向次数过多" in result["error"]


def test_credential_url_is_not_echoed():
    """被拒的带凭据 URL 不应把密码回显到结果里。"""
    result = safe_fetch.fetch_text("http://user:s3cret@example.com/")

    assert result["ok"] is False
    assert "s3cret" not in result["url"]
    assert "s3cret" not in result["error"]


# ---------------------------------------------------------------- 正常路径


def test_public_http_200_parses_title_and_text(monkeypatch):
    html = (
        "<html><head><title>示例 &amp; 标题</title>"
        "<style>body{color:red}</style></head>"
        "<body><script>var x = 1;</script>"
        "<p>Hello&nbsp; &amp; world</p></body></html>"
    )
    response = FakeResponse(
        html.encode("utf-8"), headers={"Content-Type": "text/html; charset=utf-8"}
    )
    connection = FakeConnection(response)
    fake_dns(monkeypatch, PUBLIC_IP)
    opened = install_connection(monkeypatch, connection)

    result = safe_fetch.fetch_text("http://example.com/page")

    assert result["ok"] is True
    assert result["status"] == 200
    assert result["url"] == "http://example.com/page"
    assert result["title"] == "示例 & 标题"
    assert "Hello & world" in result["text"]
    assert "var x = 1" not in result["text"]
    assert "color:red" not in result["text"]
    assert result["chars"] == len(result["text"])
    assert result["error"] == ""
    # 连接被钉在校验过的 IP 上，Host 头仍是原域名。
    assert opened == [("http", "example.com", 80, PUBLIC_IP)]
    assert connection.requests[0]["headers"]["Host"] == "example.com"


def test_redirect_to_public_is_followed(monkeypatch):
    first = FakeConnection(
        FakeResponse(status=301, headers={"Location": "/final"})
    )
    second = FakeConnection(
        FakeResponse(
            "<title>Final</title><p>ok</p>".encode("utf-8"),
            headers={"Content-Type": "text/html"},
        )
    )
    fake_dns(monkeypatch, PUBLIC_IP)
    install_connection(monkeypatch, first, second)

    result = safe_fetch.fetch_text("http://example.com/start")

    assert result["ok"] is True
    assert result["url"] == "http://example.com/final"
    assert result["title"] == "Final"
    assert second.requests[0]["headers"]["Host"] == "example.com"


def test_http_error_returns_fail_closed(monkeypatch):
    fake_dns(monkeypatch, PUBLIC_IP)
    install_connection(
        monkeypatch,
        FakeConnection(
            FakeResponse(status=503, reason="Service Unavailable")
        ),
    )

    result = safe_fetch.fetch_text("http://example.com/")

    assert result["ok"] is False
    assert result["status"] == 503
    assert "503" in result["error"]
    assert result["text"] == ""


def test_timeout_returns_fail_closed(monkeypatch):
    fake_dns(monkeypatch, PUBLIC_IP)
    install_connection(monkeypatch, TimeoutConnection(FakeResponse()))

    result = safe_fetch.fetch_text("http://example.com/")

    assert result["ok"] is False
    assert "请求失败" in result["error"] or "超时" in result["error"]


def test_second_address_is_tried_after_network_failure(monkeypatch):
    """第一个地址连接失败时换下一个已校验地址（不重新做 DNS）。"""
    class BrokenConnection(FakeConnection):
        def request(self, method, url, headers=None):  # noqa: ANN001
            raise ConnectionResetError("connection reset")

    fake_dns(monkeypatch, PUBLIC_IP, OTHER_PUBLIC_IP)
    good = FakeConnection(
        FakeResponse(
            "<title>OK</title><p>fallback</p>".encode("utf-8"),
            headers={"Content-Type": "text/html"},
        )
    )
    opened = install_connection(monkeypatch, BrokenConnection(FakeResponse()), good)

    result = safe_fetch.fetch_text("http://example.com/")

    assert result["ok"] is True
    assert [item[3] for item in opened] == [PUBLIC_IP, OTHER_PUBLIC_IP]


# ---------------------------------------------------------------- 限额


def test_max_bytes_truncates_streaming_body(monkeypatch):
    body = b"<html><body><p>" + b"A" * 5000 + b"</p></body></html>"
    response = FakeResponse(body, headers={"Content-Type": "text/html"})
    fake_dns(monkeypatch, PUBLIC_IP)
    install_connection(monkeypatch, FakeConnection(response))

    result = safe_fetch.fetch_text(
        "http://example.com/big", max_bytes=1024, max_chars=5000
    )

    assert result["ok"] is True
    assert result["truncated"] is True
    # 只多读 1 字节用于判断「是否还有内容」，不会把整个正文吞下来。
    assert response.served <= 1025
    assert result["chars"] <= 1024


def test_max_chars_truncates_text(monkeypatch):
    body = ("<html><body><p>" + "B" * 300 + "</p></body></html>").encode("utf-8")
    fake_dns(monkeypatch, PUBLIC_IP)
    install_connection(
        monkeypatch,
        FakeConnection(FakeResponse(body, headers={"Content-Type": "text/html"})),
    )

    result = safe_fetch.fetch_text(
        "http://example.com/long", max_bytes=100_000, max_chars=50
    )

    assert result["ok"] is True
    assert result["text"] == "B" * 50
    assert result["chars"] == 50
    assert result["truncated"] is False


def test_invalid_limits_fail_closed():
    for kwargs in ({"max_bytes": 0}, {"max_chars": 0}, {"timeout": 0}):
        result = safe_fetch.fetch_text("http://example.com/", **kwargs)
        assert result["ok"] is False
        assert result["error"]


# ---------------------------------------------------------------- 连接层细节


def test_https_connection_keeps_sni_and_pins_ip(monkeypatch):
    """HTTPS 连接对象：TCP 目标是校验过的 IP，TLS SNI 仍是原域名。"""
    context = FakeTLSContext()
    monkeypatch.setattr(safe_fetch, "_connect_pinned", lambda *a, **k: FakeSocket())

    connection = safe_fetch._PinnedHTTPSConnection(
        "example.com",
        443,
        pinned_ip=PUBLIC_IP,
        timeout=5.0,
        context=context,
    )
    connection.connect()

    assert context.server_names == ["example.com"]
    assert connection._pinned_ip == PUBLIC_IP
    assert connection.sock == "tls-wrapped"


def test_host_header_formatting():
    assert safe_fetch._host_header("example.com", 80, "http") == "example.com"
    assert safe_fetch._host_header("example.com", 443, "https") == "example.com"
    assert safe_fetch._host_header("example.com", 8443, "https") == "example.com:8443"
    assert safe_fetch._host_header("::1", 80, "http") == "[::1]"
    assert safe_fetch._host_header("2606:4700::1111", 8443, "https") == (
        "[2606:4700::1111]:8443"
    )


def test_private_and_public_ip_classification():
    for address in (
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.1.1",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "::",
        "::ffff:127.0.0.1",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::1",
    ):
        assert safe_fetch.is_blocked_ip(address) is True, address

    for address in ("8.8.8.8", "93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"):
        assert safe_fetch.is_blocked_ip(address) is False, address
