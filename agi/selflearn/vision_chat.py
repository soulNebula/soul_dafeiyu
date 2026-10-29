# -*- coding: utf-8 -*-
"""微信聊天图片的本地视觉路由（2026-10-29）。

背景：聊天里收到图片时，原实现把图片直接塞给 DeepSeek 视觉模型（V4.1 Flash），
按图计费；而本机已有 8125 Qwen2.5-VL-3B。这里把「看图」这一步改到本地：

- ``local_caption``（默认）：本地 VL 生成 1–3 句图片描述 → 把描述注入文本提示词，
  回复仍由 DeepSeek 文本模型生成（不发送图片 token，省钱且保留回复质量）；
- ``api``：保持旧行为，图片直接走云端视觉模型。

配置优先级：环境变量 ``CHAT_VISION_MODE`` > 网关 config.json 的 ``chat_vision_mode`` >
默认 ``local_caption``。本地失败/超时自动回落到云端视觉（不让消息卡死）。
"""
from __future__ import annotations

import os
import time

MODE_ENV = "CHAT_VISION_MODE"
VALID_MODES = ("local_caption", "api")
DEFAULT_MODE = "local_caption"
PURPOSE_CAPTION = "selflearn_chat_caption"
CAPTION_PROMPT = (
    "用 1–3 句中文描述这张图片：画面主体、图中文字/数字、值得注意的细节。\n"
    "只描述你看到的，不要猜测人物关系、不要编造图片里没有的内容。"
)

_MODE_CACHE: tuple[float, str] = (0.0, "")


def mode() -> str:
    """当前聊天识图模式：local_caption / api。"""
    raw = str(os.environ.get(MODE_ENV) or "").strip().lower()
    if raw in VALID_MODES:
        return raw
    global _MODE_CACHE
    now = time.monotonic()
    if _MODE_CACHE[1] and now - _MODE_CACHE[0] < 5.0:
        return _MODE_CACHE[1]
    value = DEFAULT_MODE
    try:
        from ..wechat_gateway import config as gw_config
        configured = str(gw_config.load().get("chat_vision_mode") or "").strip().lower()
        if configured in VALID_MODES:
            value = configured
    except Exception:  # noqa: BLE001 - 配置不可用就用默认，不影响聊天
        value = DEFAULT_MODE
    _MODE_CACHE = (now, value)
    return value


def caption(image_data_url: str, *, client=None,
            max_tokens: int = 200) -> tuple[str, str]:
    """本地 VL 生成图片描述。返回 ``(caption, error)``；失败时 caption 为空。"""
    image = str(image_data_url or "").strip()
    if not image:
        return "", "empty_image"
    if client is None:
        try:
            from ..llm.vision import RouterChatClient
            client = RouterChatClient(purpose=PURPOSE_CAPTION)
        except Exception as exc:  # noqa: BLE001
            return "", f"client_unavailable: {type(exc).__name__}: {exc}"
    messages = [{"role": "user", "content": [
        {"type": "text", "text": CAPTION_PROMPT},
        {"type": "image_url", "image_url": {"url": image}},
    ]}]
    try:
        result = client.chat(messages, purpose=PURPOSE_CAPTION,
                             max_tokens=max_tokens, temperature=0.2, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {str(exc)[:200]}"
    result = result if isinstance(result, dict) else {}
    text = str(result.get("text") or "").strip()
    if result.get("ok") and text:
        return text[:500], ""
    return "", str(result.get("error") or "empty_caption")[:200]
