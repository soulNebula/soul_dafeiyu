# -*- coding: utf-8 -*-
"""P11 B站页后端：扫码登录/凭证写入/状态/配置/日报（tmp home，HTTP 全 mock，不联网）。"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "console_server_bili", ROOT / "tools" / "console" / "server.py")
console = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(console)

POLL_CODES = {"waiting": 86101, "scanned": 86090, "expired": 86038, "ok": 0}


def _fake_http(url, headers=None):
    if "qrcode/generate" in url:
        return {"code": 0, "data": {
            "url": "https://passport.bilibili.com/login?SESSDATA=sess-abc&bili_jct=jct-xyz"
                   "&DedeUserID=12345&buvid3=buv-999&gourl=x",
            "qrcode_key": "KEY-123"}}
    if "qrcode/poll" in url:
        code = _fake_http.poll_code
        data = {"code": code}
        if code == 0:
            data["url"] = ("https://passport.bilibili.com/login?SESSDATA=sess-abc"
                           "&bili_jct=jct-xyz&DedeUserID=12345&buvid3=buv-999")
        return {"code": 0, "data": data}
    if "web-interface/nav" in url:
        cookie = str((headers or {}).get("Cookie") or "")
        if "sessdata=sess-abc" in cookie.lower() or "SESSDATA=sess-abc" in cookie:
            return {"code": 0, "data": {"isLogin": True, "uname": "测试UP", "mid": 12345}}
        return {"code": -101, "data": {"isLogin": False}}
    raise AssertionError("unexpected url: " + url)


@pytest.fixture
def bili_env(tmp_path, monkeypatch):
    home = tmp_path / "bili-duty"
    (home / "out").mkdir(parents=True)
    config = {"perDay": 3, "keywords": ["猫"], "peakValley": True,
              "unknown_keep": {"hello": 1}}
    (home / "config.json").write_text(json.dumps(config, ensure_ascii=False),
                                      encoding="utf-8")
    today = time.strftime("%Y-%m-%d")
    (home / "state.json").write_text(json.dumps({
        "day": today, "count": 2, "picked": 1,
        "windows": {today + ":noon": True}, "lastRunAt": "2026-10-27T01:00:00.000Z",
        "lastWhy": "noon"}, ensure_ascii=False), encoding="utf-8")
    (home / "out" / f"{today}.md").write_text("# 今日日报\n\n挑了 1 条：猫猫视频。\n",
                                              encoding="utf-8")
    monkeypatch.setenv("BILI_DUTY_HOME", str(home))
    monkeypatch.setattr(console, "BILI_QR_PNG", tmp_path / "bili_login_qr.png")
    monkeypatch.setattr(console, "AUDIT_PATH", tmp_path / "audit.log")
    monkeypatch.setattr(console, "_bili_http_json", _fake_http)
    _fake_http.poll_code = POLL_CODES["waiting"]
    console._BILI_NAV_CACHE.update(ts=0.0, data=None)
    return {"home": home, "tmp": tmp_path, "today": today}


def _audit_actions() -> list[str]:
    try:
        lines = console.AUDIT_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [json.loads(line).get("action") for line in lines if line.strip()]


def test_bili_qr_generate_renders_png(bili_env):
    result = console.bili_login_qr()
    assert result["ok"] is True and result["key"] == "KEY-123"
    assert result["expires_in"] == 180
    png = Path(console.BILI_QR_PNG)
    assert png.is_file()
    assert png.read_bytes().startswith(b"\x89PNG")


def test_bili_qr_network_error(bili_env, monkeypatch):
    def boom(url, headers=None):
        raise RuntimeError("timeout")

    monkeypatch.setattr(console, "_bili_http_json", boom)
    result = console.bili_login_qr()
    assert result["ok"] is False and result["error"] == "network"


def test_bili_poll_states_and_login_writes_credential(bili_env):
    _fake_http.poll_code = POLL_CODES["waiting"]
    assert console.bili_login_poll("KEY-123")["status"] == "waiting"
    _fake_http.poll_code = POLL_CODES["scanned"]
    assert console.bili_login_poll("KEY-123")["status"] == "scanned"
    _fake_http.poll_code = POLL_CODES["expired"]
    assert console.bili_login_poll("KEY-123")["status"] == "expired"

    _fake_http.poll_code = POLL_CODES["ok"]
    result = console.bili_login_poll("KEY-123")
    assert result["ok"] is True and result["status"] == "ok"
    assert result["uname"] == "测试UP" and result["mid"] == 12345
    credential_path = bili_env["home"] / "bili_credential.json"
    saved = json.loads(credential_path.read_text(encoding="utf-8"))
    assert saved == {"sessdata": "sess-abc", "bili_jct": "jct-xyz",
                     "buvid3": "buv-999", "dedeuserid": "12345"}
    assert "bili_login" in _audit_actions()

    # 凭证写入后 MCP 侧即视为已登录
    status = console.bili_status()
    assert status["logged_in"] is True and status["uname"] == "测试UP"


def test_bili_status_merges_config_state_and_report(bili_env):
    status = console.bili_status()
    assert status["ok"] is True
    assert status["logged_in"] is False
    assert status["home"] == str(bili_env["home"])
    assert status["config"]["perDay"] == 3
    assert status["config"]["keywords"] == ["猫"]
    assert status["state"]["count"] == 2 and status["state"]["picked"] == 1
    assert status["state"]["lastWhy"] == "noon"
    assert status["report"]["exists"] is True
    assert "今日日报" in status["report"]["preview"]


def test_bili_config_get_update_validation(bili_env):
    state = console.bili_config_state()
    assert state["ok"] is True
    assert state["config"]["perDay"] == 3
    assert "unknown_keep" in state["unknown_keys"]
    assert len(state["fields"]) >= 20

    result = console.bili_config_update({"perDay": 7, "peakValley": False,
                                         "keywords": ["猫", "编程"]})
    assert result["ok"] is True
    assert result["config"]["perDay"] == 7 and result["config"]["peakValley"] is False
    saved = json.loads((bili_env["home"] / "config.json").read_text(encoding="utf-8"))
    assert saved["perDay"] == 7 and saved["keywords"] == ["猫", "编程"]
    assert saved["unknown_keep"] == {"hello": 1}       # 未知键保留
    assert "bili_config_update" in _audit_actions()

    bad = console.bili_config_update({"bogus_key": 1})
    assert bad["ok"] is False and any("not_allowed:bogus_key" in error for error in bad["errors"])
    wrong_type = console.bili_config_update({"perDay": "abc"})
    assert wrong_type["ok"] is False
    too_many = console.bili_config_update({"perDay": 999})
    assert too_many["ok"] is False
    wrong_mode = console.bili_config_update({"dmMode": "spam"})
    assert wrong_mode["ok"] is False


def test_bili_report_and_errors(bili_env, monkeypatch):
    report = console.bili_report()
    assert report["ok"] is True and "今日日报" in report["content"]
    assert console.bili_report("2020-01-01")["error"] == "no_report"
    assert console.bili_report("../../etc/passwd")["error"] == "bad_date"

    monkeypatch.setenv("BILI_DUTY_HOME", str(bili_env["tmp"] / "missing-home"))
    missing = console.bili_status()
    assert missing["ok"] is False and missing["error"] == "bili_home_missing"
    assert console.bili_report()["error"] == "bili_home_missing"
