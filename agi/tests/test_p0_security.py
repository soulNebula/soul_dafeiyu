# -*- coding: utf-8 -*-
"""P0 安全回归：F2 空白名单 fail-closed、F3 弱 token 轮换、媒体兜底标记 unverified。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agi.wechat_gateway import control, server as server_mod
from agi.wechat_gateway.server import Gateway


def _gw(tmp_path, **over):
    cfg = {
        "enabled": True, "port": 801, "dry_run": True, "send_mode": "auto",
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0], "min_interval_s": 0,
    }
    cfg.update(over)
    return Gateway(cfg, stop_path=tmp_path / "STOP",
                   config_path=tmp_path / "config.json")


# --------------------------------------------------------------------------- #
# F2：空白名单 = fail-closed
# --------------------------------------------------------------------------- #
def test_empty_allowlist_blocks_send(tmp_path):
    gw = _gw(tmp_path, allowed_contacts=[])
    out = gw._send("任何人", "你好")
    assert out["ok"] is False and out["status"] == "blocked"
    assert out["error"] == "no_allowlist"
    assert any(e.get("kind") == "send_blocked" and e.get("reason") == "no_allowlist"
               for e in gw.recent)


def test_allowlist_still_blocks_other_contacts(tmp_path):
    gw = _gw(tmp_path)
    out = gw._send("陌生人", "你好")
    assert out["ok"] is False and out["error"] == "not_allowed"


# --------------------------------------------------------------------------- #
# F3：弱/空 token 启动时轮换
# --------------------------------------------------------------------------- #
def test_ensure_admin_token_rotates_weak():
    cfg = {"admin_token": "8888"}
    token, generated = control.ensure_admin_token(cfg)
    assert generated is True
    assert cfg["admin_token"] == token and len(token) >= 32
    assert control.ensure_admin_token(cfg)[1] is False  # 再跑不再换


def test_ensure_admin_token_rotates_empty_and_keeps_strong():
    cfg: dict = {}
    token, generated = control.ensure_admin_token(cfg)
    assert generated is True and len(token) >= 32
    strong = {"admin_token": "a" * 32}
    assert control.ensure_admin_token(strong) == ("a" * 32, False)


# --------------------------------------------------------------------------- #
# 媒体兜底：标记 unverified（避免"报成功其实没到"）
# --------------------------------------------------------------------------- #
def test_media_fallback_marks_unverified(tmp_path, monkeypatch):
    from agi.wechat_gateway import uia_sender

    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    py = tmp_path / "py.exe"
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr(server_mod, "WXAUTO_PYTHON", py)

    class _Proc:
        stdout = ('{"items":[{"ok":false,"status":"failed","type":"image",'
                  '"error":"boom"}]}')
        stderr = ""

    monkeypatch.setattr(server_mod.subprocess, "run", lambda *a, **k: _Proc())
    monkeypatch.setattr(uia_sender, "send_media",
                        lambda contact, item, cfg: {"ok": True, "status": "sent",
                                                    "sent": True, "method": "uia"})
    gw = _gw(tmp_path, dry_run=False, media_enabled=True,
             fallback_enabled=True, fallback_send_mode="uia")
    batch = gw.send_media_batch("好友A", [{"type": "image", "path": str(image)}])
    item = batch["items"][0]
    assert item["ok"] is True
    assert item.get("unverified") is True and item.get("verified") is False
    assert item.get("warning") == "fallback_send_unverified"
