# -*- coding: utf-8 -*-
"""微信网关代理：草稿箱（confirm 模式下的批准/拒绝）。

工作台不直接操作微信，只把请求转发到 801 网关（带 X-Admin-Token）。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from . import paths

TIMEOUT = 20.0


def _gateway() -> tuple[str, dict]:
    """从 SoulAgent config.json 读端口与 token；缺省 801/8888。"""
    port, token = 801, "8888"
    cfg_path = paths.wechat_config_path()
    if cfg_path.is_file():
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            port = int(cfg.get("port") or port)
            token = str(cfg.get("admin_token") or token)
        except Exception:  # noqa: BLE001
            pass
    return f"http://127.0.0.1:{port}", {"X-Admin-Token": token}


def _call(method: str, path: str, payload: dict | None = None) -> dict:
    base, headers = _gateway()
    try:
        r = httpx.request(method, base + path, headers=headers, json=payload,
                          timeout=TIMEOUT)
        data = r.json()
        if isinstance(data, dict):
            data["_gateway_status"] = r.status_code
        return data
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "hint": "网关 801 未启动或 token 不匹配"}


def drafts() -> dict:
    data = _call("GET", "/api/drafts")
    items = data.get("drafts") or []
    return {"ok": bool(data.get("ok")), "items": items, "count": len(items),
            "error": data.get("error")}


def approve(draft_id: str, *, force_quiet: bool = True) -> dict:
    out = _call("POST", "/api/draft/approve",
                {"id": draft_id, "confirm": True, "force_quiet": force_quiet})
    return out


def reject(draft_id: str) -> dict:
    return _call("POST", "/api/draft/reject", {"id": draft_id})


def send_now(contact: str, text: str, *, force_quiet: bool = True) -> dict:
    return _call("POST", "/api/approve",
                 {"contact": contact, "text": text, "confirm": True,
                  "force_quiet": force_quiet})
