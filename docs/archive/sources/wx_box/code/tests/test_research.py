# -*- coding: utf-8 -*-
"""Batch 2 / Agent B：research 抓正文 + 摘句。

所有请求都打本机 ``127.0.0.1`` 的临时 HTTP server，不访问外网。
"""
from __future__ import annotations

import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agi.selflearn import research as research_mod

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>学习笔记：SQLite FTS5</title>
  <style>body { color: #333; } .hidden { display: none; }</style>
  <script>var tracker = "脚本里的字不要";</script>
</head>
<body>
  <!-- 注释里的字也不要 -->
  <h1>SQLite FTS5 入门</h1>
  <p>FTS5 是 SQLite 的全文检索扩展，适合做本地搜索。</p>
  <div>
    建立虚表后，可以用 <b>MATCH</b> 查询。
  </div>
  <script>console.log("正文脚本也不要");</script>
  <ul>
    <li>第一步：创建虚表</li>
    <li>第二步：插入文本</li>
  </ul>
  <p>大文件要 stream 处理，别一次读进内存。</p>
  <p>字符实体 &amp; 也要还原。</p>
</body>
</html>
"""

TEXT = (
    "SQLite 的 FTS5 是全文检索扩展，适合本地搜索。"
    "大文件要 stream 处理，别一次读进内存。"
    "写完记得跑测试。"
)


class _Handler(BaseHTTPRequestHandler):
    server_version = "ResearchTest/1.0"

    def log_message(self, format, *args):  # noqa: A002 - 保持标准签名，静音日志
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - 基类约定的方法名
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(PAGE)
        elif path == "/plain":
            self._send("纯文本第一行\n纯文本第二行\n", "text/plain; charset=utf-8")
        elif path == "/gbk":
            page = (
                '<html><head><meta charset="gb2312"><title>旧站文章</title></head>'
                "<body><p>这是 GBK 编码的正文。</p></body></html>"
            )
            self._send(page.encode("gbk"), "text/html")
        elif path == "/slow":
            time.sleep(1.5)
            self._send(PAGE)
        elif path == "/empty":
            self._send("", "text/html; charset=utf-8")
        elif path == "/only-script":
            self._send(
                "<html><head><style>.x{color:red}</style></head>"
                '<body><script>var only = "只有脚本";</script></body></html>'
            )
        elif path == "/binary":
            self._send(b"\x00\x01\x02", "application/octet-stream")
        elif path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()
        else:
            self.send_error(404, "Not Found")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # 客户端超时断开后写回包会抛错，测试里静音即可
        pass


@pytest.fixture()
def base_url():
    server = _Server(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.05), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# ---------------------------------------------------------------------------
# fetch_text：正常抓取 / 去标签
# ---------------------------------------------------------------------------

def test_fetch_text_returns_body_without_tags(base_url):
    result = research_mod.fetch_text(base_url + "/")

    assert set(result) == {"ok", "url", "title", "text", "error"}
    assert result["ok"] is True
    assert result["error"] == ""
    assert result["url"] == base_url + "/"
    assert result["title"] == "学习笔记：SQLite FTS5"

    text = result["text"]
    assert "FTS5 是 SQLite 的全文检索扩展，适合做本地搜索。" in text
    assert "建立虚表后，可以用 MATCH 查询。" in text
    assert "第一步：创建虚表" in text
    assert "第二步：插入文本" in text
    assert "大文件要 stream 处理，别一次读进内存。" in text
    assert "字符实体 & 也要还原。" in text

    # 脚本 / 样式 / 注释都不进正文，标签本身也不留
    for leaked in ("脚本里的字不要", "正文脚本也不要", "注释里的字也不要",
                   "display: none", "color: #333"):
        assert leaked not in text
    assert "<" not in text and ">" not in text

    # 块文本逐行清理过：没有空行、没有行首尾空格
    assert "\n\n" not in text
    assert all(line == line.strip() for line in text.splitlines())


def test_fetch_text_follows_redirect_and_reports_final_url(base_url):
    result = research_mod.fetch_text(base_url + "/redirect")

    assert result["ok"] is True
    assert result["url"] == base_url + "/"
    assert "SQLite FTS5 入门" in result["text"]


def test_fetch_text_plain_text_and_gbk_page(base_url):
    plain = research_mod.fetch_text(base_url + "/plain")
    assert plain["ok"] is True
    assert plain["title"] == ""
    assert "纯文本第一行" in plain["text"]
    assert "纯文本第二行" in plain["text"]

    gbk = research_mod.fetch_text(base_url + "/gbk")
    assert gbk["ok"] is True
    assert gbk["title"] == "旧站文章"
    assert "这是 GBK 编码的正文。" in gbk["text"]


# ---------------------------------------------------------------------------
# fetch_text：失败路径 fail-closed
# ---------------------------------------------------------------------------

def test_fetch_text_404_fails_closed(base_url):
    result = research_mod.fetch_text(base_url + "/missing")

    assert result["ok"] is False
    assert result["text"] == ""
    assert result["title"] == ""
    assert result["url"] == base_url + "/missing"
    assert "404" in result["error"]


def test_fetch_text_timeout_fails_closed(base_url):
    started = time.monotonic()
    result = research_mod.fetch_text(base_url + "/slow", timeout=0.2)
    elapsed = time.monotonic() - started

    assert result["ok"] is False
    assert result["text"] == ""
    assert "timeout" in result["error"].lower()
    assert elapsed < 1.0, "客户端应早于服务端的 1.5s 延迟失败"


@pytest.mark.parametrize("path", ["/empty", "/only-script"])
def test_fetch_text_empty_content_fails_closed(base_url, path):
    result = research_mod.fetch_text(base_url + path)

    assert result["ok"] is False
    assert result["text"] == ""
    assert result["error"]


def test_fetch_text_rejects_non_text_content(base_url):
    result = research_mod.fetch_text(base_url + "/binary")

    assert result["ok"] is False
    assert result["text"] == ""
    assert "content type" in result["error"]


@pytest.mark.parametrize("url", [
    "", None, "not-a-url", "file:///C:/Windows/win.ini", "ftp://127.0.0.1/x",
])
def test_fetch_text_rejects_bad_urls_without_network(url):
    result = research_mod.fetch_text(url)

    assert result["ok"] is False
    assert result["text"] == ""
    assert result["error"]


@pytest.mark.parametrize("timeout", [0, -1, "abc", None])
def test_fetch_text_rejects_invalid_timeout(base_url, timeout):
    result = research_mod.fetch_text(base_url + "/", timeout=timeout)

    assert result["ok"] is False
    assert result["error"] == "invalid timeout"


def test_fetch_text_connection_refused_fails_closed():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    result = research_mod.fetch_text(f"http://127.0.0.1:{port}/", timeout=1)

    assert result["ok"] is False
    assert result["text"] == ""
    assert result["error"]


# ---------------------------------------------------------------------------
# extract_quote：命中 / 未命中 / 裁剪
# ---------------------------------------------------------------------------

def test_extract_quote_hits_sentence_with_keyword():
    quote = research_mod.extract_quote(TEXT, "stream")
    assert quote == "大文件要 stream 处理，别一次读进内存。"
    assert "FTS5" not in quote

    chinese = research_mod.extract_quote(TEXT, "全文检索")
    assert chinese == "SQLite 的 FTS5 是全文检索扩展，适合本地搜索。"
    assert isinstance(chinese, str)


def test_extract_quote_case_insensitive_keeps_original_case():
    quote = research_mod.extract_quote("Python 很好用。它适合写小工具。", "python")
    assert quote == "Python 很好用。"


def test_extract_quote_collapses_whitespace_to_one_line():
    text = "第一段废话。\n\n  关键  词在这一句里。\n第三段。"
    quote = research_mod.extract_quote(text, "关键")

    assert quote == "关键 词在这一句里。"
    assert "\n" not in quote


def test_extract_quote_truncates_around_keyword():
    text = "前" * 100 + "关键词" + "后" * 100 + "。"
    quote = research_mod.extract_quote(text, "关键词", max_len=20)

    assert len(quote) <= 20
    assert "关键词" in quote
    assert quote.startswith("…") and quote.endswith("…")


def test_extract_quote_returns_empty_on_miss():
    assert research_mod.extract_quote(TEXT, "量子计算") == ""
    assert research_mod.extract_quote(TEXT, "") == ""
    assert research_mod.extract_quote(TEXT, "   ") == ""
    assert research_mod.extract_quote(TEXT, None) == ""
    assert research_mod.extract_quote("", "stream") == ""
    assert research_mod.extract_quote(None, "stream") == ""


def test_extract_quote_never_raises_and_respects_max_len():
    assert research_mod.extract_quote("abc", "a", max_len=0) == ""
    assert research_mod.extract_quote("abc", "a", max_len=-5) == ""
    assert research_mod.extract_quote("abc", "a", max_len="abc") == ""

    tiny = research_mod.extract_quote(TEXT, "stream", max_len=1)
    assert isinstance(tiny, str)
    assert len(tiny) <= 1

    normal = research_mod.extract_quote(TEXT, "stream")
    assert isinstance(normal, str) and len(normal) <= 200
