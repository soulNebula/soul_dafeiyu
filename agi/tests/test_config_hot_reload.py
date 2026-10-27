# -*- coding: utf-8 -*-
"""P0-22：配置热加载 + 紧急停止开关 + 夜间静默硬保护（今晚事故的防线）。

事故：23:00 后用户要求静默，改了 config.json 的 proactive_enabled=false，
但运行中的 gateway 不热加载，仍在 23:03/23:16 发了两条；验证时又把
quiet_hours 临时设成 [3,4] 导致夜间静默没拦住。
"""
from __future__ import annotations

import io
import json
import os
import time

import pytest

from agi.selflearn import ticker
from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway import proactive_worker
from agi.wechat_gateway.server import Gateway, make_handler


# --------------------------------------------------------------------------- #
# 夹具：配置路径/STOP 路径全部落 tmp（与 config.json 同目录）
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _deterministic_probability(monkeypatch):
    """P35：P33 initiative 会把 base=1.0 的概率乘 drive 系数（约 0.81），
    这些用例验证热加载/STOP 而非概率，固定 random=0 避免 45% 概率性失败。"""
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.0)
    monkeypatch.setattr(proactive_worker.random, "uniform", lambda lo, hi: lo)


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "wechat" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gw_config, "CONFIG_PATH", path)
    gw_config._LAST_MTIME.clear()
    yield path
    gw_config._LAST_MTIME.clear()


def _write_cfg(path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _bump_mtime(path):
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000))


def _gateway(tmp_path, cfg_path, **extra):
    _write_cfg(cfg_path, {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": ["好友A"], "proactive_contacts": ["好友A"],
        "proactive_enabled": True,
        "proactive_probability_min": 1.0, "proactive_probability_max": 1.0,
        "proactive_interval_min_s": 60, "proactive_interval_max_s": 60,
        "quiet_hours": [23, 8],
        "admin_token": "tok",
        **extra,
    })
    cfg = gw_config.load()
    cfg.update({"drafts_path": str(tmp_path / "drafts.jsonl"),
                "log_path": str(tmp_path / "gateway.log")})
    gw = Gateway(cfg)
    # 起始 mtime 视为"已加载"，避免第一次 run_cycle 就触发 reload 事件
    gw_config._LAST_MTIME[str(cfg_path)] = int(cfg_path.stat().st_mtime_ns)
    return gw


def _tick(turn_id="T-PRO-1", text: str = "在吗"):
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": [text], "message": text,
                          "turn_id": turn_id}}


def _events(gateway, kind):
    return [e for e in gateway.recent if e.get("kind") == kind]


def _patch_send(gw):
    """替换真实发送为成功桩：run_cycle 只验证"是否真的去发"，不连微信。

    注意：dry_run 的 ``planned`` 在 P0-7 口径里不算送达（sent 只统计 sent/ok），
    所以这里显式返回 status=sent。
    """
    calls: list[tuple] = []

    def fake_send(contact, text, **kwargs):
        calls.append((contact, text, kwargs))
        return {"ok": True, "status": "sent"}

    gw._send = fake_send
    return calls


# --------------------------------------------------------------------------- #
# 1. 配置热加载
# --------------------------------------------------------------------------- #
def test_hot_reload_disables_then_reenables_without_restart(cfg_path, tmp_path, monkeypatch):
    texts = iter(["在吗", "还在吗", "又来啦"])
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: _tick(text=next(texts)))
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db",
                        lambda contact: tmp_path / "c.sqlite3")
    gw = _gateway(tmp_path, cfg_path, quiet_hours=[0, 0])
    sent_calls = _patch_send(gw)
    out = proactive_worker.run_cycle(gw, client=object(), now=100.0)
    assert len(sent_calls) == 1 and out["sent"] == 1

    # 运行中改 config.json → 下一轮立刻生效（不再发）
    base = {"proactive_enabled": True, "allowed_contacts": ["好友A"],
            "proactive_contacts": ["好友A"], "quiet_hours": [0, 0],
            "proactive_probability_min": 1.0, "proactive_probability_max": 1.0,
            "proactive_interval_min_s": 60, "proactive_interval_max_s": 60}
    _write_cfg(cfg_path, {**base, "proactive_enabled": False, "dropped_marker": "x"})
    _bump_mtime(cfg_path)
    out = proactive_worker.run_cycle(gw, client=object(), now=200.0)
    assert out["ran"] is False and out["reason"] == "proactive_disabled"
    assert gw.cfg["proactive_enabled"] is False
    reloaded = _events(gw, "config_reloaded")
    assert reloaded and "proactive_enabled" in reloaded[0]["changed"]

    # 改回 true → 再次生效
    _write_cfg(cfg_path, {**base, "proactive_enabled": True})
    _bump_mtime(cfg_path)
    out = proactive_worker.run_cycle(gw, client=object(), now=300.0)
    assert len(sent_calls) == 2 and gw.cfg["proactive_enabled"] is True

    # mtime 不变时不重复写 config_reloaded
    before = len(_events(gw, "config_reloaded"))
    proactive_worker.run_cycle(gw, client=object(), now=400.0)
    assert len(_events(gw, "config_reloaded")) == before


def test_hot_reload_broken_config_keeps_old_and_warns(cfg_path, tmp_path, monkeypatch):
    texts = iter(["在吗", "还在吗", "又来啦"])
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: _tick(text=next(texts)))
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db",
                        lambda contact: tmp_path / "c.sqlite3")
    gw = _gateway(tmp_path, cfg_path, quiet_hours=[0, 0])
    _patch_send(gw)
    cfg_path.write_text("{broken json", encoding="utf-8")
    _bump_mtime(cfg_path)

    out = proactive_worker.run_cycle(gw, client=object(), now=100.0)
    assert out["ok"] is True                     # 不崩
    assert gw.cfg["proactive_enabled"] is True   # 沿用旧配置
    assert _events(gw, "config_reload_failed")   # warning 事件


# --------------------------------------------------------------------------- #
# 2. 紧急停止开关
# --------------------------------------------------------------------------- #
def test_stop_file_blocks_sends_and_is_throttled(cfg_path, tmp_path, monkeypatch):
    texts = iter(["在吗", "还在吗", "又来啦"])
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: _tick(text=next(texts)))
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db",
                        lambda contact: tmp_path / "c.sqlite3")
    gw = _gateway(tmp_path, cfg_path, quiet_hours=[0, 0])
    sent_calls = _patch_send(gw)
    gw.set_stop(True)
    assert cfg_path.parent.joinpath("STOP").exists()

    out = proactive_worker.run_cycle(gw, client=object(), now=100.0)
    assert out["ran"] is False and out["reason"] == "stopped"
    assert len(_events(gw, "proactive_stopped")) == 1

    # 10 分钟节流：短时间内不再写
    proactive_worker.run_cycle(gw, client=object(), now=130.0)
    assert len(_events(gw, "proactive_stopped")) == 1
    # 超过 10 分钟再写一条
    proactive_worker.run_cycle(gw, client=object(), now=100.0 + 700)
    assert len(_events(gw, "proactive_stopped")) == 2

    # STOP 文件也拦 _send（reactive/force 都不例外）
    blocked = Gateway._send(gw, "好友A", "还在吗", force=True)
    assert blocked["status"] == "blocked" and blocked["error"] == "stop_file"
    # /status 显示 stopped
    assert gw.status()["proactive"]["stopped"] is True

    # 删除 STOP → 恢复
    gw.set_stop(False)
    assert not cfg_path.parent.joinpath("STOP").exists()
    out = proactive_worker.run_cycle(gw, client=object(), now=2000.0)
    assert len(sent_calls) == 1
    assert gw.status()["proactive"]["stopped"] is False


def test_stop_resume_api_auth_and_idempotent(cfg_path, tmp_path):
    gw = _gateway(tmp_path, cfg_path)
    Handler = make_handler(gw)
    stop_file = cfg_path.parent / "STOP"

    def call(path, token="tok"):
        raw = b"{}"
        handler = Handler.__new__(Handler)
        handler.path = path
        handler.command = "POST"
        handler.requestline = f"POST {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.rfile = io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw)), "Host": "127.0.0.1:801"}
        if token is not None:
            handler.headers["X-Admin-Token"] = token
        handler.do_POST()
        head, _, payload = handler.wfile.getvalue().decode("utf-8").partition("\r\n\r\n")
        status = int(head.split(" ")[1]) if head else 0
        return status, (json.loads(payload) if payload.strip() else None)

    status, data = call("/api/proactive/stop", token=None)
    assert status == 401 and not stop_file.exists()

    status, data = call("/api/proactive/stop")
    assert status == 200 and data["ok"] is True and data["stopped"] is True
    assert stop_file.exists()
    status, data = call("/api/proactive/stop")     # 幂等
    assert status == 200 and data["stopped"] is True

    status, data = call("/api/proactive/resume")
    assert status == 200 and data["stopped"] is False
    assert not stop_file.exists()
    status, data = call("/api/proactive/resume")   # 幂等
    assert status == 200 and data["stopped"] is False
    assert any(e["kind"] == "proactive_stop_toggled" for e in gw.recent)


# --------------------------------------------------------------------------- #
# 3. 夜间静默硬保护
# --------------------------------------------------------------------------- #
def test_quiet_hours_hard_blocks_even_with_empty_list(cfg_path, tmp_path, monkeypatch):
    gw = _gateway(tmp_path, cfg_path)
    gw.cfg["quiet_hours"] = []
    # 空列表 = 非法配置 → 回退默认 [23,8]，并记警告
    assert gw._quiet_hours_value() == [23, 8]
    assert _events(gw, "quiet_hours_invalid")
    # 23:00 命中静默 → 硬拦（P6-b：把"现在"钉死到 23:00，避免白天跑测试时
    # _send 内部 time.time() 取到非静默时段导致用例随墙钟漂移）
    night = time.mktime((2026, 10, 25, 23, 0, 0, 0, 0, -1))
    monkeypatch.setattr("agi.wechat_gateway.server.time.time", lambda: night)
    assert gw._quiet_blocked(now=night) is True
    out = gw._send("好友A", "晚上好", proactive=True)
    assert out["status"] == "blocked"


def test_quiet_hours_is_last_gate_even_if_probability_passes(cfg_path, tmp_path):
    gw = _gateway(tmp_path, cfg_path, quiet_hours=[0, 24])   # 全天静默，任意时刻都命中
    assert gw._quiet_blocked() is True
    out = gw._send("好友A", "在吗", proactive=True)
    assert out["status"] == "blocked" and out["error"] == "quiet_hours"
    assert any(e["kind"] == "send_blocked" and e.get("reason") == "quiet_hours"
               for e in gw.recent)


def test_quiet_override_allows_temporarily_and_expires(cfg_path, tmp_path):
    gw = _gateway(tmp_path, cfg_path, quiet_hours=[0, 24],
                  proactive_override_quiet=True)
    out = gw._send("好友A", "测试覆盖", proactive=True)
    assert out["status"] != "blocked"        # 覆盖期内允许（dry_run → planned）
    assert len(_events(gw, "quiet_override")) == 1

    # 24h 后自动失效
    gw._override_started_ts = time.time() - 86401
    out = gw._send("好友A", "覆盖已过期", proactive=True)
    assert out["status"] == "blocked" and out["error"] == "quiet_hours"
    assert _events(gw, "quiet_override_expired")
