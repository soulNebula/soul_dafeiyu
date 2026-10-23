# -*- coding: utf-8 -*-
"""Learn Loop 自动拆解：目标 → 3 步 → 目标树。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import goals


def decompose(goal: str, client) -> dict[str, Any]:
    text = str(goal or "").strip()
    if not text:
        return {"ok": False, "error": "goal 不能为空"}
    if client is None or not getattr(client, "available", lambda: False)():
        return {"ok": False, "error": "LLM 不可用"}
    prompt = (
        "把一个学习目标拆成 3 个可执行步骤。只输出 JSON："
        '{"steps":["步骤1","步骤2","步骤3"]}。不要空话。\n目标：' + text[:500]
    )
    try:
        r = client.chat([{"role": "user", "content": prompt}],
                        purpose="selflearn_plan", max_tokens=500, temperature=0.3,
                        json_mode=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "LLM 失败"}
    try:
        obj = json.loads(str(r.get("text") or ""))
        steps = obj.get("steps") if isinstance(obj, dict) else None
    except (TypeError, ValueError):
        return {"ok": False, "error": "拆解输出不是 JSON"}
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "没有 steps"}
    return {"ok": True, "goal": text, "steps": [str(s)[:160] for s in steps[:5] if str(s).strip()]}


def apply(path: Path | str, goal: str, client) -> dict[str, Any]:
    plan = decompose(goal, client)
    if not plan.get("ok"):
        return plan
    root = goals.add(path, plan["goal"], priority=0.8)
    if not root.get("ok"):
        return root
    children = []
    for step in plan["steps"]:
        r = goals.add(path, step, parent_id=root["id"], priority=0.6)
        if r.get("ok"):
            children.append(r["id"])
    return {"ok": True, "root": root["id"], "children": children, "steps": plan["steps"]}
