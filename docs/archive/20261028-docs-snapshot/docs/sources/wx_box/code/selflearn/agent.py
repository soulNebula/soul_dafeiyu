# -*- coding: utf-8 -*-
"""多步工具循环：LLM 自主调用 web_search / web_fetch / memory_query 等工具。

纯函数：不写数据库、不发消息；落库与发送由调用方负责。
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from . import tools as _default_tools

SYSTEM_PROMPT = """你是一个能联网查资料的助手。

规则：
1. 需要事实、数据、新闻、时效性信息时，先调用工具（web_search 搜索、web_fetch 抓取网页、memory_query 查长期记忆），不要凭记忆猜。
2. **工具调用最多 2 次**（含搜索与抓取）；信息够了立刻停止并回答，不要反复搜索同一个问题、不要重复抓同一个链接。
3. 回答简短（1–3 句话）；末尾用「来源：URL」列出来源（1–2 条即够）。没有来源或查不到时，明确说“查不到”，不许编造事实、数字或链接。
4. 不要输出工具内部 ID（如 call_xxx）、工具原始 JSON、schema 或你的思考过程。
"""

MAX_TOOL_CONTENT = 2500
MAX_SOURCES = 8

#: 预算耗尽时的收尾提示：让模型基于已抓到的资料直接作答，避免空回复。
FINAL_PROMPT = ("停止检索。请只根据上面已拿到的资料，用 1–3 句话直接回答；"
                "末尾列出 1–2 条来源 URL；资料不足就直说“查不到”。")


def _emit(on_event: Callable[[dict], Any] | None, event: dict) -> None:
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:  # noqa: BLE001 - 回调故障不能打断主循环
        pass


def _specs(registry: Any) -> list[dict]:
    getter = getattr(registry, "specs", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            return []
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    return []


def _allowed_names(registry: Any, spec_list: list[dict]) -> set[str]:
    names: set[str] = set()
    for spec in spec_list:
        function = spec.get("function") if isinstance(spec, dict) else None
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
            if name:
                names.add(name)
    if names:
        return names
    # 兼容只有 available() 的旧注册表。
    getter = getattr(registry, "available", None)
    if callable(getter):
        try:
            return {str(name).strip() for name in getter()}
        except Exception:  # noqa: BLE001
            return set()
    return set()


def _assistant_message(text: str, calls: list[dict]) -> dict:
    payload = []
    for index, call in enumerate(calls):
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            raw_args = json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            raw_args = "{}"
        payload.append({
            "id": str(call.get("id") or f"call_0_{index}"),
            "type": "function",
            "function": {
                "name": str(call.get("name") or ""),
                "arguments": raw_args,
            },
        })
    return {"role": "assistant", "content": text or "", "tool_calls": payload}


def _execute(registry: Any, name: str, args: dict, db_path: Any) -> dict:
    runner = getattr(registry, "run", None)
    if not callable(runner):
        return {"ok": False, "tool": name, "error": "工具执行器不可用"}
    kwargs = {key: value for key, value in args.items() if key != "db_path"}
    try:
        payload = runner(name, db_path=db_path, **kwargs)
    except Exception as exc:  # noqa: BLE001 - 工具异常不能打断循环
        return {"ok": False, "tool": name,
                "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    if not isinstance(payload, dict):
        return {"ok": False, "tool": name, "error": "工具返回格式错误"}
    return payload


def _tool_content(payload: dict) -> str:
    try:
        content = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        content = str(payload)
    return content[:MAX_TOOL_CONTENT]


def _collect_sources(name: str, payload: dict, sources: dict[str, dict]) -> None:
    if not payload.get("ok"):
        return
    value = payload.get("result")
    if name == "web_search" and isinstance(value, dict):
        items = value.get("results")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    _add_source(item.get("url"), item.get("title"), sources)
    elif name == "web_fetch" and isinstance(value, dict):
        _add_source(value.get("url"), value.get("title"), sources)


def _collect_notes(name: str, payload: dict, notes: list[str]) -> None:
    """把工具结果里的**事实片段**留下来（比只看模型总结更可靠）。"""
    if not payload.get("ok") or len(notes) >= 6:
        return
    value = payload.get("result")
    if not isinstance(value, dict):
        return
    if name == "web_search":
        for item in (value.get("results") or [])[:2]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            if title or snippet:
                notes.append(f"{title}：{snippet[:160]}".strip("："))
    elif name == "web_fetch":
        title = str(value.get("title") or "").strip()
        text = " ".join(str(value.get("text") or "").split())
        if text:
            notes.append(f"{title}：{text[:400]}".strip("："))


def _add_source(url: Any, title: Any, sources: dict[str, dict]) -> None:
    text = str(url or "").strip()
    if not text:
        return
    if text not in sources:
        sources[text] = {"title": str(title or "").strip(), "url": text}
    elif not sources[text]["title"] and title:
        sources[text]["title"] = str(title).strip()


def _result(*, ok: bool, reply: str, sources: list[dict], steps: list[dict],
            tool_calls: int, error: str, budget_exceeded: bool,
            notes: list[str] | None = None) -> dict:
    return {"ok": bool(ok), "reply": reply, "sources": sources, "steps": steps,
            "tool_calls": int(tool_calls), "error": error,
            "budget_exceeded": bool(budget_exceeded), "notes": list(notes or [])}


def run_agent(client, text: str, *, db_path=None, tools=None, max_steps: int = 5,
              max_tool_calls: int = 6, deadline_s: float = 60.0,
              on_event: Callable[[dict], Any] | None = None) -> dict:
    """多步工具循环。

    返回 ``{"ok","reply","sources","steps","tool_calls","error","budget_exceeded"}``。
    预算（步数/工具调用数/墙钟）耗尽时停止并返回已有文本，不抛异常。
    """
    registry = tools if tools is not None else _default_tools
    try:
        max_steps = max(0, int(max_steps))
    except (TypeError, ValueError):
        max_steps = 5
    try:
        max_tool_calls = max(0, int(max_tool_calls))
    except (TypeError, ValueError):
        max_tool_calls = 6
    try:
        deadline_s = max(0.0, float(deadline_s))
    except (TypeError, ValueError):
        deadline_s = 60.0

    user_text = str(text or "").strip()
    if not user_text:
        return _result(ok=False, reply="", sources=[], steps=[], tool_calls=0,
                       error="输入为空", budget_exceeded=False)
    if not callable(getattr(client, "chat_with_tools", None)):
        return _result(ok=False, reply="", sources=[], steps=[], tool_calls=0,
                       error="client 不支持 chat_with_tools", budget_exceeded=False)

    started = time.monotonic()
    deadline = started + deadline_s
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]
    spec_list = _specs(registry)
    allowed = _allowed_names(registry, spec_list)
    steps: list[dict] = []
    sources: dict[str, dict] = {}
    notes: list[str] = []
    seen_calls: set[tuple[str, str]] = set()
    reply = ""
    error = ""
    budget_exceeded = False
    calls_used = 0
    step_no = 0

    while step_no < max_steps:
        step_no += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            budget_exceeded = True
            break
        try:
            response = client.chat_with_tools(
                messages, spec_list, max_tokens=800, temperature=0.3,
                timeout=max(0.1, remaining))
        except Exception as exc:  # noqa: BLE001 - 模型侧异常不向上抛
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            break
        if not isinstance(response, dict):
            error = "模型返回格式错误"
            break
        text_out = str(response.get("text") or "")
        if text_out:
            reply = text_out
        if not response.get("ok"):
            error = str(response.get("error") or "模型调用失败")
            break
        raw_calls = response.get("tool_calls") or []
        pending = [call for call in raw_calls if isinstance(call, dict)]
        if not pending:
            if text_out:
                reply = text_out
            break

        messages.append(_assistant_message(text_out, pending))
        stop = False
        for call in pending:
            if time.monotonic() >= deadline:
                budget_exceeded = True
                stop = True
                break
            if calls_used >= max_tool_calls:
                budget_exceeded = True
                stop = True
                break
            calls_used += 1
            name = str(call.get("name") or "").strip()
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            call_id = str(call.get("id") or f"call_{step_no}_{calls_used}")
            try:
                dedupe_key = (name, json.dumps(arguments, ensure_ascii=False,
                                               sort_keys=True, default=str))
            except (TypeError, ValueError):
                dedupe_key = (name, str(arguments))
            _emit(on_event, {"type": "tool_call", "name": name})
            if dedupe_key in seen_calls:
                # 模型有时会反复查同一条；跳过执行，直接告诉它别磨蹭。
                payload = {"ok": False, "tool": name, "duplicate": True,
                           "error": "重复调用已跳过（这条已经查过），请直接回答"}
                elapsed = 0.0
            else:
                seen_calls.add(dedupe_key)
                tool_started = time.monotonic()
                if name and name in allowed:
                    payload = _execute(registry, name, arguments, db_path)
                else:
                    payload = {"ok": False, "tool": name, "error": "工具不在白名单"}
                elapsed = round(time.monotonic() - tool_started, 3)
            ok = bool(payload.get("ok"))
            _emit(on_event, {"type": "tool_result", "name": name, "ok": ok})
            steps.append({"tool": name, "ok": ok, "elapsed": elapsed})
            if ok:
                _collect_sources(name, payload, sources)
                _collect_notes(name, payload, notes)
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "name": name, "content": _tool_content(payload)})
        if stop:
            break
    else:
        # max_steps 用尽且最后一轮仍在要求工具调用。
        budget_exceeded = True

    # 预算耗尽 / 模型只调工具没给答案时，强制收尾一次（不带 tools），避免空回复。
    if not reply:
        _chat = getattr(client, "chat", None)
        if callable(_chat):
            try:
                final = _chat(list(messages) + [{"role": "user", "content": FINAL_PROMPT}],
                              purpose="agent_final", max_tokens=400, temperature=0.3)
                if isinstance(final, dict) and final.get("ok"):
                    reply = str(final.get("text") or "")
            except Exception as exc:  # noqa: BLE001
                if not error:
                    error = f"{type(exc).__name__}: {str(exc)[:200]}"

    reply = str(reply or "").strip()
    ok = (not error) and bool(reply)
    return _result(ok=ok, reply=reply, sources=list(sources.values())[:MAX_SOURCES],
                   steps=steps, tool_calls=calls_used, error=error,
                   budget_exceeded=budget_exceeded, notes=notes)
