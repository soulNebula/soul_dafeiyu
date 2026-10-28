# -*- coding: utf-8 -*-
"""验收回归：外部改 config.json 后 /status 应反映新值（懒重载）。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agi.wechat_gateway.server import Gateway


def _gateway(tmp_path):
    cfg = {
        "port": 801, "dry_run": True, "send_mode": "auto",
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0], "min_interval_s": 0,
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    gw = Gateway(cfg, stop_path=tmp_path / "STOP", config_path=cfg_path)
    gw._config_hot_reload = True  # 生产默认开；测试里显式打开（自定义路径默认拒绝热加载）
    return gw, cfg_path


def test_status_reflects_external_config_change(tmp_path, monkeypatch):
    from agi.wechat_gateway import config as gw_config
    gw, cfg_path = _gateway(tmp_path)
    # 让懒重载认为 config_path 就是"生产配置路径"
    monkeypatch.setattr(gw_config, "CONFIG_PATH", cfg_path)
    assert gw.status()["send_mode"] == "auto"
    time.sleep(0.05)
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["send_mode"] = "confirm"
    cfg_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert gw.status()["send_mode"] == "confirm"
