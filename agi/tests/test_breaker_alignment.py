# -*- coding: utf-8 -*-
"""P0-6：熔断与 P0-4 draft 语义对齐。

P0-4 把真实发送失败从 ``status=failed`` 改成了
``status=draft + send_failed=True + error_key=...``（内容进草稿、不丢、不重复发）。
但 P0-2 的 ``is_send_failure()`` 只把 ``failed`` 当失败、把 ``draft`` 当非故障 ——
topui 连续失败不再触发熔断。本文件锁住对齐后的口径：

- ``status == "failed"`` → 失败；
- ``status == "draft"`` 且 ``send_failed`` 为真 → 失败（参与熔断计数）；
- ``status == "draft"`` 无 ``send_failed``（confirm 人工草稿）→ 非故障；
- ``blocked``（闸门拦截）→ 非故障；
- 熔断按 ``error_key``（P0-4 写入，缺省回退归一化 error 前 60 字）计数。
"""
from __future__ import annotations

import pytest

from agi.selflearn import ticker
from agi.wechat_gateway import proactive_worker

CONTACT = "好友A"
TOPUI_ERROR = ("AttributeError: 'ProfileWnd' object has no attribute 'topui' "
               "at 0x{addr}")


@pytest.fixture(autouse=True)
def _contact_db(tmp_path, monkeypatch):
    """contact_db 固定到 tmp（run_cycle 会写 proactive_events）。"""
    path = tmp_path / "contact.sqlite3"
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", lambda contact: path)
    return path


class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            # 概率闸门恒开，只测熔断
            "proactive_probability_min": 1.0,
            "proactive_probability_max": 1.0,
            "allowed_contacts": [CONTACT],
            "proactive_contacts": [],
            **cfg,
        }
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}
        self.send_results: list[dict] = []

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        if self.send_results:
            return self.send_results.pop(0)
        return {"ok": True, "status": "sent"}

    def _record(self, kind, data):
        self.records.append((kind, data))


def _tick(text: str, turn_id: str = "T-PRO-1") -> dict:
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": [text], "message": text,
                          "turn_id": turn_id}}


def _draft_failed(addr: str = "7f00aa11", *, error_key: str | None = None,
                  stage: str = "ChatWith") -> dict:
    """P0-4 真失败落草稿的返回形态（server._on_send_failure 会写 error_key）。"""
    error = TOPUI_ERROR.format(addr=addr)
    status = {
        "ok": False, "status": "draft", "draft_id": "D-1",
        "requires_confirm": True, "send_failed": True,
        "error": error, "stage": stage,
        "error_key": error_key or proactive_worker.normalize_error(error),
    }
    return status


def _confirm_draft() -> dict:
    """P0-4 之前/confirm 模式的普通草稿：不应算失败。"""
    return {"ok": False, "status": "draft", "draft_id": "D-2",
            "requires_confirm": True}


def _events(gateway, kind: str) -> list[dict]:
    return [data for k, data in gateway.records if k == kind]


def _run_cycles(monkeypatch, g, cycles: int, start: float = 100.0):
    texts = iter([f"第{i}句话" for i in range(cycles + 1)])
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick(next(texts)))
    outs = []
    for i in range(cycles):
        outs.append(proactive_worker.run_cycle(g, client=object(), now=start + i))
    return outs


# --------------------------------------------------------------------------- #
# 判定函数本身
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,expected", [
    ({"ok": False, "status": "failed", "error": "x"}, True),
    ({"ok": False, "status": "draft", "send_failed": True, "draft_id": "D"}, True),
    ({"ok": False, "status": "draft", "send_failed": False}, False),
    ({"ok": False, "status": "draft"}, False),                 # 旧 confirm 草稿
    ({"ok": False, "status": "blocked", "error": "quiet_hours"}, False),
    ({"ok": False, "status": "clipboard"}, True),              # 未知状态保守算失败
    ({"ok": True, "status": "sent"}, False),
    (None, True),
])
def test_is_send_failure_semantics(status, expected):
    assert proactive_worker.is_send_failure(status) is expected


# --------------------------------------------------------------------------- #
# draft + send_failed 连续 3 次 → 熔断
# --------------------------------------------------------------------------- #
def test_three_draft_send_failed_open_breaker(monkeypatch):
    g = FakeGateway()
    # 同一句 topui 错误，只是对象地址不同 → 归一化后是同一个 error_key
    g.send_results = [_draft_failed("7f00aa11"), _draft_failed("7f00bb22"),
                      _draft_failed("7f00cc33")]
    outs = _run_cycles(monkeypatch, g, 3)
    assert all(out["sent"] == 0 for out in outs)
    assert all(out["drafts"] == 1 for out in outs)  # 每次都是 draft 兜底

    opens = _events(g, "proactive_breaker_open")
    assert len(opens) == 1, g.records
    event = opens[0]
    assert event["consecutive"] == 3
    assert "topui" in event["error_key"]
    assert "0x7f00" not in event["error_key"]      # 地址已归一化
    assert event["status"] == "draft"              # 失败形态是 draft...
    assert event["send_failed"] is True            # ...真正证据是 send_failed
    assert event["stage"] == "ChatWith"
    assert proactive_worker.breaker_snapshot(g, now=130.0)["open"] is True
    # proactive_send 日志也要带失败证据，否则按 gateway.log 统计会把失败 draft 当人工草稿
    sends = _events(g, "proactive_send")
    assert sends and sends[0]["status"] == "draft"
    assert sends[0]["send_failed"] is True
    assert "topui" in sends[0]["error_key"]
    assert sends[0]["stage"] == "ChatWith"

    # 第 4 轮：熔断期内不发送、只记 proactive_breaker_open
    sends_before = len(g.sent)
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("第4句话"))
    out4 = proactive_worker.run_cycle(g, client=object(), now=200.0)
    assert out4["ran"] is False and out4["reason"] == "breaker_open"
    assert len(g.sent) == sends_before
    assert len(_events(g, "proactive_breaker_open")) == 2


def test_old_confirm_draft_is_not_failure(monkeypatch):
    """无 send_failed 的 draft（P0-4 之前语义）不计失败、不熔断。"""
    g = FakeGateway()
    g.send_results = [_confirm_draft() for _ in range(5)]
    outs = _run_cycles(monkeypatch, g, 5)
    assert all(out["drafts"] == 1 for out in outs)
    assert _events(g, "proactive_breaker_open") == []
    snapshot = proactive_worker.breaker_snapshot(g, now=200.0)
    assert snapshot["open"] is False and snapshot["consecutive"] == 0


def test_two_different_error_keys_do_not_open_breaker(monkeypatch):
    g = FakeGateway()
    g.send_results = [
        _draft_failed(error_key="ProfileWnd.topui"),
        _draft_failed(error_key="ProfileWnd.topui"),
        _draft_failed(error_key="Pyperclip.OpenClipboard"),
        _draft_failed(error_key="Pyperclip.OpenClipboard"),
    ]
    _run_cycles(monkeypatch, g, 4)
    assert _events(g, "proactive_breaker_open") == []
    snapshot = proactive_worker.breaker_snapshot(g, now=200.0)
    assert snapshot["open"] is False
    assert snapshot["consecutive"] == 2
    assert snapshot["error_key"] == "Pyperclip.OpenClipboard"


def test_error_key_field_is_preferred_over_raw_text(monkeypatch):
    """不同原文地址/措辞但同 error_key → 必须算同类（P0-4 已写入 error_key）。"""
    g = FakeGateway()
    variants = [
        {"ok": False, "status": "draft", "send_failed": True, "draft_id": "D-1",
         "error": f"variant {i} at 0x{i:08x}", "error_key": "ProfileWnd.topui"}
        for i in range(3)
    ]
    g.send_results = variants
    _run_cycles(monkeypatch, g, 3)
    opens = _events(g, "proactive_breaker_open")
    assert len(opens) == 1 and opens[0]["error_key"] == "ProfileWnd.topui"


def test_success_resets_failure_counter(monkeypatch):
    g = FakeGateway()
    g.send_results = [
        _draft_failed("7f000001"), _draft_failed("7f000002"),
        {"ok": True, "status": "sent"},
        _draft_failed("7f000003"), _draft_failed("7f000004"),
    ]
    outs = _run_cycles(monkeypatch, g, 5)
    assert outs[2]["sent"] == 1
    assert _events(g, "proactive_breaker_open") == []
    snapshot = proactive_worker.breaker_snapshot(g, now=200.0)
    assert snapshot["open"] is False and snapshot["consecutive"] == 2


def test_failed_status_still_works(monkeypatch):
    """旧形态 status=failed 仍然计入熔断（不能为了 P0-4 回退旧语义）。"""
    g = FakeGateway()
    g.send_results = [
        {"ok": False, "status": "failed", "error": TOPUI_ERROR.format(addr="7f00000a")}
        for _ in range(3)
    ]
    _run_cycles(monkeypatch, g, 3)
    opens = _events(g, "proactive_breaker_open")
    assert len(opens) == 1 and opens[0]["status"] == "failed"
