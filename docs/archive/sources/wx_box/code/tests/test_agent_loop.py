# -*- coding: utf-8 -*-
"""多步工具循环（agent）与 chat_with_tools 解析测试。"""
from __future__ import annotations

import copy
import json

from agi.selflearn import agent, tools
from agi.selflearn.llm import Client

SEARCH_URL = "https://example.com/a"


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {"id": call_id, "name": name, "arguments": dict(arguments)}


def _final(text: str) -> dict:
    return {"ok": True, "text": text, "tool_calls": [], "usage": {},
            "finish_reason": "stop", "error": ""}


class ScriptedClient:
    """按脚本返回 chat_with_tools 结果，并记录每次请求。"""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def available(self) -> bool:
        return True

    def chat_with_tools(self, messages, tools, **kwargs):
        self.calls.append({"messages": copy.deepcopy(messages),
                           "tools": copy.deepcopy(tools),
                           "kwargs": dict(kwargs)})
        if not self.responses:
            return _final("")
        return self.responses.pop(0)


class FakeRegistry:
    """只暴露指定工具名的假注册表；run 行为可按工具定制。"""

    def __init__(self, names=("web_search",), behavior: dict | None = None) -> None:
        self.names = list(names)
        self.behavior = dict(behavior or {})
        self.calls: list[dict] = []

    def specs(self) -> list[dict]:
        return [{"type": "function",
                 "function": {"name": name, "description": name,
                              "parameters": {"type": "object",
                                             "properties": {},
                                             "required": []}}}
                for name in self.names]

    def run(self, name, *, db_path=None, **kwargs):
        self.calls.append({"name": name, "db_path": db_path, "kwargs": dict(kwargs)})
        custom = self.behavior.get(name)
        if custom is not None:
            return custom(name=name, db_path=db_path, **kwargs)
        if name == "web_search":
            return {"ok": True, "tool": name,
                    "result": {"ok": True, "query": kwargs.get("query"),
                               "provider": "fake",
                               "results": [{"title": "示例页面", "url": SEARCH_URL,
                                            "snippet": "摘要"}]}}
        return {"ok": False, "tool": name, "error": "工具不在白名单"}


# ---------------------------------------------------------------------------
# 多步循环
# ---------------------------------------------------------------------------

def test_agent_search_then_final_answer():
    client = ScriptedClient([
        {"ok": True, "text": "", "usage": {},
         "tool_calls": [_tool_call("call_1", "web_search", {"query": "今天A股"})],
         "finish_reason": "tool_calls", "error": ""},
        _final("今天A股上涨。\n来源：https://example.com/a"),
    ])
    registry = FakeRegistry()
    events: list[dict] = []

    result = agent.run_agent(client, "今天A股怎么样", tools=registry,
                             on_event=events.append)

    assert result["ok"] is True
    assert result["tool_calls"] == 1
    assert result["reply"] == "今天A股上涨。\n来源：https://example.com/a"
    assert {"title": "示例页面", "url": SEARCH_URL} in result["sources"]
    assert result["steps"] == [{"tool": "web_search", "ok": True,
                               "elapsed": result["steps"][0]["elapsed"]}]
    assert result["budget_exceeded"] is False
    assert result["error"] == ""
    assert client.calls[0]["tools"] == registry.specs()
    # 第二次模型请求带上了 assistant(tool_calls) 与 tool 结果
    messages = client.calls[1]["messages"]
    assert messages[1] == {"role": "user", "content": "今天A股怎么样"}
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["tool_call_id"] == "call_1"
    assert SEARCH_URL in tool_messages[0]["content"]
    assert [event["type"] for event in events] == ["tool_call", "tool_result"]
    assert events[1]["name"] == "web_search" and events[1]["ok"] is True


def test_unknown_tool_is_rejected_but_loop_continues():
    client = ScriptedClient([
        {"ok": True, "text": "", "usage": {},
         "tool_calls": [_tool_call("call_x", "rm_rf", {"path": "/"})],
         "finish_reason": "tool_calls", "error": ""},
        _final("我换一种方式回答。"),
    ])
    registry = FakeRegistry()
    events: list[dict] = []

    result = agent.run_agent(client, "清理磁盘", tools=registry,
                             on_event=events.append)

    assert result["ok"] is True
    assert result["reply"] == "我换一种方式回答。"
    assert registry.calls == []  # 未知工具没有被执行
    assert any(event["type"] == "tool_result" and event["ok"] is False
               for event in events)
    tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and "白名单" in tool_messages[0]["content"]


def test_budget_exceeded_stops_without_raising():
    endless = {"ok": True, "text": "再查一下", "usage": {},
               "tool_calls": [_tool_call("call_loop", "web_search", {"query": "x"})],
               "finish_reason": "tool_calls", "error": ""}

    # 步数预算
    client = ScriptedClient([copy.deepcopy(endless) for _ in range(10)])
    registry = FakeRegistry()
    result = agent.run_agent(client, "查资料", tools=registry,
                             max_steps=2, max_tool_calls=6)
    assert result["budget_exceeded"] is True
    assert result["reply"] == "再查一下"
    # 两次模型请求都会发生；但同一个工具+参数重复调用会被去重跳过，
    # 所以注册表只真正执行 1 次（防模型反复查同一条把自己磨死）。
    assert len(client.calls) == 2 and len(registry.calls) == 1

    # 工具调用数预算：一次响应里两个调用，只允许 1 个
    client2 = ScriptedClient([{
        "ok": True, "text": "", "usage": {},
        "tool_calls": [_tool_call("call_a", "web_search", {"query": "a"}),
                       _tool_call("call_b", "web_search", {"query": "b"})],
        "finish_reason": "tool_calls", "error": ""}])
    registry2 = FakeRegistry()
    result2 = agent.run_agent(client2, "查资料", tools=registry2,
                              max_steps=5, max_tool_calls=1)
    assert result2["budget_exceeded"] is True
    assert len(registry2.calls) == 1
    assert result2["tool_calls"] == 1


def test_agent_deadline_is_a_budget_not_an_exception():
    client = ScriptedClient([_final("不该被调用")])
    result = agent.run_agent(client, "现在时间", tools=FakeRegistry(),
                             deadline_s=0.0)
    assert result["budget_exceeded"] is True
    assert result["ok"] is False
    assert client.calls == []


def test_tool_exception_is_captured_and_loop_continues():
    def boom(**kwargs):
        raise RuntimeError("boom")

    client = ScriptedClient([
        {"ok": True, "text": "", "usage": {},
         "tool_calls": [_tool_call("call_boom", "web_search", {"query": "x"})],
         "finish_reason": "tool_calls", "error": ""},
        _final("搜索失败了，先按已有信息回答。"),
    ])
    registry = FakeRegistry(behavior={"web_search": boom})
    events: list[dict] = []

    result = agent.run_agent(client, "查一下", tools=registry,
                             on_event=events.append)

    assert result["ok"] is True
    assert result["reply"] == "搜索失败了，先按已有信息回答。"
    assert result["steps"][0]["ok"] is False
    tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and "boom" in tool_messages[0]["content"]
    assert any(event["type"] == "tool_result" and event["ok"] is False
               for event in events)


def test_agent_uses_real_default_registry_for_calc():
    client = ScriptedClient([
        {"ok": True, "text": "", "usage": {},
         "tool_calls": [_tool_call("call_calc", "calc", {"expression": "1+2"})],
         "finish_reason": "tool_calls", "error": ""},
        _final("1+2 等于 3。"),
    ])

    result = agent.run_agent(client, "1+2 等于几")

    assert result["ok"] is True
    assert result["tool_calls"] == 1
    assert result["steps"][0]["tool"] == "calc" and result["steps"][0]["ok"] is True
    tool_messages = [m for m in client.calls[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages and "3" in tool_messages[0]["content"]
    # 默认注册表发给模型的就是 tools.specs()
    assert client.calls[0]["tools"] == tools.specs()


# ---------------------------------------------------------------------------
# chat_with_tools（llm.py 新增方法）
# ---------------------------------------------------------------------------

def _client(transport) -> Client:
    return Client(config={"model": "fake", "api_key": "", "base_url": "http://example"},
                  transport=transport)


def test_chat_with_tools_parses_openai_tool_calls():
    seen: dict = {}

    def transport(messages, body):
        seen["messages"] = messages
        seen["body"] = body
        return {"choices": [{
            "message": {"content": "", "tool_calls": [{
                "id": "call_9", "type": "function",
                "function": {"name": "web_fetch",
                             "arguments": '{"url": "https://example.com/x"}'}}]},
            "finish_reason": "tool_calls"}],
            "usage": {"total_tokens": 12}}

    result = _client(transport).chat_with_tools(
        [{"role": "user", "content": "hi"}], tools.specs())

    assert result["ok"] is True
    assert result["text"] == ""
    assert result["tool_calls"] == [{"id": "call_9", "name": "web_fetch",
                                     "arguments": {"url": "https://example.com/x"}}]
    assert result["finish_reason"] == "tool_calls"
    assert result["usage"] == {"total_tokens": 12}
    assert result["error"] == ""
    # tools 被放进发给 transport 的 body
    assert seen["body"]["tools"] == tools.specs()
    assert seen["body"]["temperature"] == 0.3


def test_chat_with_tools_plain_text_and_bad_arguments():
    def plain(messages, body):
        return {"choices": [{"message": {"content": "直接回答"},
                             "finish_reason": "stop"}]}

    result = _client(plain).chat_with_tools([], [])
    assert result["ok"] is True and result["text"] == "直接回答"
    assert result["tool_calls"] == []

    def broken(messages, body):
        return {"choices": [{"message": {"content": "", "tool_calls": [{
            "id": "call_bad", "type": "function",
            "function": {"name": "calc", "arguments": "{不是 JSON"}}]},
            "finish_reason": "tool_calls"}]}

    bad = _client(broken).chat_with_tools([], tools.specs())
    assert bad["ok"] is True
    assert bad["tool_calls"][0]["arguments"] == {}
    assert "JSON" in bad["error"]


def test_chat_with_tools_legacy_function_call():
    def transport(messages, body):
        return {"choices": [{"message": {
            "content": "",
            "function_call": {"name": "calc",
                              "arguments": '{"expression": "1+1"}'}},
            "finish_reason": "function_call"}]}

    result = _client(transport).chat_with_tools([], tools.specs())
    assert result["ok"] is True
    assert len(result["tool_calls"]) == 1
    call = result["tool_calls"][0]
    assert call["name"] == "calc" and call["arguments"] == {"expression": "1+1"}


def test_chat_with_tools_transport_error_is_ok_false():
    def transport(messages, body):
        raise RuntimeError("网络断了")

    result = _client(transport).chat_with_tools([], tools.specs())
    assert result["ok"] is False
    assert result["tool_calls"] == []
    assert "网络断了" in result["error"]
