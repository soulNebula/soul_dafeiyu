# -*- coding: utf-8 -*-
"""803 硬件探测：无 GPU 走云端；有显存给推荐。"""
from __future__ import annotations

from app import hardware


def test_no_gpu_forces_cloud(monkeypatch):
    monkeypatch.setattr(hardware, "_nvidia", lambda: [])
    data = hardware.probe()
    assert data["recommendation"]["mode"] == "cloud"
    assert data["recommendation"]["local_training"] is False


def test_high_vram_recommends_local(monkeypatch):
    monkeypatch.setattr(hardware, "_nvidia", lambda: [
        {"name": "RTX 4090", "vram_mb": 24576, "driver": "555"}])
    data = hardware.probe()
    rec = data["recommendation"]
    assert rec["mode"] == "local+cloud"
    assert rec["models"]
    assert "7b" in " ".join(rec["models"])
