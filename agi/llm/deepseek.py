# -*- coding: utf-8 -*-
"""DeepSeek API 后端 —— 直接复用 ``agi.selflearn.llm.Client``（不重复造轮子）。

审计点：这是路由层里唯一会**花钱**的后端；``chat_reply`` / ``mood_judge`` /
``video_understanding`` 等需要判断力的用途走它，高量提取类走本地 8125。
"""
from __future__ import annotations

from typing import Any

LAYER_NAME = "deepseek"
DEFAULT_TIMEOUT_S = 30.0


class DeepSeekBackend:
    """包装 ``agi.selflearn.llm.Client``（生产同款：读 web_config.json 的 key/model）。"""

    kind = "deepseek"

    def __init__(self, name: str = "api", *, model: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S, client=None) -> None:
        self.name = str(name)
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.model = str(model or "")
        if client is not None:
            self.client = client
        else:  # 延迟 import：llm 路由包被 import 时不强行拉 selflearn 依赖
            from ..selflearn.llm import Client
            self.client = Client(timeout=self.timeout_s)

    def available(self) -> bool:
        try:
            return bool(self.client.available())
        except Exception:  # noqa: BLE001
            return False

    def chat(self, messages: list[dict], *, purpose: str = "chat",
             max_tokens: int = 800, temperature: float = 0.7,
             timeout: float | None = None, json_mode: bool = False) -> dict:
        """调用 DeepSeek；失败返回 ``{ok: False, error}``（不抛异常）。

        ``timeout`` 由 client 构造时的 timeout 决定（Client.chat 不接受逐次 timeout），
        这里保留参数只为与其它后端签名一致；需要短超时就传 ``timeout_s`` 构造 client。
        """
        try:
            result = self.client.chat(
                list(messages or []), purpose=str(purpose or "chat"),
                max_tokens=int(max_tokens), temperature=float(temperature),
                json_mode=bool(json_mode))
        except Exception as exc:  # noqa: BLE001 - 路由层统一兜底
            return {"ok": False, "text": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "model": self.model}
        result = result if isinstance(result, dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        text = str(result.get("text") or "")
        return {
            "ok": bool(result.get("ok")) and bool(text),
            "text": text,
            "error": "" if text else str(result.get("error") or "空回复"),
            "usage": {
                "tokens_in": int(usage.get("prompt_tokens") or 0),
                "tokens_out": int(usage.get("completion_tokens") or 0),
            },
            "model": str(result.get("model") or self.model),
            "elapsed": result.get("elapsed"),
        }
