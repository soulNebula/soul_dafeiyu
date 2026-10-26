# -*- coding: utf-8 -*-
"""本地 OpenAI 兼容后端（llama-server 8125，``/v1/chat/completions``）。

- 纯标准库（urllib），不引入新依赖；
- 探活 ``GET {base_url}/models``（带 TTL 缓存，避免每次调用都多打一次 HTTP）；
- ``transport(messages, body, timeout) -> payload`` 可注入（单测不联网）；
- JSON mode：``response_format={"type":"json_object"}``（llama-server 支持）。
"""
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

LAYER_NAME = "openai_local"
DEFAULT_TIMEOUT_S = 60.0
PROBE_TTL_S = 15.0

_PROBE_CACHE: dict[str, tuple[float, bool]] = {}


class OpenAILocalBackend:
    """本地 llama-server（OpenAI 兼容）后端。"""

    kind = "openai"

    def __init__(self, name: str = "local", *, base_url: str = "http://127.0.0.1:8125/v1",
                 model: str = "local-3b", timeout_s: float = DEFAULT_TIMEOUT_S,
                 api_key: str = "", transport=None) -> None:
        self.name = str(name)
        self.base_url = str(base_url or "http://127.0.0.1:8125/v1").rstrip("/")
        self.model = str(model or "local-3b")
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.api_key = str(api_key or "")
        self.transport = transport

    # -- 探活 -------------------------------------------------------------
    def available(self, *, now: float | None = None) -> bool:
        if self.transport is not None:
            return True
        now = time.time() if now is None else float(now)
        cached = _PROBE_CACHE.get(self.base_url)
        if cached is not None and now - cached[0] < PROBE_TTL_S:
            return cached[1]
        ok = False
        try:
            req = urllib.request.Request(
                self.base_url + "/models", method="GET",
                headers=self._headers())
            with urllib.request.urlopen(req, timeout=min(3.0, self.timeout_s)) as resp:
                ok = 200 <= int(getattr(resp, "status", 200)) < 300
        except Exception:  # noqa: BLE001 - 探活失败就是不可用
            ok = False
        _PROBE_CACHE[self.base_url] = (now, ok)
        return ok

    # -- 调用 -------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        return headers

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 800, temperature: float = 0.7,
             timeout: float | None = None, json_mode: bool = False) -> dict:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages or []),
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        eff_timeout = float(timeout or self.timeout_s)
        started = time.perf_counter()
        try:
            if self.transport is not None:
                payload = self.transport(body["messages"], body, eff_timeout)
            else:
                req = urllib.request.Request(
                    self.base_url + "/chat/completions",
                    data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    method="POST", headers=self._headers())
                with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001 - HTTP/超时/连接失败统一失败返回
            return {"ok": False, "text": "", "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "model": self.model,
                    "elapsed": round(time.perf_counter() - started, 3)}
        if not isinstance(payload, dict):
            return {"ok": False, "text": "", "error": "响应不是 JSON 对象",
                    "model": self.model,
                    "elapsed": round(time.perf_counter() - started, 3)}
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices else {}
        message = choice.get("message") if isinstance(choice, dict) else None
        text = ""
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                text = "".join(str(part.get("text") or "") for part in content
                               if isinstance(part, dict))
        usage_raw = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        usage = {
            "tokens_in": int(usage_raw.get("prompt_tokens") or 0),
            "tokens_out": int(usage_raw.get("completion_tokens") or 0),
        }
        return {
            "ok": bool(text),
            "text": text,
            "usage": usage,
            "model": str(payload.get("model") or self.model),
            "elapsed": round(time.perf_counter() - started, 3),
            "error": "" if text else "空回复（检查本地服务是否 OOM / 被截断）",
        }
