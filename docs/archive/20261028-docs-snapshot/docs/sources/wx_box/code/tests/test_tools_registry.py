# -*- coding: utf-8 -*-
"""工具注册表：schema、白名单、失败降级与旧接口兼容。"""
from __future__ import annotations

from agi.selflearn import db, tools

SPEC_NAMES = {"web_search", "web_fetch", "memory_query", "calc", "python_verify"}


def test_specs_are_valid_openai_schema():
    specs = tools.specs()
    assert len(specs) == 5
    names = set()
    for spec in specs:
        assert spec["type"] == "function"
        function = spec["function"]
        assert isinstance(function["name"], str) and function["name"]
        assert isinstance(function["description"], str) and function["description"]
        names.add(function["name"])
        params = function["parameters"]
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict) and params["properties"]
        for prop, schema in params["properties"].items():
            assert isinstance(prop, str) and prop
            assert schema["type"] in {"string", "integer", "number", "boolean",
                                      "array", "object"}
        assert isinstance(params["required"], list)
        for required in params["required"]:
            assert required in params["properties"]
    assert names == SPEC_NAMES
    # 返回的是副本，调用方改动不会污染模块状态
    specs[0]["function"]["name"] = "mutated"
    assert "mutated" not in {s["function"]["name"] for s in tools.specs()}
    # 旧 time 工具仍在注册表里，只是不暴露给模型
    assert "time" in tools.available()
    assert set(SPEC_NAMES) <= set(tools.available())


def test_run_rejects_unknown_tool():
    result = tools.run("rm_rf", path="/")
    assert result["ok"] is False
    assert result["error"] == "工具不在白名单"
    assert "calc" in result["available"]


def test_old_tools_still_work():
    assert tools.run("calc", expression="1+2*3")["result"] == 7
    assert isinstance(tools.run("time")["result"], float)
    verified = tools.run("python_verify", code="print(1+1)",
                         test_code="assert 1+1==2")
    assert verified["ok"] is True and verified["result"]["passed"] is True
    # 参数类型校验：旧调用方传错类型也 fail-closed
    assert tools.run("calc", expression=123)["ok"] is False
    assert tools.run("calc", expression="__import__('os')")["ok"] is False


def test_web_search_module_missing_and_exception(monkeypatch):
    def missing():
        raise ImportError("No module named 'wx_core.web_search'")

    monkeypatch.setattr(tools, "_load_search", missing)
    result = tools.run("web_search", query="今天新闻")
    assert result["ok"] is False
    assert "web_search 不可用" in result["error"]

    def boom(query, count=5):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tools, "_load_search", lambda: boom)
    result = tools.run("web_search", query="今天新闻")
    assert result["ok"] is False
    assert "provider down" in result["error"]

    # 参数校验先于网络调用
    assert tools.run("web_search", query="x", count="5")["ok"] is False
    assert tools.run("web_search", query="")["ok"] is False


def test_web_fetch_truncates_and_fails_closed(monkeypatch):
    def fake_fetch(url):
        return {"ok": True, "url": url, "status": 200, "title": "标题",
                "text": "长" * 5000, "chars": 5000, "error": ""}

    monkeypatch.setattr(tools, "_load_fetch", lambda: fake_fetch)
    result = tools.run("web_fetch", url="https://example.com/x")
    assert result["ok"] is True
    assert len(result["result"]["text"]) == 4000
    assert result["result"]["truncated"] is True
    assert result["result"]["url"] == "https://example.com/x"

    def missing():
        raise ImportError("safe_fetch 缺失")

    monkeypatch.setattr(tools, "_load_fetch", missing)
    result = tools.run("web_fetch", url="https://example.com/x")
    assert result["ok"] is False
    assert "web_fetch 不可用" in result["error"]

    def boom(url):
        raise TimeoutError("timed out")

    monkeypatch.setattr(tools, "_load_fetch", lambda: boom)
    result = tools.run("web_fetch", url="https://example.com/x")
    assert result["ok"] is False
    assert "timed out" in result["error"]

    assert tools.run("web_fetch", url="file:///etc/passwd")["ok"] is False


def test_result_is_capped(monkeypatch):
    def huge(query, count=5):
        return {"ok": True, "query": query, "provider": "fake",
                "results": [{"title": "t", "url": "https://example.com",
                             "snippet": "x" * 12000}]}

    monkeypatch.setattr(tools, "_load_search", lambda: huge)
    result = tools.run("web_search", query="大结果")
    assert result["ok"] is True
    assert result["result"]["_truncated"] is True
    assert len(result["result"]["preview"]) == tools.MAX_RESULT_CHARS
    assert result["result"]["chars"] > tools.MAX_RESULT_CHARS


def test_memory_query_requires_db_path(tmp_path):
    missing = tools.run("memory_query", query="猫")
    assert missing["ok"] is False
    assert "db_path" in missing["error"]

    path = tmp_path / "selflearn.sqlite3"
    db.connect(path).close()
    result = tools.run("memory_query", db_path=path, query="猫", k=3)
    assert result["ok"] is True
    assert result["result"]["query"] == "猫"
    assert result["result"]["results"] == []

    assert tools.run("memory_query", db_path=tmp_path / "nope.sqlite3",
                     query="猫")["ok"] is False
