# -*- coding: utf-8 -*-
"""M4 夜间整理：摘要 / 画像 / 自反思 / 弱验证状态。

只写本地状态，不发送微信、不自动改人格/技能等级。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import ingest, memory, personality, profile, summary, weak_validation


def run(path: Path | str, client=None, *, force: bool = False) -> dict[str, Any]:
    started = time.time()
    out: dict[str, Any] = {"ok": True, "started_at": started, "steps": {}}
    steps: dict[str, Any] = {}
    can_llm = client is not None and getattr(client, "available", lambda: False)()
    if can_llm:
        try:
            steps["summary"] = summary.build(path, client, force=force)
        except Exception as exc:  # noqa: BLE001
            steps["summary"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            steps["profile"] = profile.distill(path, client)
        except Exception as exc:  # noqa: BLE001
            steps["profile"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        try:
            steps["reflect"] = personality.reflect(path, client)
        except Exception as exc:  # noqa: BLE001
            steps["reflect"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    else:
        steps["summary"] = {"ok": False, "skipped": "no_client"}
        steps["profile"] = {"ok": False, "skipped": "no_client"}
        steps["reflect"] = {"ok": False, "skipped": "no_client"}
    try:
        sessions = ingest.build_sessions(path)
        segments = 0
        for session in sessions:
            segments += len(ingest.segment_session(path, str(session["id"])))
        steps["sessions"] = {"ok": True, "sessions": len(sessions), "segments": segments}
    except Exception as exc:  # noqa: BLE001
        steps["sessions"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    try:
        steps["weak"] = weak_validation.status(path)
    except Exception as exc:  # noqa: BLE001
        steps["weak"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    # P1-a：记忆重要性按天衰减 + 长期低重要且未访问 → dormant（不删除、检索排除）
    try:
        steps["memory_decay"] = memory.decay(path)
    except Exception as exc:  # noqa: BLE001
        steps["memory_decay"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out["steps"] = steps
    out["ok"] = all(not (isinstance(v, dict) and v.get("error")) for v in steps.values())
    out["finished_at"] = time.time()
    out["auto_send"] = False
    return out
