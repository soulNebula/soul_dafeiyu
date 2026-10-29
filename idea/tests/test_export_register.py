# -*- coding: utf-8 -*-
"""803 导出完成后自动注册模型到 local_models.json。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))


def test_register_export_model(tmp_path, monkeypatch):
    from app import trainstudio

    monkeypatch.setattr(trainstudio.paths, "soulagent_home", lambda: tmp_path)
    monkeypatch.setattr(trainstudio.paths, "agent_home", lambda: tmp_path / "agent")
    import agi.bot_runtime as bot_runtime
    monkeypatch.setattr(bot_runtime, "read_active", lambda: None)
    gguf = tmp_path / "out" / "soul_v2-q4_k_m.gguf"
    result = trainstudio.register_export_model(
        name="soul_v2", gguf=str(gguf), base=str(tmp_path / "base"),
        adapter=str(tmp_path / "adapter"))
    assert result["ok"] is True
    path = Path(result["path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = [m for m in data["models"] if m["name"] == "soul_v2"][0]
    assert entry["role"] == "soul" and entry["source"] == "train_export"
    assert entry["gguf"] == str(gguf)
