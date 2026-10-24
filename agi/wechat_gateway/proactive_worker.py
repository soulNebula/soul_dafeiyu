# -*- coding: utf-8 -*-
"""M5 主动链：把「主动想说」真正接到微信上。

之前的结构性断点（2026-10-24 评估）：
1. `proactive.tick` 只把消息写进 selflearn 的 messages 表，**从不发送**；
2. 网关只被动回复，全仓库 0 处 proactive 调用；
3. 调度在网页的 `setInterval` 里——关掉页面就全停；
4. 主动链跑活动库，真实对话在 per-contact 库，**对空库说话**；
5. 主动消息不建 turn，反馈永远收不回来。

本模块一次解决 1–4；第 5 点由 `proactive.record_proactive_turn` 补 turn，
发送成功后登记到 `gateway.last_turns[contact]`，之后的「反馈 好/差」就能落到该轮。

安全：所有发送仍走 `gateway._send(proactive=True)`，因此 confirm/draft、静默时段、
频率窗口、内容闸门、白名单全部照常生效；`autonomy.gate` 再挡一层。
"""
from __future__ import annotations

import time
from typing import Any


def default_client():
    from agi.selflearn import llm
    return llm.Client()


def _contacts(gateway, contacts=None) -> list[str]:
    raw = contacts or gateway.cfg.get("proactive_contacts") \
        or gateway.cfg.get("allowed_contacts") or []
    return [str(x) for x in raw if str(x).strip()]


def run_cycle(gateway, *, contacts=None, client=None, now=None) -> dict[str, Any]:
    """对所有（或指定）联系人跑一次主动循环；返回统计，不抛异常。"""
    from agi.selflearn import ticker
    from . import config as gw_cfg

    now = time.time() if now is None else float(now)
    if not gateway.cfg.get("proactive_enabled"):
        return {"ok": True, "ran": False, "reason": "proactive_disabled"}
    contact_list = _contacts(gateway, contacts)
    client = client if client is not None else default_client()
    out: dict[str, Any] = {"ok": True, "ran": True, "contacts": len(contact_list),
                           "sent": 0, "drafts": 0, "skipped": []}
    for contact in contact_list:
        db_path = gw_cfg.contact_db(contact)
        try:
            tick = ticker.run_once(db_path, client, now=now) or {}
        except Exception as exc:  # noqa: BLE001 - 单个联系人失败不影响其他人
            out["skipped"].append({"contact": contact,
                                   "reason": f"{type(exc).__name__}: {str(exc)[:120]}"})
            continue
        pro = tick.get("proactive") or {}
        if not tick.get("ran") or not pro.get("available"):
            out["skipped"].append({
                "contact": contact,
                "reason": str(tick.get("reason") or pro.get("reason") or "gate")[:80]})
            continue
        bubbles = pro.get("messages") or ([pro.get("message")] if pro.get("message") else [])
        turn_id = str(pro.get("turn_id") or "")
        sent_any = False
        for bubble in bubbles:
            text = str(bubble or "").strip()
            if not text:
                continue
            try:
                status = gateway._send(contact, text, proactive=True)
            except Exception as exc:  # noqa: BLE001
                status = {"ok": False, "status": "failed",
                          "error": f"{type(exc).__name__}: {exc}"}
            gateway._record("proactive_send", {
                "friend": contact, "text": text[:120],
                "status": status.get("status"), "draft_id": status.get("draft_id"),
                "error": str(status.get("error") or "")[:120]})
            if status.get("ok") and status.get("status") != "draft":
                out["sent"] += 1
                sent_any = True
            elif status.get("status") == "draft":
                out["drafts"] += 1
        if sent_any and turn_id:
            # 只有真的发出去了才登记：草稿还没到对方手里，反馈会错位
            gateway.last_turns[contact] = turn_id
    outcome = {"ts": now, "contacts": out["contacts"],
               "sent": out["sent"], "drafts": out["drafts"],
               "skipped": len(out["skipped"])}
    try:
        gateway.last_proactive = outcome
    except Exception:  # noqa: BLE001
        pass
    return out
