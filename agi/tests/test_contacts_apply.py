# -*- coding: utf-8 -*-
"""802 联系人选择应用：写白名单 + 即时更新 receiver/backend。"""
from __future__ import annotations

from types import SimpleNamespace

from agi.wechat_gateway.server import Gateway


def _gw(tmp_path):
    path = tmp_path / "wechat" / "config.json"
    path.parent.mkdir(parents=True)
    cfg = {"enabled": True, "allowed_contacts": ["旧"], "monitored_contacts": ["旧"],
           "admin_token": "tok", "port": 801, "auto_reply": True}
    import json
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return Gateway(cfg, config_path=path)


def test_apply_contacts_updates_runtime(tmp_path):
    gw = _gw(tmp_path)
    calls = []
    gw.receiver = SimpleNamespace(allowed=set())
    gw.backend = SimpleNamespace(set_monitored=lambda names: calls.append(list(names)))
    result = gw.apply_contacts(["A", "B"], ["B"])
    assert result["ok"] is True
    assert result["allowed_contacts"] == ["A", "B"]
    assert result["monitored_contacts"] == ["B"]
    assert gw.receiver.allowed == {"A", "B"}
    assert calls == [["B"]]
    assert gw.cfg["allowed_contacts"] == ["A", "B"]
