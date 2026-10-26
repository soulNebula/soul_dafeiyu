# -*- coding: utf-8 -*-
"""P7-b/backlog：发送结果统一口径（sendclass）与"假成功"回归测试。

锁住三件事：
1. ``classify_send_result`` 七类语义（含窗口兜底 verified=False → unverified）；
2. ``/status`` 的 sent 只统计 delivered，新增 unverified/clipboard/planned 单列；
3. ``proactive_worker`` 的 out.* 与 /status 同口径；unverified **不重置熔断**。
"""
from __future__ import annotations

import json

import pytest

from agi.selflearn import ticker
from agi.wechat_gateway import drafts
from agi.wechat_gateway import proactive_worker
from agi.wechat_gateway import sendclass
from agi.wechat_gateway import server as server_mod
from agi.wechat_gateway import win_send
from agi.wechat_gateway.server import Gateway

CONTACT = "好友A"
TOPUI_ERROR = "AttributeError: 'ProfileWnd' object has no attribute 'topui'"


def delivered() -> dict:
    return {"ok": True, "status": "sent"}


def unverified() -> dict:
    """窗口级兜底：粘贴+回车发出去，但无法校验会话。"""
    return {"ok": True, "status": "sent", "verified": False, "method": "window",
            "warning": "窗口级发送无法校验会话，请人工抽查"}


def clipboard() -> dict:
    return {"ok": True, "status": "clipboard", "method": "window",
            "verified": False, "warning": "已复制到剪贴板：请手动粘贴"}


def planned() -> dict:
    return {"ok": True, "status": "planned"}


def blocked() -> dict:
    return {"ok": False, "status": "blocked", "error": "stop_file"}


def confirm_draft() -> dict:
    return {"ok": False, "status": "draft", "draft_id": "D-1", "requires_confirm": True}


def failed_draft() -> dict:
    return {"ok": False, "status": "draft", "draft_id": "D-2",
            "requires_confirm": True, "send_failed": True,
            "error": TOPUI_ERROR, "error_key": TOPUI_ERROR,
            "stage": "ChatWith"}


# --------------------------------------------------------------------------- #
# 1. classify_send_result 语义矩阵（唯一真源）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("result,expected", [
    (delivered(), "delivered"),
    ({"ok": True, "status": "ok"}, "delivered"),
    (unverified(), "unverified"),
    ({"ok": True, "status": "weird"}, "unverified"),      # 未知成功状态：无送达证据
    (clipboard(), "clipboard"),
    ({"ok": False, "status": "clipboard"}, "failed"),     # ok=False 的 clipboard 保守算失败
    (planned(), "planned"),
    ({"ok": False, "status": "planned"}, "failed"),
    (blocked(), "blocked"),
    (confirm_draft(), "draft"),
    (failed_draft(), "failed"),
    ({"ok": False, "status": "failed", "error": "boom"}, "failed"),
    ({"ok": True, "status": "failed"}, "failed"),         # 自相矛盾保守算失败
    (None, "failed"),
    ("weird", "failed"),
])
def test_classify_send_result_matrix(result, expected):
    assert sendclass.classify_send_result(result) == expected


def test_classify_helpers_are_consistent():
    assert sendclass.is_delivered(delivered()) is True
    assert sendclass.is_delivered(unverified()) is False
    assert sendclass.is_failure(failed_draft()) is True
    assert sendclass.is_failure(confirm_draft()) is False
    assert sendclass.is_failure(blocked()) is False
    assert sendclass.is_failure(unverified()) is False


# --------------------------------------------------------------------------- #
# 2. /status：sent 只统计 delivered；非送达单列
# --------------------------------------------------------------------------- #
def _gw(tmp_path, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": [CONTACT], "send_mode": "auto",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0],
    }
    cfg.update(overrides)
    return Gateway(cfg)


class _FakeAgent:
    def __init__(self, *_a, **_k):
        pass

    def send(self, text, image=None):
        return {"bubbles": [text or "回复"], "reply": text or "回复", "turn_id": "T-1"}


def _reply_with_results(g: Gateway, results: list[dict], monkeypatch) -> None:
    monkeypatch.setattr("agi.selflearn.chat.SelfLearnAgent", _FakeAgent)
    queue = list(results)
    monkeypatch.setattr(g, "_send", lambda contact, text, **kw: queue.pop(0))
    for _ in results:                       # 每次 _reply 只发一个气泡
        g._reply({"friend": CONTACT, "text": "在吗"})


def test_reply_counts_by_classification(tmp_path, monkeypatch):
    g = _gw(tmp_path)
    _reply_with_results(g, [delivered(), unverified(), clipboard(), planned(),
                            failed_draft(), blocked(), confirm_draft()], monkeypatch)

    assert g.sent == 1          # 只有 delivered
    assert g.unverified == 1
    assert g.clipboard == 1
    assert g.planned == 1
    assert g.failed == 1        # 只有 failed（draft/blocked 不再虚增 failed）
    status = g.status()
    assert status["sent"] == 1 and status["failed"] == 1
    assert status["unverified"] == 1
    assert status["clipboard"] == 1
    assert status["planned"] == 1
    # outbound 审计里带分类
    classes = [e.get("classification") for e in g.recent if e.get("kind") == "outbound"]
    assert classes == ["delivered", "unverified", "clipboard", "planned",
                       "failed", "blocked", "draft"]


# --------------------------------------------------------------------------- #
# 3. proactive_worker 计数口径与熔断
# --------------------------------------------------------------------------- #
class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "proactive_probability_min": 1.0,
            "proactive_probability_max": 1.0,
            "allowed_contacts": [CONTACT],
            "proactive_contacts": [],
            "media_enabled": False,
            **cfg,
        }
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}
        self.send_results: list[dict] = []

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        return self.send_results.pop(0) if self.send_results else delivered()

    def _record(self, kind, data):
        self.records.append((kind, data))


def _tick(text: str = "在吗") -> dict:
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": [text], "message": text, "turn_id": "T-PRO-1"}}


def _run_cycles(monkeypatch, g, cycles: int, start: float = 100.0):
    texts = iter([f"第{i}句话" for i in range(cycles + 1)])
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick(next(texts)))
    return [proactive_worker.run_cycle(g, client=object(), now=start + i)
            for i in range(cycles)]


def _events(g, kind):
    return [data for k, data in g.records if k == kind]


def test_run_cycle_counts_match_status_semantics(monkeypatch):
    g = FakeGateway()
    g.send_results = [delivered(), unverified(), clipboard(), planned()]
    outs = _run_cycles(monkeypatch, g, 4)

    assert outs[0]["sent"] == 1
    assert outs[1]["sent"] == 0 and outs[1]["unverified"] == 1
    assert outs[2]["sent"] == 0 and outs[2]["clipboard"] == 1
    assert outs[3]["sent"] == 0 and outs[3]["planned"] == 1
    # 4 轮汇总：只有 delivered 计 sent，其余三类单列（与 /status 字段同名）
    assert sum(o["sent"] for o in outs) == 1
    assert sum(o["unverified"] for o in outs) == 1
    assert sum(o["clipboard"] for o in outs) == 1
    assert sum(o["planned"] for o in outs) == 1
    # last_proactive 是最后一次 outcome（第 4 轮 planned）
    last = g.last_proactive
    assert last["planned"] == 1 and last["sent"] == 0
    assert _events(g, "proactive_unverified") and _events(g, "proactive_clipboard")


def test_unverified_does_not_reset_breaker(monkeypatch):
    """窗口兜底 unverified 不能把连续失败计数清零（否则 3 次阈值永远凑不够）。"""
    g = FakeGateway()
    g.send_results = [failed_draft(), failed_draft(), unverified(), failed_draft()]
    outs = _run_cycles(monkeypatch, g, 4)

    assert outs[2]["unverified"] == 1 and outs[2]["sent"] == 0
    snapshot = proactive_worker.breaker_snapshot(g, now=200.0)
    assert snapshot["open"] is True
    opens = _events(g, "proactive_breaker_open")
    assert len(opens) == 1 and opens[0]["consecutive"] == 3


def test_planned_and_clipboard_do_not_reset_breaker(monkeypatch):
    g = FakeGateway()
    g.send_results = [failed_draft(), clipboard(), planned(), failed_draft(),
                      failed_draft()]
    _run_cycles(monkeypatch, g, 5)
    opens = _events(g, "proactive_breaker_open")
    assert len(opens) == 1 and opens[0]["consecutive"] == 3


# --------------------------------------------------------------------------- #
# 4. 窗口兜底 verified=False：/status 与 proactive 都不算成功
# --------------------------------------------------------------------------- #
def test_window_fallback_verified_false_is_unverified(tmp_path, monkeypatch):
    g = _gw(tmp_path, dry_run=False, fallback_enabled=True, fallback_mode="auto")
    monkeypatch.setattr(win_send, "send_via_window", lambda text, cfg: unverified())

    out = g._on_send_failure(CONTACT, "窗口发出去的一句", "wxauto boom")
    assert out["ok"] is True and out["status"] == "sent"
    assert out["classification"] == "unverified"
    assert g.sent_today == 0 and len(g.send_times) == 0     # 不算 sent
    assert drafts.list_all(path=g.draft_path) == []          # 也不落草稿
    assert any(e.get("kind") == "fallback_unverified" for e in g.recent)
    assert sendclass.classify_send_result(out) == "unverified"


def test_window_fallback_verified_true_is_delivered(tmp_path, monkeypatch):
    g = _gw(tmp_path, dry_run=False, fallback_enabled=True, fallback_mode="auto")
    monkeypatch.setattr(win_send, "send_via_window",
                        lambda text, cfg: {"ok": True, "status": "sent",
                                           "verified": True})
    out = g._on_send_failure(CONTACT, "真正发出的一句", "wxauto boom")
    assert out["classification"] == "delivered"
    assert g.sent_today == 1 and len(g.send_times) == 1


def test_window_fallback_unverified_in_proactive_counts(monkeypatch):
    """把 _on_send_failure 的 unverified 结果喂给主动链：不计 sent、不动熔断。"""
    g = FakeGateway()
    g.send_results = [unverified()]
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("hi"))
    out = proactive_worker.run_cycle(g, client=object(), now=100.0)
    assert out["sent"] == 0 and out["unverified"] == 1
    snapshot = proactive_worker.breaker_snapshot(g, now=200.0)
    assert snapshot["consecutive"] == 0 and snapshot["open"] is False


# --------------------------------------------------------------------------- #
# 5. dry-run planned：/status 与 proactive 都单列
# --------------------------------------------------------------------------- #
def test_planned_dry_run_counting(tmp_path, monkeypatch):
    # Gateway 层：dry_run 下 _send 返回 planned → _reply 统计 planned，不计 sent
    g = _gw(tmp_path)                                        # dry_run=True 默认
    _reply_with_results(g, [planned()], monkeypatch)
    assert g.planned == 1 and g.sent == 0
    assert g.status()["planned"] == 1 and g.status()["sent"] == 0

    # proactive 层：planned 结果 → out.planned +1，不计 sent
    fake = FakeGateway()
    fake.send_results = [planned()]
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _tick("hi"))
    out = proactive_worker.run_cycle(fake, client=object(), now=100.0)
    assert out["planned"] == 1 and out["sent"] == 0
    assert fake.last_proactive["planned"] == 1
