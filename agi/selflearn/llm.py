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

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 800, temperature: float = 0.7,
             json_mode: bool = False) -> dict[str, Any]:
        body = {"model": self.model, "messages": messages, "stream": False,
                "max_tokens": int(max_tokens), "temperature": float(temperature)}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        started = time.time()
        try:
            if self.transport is not None:
                payload = self.transport(messages, body)
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
            text = ""
            try:
                text = (payload.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            except Exception:  # noqa: BLE001
                text = ""
            return {"ok": bool(text), "text": text, "model": self.model,
                    "usage": payload.get("usage") or {},
                    "elapsed": round(time.time() - started, 3)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "text": "", "model": self.model, "usage": {},
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "elapsed": round(time.time() - started, 3)}
