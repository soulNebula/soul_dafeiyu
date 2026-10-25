# -*- coding: utf-8 -*-
"""P0-7：剪贴板兜底（status="clipboard"）不是"发送成功"。

``win_send.send_via_window`` 人工协助模式只把文本放进剪贴板（等人手动粘贴），
返回 ``{ok: True, status: "clipboard"}`` —— **内容已保留但没送达**。旧逻辑
「ok 且 status != draft → sent + 重置熔断」会把它当成功：失败率被稀释成 0%，
连续失败里夹一次 clipboard 就把熔断计数清零。

本文件锁住 P0-7 口径：
- clipboard → 不计 ``out.sent``、不重置熔断、单列 ``out.clipboard`` +
  ``proactive_clipboard`` 事件；
- 真正送达 = ``ok=True`` 且 ``status ∈ {sent, ok}``；
- ``draft + send_failed``（P0-4 真失败落草稿）语义不受影响；
- 失败率 = failed + draft(send_failed) + clipboard。
"""
from __future__ import annotations

import pytest

from agi.selflearn import ticker
from agi.wechat_gateway import drafts, proactive_worker
from agi.wechat_gateway import server as server_mod
from agi.wechat_gateway import win_send
from agi.wechat_gateway.server import Gateway

CONTACT = "好友A"
CLIPBOARD = {"ok": True, "status": "clipboard", "method": "window",
             "verified": False,
             "warning": "已复制到剪贴板：请切到目标聊天手动粘贴后发送"}
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


def _draft_failed(addr: str = "7f00aa11", *, error_key: str | None = None) -> dict:
    """P0-4 真失败落草稿的返回形态（内容保住但仍算失败）。"""
    error = TOPUI_ERROR.format(addr=addr)
    return {
        "ok": False, "status": "draft", "draft_id": "D-1",
        "requires_confirm": True, "send_failed": True,
        "error": error, "stage": "ChatWith",
        "error_key": error_key or proactive_worker.normalize_error(error),
    }


def _events(gateway, kind: str) -> list[dict]:
    return [data for k, data in gateway.records if k == kind]


def _run_cycles(monkeypatch, g, cycles: int, start: float = 100.0):
    texts = iter([f"第{i}句话" for i in range(cycles + 1)])
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick(next(texts)))
    return [proactive_worker.run_cycle(g, client=object(), now=start + i)
            for i in range(cycles)]


# --------------------------------------------------------------------------- #
# 1. clipboard：不计 sent、单列、写 proactive_clipboard
# --------------------------------------------------------------------------- #
def test_clipboard_not_counted_as_sent_and_logs_event(monkeypatch):
    g = FakeGateway()
    g.send_results = [dict(CLIPBOARD)]
    out = _run_cycles(monkeypatch, g, 1)[0]

    assert out["sent"] == 0, out
    assert out["drafts"] == 0
    assert out["clipboard"] == 1
    assert out["breaker_open"] is False
    # 没送达 → 不算活动，last_proactive 也要单列 clipboard
    assert g.last_proactive["clipboard"] == 1
    assert g.last_proactive["sent"] == 0

    events = _events(g, "proactive_clipboard")
    assert len(events) == 1, g.records
    assert events[0]["friend"] == CONTACT
    assert events[0]["status"] == "clipboard"
    assert "剪贴板" in events[0]["warning"]
    # proactive_send 日志也保留 status=clipboard + warning（统计口径用）
    sends = _events(g, "proactive_send")
    assert sends and sends[0]["status"] == "clipboard"
    assert "剪贴板" in sends[0]["warning"]


# --------------------------------------------------------------------------- #
# 2. clipboard 不重置熔断：随后 draft+send_failed 仍能凑够 3 次
# --------------------------------------------------------------------------- #
def test_clipboard_does_not_reset_breaker_then_draft_opens(monkeypatch):
    g = FakeGateway()
    g.send_results = [
        _draft_failed("7f000001"), _draft_failed("7f000002"),
        dict(CLIPBOARD), dict(CLIPBOARD), dict(CLIPBOARD),
        _draft_failed("7f000003"),
    ]
    outs = _run_cycles(monkeypatch, g, 5)

    # 前两次失败：consecutive=2，未熔断
    snapshot = proactive_worker.breaker_snapshot(g, now=104.5)
    assert snapshot["open"] is False
    assert snapshot["consecutive"] == 2

    # 3 次 clipboard：不计 sent、不清零既有失败计数
    for out in outs[2:5]:
        assert out["sent"] == 0 and out["clipboard"] == 1
    assert proactive_worker.breaker_snapshot(g, now=104.5)["consecutive"] == 2
    assert len(_events(g, "proactive_clipboard")) == 3
    assert _events(g, "proactive_breaker_open") == []

    # 第 6 轮：同一 error_key 的第 3 次真失败 → 熔断
    _run_cycles(monkeypatch, g, 1, start=105.0)
    opens = _events(g, "proactive_breaker_open")
    assert len(opens) == 1, g.records
    assert opens[0]["consecutive"] == 3
    assert "topui" in opens[0]["error_key"]
    assert opens[0]["status"] == "draft" and opens[0]["send_failed"] is True
    assert proactive_worker.breaker_snapshot(g, now=130.0)["open"] is True


# --------------------------------------------------------------------------- #
# 3. 真正送达（sent/ok）仍然计 sent 并重置熔断
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("delivered_status", ["sent", "ok"])
def test_delivered_statuses_still_count_and_reset(monkeypatch, delivered_status):
    g = FakeGateway()
    g.send_results = [
        _draft_failed("7f000001"), _draft_failed("7f000002"),
        {"ok": True, "status": delivered_status},
        _draft_failed("7f000003"), _draft_failed("7f000004"),
    ]
    outs = _run_cycles(monkeypatch, g, 5)

    assert outs[2]["sent"] == 1
    assert outs[2]["clipboard"] == 0
    assert _events(g, "proactive_breaker_open") == []
    snapshot = proactive_worker.breaker_snapshot(g, now=200.0)
    assert snapshot["open"] is False and snapshot["consecutive"] == 2


# --------------------------------------------------------------------------- #
# 4. 判定口径：clipboard 既非失败也非送达；P0-4 draft+send_failed 仍是失败
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status,delivered,clipboard,failure", [
    ({"ok": True, "status": "sent"}, True, False, False),
    ({"ok": True, "status": "ok"}, True, False, False),
    ({"ok": True, "status": "clipboard", "warning": "x"}, False, True, False),
    ({"ok": True, "status": "planned"}, False, False, False),
    ({"ok": False, "status": "clipboard"}, False, False, True),   # ok=False → 保守算失败
    ({"ok": False, "status": "draft", "send_failed": True}, False, False, True),
    ({"ok": False, "status": "draft"}, False, False, False),      # confirm 人工草稿
    ({"ok": False, "status": "blocked"}, False, False, False),    # 闸门拦截
])
def test_clipboard_semantics(status, delivered, clipboard, failure):
    assert proactive_worker.is_delivered(status) is delivered
    assert proactive_worker.is_clipboard(status) is clipboard
    assert proactive_worker.is_send_failure(status) is failure


# --------------------------------------------------------------------------- #
# 5. Gateway._send 原样透传 clipboard（不提前改写成 sent/draft）
# --------------------------------------------------------------------------- #
def test_gateway_send_returns_clipboard_unchanged(tmp_path, monkeypatch):
    cfg = {
        "enabled": True,
        "dry_run": False,
        "allowed_contacts": [CONTACT],
        "send_mode": "auto",
        "max_chars": 500,
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "fallback_enabled": True,
        "fallback_mode": "manual",
    }
    g = Gateway(cfg)
    # 真实发送入口走不通（venv 不存在）→ 触发 fallback；fallback 返回 clipboard
    monkeypatch.setattr(server_mod, "WXAUTO_PYTHON", tmp_path / "missing-python.exe")
    monkeypatch.setattr(win_send, "send_via_window",
                        lambda text, cfg: dict(CLIPBOARD))

    out = g._send(CONTACT, "兜底一句", reactive=True)

    assert out["ok"] is True and out["status"] == "clipboard"
    assert out["method"] == "window" and out["verified"] is False
    assert g.sent_today == 0          # clipboard 不计 sent
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1 and rows[0]["reason"] == "fallback_clipboard"
