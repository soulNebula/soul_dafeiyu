# -*- coding: utf-8 -*-
"""视觉链路：把 image_understanding 的请求按用途路由到本地 VL（8125）/线上 API。

背景（2026-10-29）：8125 从 Qwen2.5-3B-Instruct 换成 Qwen2.5-VL-3B-Instruct，
文本提取（关键词/摘要/聚类）之外兼做「识图」；识图优先本地、失败回落 DeepSeek。
"""
from __future__ import annotations

from typing import Any

from . import router as router_mod


class RouterChatClient:
    """给 image_understanding 用的 client 适配器（接口同 selflearn.llm.Client.chat）。"""

    def __init__(self, purpose: str = "selflearn_image_understanding",
                 model: str = "", *, router: Any = None) -> None:
        self.purpose = str(purpose or "selflearn_image_understanding")
        self.model = str(model or "local-vl-3b")
        self._router = router

    def available(self) -> bool:
        return True

    def chat(self, messages: list[dict], *, purpose: str = "",
             max_tokens: int = 800, temperature: float = 0.7,
             json_mode: bool = False, timeout: float | None = None) -> dict:
        target = str(purpose or self.purpose)
        try:
            router = self._router or router_mod.default_router()
            result = router.chat(target, messages, max_tokens=max_tokens,
                                 temperature=temperature, json_mode=json_mode,
                                 timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 路由异常按失败返回
            return {"ok": False, "text": "", "usage": {}, "model": self.model,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        if not isinstance(result, dict):
            return {"ok": False, "text": "", "usage": {}, "model": self.model,
                    "error": "router_returned_non_dict"}
        return {
            "ok": bool(result.get("ok")),
            "text": str(result.get("text") or ""),
            "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
            "model": str(result.get("backend") or self.model),
            "elapsed": result.get("latency_ms"),
            "error": str(result.get("error") or ""),
        }
