# -*- coding: utf-8 -*-
"""P3-d：主动消息接内状态（slim InnerState → proactive prompt + behavior 落地）。

覆盖：
- fake slim client → prompt 含 <inner_state>，每 tick 只调一次；
- behavior.ask=false → 不提问；length=short → ≤12 字；advice=false → 无建议句式；
- 超时/垃圾 JSON/500 → rule 回退、tick 成功、fallback 节流；
- proactive_inner=off / inner_mode=off → prompt 不含内状态（字节不变）；
- 20 tick 注入率 ≥90%。
"""
from __future__ import annotations

import json
import time

import pytest

from agi.selflearn import db, proactive, trust

T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _isolate_inner_log(tmp_path, monkeypatch):
    """测试不写生产 inner.log；每个测试独立日志文件。"""
    from agi.inner import inject
    monkeypatch.setattr(inject, "INNER_LOG_PATH", tmp_path / "inner.log")
    monkeypatch.setattr(proactive, "_INNER_FALLBACK_LAST", 0.0)


def _slim(behavior: dict | None = None) -> str:
    return json.dumps({
        "stance": "简短",
        "intent": "自然接话",
        "monologue": "不想多说",
        "behavior": behavior or {"length": "normal", "ask": True,
                                "advice": True, "emoji": "偶尔"},
    }, ensure_ascii=False)


class Gen:
    def __init__(self, text: str = "今天有点累，你最近怎么样？"):
        self.text = text
        self.prompts: list[str] = []
        self.calls = 0

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(str(messages[-1].get("content") or ""))
        return {"ok": True, "text": self.text}


class SlimInner:
    local_soul = True
    base_url = "fake-slim"

    def __init__(self, text: str | None = None, error: Exception | None = None):
        self.text = _slim() if text is None else text
        self.error = error
        self.calls = 0

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"ok": True, "text": self.text}


def _seed(tmp_path):
    path = tmp_path / "p.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "周末去爬山了，累死", T0 - 6 * 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}, ensure_ascii=False), T0))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    return path


def _enable(monkeypatch):
    monkeypatch.setenv("INNER_MODE", "model")
    monkeypatch.setenv("PROACTIVE_INNER", "force")
    monkeypatch.setenv("INNER_MODE_SCOPE", "all")
    monkeypatch.setenv("INNER_SHADOW_ONLY", "0")


def _tick(path, *, gen, inner, now=T0):
    old = proactive.build_candidates
    proactive.build_candidates = lambda p, n=None: [{
        "kind": "care", "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": "", "topic": "",
    }]
    try:
        return proactive.tick(path, client=gen, inner_client=inner,
                              now=now, cooldown=0, min_score=0,
                              respect_state=False)
    finally:
        proactive.build_candidates = old


def test_slim_inner_injected_and_single_call(tmp_path, monkeypatch):
    _enable(monkeypatch)
    path = _seed(tmp_path)
    gen, inner = Gen("资料还没理完，有点烦"), SlimInner()
    row = _tick(path, gen=gen, inner=inner)
    assert row["available"] is True
    assert inner.calls == 1
    assert row["inner"]["injected"] is True
    assert row["inner"]["source"] == "local"
    assert row["inner"]["mode"] == "model"
    assert any("<inner_state>" in prompt for prompt in gen.prompts)
    assert row["inner"]["behavior"]["length"] == "normal"


def test_ask_false_forces_no_question(tmp_path, monkeypatch):
    _enable(monkeypatch)
    path = _seed(tmp_path)
    inner = SlimInner(_slim({"length": "normal", "ask": False,
                             "advice": True, "emoji": "偶尔"}))
    gen = Gen("今天有点累，你最近还好吗？")
    row = _tick(path, gen=gen, inner=inner)
    assert row["available"] is True
    assert not row["message"].strip().endswith(("？", "?"))
    assert "？" not in row["message"] and "?" not in row["message"]


def test_length_short_and_advice_false(tmp_path, monkeypatch):
    _enable(monkeypatch)
    path = _seed(tmp_path)
    inner = SlimInner(_slim({"length": "short", "ask": True,
                             "advice": False, "emoji": "不用"}))
    gen = Gen("今天有点累，建议你早点睡，明天再处理那些资料")
    row = _tick(path, gen=gen, inner=inner)
    assert row["available"] is True
    assert len(row["message"]) <= 12, row["message"]
    assert "建议" not in row["message"]


def test_inner_timeout_falls_back_and_tick_succeeds(tmp_path, monkeypatch):
    _enable(monkeypatch)
    path = _seed(tmp_path)
    inner = SlimInner(error=TimeoutError("timeout"))
    gen = Gen("资料还没理完，有点烦")
    row = _tick(path, gen=gen, inner=inner)
    assert row["available"] is True
    assert row["inner"]["source"] == "rule"
    assert row["inner"]["fallback"] != ""
    assert row["inner"]["injected"] is True   # 回退 rule 块仍可注入 prompt
    assert row["message"]


def test_inner_garbage_falls_back(tmp_path, monkeypatch):
    _enable(monkeypatch)
    path = _seed(tmp_path)
    inner = SlimInner("not json")
    row = _tick(path, gen=Gen("资料还没理完，有点烦"), inner=inner)
    assert row["available"] is True
    assert row["inner"]["source"] == "rule"
    assert row["inner"]["fallback"] in ("bad_json", "invalid_state", "empty_json")


def test_proactive_inner_off_keeps_prompt_clean(tmp_path, monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setenv("PROACTIVE_INNER", "off")
    path = _seed(tmp_path)
    gen, inner = Gen("资料还没理完，有点烦"), SlimInner()
    row = _tick(path, gen=gen, inner=inner)
    assert row["available"] is True
    assert inner.calls == 0
    assert row["inner"]["mode"] == "off"
    assert all("<inner_state>" not in prompt for prompt in gen.prompts)


def test_inner_mode_off_keeps_prompt_byte_identical(monkeypatch):
    monkeypatch.setenv("INNER_MODE", "off")
    for kind in proactive.KINDS:
        prompt = proactive._kind_prompt(
            kind, {"kind": kind, "anchor": "", "topic": ""},
            0.55, 0.45, "用户:在吗")
        assert "<inner_state>" not in prompt
        assert prompt == proactive._kind_prompt(
            kind, {"kind": kind, "anchor": "", "topic": ""},
            0.55, 0.45, "用户:在吗", inner_ctx="")


def test_twenty_ticks_injection_rate(tmp_path, monkeypatch):
    _enable(monkeypatch)
    path = _seed(tmp_path)
    gen, inner = Gen("资料还没理完，有点烦"), SlimInner()
    rows = [_tick(path, gen=gen, inner=inner, now=T0 + index * 180)
            for index in range(20)]
    available = [row for row in rows if row.get("available")]
    assert len(available) >= 18
    injected = sum(1 for row in available if (row.get("inner") or {}).get("injected"))
    assert injected / len(available) >= 0.90
    assert inner.calls == len(available)   # 每 tick 只调一次


def test_proactive_inner_fallback_throttled(monkeypatch, tmp_path):
    monkeypatch.setattr(proactive, "_INNER_FALLBACK_LAST", 0.0)
    from agi.inner import inject
    monkeypatch.setattr(inject, "INNER_LOG_PATH", tmp_path / "inner.log")
    first = proactive._log_proactive_inner_fallback("timeout", now=1000.0)
    second = proactive._log_proactive_inner_fallback("timeout", now=1100.0)
    assert first is True
    assert second is False
    lines = (tmp_path / "inner.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "proactive_inner_fallback" in lines[0]
