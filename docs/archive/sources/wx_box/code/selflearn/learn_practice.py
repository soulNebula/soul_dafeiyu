# -*- coding: utf-8 -*-
"""Learn Loop 后半：出题 → 判分 → 记录 practice；来源二源检查。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import learn_loop, verifier


def make_exercise(card: dict[str, Any] | str, client) -> dict[str, Any]:
    text = card.get("content") if isinstance(card, dict) else str(card)
    if not str(text or "").strip():
        return {"ok": False, "error": "卡片为空"}
    if client is None or not getattr(client, "available", lambda: False)():
        return {"ok": False, "error": "LLM 不可用"}
    prompt = (
        "根据技能卡出一道最小练习题。只输出 JSON："
        '{"question":"问题","expected_terms":["关键词1","关键词2"]}。\n卡片：' + str(text)[:1500]
    )
    try:
        r = client.chat([{"role": "user", "content": prompt}],
                        purpose="selflearn_exercise", max_tokens=600, temperature=0.3,
                        json_mode=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:150]}"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "LLM 失败"}
    try:
        obj = json.loads(str(r.get("text") or ""))
    except (TypeError, ValueError):
        return {"ok": False, "error": "练习输出不是 JSON"}
    q = str(obj.get("question") or "").strip()
    terms = obj.get("expected_terms") or []
    if not q or not isinstance(terms, list):
        return {"ok": False, "error": "练习缺少 question/expected_terms"}
    return {"ok": True, "question": q[:500],
            "expected_terms": [str(t) for t in terms[:8]]}


def answer_practice(path: Path | str, task_id: str, answer: str,
                    expected_terms: list[str]) -> dict[str, Any]:
    graded = verifier.verify_answer(answer, expected_terms)
    passed = bool(graded.get("passed"))
    recorded = learn_loop.record_practice(path, task_id, answer or "", passed,
                                          "" if passed else str(graded.get("error") or ""))
    return {"ok": bool(graded.get("ok")), "graded": graded, "recorded": recorded}


def source_count(path: Path | str, skill_id: str) -> int:
    try:
        st = learn_loop.status(path, skill_id)
        return int(st.get("source_count") or 0)
    except Exception:  # noqa: BLE001
        return 0


def has_two_sources(path: Path | str, skill_id: str) -> bool:
    return source_count(path, skill_id) >= 2
