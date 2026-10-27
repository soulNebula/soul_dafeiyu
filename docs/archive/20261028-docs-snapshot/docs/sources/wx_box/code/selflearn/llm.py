# -*- coding: utf-8 -*-
"""最小 DeepSeek V4.1 Flash 客户端（selflearn 独立实现）。"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from .config import WEB_CONFIG, DEFAULT_MODEL


def load_config() -> dict[str, Any]:
    config = {"api_key": "", "model": DEFAULT_MODEL, "base_url": "https://api.deepseek.com"}
    try:
        raw = json.loads(WEB_CONFIG.read_text(encoding="utf-8"))
        llm = raw.get("llm") if isinstance(raw, dict) else None
        if isinstance(llm, dict):
            for key in ("api_key", "model", "base_url"):
                if isinstance(llm.get(key), str) and llm[key].strip():
                    config[key] = llm[key].strip()
    except Exception:  # noqa: BLE001
        pass
    config["base_url"] = config["base_url"].rstrip("/")
    return config


def parse_json(text: str) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


class Client:
    def __init__(self, config: dict[str, Any] | None = None,
                 transport=None, timeout: float = 60.0) -> None:
        self.config = config or load_config()
        self.api_key = str(self.config.get("api_key") or "")
        self.model = str(self.config.get("model") or DEFAULT_MODEL)
        self.base_url = str(self.config.get("base_url") or "https://api.deepseek.com").rstrip("/")
        self.transport = transport
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key) or self.transport is not None

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        started = time.time()
        try:
            if self.transport is not None:
                payload = self.transport(body["messages"], body)
            else:
                if not self.api_key:
                    raise RuntimeError("缺少 DeepSeek API key")
                req = urllib.request.Request(
                    self.base_url + "/chat/completions",
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST",
                    headers={"Authorization": "Bearer " + self.api_key,
                             "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
            choice = (payload.get("choices") or [{}])[0]
            text = (choice.get("message") or {}).get("content") or ""
            return {"ok": bool(text), "text": text, "model": self.model,
                    "usage": payload.get("usage") or {},
                    "finish_reason": choice.get("finish_reason") or "",
                    "elapsed": round(time.time() - started, 3)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "model": self.model, "usage": {},
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "elapsed": round(time.time() - started, 3)}

    def _request_raw(self, body: dict[str, Any], *,
                     timeout: float | None = None) -> dict[str, Any]:
        """与 ``_post`` 相同的传输层，但返回原始 payload。

        多步工具循环需要读取 ``choices[0].message.tool_calls``，而
        ``_post`` 只保留文本；这里复用同一套 transport 注入约定
        （``transport(body["messages"], body)``），不改变 ``_post`` 行为。
        """
        if self.transport is not None:
            return self.transport(body["messages"], body)
        if not self.api_key:
            raise RuntimeError("缺少 DeepSeek API key")
        req = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={"Authorization": "Bearer " + self.api_key,
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and part.get("type") in (None, "text"):
                    if isinstance(part.get("text"), str):
                        parts.append(part["text"])
            return "".join(parts)
        return ""

    @staticmethod
    def _parse_arguments(raw: Any, name: str, errors: list[str]) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return {}
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except (TypeError, ValueError) as exc:
                errors.append(f"{name} 的 arguments 不是合法 JSON: {str(exc)[:120]}")
                return {}
            if isinstance(value, dict):
                return value
            errors.append(f"{name} 的 arguments 不是 JSON 对象")
            return {}
        errors.append(f"{name} 的 arguments 类型不支持: {type(raw).__name__}")
        return {}

    def chat_with_tools(self, messages: list[dict], tools: list[dict], *,
                        max_tokens: int = 800, temperature: float = 0.3,
                        timeout: float | None = None) -> dict[str, Any]:
        """OpenAI 兼容的工具调用请求；不解析/执行工具，只返回候选调用。"""
        started = time.time()
        body: dict[str, Any] = {
            "model": self.model, "messages": messages, "stream": False,
            "max_tokens": int(max_tokens), "temperature": float(temperature),
        }
        specs = list(tools or [])
        if specs:
            body["tools"] = specs
            body["tool_choice"] = "auto"
        try:
            payload = self._request_raw(body, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "tool_calls": [], "usage": {},
                    "finish_reason": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "elapsed": round(time.time() - started, 3)}
        if not isinstance(payload, dict):
            return {"ok": False, "text": "", "tool_calls": [], "usage": {},
                    "finish_reason": "", "error": "响应不是 JSON 对象",
                    "elapsed": round(time.time() - started, 3)}
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            detail = payload.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message") or str(detail)
            return {"ok": False, "text": "", "tool_calls": [],
                    "usage": payload.get("usage") or {}, "finish_reason": "",
                    "error": f"响应缺少 choices: {str(detail or '')[:160]}",
                    "elapsed": round(time.time() - started, 3)}
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message")
        if not isinstance(message, dict):
            message = {}
        text = self._message_text(message)
        errors: list[str] = []
        calls: list[dict[str, Any]] = []
        raw_calls = message.get("tool_calls")
        if isinstance(raw_calls, list):
            for index, item in enumerate(raw_calls):
                if not isinstance(item, dict):
                    errors.append(f"第 {index} 个 tool_call 不是对象")
                    continue
                function = item.get("function")
                if not isinstance(function, dict):
                    errors.append(f"第 {index} 个 tool_call 缺少 function")
                    continue
                name = str(function.get("name") or "").strip()
                if not name:
                    errors.append(f"第 {index} 个 tool_call 缺少工具名")
                    continue
                calls.append({
                    "id": str(item.get("id") or f"call_{index}"),
                    "name": name,
                    "arguments": self._parse_arguments(
                        function.get("arguments"), name, errors),
                })
        if not calls and isinstance(message.get("function_call"), dict):
            # 兼容旧式 message.function_call（单工具调用）。
            function = message["function_call"]
            name = str(function.get("name") or "").strip()
            if name:
                calls.append({
                    "id": "call_legacy_0",
                    "name": name,
                    "arguments": self._parse_arguments(
                        function.get("arguments"), name, errors),
                })
            else:
                errors.append("function_call 缺少工具名")
        return {"ok": True, "text": text, "tool_calls": calls,
                "usage": payload.get("usage") or {},
                "finish_reason": str(choice.get("finish_reason") or ""),
                "error": "; ".join(errors),
                "elapsed": round(time.time() - started, 3)}

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 800, temperature: float = 0.7,
             json_mode: bool = False) -> dict[str, Any]:
        body = {"model": self.model, "messages": messages, "stream": False,
                "max_tokens": int(max_tokens), "temperature": float(temperature)}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        result = self._post(body)
        # deepseek-flash 可能把预算耗在 reasoning tokens 上，正文为空；
        # 真实网络请求遇到这种情况自动扩容重试一次（fake transport 不重试）。
        usage = result.get("usage") or {}
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        if (not result.get("ok") and self.transport is None
                and (reasoning >= int(max_tokens) * 0.8
                     or result.get("finish_reason") == "length")):
            retry = dict(body)
            retry["max_tokens"] = min(8192, max(int(max_tokens) * 3, 2048))
            second = self._post(retry)
            second["retried"] = True
            second["retry_max_tokens"] = retry["max_tokens"]
            return second
        return result
