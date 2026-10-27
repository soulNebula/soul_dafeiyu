# -*- coding: utf-8 -*-
"""P24：小鱼实时信息（天气/公众号文章/少量新闻/搜索/网页）测试。

全部 mock `_get`，不联网；覆盖 intent、解析、失败、超时、SSRF、缓存、
开关，以及 chat 注入（命中注入 / 失败不注入 / 未命中 prompt 逐字节不变）。
"""
from __future__ import annotations

import json
import random
import time

import pytest

from agi.integrations import web_lookup as wl


class FakeResp:
    def __init__(self, *, status=200, text="", content=None, encoding="utf-8"):
        self.status_code = status
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.encoding = encoding
        self.url = ""


class FakeGet:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def __call__(self, url, *, timeout, headers=None, params=None):
        self.calls.append({"url": url, "timeout": timeout, "params": params})
        return self.handler(url, params=params, timeout=timeout)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    cache = tmp_path / "web_cache.json"
    monkeypatch.setenv("WEB_LOOKUP_CACHE", str(cache))
    # conftest 默认关闭联网，专项测试显式打开
    monkeypatch.setenv("WEB_LOOKUP_ENABLED", "1")
    for name in ("WEB_LOOKUP_TIMEOUT_S", "WEATHER_CITY", "NEWS_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    wl.clear_cache()
    yield


# --------------------------------------------------------------------------- #
# 1. intent 识别
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("text,kind", [
    ("北京今天天气怎么样", "weather"),
    ("冷不冷，要不要穿外套", "weather"),
    ("上海天气", "weather"),
    ("https://mp.weixin.qq.com/s/AbCdEf123", "wechat_article"),
    ("看看这篇 https://mp.weixin.qq.com/s/xyz789 讲了啥", "wechat_article"),
    ("帮我搜一下公众号文章：AI 编程", "wechat_search"),
    ("看看公众号有没有讲摄影的", "wechat_search"),
    ("最近有什么新闻", "news"),
    ("今天有什么热点", "news"),
    ("帮我查一下杭州亚运会场馆", "search"),
    ("搜一下 deepseek v4", "search"),
    ("https://example.com/post/1", "url"),
])
def test_detect_intent_kinds(text, kind):
    intent = wl.detect_intent(text)
    assert intent is not None and intent["kind"] == kind, (text, intent)


@pytest.mark.parametrize("text", [
    "今天好累啊", "晚上吃什么", "在吗", "我心情不好", "股价多少", "上证指数",
    "美元汇率", "比特币涨了吗", "黄金价格",
])
def test_detect_intent_does_not_trigger_for_chat_or_removed_stock(text):
    assert wl.detect_intent(text) is None, text


def test_detect_intent_extracts_city_and_topic():
    assert wl.detect_intent("北京天气")["params"]["city"] == "北京"
    news = wl.detect_intent("最近有什么新闻，关于人工智能")
    assert news["kind"] == "news" and "人工智能" in news["params"]["topic"]


def test_news_disabled_does_not_trigger(monkeypatch):
    monkeypatch.setenv("NEWS_ENABLED", "0")
    assert wl.detect_intent("最近有什么新闻") is None


# --------------------------------------------------------------------------- #
# 2. 天气
# --------------------------------------------------------------------------- #
def _wttr_payload():
    return json.dumps({
        "current_condition": [{
            "temp_C": "18", "FeelsLikeC": "17", "humidity": "55",
            "windspeedKmph": "12",
            "weatherDesc": [{"value": "Partly cloudy"}],
        }],
        "nearest_area": [{"areaName": [{"value": "Beijing"}]}],
    })


def test_weather_parses_wttr(monkeypatch):
    fake = FakeGet(lambda url, **kw: FakeResp(text=_wttr_payload()))
    monkeypatch.setattr(wl, "_get", fake)
    result = wl.weather("北京")
    assert result["ok"] is True
    assert result["temp"] == 18 and result["feels"] == 17
    assert result["desc"] == "Partly cloudy"
    assert result["city"] == "北京"   # 用户给的城市优先（不用英文 areaName）
    assert result["source"] == "wttr.in"
    assert fake.calls[0]["params"] == {"format": "j1"}


def test_weather_falls_back_to_tencent(monkeypatch):
    def handler(url, **kwargs):
        if "wttr.in" in url:
            raise RuntimeError("wttr down")
        return FakeResp(text=json.dumps({"data": {"observe": {
            "weather": "晴", "degree": "20", "humidity": "40",
            "windpower": "3"}}}))

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.weather("上海")
    assert result["ok"] is True and result["source"] == "腾讯天气"
    assert result["temp"] == 20 and result["desc"] == "晴"


def test_weather_failure_is_explicit(monkeypatch):
    def handler(url, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.weather("北京")
    assert result["ok"] is False
    assert "wttr" in result["error"] or "timed out" in result["error"]


# --------------------------------------------------------------------------- #
# 3. 公众号文章（核心）
# --------------------------------------------------------------------------- #
ARTICLE_HTML = """<html><head><title>页面标题</title></head><body>
<h1 id="activity-name">  AI 编程这一年  </h1>
<a id="js_name"> 36氪 </a>
<em id="publish_time">2026-10-20 08:30</em>
<div id="js_content">
  <p>第一段：AI 编程工具在过去一年变化很大。</p>
  <p>第二段：很多人开始把写代码交给模型。</p>
  <script>var x=1</script>
</div></body></html>"""


def test_wechat_article_parses_title_account_text(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=ARTICLE_HTML)))
    result = wl.wechat_article("https://mp.weixin.qq.com/s/abc")
    assert result["ok"] is True
    assert result["title"] == "AI 编程这一年"
    assert result["account"] == "36氪"
    assert "2026-10-20" in result["published_at"]
    assert result["text"].startswith("第一段：AI 编程工具")
    assert "var x=1" not in result["text"]
    assert len(result["text"]) <= wl.MAX_ARTICLE_TEXT


def test_wechat_article_blocked_page(monkeypatch):
    blocked = "<html><body><h2>环境异常</h2><p>请在微信客户端打开链接</p></body></html>"
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=blocked)))
    result = wl.wechat_article("https://mp.weixin.qq.com/s/blocked")
    assert result["ok"] is False and result["error"] == "blocked"


def test_wechat_article_rejects_non_mp_url():
    result = wl.wechat_article("https://example.com/article")
    assert result["ok"] is False and result["error"] == "not_mp_weixin_url"


def test_wechat_article_http_failure(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(status=500, text="err")))
    result = wl.wechat_article("https://mp.weixin.qq.com/s/x")
    assert result["ok"] is False and "http 500" in result["error"]


# --------------------------------------------------------------------------- #
# 4. 公众号搜索
# --------------------------------------------------------------------------- #
WECHAT_SEARCH_HTML = """<html><body><ul class="news-list">
<li><h3><a href="/link?url=1">文章一：AI 编程</a></h3>
    <a class="account">硅星人</a><p class="txt-info">摘要一</p></li>
<li><h3><a href="/link?url=2">文章二：模型评测</a></h3>
    <a class="account">量子位</a><p class="txt-info">摘要二</p></li>
</ul></body></html>"""


def test_wechat_search_parses_items(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=WECHAT_SEARCH_HTML)))
    result = wl.wechat_search("AI 编程")
    assert result["ok"] is True and result["count"] == 2
    assert result["items"][0]["title"].startswith("文章一")
    assert result["items"][0]["account"] == "硅星人"
    assert result["items"][0]["url"].startswith("https://weixin.sogou.com")


def test_wechat_search_captcha_is_friendly_failure(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text="<html>请输入验证码</html>")))
    result = wl.wechat_search("AI")
    assert result["ok"] is False
    assert "验证码" in result["error"] or "限流" in result["error"]
    assert "链接直接发我" in result["error"]


# --------------------------------------------------------------------------- #
# 5. 新闻（top3）/ 搜索（top3）
# --------------------------------------------------------------------------- #
RSS_XML = """<rss><channel>
<item><title>新闻一</title><link>https://a/1</link><description>摘要一</description></item>
<item><title>新闻二</title><link>https://a/2</link><description>摘要二</description></item>
<item><title>新闻三</title><link>https://a/3</link><description>摘要三</description></item>
<item><title>新闻四</title><link>https://a/4</link><description>摘要四</description></item>
</channel></rss>"""


def test_news_rss_returns_top3(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(content=RSS_XML.encode("utf-8"), text=RSS_XML)))
    result = wl.news("")
    assert result["ok"] is True and result["count"] == 3
    assert [item["title"] for item in result["items"]] == ["新闻一", "新闻二", "新闻三"]


def test_news_falls_back_to_sogou(monkeypatch):
    sogou = ('<html><body><h3><a href="/n/1">搜狗新闻一</a></h3>'
             '<h3><a href="/n/2">搜狗新闻二</a></h3></body></html>')

    def handler(url, **kwargs):
        if "rss" in url or url.startswith("https://36kr") or "sina" in url:
            raise RuntimeError("rss down")
        return FakeResp(text=sogou)

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.news("科技")
    assert result["ok"] is True and result["source"] == "搜狗新闻"
    assert result["count"] == 2


SEARCH_HTML = """<html><body>
<div class="vrwrap"><h3><a href="https://a.example/1">结果一</a></h3>
<div class="str-info">片段一</div></div>
<div class="vrwrap"><h3><a href="https://b.example/2">结果二</a></h3>
<div class="str-info">片段二</div></div>
<div class="vrwrap"><h3><a href="https://c.example/3">结果三</a></h3>
<div class="str-info">片段三</div></div>
<div class="vrwrap"><h3><a href="https://d.example/4">结果四</a></h3>
<div class="str-info">片段四</div></div>
</body></html>"""


def test_search_returns_results_with_domains(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=SEARCH_HTML)))
    result = wl.search("deepseek")
    assert result["ok"] is True and result["count"] == 4
    assert result["items"][0]["title"] == "结果一"
    assert result["items"][0]["domain"] == "a.example"


def test_search_limit_and_same_domain_dedupe(monkeypatch):
    many = """<html><body>
<div class="vrwrap"><h3><a href="https://same.example/1">同域一</a></h3></div>
<div class="vrwrap"><h3><a href="https://same.example/2">同域二</a></h3></div>
<div class="vrwrap"><h3><a href="https://same.example/3">同域三</a></h3></div>
<div class="vrwrap"><h3><a href="https://other.example/4">异域一</a></h3></div>
<div class="vrwrap"><h3><a href="https://third.example/5">异域二</a></h3></div>
</body></html>"""
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=many)))
    result = wl.search("test", limit=3)
    assert result["ok"] is True and result["count"] == 3
    domains = [item["domain"] for item in result["items"]]
    assert domains.count("same.example") == 2   # 同域名最多 2 条
    limited = wl.search("test", limit=2)
    assert len(limited["items"]) == 2


def test_search_falls_back_to_bing(monkeypatch):
    bing = ('<html><body><li class="b_algo"><h2><a href="https://b.example/1">'
            '必应结果</a></h2><p>摘要</p></li></body></html>')

    def handler(url, **kwargs):
        if "sogou.com" in url:
            raise RuntimeError("sogou down")
        return FakeResp(text=bing)

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.search("test")
    assert result["ok"] is True and result["source"].startswith("必应")
    assert result["items"][0]["title"] == "必应结果"


def test_search_falls_back_to_cn_bing(monkeypatch):
    def handler(url, **kwargs):
        if "sogou.com" in url or "www.bing.com" in url:
            return FakeResp(text="<html><body>no result</body></html>")
        return FakeResp(text='<html><body><li class="b_algo">'
                             '<h2><a href="https://cn.example/1">中文必应结果</a></h2>'
                             '<p>摘要</p></li></body></html>')

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.search("test")
    assert result["ok"] is True and "必应中文" in result["source"]
    assert result["items"][0]["title"] == "中文必应结果"


# --------------------------------------------------------------------------- #
# 6. fetch_url：SSRF / 截断 / 失败
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("host", [
    "127.0.0.1", "10.1.2.3", "192.168.1.1", "169.254.169.254",
    "localhost", "::1", "0.0.0.0",
])
def test_ssrf_blocks_private_hosts(host):
    assert wl.is_forbidden_host(host) is True


def test_ssrf_blocks_domain_resolving_to_private(monkeypatch):
    monkeypatch.setattr(wl, "_resolve_ips", lambda host: ["127.0.0.1"])
    assert wl.is_forbidden_host("evil.example") is True


def test_public_domain_allowed(monkeypatch):
    monkeypatch.setattr(wl, "_resolve_ips", lambda host: ["93.184.216.34"])
    assert wl.is_forbidden_host("example.com") is False


def test_fetch_url_ssrf_rejected(monkeypatch):
    called = {"value": False}

    def handler(url, **kw):
        called["value"] = True
        return FakeResp(text="ok")

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.fetch_url("http://127.0.0.1:801/status")
    assert result["ok"] is False and "SSRF" in result["error"]
    assert called["value"] is False


def test_fetch_url_extracts_text_and_truncates_size(monkeypatch):
    html = "<html><head><title>标题</title></head><body><p>正文内容</p>" \
           "<script>bad()</script></body></html>"
    big = html.encode("utf-8") + b"x" * (wl.MAX_URL_BYTES + 1000)
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(content=big, text=html)))
    result = wl.fetch_url("https://example.com/page")
    assert result["ok"] is True
    assert result["title"] == "标题"
    assert result["size"] == wl.MAX_URL_BYTES and result["truncated"] is True
    assert len(result["text"]) <= wl.MAX_URL_TEXT


def test_fetch_url_rejects_non_http_and_failures(monkeypatch):
    assert wl.fetch_url("file:///etc/passwd")["ok"] is False

    def handler(url, **kw):
        raise TimeoutError("timed out")

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.fetch_url("https://example.com")
    assert result["ok"] is False and "TimeoutError" in result["error"]


# --------------------------------------------------------------------------- #
# 7. lookup 统一 / 缓存 / 开关
# --------------------------------------------------------------------------- #
def test_lookup_weather_summary_and_cache(monkeypatch):
    fake = FakeGet(lambda url, **kw: FakeResp(text=_wttr_payload()))
    monkeypatch.setattr(wl, "_get", fake)
    first = wl.lookup("北京天气", now=1000.0)
    assert first["ok"] is True and first["kind"] == "weather"
    assert "北京" in first["summary"] or "Beijing" in first["summary"]
    assert first["cached"] is False
    second = wl.lookup("北京天气", now=1000.0 + 120)
    assert second["cached"] is True
    assert len(fake.calls) == 1
    third = wl.lookup("北京天气", now=1000.0 + 601)
    assert third["cached"] is False and len(fake.calls) == 2


def test_lookup_failure_not_cached(monkeypatch):
    calls = {"n": 0}

    def handler(url, **kw):
        calls["n"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    first = wl.lookup("北京天气", now=1000.0)
    second = wl.lookup("北京天气", now=1001.0)
    assert first["ok"] is False and second["ok"] is False
    assert calls["n"] == 4  # 每次 lookup 会试 wttr + 腾讯回退


def test_lookup_wechat_article_summary(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=ARTICLE_HTML)))
    result = wl.lookup("https://mp.weixin.qq.com/s/abc", now=1000.0)
    assert result["ok"] is True and result["kind"] == "wechat_article"
    assert "AI 编程这一年" in result["summary"]
    assert "36氪" in result["summary"]


def test_lookup_no_intent():
    result = wl.lookup("今天好累")
    assert result["ok"] is False and result["error"] == "no_intent"


# --------------------------------------------------------------------------- #
# 8. chat 注入
# --------------------------------------------------------------------------- #
class _ChatClient:
    def __init__(self, reply="刚查了下，今天北京挺凉的，出门穿件外套吧。"):
        self.reply = reply
        self.calls: list[dict] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({"messages": messages, "kwargs": kwargs, "text": text})
        return {"ok": True, "text": self.reply, "reasoning": "",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "finish_reason": "stop"}


def _agent(tmp_path, name="web_chat", reply=None):
    from agi.selflearn.chat import SelfLearnAgent
    path = tmp_path / f"{name}.sqlite3"
    return SelfLearnAgent(path, client=_ChatClient(reply or "刚查了下，今天北京挺凉的。"),
                          rng=random.Random(7)), path


def _generation_prompt(client) -> str:
    for call in client.calls:
        content = call["messages"][1]["content"]
        if isinstance(content, str) and "用户刚刚说" in content and "直接回复" in content:
            return content
    raise AssertionError("没捕获到生成 prompt")


def test_chat_injects_web_summary_when_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "detect_intent", lambda text: {
        "kind": "weather", "query": "北京", "params": {"city": "北京"}})
    monkeypatch.setattr(wl, "lookup", lambda text, now=None: {
        "ok": True, "kind": "weather", "source": "wttr.in",
        "summary": "北京现在晴，气温18℃，出门穿件外套。", "items": [],
        "error": "", "title": ""})
    agent, path = _agent(tmp_path, "web_hit")
    result = agent.send("北京天气怎么样")
    assert result["ok"] is True and result["web_lookup"]["kind"] == "weather"
    assert result["web_lookup"]["ok"] is True
    prompt = _generation_prompt(agent.client)
    assert "实时资料" in prompt
    assert "北京现在晴" in prompt
    assert "只能依据它回答" in prompt
    # trace 可见：generation_json.web_lookup
    from agi.selflearn import trace
    item = trace.get_turn(path, trace.list_turns(path)[0]["id"])
    assert item["generation"]["web_lookup"]["kind"] == "weather"
    assert item["generation"]["web_lookup"]["ok"] is True


def test_chat_injects_wechat_article_instruction(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "detect_intent", lambda text: {
        "kind": "wechat_article", "query": "https://mp.weixin.qq.com/s/x",
        "params": {"url": "https://mp.weixin.qq.com/s/x"}})
    monkeypatch.setattr(wl, "lookup", lambda text, now=None: {
        "ok": True, "kind": "wechat_article", "source": "mp.weixin.qq.com",
        "summary": "刚看完公众号「36氪」的文章《AI 编程这一年》。正文摘要：AI 编程变化很大。",
        "items": [], "error": "", "title": "AI 编程这一年"})
    agent, path = _agent(tmp_path, "web_article",
                         reply="这篇我看完了，作者对 AI 编程的判断挺实在的。")
    result = agent.send("https://mp.weixin.qq.com/s/x")
    prompt = _generation_prompt(agent.client)
    assert "这篇我看完了" in prompt
    assert "公众号" in prompt and "AI 编程这一年" in prompt
    assert result["web_lookup"]["title"] == "AI 编程这一年"


def test_chat_failure_does_not_inject_and_prompt_unchanged(tmp_path, monkeypatch):
    def fail_lookup(text, now=None):
        return {"ok": False, "kind": "weather", "summary": "", "items": [],
                "source": "", "error": "network down", "title": ""}

    monkeypatch.setattr(wl, "detect_intent", lambda text: None)
    base_agent, _ = _agent(tmp_path, "web_base")
    base_agent.send("今天有点累")
    base_prompt = _generation_prompt(base_agent.client)

    monkeypatch.setattr(wl, "detect_intent", lambda text: {
        "kind": "weather", "query": "北京", "params": {"city": "北京"}})
    monkeypatch.setattr(wl, "lookup", fail_lookup)
    agent, path = _agent(tmp_path, "web_fail")
    result = agent.send("今天有点累")
    assert result["web_lookup"]["ok"] is False
    assert _generation_prompt(agent.client) == base_prompt


def test_chat_web_lookup_disabled_does_not_query(tmp_path, monkeypatch):
    called = {"value": False}

    def spy(text):
        called["value"] = True
        return None

    monkeypatch.setattr(wl, "detect_intent", spy)
    monkeypatch.setenv("WEB_LOOKUP_ENABLED", "0")
    agent, _ = _agent(tmp_path, "web_off")
    result = agent.send("北京天气怎么样")
    assert called["value"] is False
    assert result["web_lookup"] is None


def test_news_topic_leftover_particles_ignored():
    news = wl.detect_intent("最近有什么新闻")
    assert news["kind"] == "news" and news["params"]["topic"] == ""
    news2 = wl.detect_intent("今天有什么热点新闻")
    assert news2["params"]["topic"] == ""


def test_news_stale_rss_falls_back_to_sogou(monkeypatch):
    stale = """<rss><channel>
<item><title>2018 老新闻一</title><link>https://a/1</link>
<description>旧</description><pubDate>Sun, 23 Sep 2018 10:57:02 GMT</pubDate></item>
<item><title>2018 老新闻二</title><link>https://a/2</link>
<description>旧</description><pubDate>Sun, 23 Sep 2018 10:50:00 GMT</pubDate></item>
<item><title>2018 老新闻三</title><link>https://a/3</link>
<description>旧</description><pubDate>Sun, 23 Sep 2018 10:40:00 GMT</pubDate></item>
</channel></rss>"""
    sogou = ('<html><body><h3><a href="/n/1">新鲜一</a></h3>'
             '<h3><a href="/n/2">新鲜二</a></h3></body></html>')

    def handler(url, **kwargs):
        if "sogou" in url:
            return FakeResp(text=sogou)
        return FakeResp(content=stale.encode("utf-8"), text=stale)

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.news("")
    assert result["ok"] is True
    assert result["source"] == "搜狗新闻"
    assert result["fresh"] is True
    assert result["items"][0]["title"] == "新鲜一"


def test_lookup_weather_summary_is_chinese(monkeypatch):
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=_wttr_payload())))
    result = wl.lookup("北京天气", now=2000.0)
    assert result["ok"] is True
    assert "北京现在多云" in result["summary"]
    assert "Partial" not in result["summary"]


# --------------------------------------------------------------------------- #
# 9. blocked 出路 / 转发标题检索 / 外部正文钩子（P24 补充）
# --------------------------------------------------------------------------- #
def test_wechat_article_blocked_has_actionable_hint(monkeypatch):
    blocked = "<html><body><h2>环境异常</h2><p>请在微信客户端打开</p></body></html>"
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=blocked)))
    result = wl.wechat_article("https://mp.weixin.qq.com/s/blocked")
    assert result["ok"] is False and result["error"] == "blocked"
    assert "反爬" in result["hint"] and "把正文复制发我" in result["hint"]


def test_lookup_blocked_summary_has_hint(monkeypatch):
    blocked = "<html><body>环境异常，请用微信扫描二维码</body></html>"
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=blocked)))
    result = wl.lookup("https://mp.weixin.qq.com/s/x", now=3000.0)
    assert result["ok"] is False and result["kind"] == "wechat_article"
    assert "把正文复制发我" in result["summary"]
    assert "别假装" in result["summary"]


def test_wechat_article_uses_fallback_text_without_network(monkeypatch):
    def boom(url, **kwargs):
        raise AssertionError("有 fallback_text 时不应该联网")

    monkeypatch.setattr(wl, "_get", boom)
    result = wl.wechat_article(
        "https://mp.weixin.qq.com/s/x",
        fallback_text="这是用户从微信里复制出来的正文第一段。" + chr(10) + "第二段继续。",
        fallback_title="用户转发标题", fallback_account="某公众号")
    assert result["ok"] is True and result["source"].startswith("external")
    assert result["title"] == "用户转发标题"
    assert result["account"] == "某公众号"
    assert result["text"].startswith("这是用户从微信里复制出来")


def test_detect_intent_forwarded_title_only():
    intent = wl.detect_intent("OpenAI 官宣新模型：深度解读")
    assert intent is not None and intent["kind"] == "wechat_title"
    assert "官宣" in intent["params"]["query"]
    # 普通聊天不应误触发
    assert wl.detect_intent("今天好累啊") is None
    assert wl.detect_intent("晚上吃什么") is None
    assert wl.detect_intent("这个链接你看看") is None
    assert wl.detect_intent("会议主题：季度复盘") is None
    # 转发卡片标题 + 「链接」后缀（没有 URL）能兜底搜摘要
    linked = wl.detect_intent("AI 编程这一年 链接")
    assert linked is not None and linked["kind"] == "wechat_search"
    assert "AI编程这一年" in linked["params"]["query"]
    # 搜索意图去掉「是什么/是」等尾巴，避免搜索引擎只命中第一个词
    searched = wl.detect_intent("帮我查一下光合作用是什么")
    assert searched["kind"] == "search"
    assert searched["params"]["query"] == "光合作用"


def test_lookup_title_only_returns_search_summary(monkeypatch):
    monkeypatch.setattr(wl, "wechat_search", lambda query: {
        "ok": True, "items": [
            {"title": "标题一", "account": "公众号A", "summary": "摘要一",
             "url": "https://weixin.sogou.com/link?url=1"},
            {"title": "标题二", "account": "公众号B", "summary": "摘要二",
             "url": "https://weixin.sogou.com/link?url=2"},
        ], "count": 2, "source": "搜狗微信", "query": query, "error": ""})
    result = wl.lookup("标题一：深度解读", now=4000.0)
    assert result["ok"] is True and result["kind"] == "wechat_title"
    assert "只看到检索摘要" in result["summary"]
    assert "标题一" in result["summary"]


def test_chat_blocked_article_injects_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "detect_intent", lambda text: {
        "kind": "wechat_article", "query": "https://mp.weixin.qq.com/s/x",
        "params": {"url": "https://mp.weixin.qq.com/s/x"}})
    monkeypatch.setattr(wl, "lookup", lambda text, now=None: {
        "ok": False, "kind": "wechat_article", "source": "mp.weixin.qq.com",
        "summary": "这篇被微信拦了（反爬验证），你把正文复制发我就行（只看到反爬验证页，没读到正文；别假装读过）",
        "items": [], "error": "blocked", "title": "", "hint": "这篇被微信拦了（反爬验证），你把正文复制发我就行"})
    agent, path = _agent(tmp_path, "web_blocked",
                         reply="这篇被微信拦了，你把正文复制发我就行。")
    result = agent.send("https://mp.weixin.qq.com/s/x")
    assert result["web_lookup"]["ok"] is False
    assert result["web_lookup"]["error"] == "blocked"
    prompt = _generation_prompt(agent.client)
    assert "把正文复制发我" in prompt
    assert "别假装读过" in prompt


def test_chat_title_only_injects_summary_note(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "detect_intent", lambda text: {
        "kind": "wechat_title", "query": "标题一", "params": {"query": "标题一"}})
    monkeypatch.setattr(wl, "lookup", lambda text, now=None: {
        "ok": True, "kind": "wechat_title", "source": "搜狗微信",
        "summary": "只看到检索摘要（没读到全文）：" + chr(10) + "- 标题一（公众号A）：摘要一",
        "items": [], "error": "", "title": "", "hint": ""})
    agent, path = _agent(tmp_path, "web_title",
                         reply="我只看个大概，这篇讲的是……")
    result = agent.send("标题一：深度解读")
    assert result["web_lookup"]["kind"] == "wechat_title"
    prompt = _generation_prompt(agent.client)
    assert "只看到检索摘要" in prompt
    assert "没读到全文" in prompt


# --------------------------------------------------------------------------- #
# 10. 普通网页搜索主路径：正文提取质量 / chat 搜索注入（P24 收尾）
# --------------------------------------------------------------------------- #
def test_fetch_url_extracts_article_and_drops_noise(monkeypatch):
    html = """<html><head><title>测试文章标题</title></head><body>
<nav><a href="/">首页</a><a href="/hot">热榜</a></nav>
<header class="site-header">站点头部</header>
<article>
  <h1>文章大标题</h1>
  <p>第一段：这是文章正文的第一段，包含足够多的信息量。</p>
  <p>第二段：继续讲正文内容，测试段落保留与噪声清理。</p>
  <p>版权所有 © 2026 某公司 保留所有权利</p>
</article>
<aside class="sidebar">侧边栏推荐广告</aside>
<div class="ad-banner">广告位</div>
<footer>版权所有 联系邮箱 x@example.com</footer>
</body></html>"""
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=html)))
    result = wl.fetch_url("https://example.com/news/1")
    assert result["ok"] is True
    assert result["title"] == "测试文章标题"
    text = result["text"]
    assert "第一段：这是文章正文" in text
    assert "第二段：继续讲正文内容" in text
    assert "站点头部" not in text
    assert "侧边栏推荐广告" not in text
    assert "广告位" not in text
    assert "版权所有" not in text
    assert "首页" not in text


def test_fetch_url_picks_paragraph_heavy_container(monkeypatch):
    html = """<html><body>
<div class="content">
  <p>短导航</p>
</div>
<div class="content">
  <p>正文容器里第一段内容，长度足够。</p>
  <p>正文容器里第二段内容，继续说明。</p>
  <p>正文容器里第三段内容，信息量不少。</p>
</div>
</body></html>"""
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=html)))
    result = wl.fetch_url("https://example.com/a")
    assert result["ok"] is True
    assert result["text"].count("正文容器") >= 3


def test_chat_search_injection_has_summary_and_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(wl, "detect_intent", lambda text: {
        "kind": "search", "query": "deepseek", "params": {"query": "deepseek"}})
    monkeypatch.setattr(wl, "lookup", lambda text, now=None: {
        "ok": True, "kind": "search", "source": "搜狗",
        "summary": "搜了下，找到这些：" + chr(10) + "- 结果一：片段一"
                   + chr(10) + "来源：example.com",
        "items": [{"title": "结果一", "summary": "片段一",
                   "domain": "example.com"}],
        "error": "", "title": ""})
    agent, path = _agent(tmp_path, "web_search",
                         reply="搜了下，DeepSeek 最近在扩团队。")
    result = agent.send("帮我查一下 DeepSeek 最近的新闻")
    assert result["web_lookup"]["kind"] == "search"
    prompt = _generation_prompt(agent.client)
    assert "搜了下" in prompt and "来源：example.com" in prompt
    assert "不要贴长链接列表" in prompt


def test_search_cleans_question_prefix(monkeypatch):
    seen = {}

    def handler(url, **kwargs):
        seen["query"] = (kwargs.get("params") or {}).get("query")
        return FakeResp(text=SEARCH_HTML)

    monkeypatch.setattr(wl, "_get", FakeGet(handler))
    result = wl.search("什么是光合作用", limit=2)
    assert result["ok"] is True
    assert seen["query"] == "光合作用"


def test_fetch_url_drops_ui_noise_and_trims_before_title(monkeypatch):
    html = """<html><head><title>文章标题</title></head><body>
<div>浅色 深色 主题色 夜间</div>
<div>登录 注册 搜索</div>
<h1>文章标题</h1>
<p>这是正文第一段，内容足够长。</p>
<p>这是正文第二段，继续说明。</p>
<div>分享到 收藏 点赞</div>
</body></html>"""
    monkeypatch.setattr(wl, "_get", FakeGet(
        lambda url, **kw: FakeResp(text=html)))
    result = wl.fetch_url("https://example.com/a")
    assert result["ok"] is True
    text = result["text"]
    assert "浅色" not in text and "主题色" not in text
    assert "登录" not in text and "分享到" not in text
    assert text.split(chr(10))[0].startswith("文章标题")
