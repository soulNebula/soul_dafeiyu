# -*- coding: utf-8 -*-
"""V1 PR8：selflearn 生产 client 的 bot 接线（从 chat.py 抽出来，控制单文件预算）。

`SelfLearnAgent` 的既有调用点（``self.client.chat(purpose=...)``）在 bot 工作区
生效时改用 ``agi.llm.client.RouterClient``；云端灵魂方案下，内状态/人格 client
也走 bot 的 soul 通道（本地方案仍用 llama-server，见 docs/plans/64 的 D1）。
未启用 bot 时两条入口都返回 None / 历史 ``Client``，旧安装零变化。
"""
from __future__ import annotations

from typing import Any


def default_client() -> Any:
    """SelfLearnAgent 的默认 client：bot 生效 → 路由客户端；否则历史 Client。"""
    try:
        from ..llm.client import make_bot_client
        routed = make_bot_client(default_purpose="selflearn_chat")
        if routed is not None:
            return routed
    except Exception:  # noqa: BLE001 - 路由层故障不能挡住聊天
        pass
    from .llm import Client
    return Client()


def cloud_soul_client() -> Any | None:
    """云端灵魂方案下的 soul 通道 client；本地/未启用 bot 返回 None（调用方回退）。"""
    try:
        from agi import bot_runtime
        if bot_runtime.soul_provider() != "cloud":
            return None
        from ..llm.client import RouterClient
        return RouterClient(default_purpose="inner_state")
    except Exception:  # noqa: BLE001 - 取不到 bot 配置就按旧路径
        return None