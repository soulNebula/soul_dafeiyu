# -*- coding: utf-8 -*-
"""P13 统一 LLM 路由层：按用途决定本地/API，带兜底、记账、日预算。

用法::

    from agi.llm import chat, usage_summary, purposes, available_backends

    result = chat("chat_reply", [{"role": "user", "content": "在吗"}])
    # -> {ok, text, backend, purpose, usage{tokens_in,tokens_out}, latency_ms,
    #     fallback_used, budget_exceeded, error, warnings, attempts}

    summary = usage_summary(days=7)   # 控制台统计页用

配置：``agi/data/llm_router.json``（env ``LLM_ROUTER_CONFIG`` 可覆盖路径）。
本包只做路由，不改任何现有业务调用；接线由后续任务做。
"""
from __future__ import annotations

from .router import (
    DEFAULT_CONFIG_PATH,
    PURPOSE_CATEGORIES,
    Router,
    RouterConfigError,
    available_backends,
    chat,
    default_router,
    load_config,
    purpose_category,
    purposes,
    reset_default_router,
    usage_summary,
    validate_config,
)

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "PURPOSE_CATEGORIES",
    "Router",
    "RouterConfigError",
    "available_backends",
    "chat",
    "default_router",
    "load_config",
    "purpose_category",
    "purposes",
    "reset_default_router",
    "usage_summary",
    "validate_config",
]
