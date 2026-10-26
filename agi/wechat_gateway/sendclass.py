# -*- coding: utf-8 -*-
"""P7-b/backlog：发送结果统一口径（唯一真源）。

之前 `server._reply` / `server._send` / `proactive_worker.run_cycle` 三处各自判断
"是否送达/是否失败"，语义漂移出过两次"假成功"：

- P0-7：剪贴板兜底 `{ok: True, status:"clipboard"}` 被当成功（内容只在剪贴板，没发出去）；
- backlog：窗口级兜底 `{ok: True, status:"sent", verified:False}` 也被当成功
  （粘贴+回车发了，但**无法校验会话**，可能发错人）；
- dry-run 的 `planned` 既不计 sent 也不计失败，验收时看不见。

现在统一走 :func:`classify_send_result`，返回七类之一：

================  ==========================================================
分类              含义 / 计数影响
================  ==========================================================
``delivered``     真正送达（``ok=True`` 且 status∈{sent,ok} 且 verified 不是 False）→ 计 sent、重置熔断
``failed``        明确失败（含 draft+send_failed 的真失败）→ 计 failed、参与熔断
``draft``         confirm 人工草稿（无 send_failed）→ 内容保留，非故障
``clipboard``     内容进剪贴板等人工粘贴，**未送达** → 不计 sent、不重置熔断
``planned``       dry-run 计划发送，**未发生** → 单列计数，不计 sent/failed
``blocked``       闸门拦截（STOP/静默/限流/内容）→ 非故障、不计 sent
``unverified``    ok=True 但没有"送达证据"（如窗口兜底 verified=False，或未知状态）
                  → 不计 sent、不重置熔断，单列 ``unverified``
================  ==========================================================

设计约束：本模块只做纯函数（不 import gateway 其它模块），避免循环依赖。
"""
from __future__ import annotations

from typing import Any

DELIVERED = "delivered"
FAILED = "failed"
DRAFT = "draft"
CLIPBOARD = "clipboard"
PLANNED = "planned"
BLOCKED = "blocked"
UNVERIFIED = "unverified"

SEND_CLASSES = (DELIVERED, FAILED, DRAFT, CLIPBOARD, PLANNED, BLOCKED, UNVERIFIED)
#: 不是"真正送达"、但也不是故障的类（内容可能保留，不计 sent、不重置熔断）
NON_DELIVERED_OK = (DRAFT, CLIPBOARD, PLANNED, BLOCKED, UNVERIFIED)


def send_status(result: Any) -> str:
    """取 result 的 status 字符串（小写归一）；非 dict 返回空串。"""
    if not isinstance(result, dict):
        return ""
    return str(result.get("status") or "").strip().lower()


def classify_send_result(result: Any) -> str:
    """把一次发送结果归入 7 类之一（口径见模块 docstring）。

    - 非 dict → ``failed``（保守：不知道结果就算失败，参与熔断）
    - status 优先判 planned/blocked/clipboard/draft；再看 ok 与 verified
    - ``ok=True`` + ``verified is False`` → ``unverified``（窗口级兜底）
    """
    if not isinstance(result, dict):
        return FAILED
    state = send_status(result)
    ok = result.get("ok") is True

    if state == "planned":
        return PLANNED if ok else FAILED
    if state == "blocked":
        return BLOCKED
    if state == "clipboard":
        # 只有 ok=True 的 clipboard 才是"内容保留、等人工粘贴"；ok=False 保守算失败
        return CLIPBOARD if ok else FAILED
    if state == "draft":
        # P0-4：真失败落草稿会打 send_failed=True；旧 confirm 草稿没有该键
        if not ok and result.get("send_failed"):
            return FAILED
        return DRAFT
    if not ok:
        return FAILED

    # ---- ok=True：必须有"送达证据"才算 delivered ----
    if state in ("sent", "ok"):
        if result.get("verified") is False:
            return UNVERIFIED          # 窗口级兜底：发了，但无法校验会话
        return DELIVERED
    if state == "failed":
        return FAILED                  # 自相矛盾（ok=True + failed）→ 保守算失败
    return UNVERIFIED                  # 未知成功状态：未验证送达，不计 sent


def is_delivered(result: Any) -> bool:
    """真正送达（唯一可以计 sent / 重置熔断的类）。"""
    return classify_send_result(result) == DELIVERED


def is_failure(result: Any) -> bool:
    """明确失败（参与熔断计数）。"""
    return classify_send_result(result) == FAILED
