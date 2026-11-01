# -*- coding: utf-8 -*-
"""V1 PR8：路由客户端适配器（``selflearn.llm.Client`` 兼容）。

为什么需要
----------
801/802 里既有的 LLM 调用点（`selflearn.chat` / `mood_judge` / `image_understanding`
等）都通过 ``client.chat(messages, purpose=...)`` 调用，默认 client 是
``agi.selflearn.llm.Client``（只认一个 API base/key）。bot 包要求「灵魂模型
local / cloud 由用户选」，于是需要把同一批调用点按 purpose 落到
``agi/llm/router.py`` 的通道上。

本模块提供 :class:`RouterClient`（接口与 ``selflearn.llm.Client`` 一致）和
:func:`make_bot_client`（bot 工作区生效时返回 RouterClient，否则 None）。
只在 bot 生效时启用；旧安装的默认 client 保持逐字节不变。
"""
from __future__ import annotations

from typing import Any

from . import router as router_mod


class RouterClient:
    """按 purpose 路由的 client 适配器（返回值字段兼容 selflearn.llm.Client）。"""

    def __init__(self, router: Any = None, *, default_purpose: str = "",
                 model: str = "") -> None:
        self._router = router
        self.default_purpose = str(default_purpose or "")
        self.model = str(model or "")

    # -- 与 selflearn.llm.Client 对齐的接口 --------------------------------
    def available(self) -> bool:
        try:
            return bool(self._router_obj().purposes())
        except Exception:  # noqa: BLE001 - 路由不可用按 False 报
            return False

    def _router_obj(self):
        if isinstance(self._router, router_mod.Router):
            return self._router
        if callable(self._router):
            return self._router()
        return self._router or router_mod.default_router()

    def chat(self, messages: list[dict], *, purpose: str = "",
             max_tokens: int = 800, temperature: float = 0.7,
             json_mode: bool = False, timeout: float | None = None) -> dict:
        target = str(purpose or self.default_purpose or "selflearn_chat")
        try:
            result = self._router_obj().chat(
                target, list(messages or []), max_tokens=int(max_tokens),
                temperature=float(temperature), timeout=timeout,
                json_mode=bool(json_mode))
        except Exception as exc:  # noqa: BLE001 - 路由异常按失败返回
            return {"ok": False, "text": "", "usage": {}, "model": self.model,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
        result = result if isinstance(result, dict) else {}
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        tokens_in = int(usage.get("tokens_in") or 0)
        tokens_out = int(usage.get("tokens_out") or 0)
        return {
            "ok": bool(result.get("ok")),
            "text": str(result.get("text") or ""),
            "usage": {"tokens_in": tokens_in, "tokens_out": tokens_out,
                      # 兼容直接读 prompt_tokens/completion_tokens 的既有代码
                      "prompt_tokens": tokens_in, "completion_tokens": tokens_out,
                      "raw": usage},
            "model": str(result.get("model") or result.get("backend") or self.model),
            "elapsed": result.get("latency_ms"),
            "error": str(result.get("error") or ""),
            "backend": str(result.get("backend") or ""),
            "category": str(result.get("category") or ""),
            "purpose": target,
        }


def make_bot_client(*, default_purpose: str = "selflearn_chat") -> RouterClient | None:
    """bot 工作区生效时返回路由客户端；否则 None（调用方回退旧 Client）。"""
    try:
        from agi import bot_runtime
    except Exception:  # noqa: BLE001
        return None
    try:
        if bot_runtime.bot_dir() is None:
            return None
        if bot_runtime.active_router_config() is None:
            return None
    except Exception:  # noqa: BLE001 - bot 配置坏了不阻塞旧路径
        return None
    return RouterClient(router_mod.default_router(),
                        default_purpose=default_purpose)