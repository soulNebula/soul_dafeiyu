"""web_search 离线测试（**不联网**）。

用 monkeypatch 替换 HTTP 层（``_http_request``）与凭据解析，验证：

1. DeepSeek 成功时能解析出结构化结果与文本里的引用；
2. DeepSeek 失败（无 key / HTTP 错误）时自动回退 Bing RSS；
3. 两个后端都失败时 ``provider="none"`` 且错误带上两边原因；
4. 结果条数上限与按 URL 去重；
5. 凭据解析顺序（web_config.json → DEEPSEEK_API_KEY）；
6. 请求不读代理环境变量。
"""
from __future__ import annotations

import json
import urllib.request

import pytest

from wx_core import web_search

DEEPSEEK_ENDPOINT = "https://api.deepseek.com/anthropic/v1/messages"

# 模拟 Anthropic 兼容响应：结构化结果 + 文本里另有一个未被结构化的 URL。
DEEPSEEK_OK = {
    "content": [
        {
            "type": "text",
            "text": "参考 https://c.example/3 的说明，另外 a 的摘要见引用。",
            "citations": [
                {"url": "https://a.example/1", "cited_text": "A 的摘要"},
                {"url": "https://b.example/2", "cited_text": "B 的摘要"},
            ],
        },
        {
            "type": "web_search_tool_result",
            "content": [
                {
                    "type": "web_search_result",
                    "title": "A 标题",
                    "url": "https://a.example/1",
                    "page_age": "1 day",
                },
                {
                    "type": "web_search_result",
                    "title": "B 标题",
                    "url": "https://b.example/2",
                },
                # 同一个 URL 重复出现一次，去重后不应多出结果
                {
                    "type": "web_search_result",
                    "title": "A 标题（重复）",
                    "url": "https://a.example/1#1",
                },
            ],
        },
    ]
}

BING_RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0">
  <channel>
    <title>Bing: 测试</title>
    <item>
      <title>结果一 &amp; 标题</title>
      <link>https://r1.example/page</link>
      <description>&lt;b&gt;摘要一&lt;/b&gt;：第一段内容</description>
    </item>
    <item>
      <title>结果二</title>
      <link>https://r2.example/</link>
      <description>摘要二</description>
    </item>
    <item>
      <title>重复结果一</title>
      <link>https://r1.example/page</link>
      <description>重复条目</description>
    </item>
    <item>
      <title>结果三</title>
      <link>https://r3.example/</link>
      <description>摘要三</description>
    </item>
  </channel>
</rss>
"""


# ---------------------------------------------------------------- 夹具


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """默认禁止任何真实请求；需要的用例在函数体内再覆盖。"""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("测试不应发起真实网络请求")

    monkeypatch.setattr(web_search, "_http_request", boom)


def fake_http(monkeypatch, handler):
    """替换 HTTP 层；返回调用记录。handler(url, kwargs) -> (status, body)。"""
    calls: list[dict] = []

    def fake(url, **kwargs):  # noqa: ANN001
        calls.append({"url": url, "kwargs": kwargs})
        return handler(url, kwargs)

    monkeypatch.setattr(web_search, "_http_request", fake)
    return calls


def use_credentials(monkeypatch, key: str = "sk-test", base_url: str = "https://api.deepseek.com"):
    monkeypatch.setattr(
        web_search, "_resolve_credentials", lambda: (key, base_url)
    )


# ---------------------------------------------------------------- DeepSeek


def test_deepseek_success_parses_results(monkeypatch):
    use_credentials(monkeypatch)
    calls = fake_http(
        monkeypatch, lambda url, kwargs: (200, json.dumps(DEEPSEEK_OK).encode("utf-8"))
    )

    result = web_search.search("Python 3.14", count=5)

    assert result["ok"] is True
    assert result["provider"] == "deepseek"
    assert result["error"] == ""
    urls = [item["url"] for item in result["results"]]
    assert urls == [
        "https://a.example/1",
        "https://b.example/2",
        "https://c.example/3",
    ]
    assert result["results"][0]["title"] == "A 标题"
    assert result["results"][0]["snippet"] == "A 的摘要"
    assert result["results"][1]["snippet"] == "B 的摘要"
    # 文本里只有 URL、没有结构化 title 时，用相邻上下文兜底
    assert result["results"][2]["snippet"]
    assert "c.example" in result["results"][2]["title"]

    # 请求确实打到了 Anthropic 兼容端点，并带上 web_search 工具
    assert calls[0]["url"] == DEEPSEEK_ENDPOINT
    assert calls[0]["kwargs"]["method"] == "POST"
    headers = calls[0]["kwargs"]["headers"]
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"]
    payload = json.loads(calls[0]["kwargs"]["body"].decode("utf-8"))
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["tools"] == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
    ]
    assert "Python 3.14" in json.dumps(payload, ensure_ascii=False)


def test_deepseek_count_limit_and_dedupe(monkeypatch):
    use_credentials(monkeypatch)

    def handler(url, kwargs):  # noqa: ANN001
        payload = {
            "content": [
                {
                    "type": "web_search_tool_result",
                    "content": [
                        {"type": "web_search_result", "title": "A", "url": "https://a.example/1"},
                        {"type": "web_search_result", "title": "A2", "url": "https://a.example/1#2"},
                        {"type": "web_search_result", "title": "B", "url": "https://b.example/"},
                        {"type": "web_search_result", "title": "C", "url": "https://c.example/"},
                    ],
                }
            ]
        }
        return 200, json.dumps(payload).encode("utf-8")

    fake_http(monkeypatch, handler)

    result = web_search.search("去重", count=2)

    assert result["provider"] == "deepseek"
    assert len(result["results"]) == 2
    urls = [item["url"] for item in result["results"]]
    assert len(set(urls)) == len(urls)
    assert urls == ["https://a.example/1", "https://b.example/"]


def test_deepseek_http_error_falls_back_to_bing(monkeypatch):
    use_credentials(monkeypatch)

    def handler(url, kwargs):  # noqa: ANN001
        if "bing.com" in url:
            return 200, BING_RSS.encode("utf-8")
        return 401, json.dumps({"error": {"message": "invalid api key"}}).encode("utf-8")

    calls = fake_http(monkeypatch, handler)

    result = web_search.search("回退测试", count=5)

    assert result["ok"] is True
    assert result["provider"] == "bing-rss"
    assert any("bing.com" in item["url"] for item in calls)
    assert result["results"][0]["title"] == "结果一 & 标题"
    assert result["results"][0]["snippet"].startswith("摘要一")
    assert "<b>" not in result["results"][0]["snippet"]


def test_no_key_falls_back_to_bing(monkeypatch):
    use_credentials(monkeypatch, key="")
    calls = fake_http(
        monkeypatch,
        lambda url, kwargs: (
            (200, BING_RSS.encode("utf-8"))
            if "bing.com" in url
            else (500, b"{}")
        ),
    )

    result = web_search.search("没有 key", count=3)

    assert result["provider"] == "bing-rss"
    # 没有 key 时不应该访问 DeepSeek 端点
    assert all("api.deepseek.com" not in call["url"] for call in calls)


def test_both_backends_fail_returns_none(monkeypatch):
    use_credentials(monkeypatch)

    def handler(url, kwargs):  # noqa: ANN001
        if "bing.com" in url:
            return 500, b"<html>down</html>"
        return 500, json.dumps({"error": {"message": "boom"}}).encode("utf-8")

    fake_http(monkeypatch, handler)

    result = web_search.search("两边都挂", count=5)

    assert result["ok"] is False
    assert result["provider"] == "none"
    assert result["results"] == []
    assert "DeepSeek" in result["error"]
    assert "Bing" in result["error"]


def test_bing_network_error_returns_none(monkeypatch):
    use_credentials(monkeypatch, key="")

    def handler(url, kwargs):  # noqa: ANN001
        raise OSError("network unreachable")

    fake_http(monkeypatch, handler)

    result = web_search.search("断网", count=5)

    assert result["ok"] is False
    assert result["provider"] == "none"
    assert "network unreachable" in result["error"]


def test_empty_query_returns_early():
    result = web_search.search("   ", count=5)

    assert result["ok"] is False
    assert result["provider"] == "none"
    assert result["error"] == "查询词为空"


def test_bing_rss_count_limit_and_dedupe(monkeypatch):
    use_credentials(monkeypatch, key="")
    fake_http(
        monkeypatch,
        lambda url, kwargs: (200, BING_RSS.encode("utf-8")),
    )

    result = web_search.search("条数", count=2)

    assert result["provider"] == "bing-rss"
    assert len(result["results"]) == 2
    assert [item["url"] for item in result["results"]] == [
        "https://r1.example/page",
        "https://r2.example/",
    ]


def test_bing_rss_url_is_built_with_format_rss(monkeypatch):
    use_credentials(monkeypatch, key="")
    calls = fake_http(
        monkeypatch,
        lambda url, kwargs: (200, BING_RSS.encode("utf-8")),
    )

    web_search.search("中文 查询", count=4)

    url = calls[0]["url"]
    assert "bing.com/search" in url
    assert "format=rss" in url
    assert "count=4" in url
    # 查询词必须 URL 编码
    assert "q=%E4%B8%AD%E6%96%87" in url


# ---------------------------------------------------------------- 解析器单测


def test_parse_bing_rss_handles_entities_and_namespaces():
    xml = """<?xml version="1.0"?>
    <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel>
        <item>
          <title>带 &amp; 实体</title>
          <link>https://ns.example/a</link>
          <description>&lt;p&gt;正文&lt;/p&gt;</description>
        </item>
      </channel>
    </rss>"""

    results = web_search._parse_bing_rss(xml, 5)

    assert results == [
        {
            "title": "带 & 实体",
            "url": "https://ns.example/a",
            "snippet": "正文",
        }
    ]


def test_normalize_url_rejects_unsafe_and_cleans():
    assert web_search._normalize_url("https://ok.example/a.") == "https://ok.example/a"
    assert web_search._normalize_url("HTTPS://OK.EXAMPLE/a") == "https://ok.example/a"
    assert web_search._normalize_url("https://ok.example/a#2") == "https://ok.example/a"
    assert web_search._normalize_url("javascript:alert(1)") == ""
    assert web_search._normalize_url("ftp://ok.example/") == ""
    assert web_search._normalize_url("https://user:pass@ok.example/") == ""
    assert web_search._normalize_url("") == ""


def test_api_endpoint_variants():
    assert (
        web_search._api_endpoint("https://api.deepseek.com")
        == DEEPSEEK_ENDPOINT
    )
    assert (
        web_search._api_endpoint("https://api.deepseek.com/")
        == DEEPSEEK_ENDPOINT
    )
    assert (
        web_search._api_endpoint("https://api.deepseek.com/anthropic/v1")
        == DEEPSEEK_ENDPOINT
    )
    assert (
        web_search._api_endpoint("https://api.deepseek.com/anthropic")
        == DEEPSEEK_ENDPOINT
    )


# ---------------------------------------------------------------- 配置与代理


def test_credentials_config_then_env(monkeypatch, tmp_path):
    config = tmp_path / "web_config.json"
    config.write_text(
        json.dumps(
            {"llm": {"api_key": "sk-config", "base_url": "https://api.deepseek.com"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(web_search, "CONFIG_PATH", config)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert web_search._resolve_credentials() == ("sk-config", "https://api.deepseek.com")

    # 配置文件没有 key 时用环境变量
    config.write_text(json.dumps({"llm": {"base_url": "https://api.deepseek.com"}}), encoding="utf-8")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    assert web_search._resolve_credentials() == ("sk-env", "https://api.deepseek.com")

    # 配置文件缺失时只有环境变量
    monkeypatch.setattr(web_search, "CONFIG_PATH", tmp_path / "missing.json")
    assert web_search._resolve_credentials() == ("sk-env", "https://api.deepseek.com")


def test_config_file_is_optional(monkeypatch, tmp_path):
    monkeypatch.setattr(web_search, "CONFIG_PATH", tmp_path / "nope.json")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert web_search._read_config() == {}
    key, base_url = web_search._resolve_credentials()
    assert key == ""
    assert base_url == "https://api.deepseek.com"


def test_opener_ignores_proxy_environment(monkeypatch):
    """构造的 opener 必须显式传空代理表，不调用 getproxies()（即不读环境变量）。"""
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    def forbidden(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("不应读取代理环境变量")

    monkeypatch.setattr(urllib.request, "getproxies", forbidden)

    # 两个 opener 都要能构造成功；一旦读代理环境变量这里就会炸。
    for follow in (False, True):
        assert web_search._build_opener(follow_redirects=follow) is not None
