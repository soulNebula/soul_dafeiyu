# -*- coding: utf-8 -*-
"""V1 PR9：802 B站页的「展示开关 + 仓库内模块接线」测试（不联网）。

覆盖：
- 弹幕/评论开关默认关、写回网关配置、非布尔拒绝；
- 开关全关时 `/api/bili/video` 的取数函数直接返回 display_disabled（不发请求）；
- 开关打开后按上限取评论/弹幕、只展示不落库；
- 仓库内扫码轮询不回传凭证、登录状态查询失败降级。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "console_server_bili_display", ROOT / "tools" / "console" / "server.py")
console = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(console)


def _fake_modules(**overrides):
    base = {
        "auth": SimpleNamespace(),
        "feed": SimpleNamespace(recommend=lambda limit=20: {"ok": True, "items": []}),
        "worker": SimpleNamespace(run_once=lambda **kwargs: {"ok": True, "items": []}),
        "video": SimpleNamespace(display=lambda *a, **k: {"ok": True, "comments": [],
                                                        "danmaku": []}),
        "common": SimpleNamespace(credential_path=lambda: "/tmp/bili.json"),
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _isolated_gateway_config(tmp_path, monkeypatch):
    """网关配置指到 tmp：绝不写生产 agi/data/wechat/config.json。"""
    target = tmp_path / "gateway_config.json"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(console, "GATEWAY_CONFIG", target)


def test_display_defaults_off():
    settings = console.bili_display_settings()
    assert settings["show_danmaku"] is False and settings["show_comments"] is False


def test_display_update_persists_and_reads_back():
    result = console.bili_display_update({"show_danmaku": True})
    assert result["ok"] is True
    assert console.bili_display_settings()["show_danmaku"] is True
    assert console.gateway_config()["bili_show_danmaku"] is True


def test_display_update_rejects_non_bool():
    result = console.bili_display_update({"show_comments": "maybe"})
    assert result["ok"] is False and result["errors"]


def test_display_update_requires_a_field():
    assert console.bili_display_update({})["ok"] is False


def test_video_display_disabled_makes_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not touch repo module")
    monkeypatch.setattr(console, "_bili_repo_modules",
                        lambda: _fake_modules(video=SimpleNamespace(display=boom)))
    result = console.bili_video_display("BV1")
    assert result["ok"] is False and result["error"] == "display_disabled"


def test_video_display_passes_switches_and_caps(monkeypatch):
    captured = {}

    def fake_display(bvid, **kwargs):
        captured.update(bvid=bvid, **kwargs)
        return {"ok": True, "comments": [{"text": "hi"}], "danmaku": []}
    console.apply_config({"bili_show_comments": True, "bili_show_danmaku": True})
    monkeypatch.setattr(console, "_bili_repo_modules",
                        lambda: _fake_modules(video=SimpleNamespace(display=fake_display)))
    result = console.bili_video_display("BV1", limit=99)
    assert result["ok"] is True
    assert captured["bvid"] == "BV1"
    assert captured["show_comments"] is True and captured["show_danmaku"] is True
    assert captured["comment_limit"] == 20      # 上限钳制


def test_repo_status_degrades_on_error(monkeypatch):
    def boom():
        raise RuntimeError("network down")
    monkeypatch.setattr(console, "_bili_repo_modules",
                        lambda: _fake_modules(auth=SimpleNamespace(login_status=boom)))
    result = console.bili_repo_status()
    assert result["ok"] is True
    assert result["status"]["logged_in"] is False
    assert "network down" in result["status"]["error"]


def test_repo_login_poll_never_returns_credential(monkeypatch):
    saved = {}

    def fake_poll(key):
        return {"ok": True, "state": "ok", "code": 0,
                "credential": {"sessdata": "secret"}}

    def fake_save(cred):
        saved.update(cred)
        return {"ok": True, "credential_path": "/tmp/bili.json"}

    monkeypatch.setattr(console, "_bili_repo_modules", lambda: _fake_modules(
        auth=SimpleNamespace(qr_poll=fake_poll, save_login=fake_save)))
    result = console.bili_repo_login_poll("KEY")
    assert "credential" not in result
    assert saved == {"sessdata": "secret"}
    assert result["saved"]["ok"] is True


def test_repo_login_qr_writes_image(monkeypatch, tmp_path):
    png = tmp_path / "qr.png"
    png.write_bytes(b"png-bytes")
    monkeypatch.setattr(console, "BILI_REPO_QR_PNG", png)
    monkeypatch.setattr(console, "_render_qr_png", lambda url, target: None)
    monkeypatch.setattr(console, "_bili_repo_modules", lambda: _fake_modules(
        auth=SimpleNamespace(qr_generate=lambda: {"ok": True, "url": "u",
                                                  "key": "K", "expires_in_s": 180})))
    result = console.bili_repo_login_qr()
    assert result["ok"] is True and result["key"] == "K"
    assert "qr/image" in result["image"]