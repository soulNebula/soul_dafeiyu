# -*- coding: utf-8 -*-
"""P29：聊天上下文/拍一拍诊断脚本测试（tmp 库 + mock 日志，不联网）。"""
from __future__ import annotations

import importlib.util
import json
import time
from pathlib import Path

from agi.selflearn import db, memory

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "chat_context_probe", ROOT / "tools" / "chat_context_probe.py")
probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(probe)

DAY = 1_000_000.0


def _db(tmp_path: Path) -> Path:
    base = tmp_path / "contact.sqlite3"
    db.connect(base)
    return base


def _write_log(tmp_path: Path) -> Path:
    log = tmp_path / "gateway.log"
    rows = [
        {"ts": DAY - 60, "kind": "inbound", "friend": "好友A",
         "text": "现在几点了"},
        {"ts": DAY - 50, "kind": "outbound", "friend": "好友A",
         "text": "三点零六"},
        {"ts": DAY - 10, "kind": "inbound", "friend": "好友A",
         "text": '"好友A" 拍了拍我'},
        {"ts": DAY - 5, "kind": "outbound", "friend": "好友A",
         "text": "刚才那句没组织好，你再说一次？"},
        {"ts": DAY - 3, "kind": "inbound", "friend": "别人",
         "text": "别人的消息"},
    ]
    log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                   encoding="utf-8")
    return log


def test_detect_inbound_variants():
    for raw in ('"好友A" 拍了拍我', '"好友A"拍了拍我', "「好友A」拍了拍我",
                "拍了拍你", "我拍了拍你"):
        result = probe.detect_inbound(raw)
        assert result["pat"] is True, raw
        assert result["as_text"].startswith("[拍一拍]"), raw
        assert result["as_system"].startswith("[拍一拍]"), raw

    normal = probe.detect_inbound("今天拍了拍照，光线不错")
    assert normal["pat"] is False
    assert normal["as_text"] == "今天拍了拍照，光线不错"

    quote = probe.detect_inbound("对方引用了这条消息：原话")
    assert quote["quote_hint"] is True
    assert probe.detect_inbound("[引用消息] 原话")["as_system"].startswith("[引用]")


def test_parse_gateway_events_and_guard_fallback(tmp_path):
    log = _write_log(tmp_path)
    events = probe.parse_gateway_events(log, contact="好友A", limit=10)
    assert [e["kind"] for e in events] == ["inbound", "outbound", "inbound", "outbound"]
    pat = events[2]
    assert pat["pat"] is True and pat["as_text"] == "[拍一拍] 对方拍了拍你"
    assert events[1].get("guard_fallback") is False
    assert events[3].get("guard_fallback") is True

    # --around 按「今天的」时刻过滤：用当前时刻重写一份日志再验证
    now = time.time()
    around_log = tmp_path / "gateway_now.log"
    rows = [
        {"ts": now - 60, "kind": "inbound", "friend": "好友A", "text": "在吗"},
        {"ts": now + 600, "kind": "inbound", "friend": "好友A", "text": "很久以后"},
    ]
    around_log.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                          encoding="utf-8")
    around = probe.parse_gateway_events(
        around_log, contact="好友A", limit=10,
        around=time.strftime("%H:%M", time.localtime(now)))
    assert len(around) == 1 and around[0]["text"] == "在吗"


def test_load_history_readonly(tmp_path):
    base = _db(tmp_path)
    memory.store_message(base, "user", "你好")
    memory.store_message(base, "assistant", "在呢")
    rows = probe.load_history(base, limit=12)
    assert [r["role"] for r in rows] == ["user", "assistant"]
    assert rows[-1]["text"] == "在呢"
    assert probe.load_history(tmp_path / "missing.sqlite3") == []


def test_build_report_summary(tmp_path):
    log = _write_log(tmp_path)
    base = _db(tmp_path)
    memory.store_message(base, "user", "你好")
    memory.store_message(base, "assistant", "在呢")
    events = probe.parse_gateway_events(log, contact="好友A", limit=10)
    history = probe.load_history(base, limit=12)
    report = probe.build_report(events, history, probe.chat_history_limits(),
                                contact="好友A")
    assert report["summary"]["inbound"] == 2
    assert report["summary"]["pat_inbound"] == 1
    assert report["summary"]["guard_fallback_replies"] == 1
    assert report["history_count"] == 2
    assert report["history_chars"] == len("你好") + len("在呢")
    limits = report["prompt_history_limits"]["limits"]
    assert limits and (6 in limits or 12 in limits)


def test_probe_main_json(tmp_path, capsys):
    log = _write_log(tmp_path)
    base = _db(tmp_path)
    memory.store_message(base, "user", "你好")
    code = probe.main(["--contact", "好友A", "--log", str(log),
                       "--db", str(base), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["summary"]["pat_inbound"] == 1
    assert payload["summary"]["guard_fallback_replies"] == 1


# --------------------------------------------------------------------------- #
# P35：chat.py 收尾接线（拍一拍兜底 / 历史 12 / 图片 caption / 叙事注入 / 触发点）
# --------------------------------------------------------------------------- #
import io  # noqa: E402

import pytest  # noqa: E402

from agi.selflearn import chat, generation, self_narrative  # noqa: E402
from agi.selflearn import urges as urges_mod  # noqa: E402

PNG_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
           "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class _FakeClient:
    model = "fake"

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        return {"ok": True, "text": "好啊", "usage": {}}


def _agent(tmp_path: Path) -> chat.SelfLearnAgent:
    return chat.SelfLearnAgent(tmp_path / "selflearn.sqlite3", client=_FakeClient())


def _capture_reply(monkeypatch):
    seen: dict = {}

    def fake_simple(self, user_id, reply, st, attachment, **kwargs):
        seen["reply"] = reply
        return {"ok": True, "reply": reply, "bubbles": [reply]}

    monkeypatch.setattr(chat.SelfLearnAgent, "_simple_reply", fake_simple)
    return seen


def test_chat_pat_fallback_raw_variants(tmp_path, monkeypatch):
    seen = _capture_reply(monkeypatch)
    agent = _agent(tmp_path)
    for raw in ('"好友A" 拍了拍我', '"好友A"拍了拍我',
                "「好友A」拍了拍我", "拍了拍你", "我拍了拍你"):
        out = agent.send(raw)
        assert out["reply"] == "干嘛？", raw
        assert seen["reply"] == "干嘛？"


def test_chat_pat_does_not_misfire_on_normal_text(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_generate(client, system, prompt, memories, **kwargs):
        captured["prompt"] = prompt
        return {"ok": True, "text": "拍出来应该挺好看的，光线不错"}, {
            "two_stage": False, "claims": []}

    monkeypatch.setattr(generation, "generate", fake_generate)
    agent = _agent(tmp_path)
    out = agent.send("今天拍了拍照，光线不错")
    assert out["reply"] != "干嘛？"
    assert "拍了拍" in captured["prompt"]  # 原文进 prompt，但没走 [拍一拍] 分支


def test_render_dialog_keeps_12_and_budget():
    rows = [{"role": "user" if i % 2 else "assistant", "text": f"消息{i}"}
            for i in range(20)]
    dialog = chat._render_dialog(rows)
    assert len(dialog.splitlines()) <= 12
    huge = [{"role": "user", "text": "长" * 500} for _ in range(12)]
    small = chat._render_dialog(huge)
    assert len(small) <= chat.DIALOG_CHAR_BUDGET + 20


def test_chat_history_uses_12_messages(tmp_path, monkeypatch):
    captured: dict = {}

    def fake_generate(client, system, prompt, memories, **kwargs):
        captured["prompt"] = prompt
        return {"ok": True, "text": "拍出来应该挺好看的，光线不错"}, {
            "two_stage": False, "claims": []}

    monkeypatch.setattr(generation, "generate", fake_generate)
    base = tmp_path / "selflearn.sqlite3"
    db.connect(base)
    for i in range(14):
        memory.store_message(base, "user", f"第{i}条")
    agent = chat.SelfLearnAgent(base, client=_FakeClient())
    agent.send("现在呢")
    prompt = captured["prompt"]
    # 12 条历史里：第 2~13 条应出现，最早的第 0/1 条被挤出
    assert "第13条" in prompt and "第3条" in prompt
    assert "第0条" not in prompt and "第1条" not in prompt


def test_image_caption_goes_into_history(tmp_path, monkeypatch):
    from agi.selflearn import image_understanding

    monkeypatch.setattr(image_understanding, "describe_image",
                        lambda *a, **k: {"ok": True, "caption": "一盘寿司和仙人掌"})
    agent = _agent(tmp_path)
    agent.send("", image="data:image/png;base64," + PNG_1PX)
    rows = memory.recent_messages(agent.path, limit=5)
    user_rows = [r for r in rows if r["role"] == "user"]
    assert user_rows and "图里是一盘寿司和仙人掌" in user_rows[-1]["text"]


def test_system_prompt_narrative_injection(tmp_path, monkeypatch):
    path = tmp_path / "selflearn.sqlite3"
    db.connect(path)
    monkeypatch.setattr(chat, "mood_judge", type("M", (), {
        "context_line": staticmethod(lambda _p: "")}))
    monkeypatch.setattr("agi.selflearn.config.time_context_enabled", lambda: False)

    monkeypatch.setattr(self_narrative, "inject_line", lambda **kw: "")
    baseline = chat._system_prompt(path)
    monkeypatch.setattr(self_narrative, "inject_line",
                        lambda **kw: "你最近是这样理解自己的：想陪着他慢慢走。")
    injected = chat._system_prompt(path)
    assert injected == baseline + "\n\n你最近是这样理解自己的：想陪着他慢慢走。"


def test_chat_triggers_future_event_and_mood(tmp_path, monkeypatch):
    future_calls: list[str] = []
    mood_calls: list[float] = []
    monkeypatch.setattr(urges_mod, "observe_future_event",
                        lambda path, text, **kw: future_calls.append(text))
    monkeypatch.setattr(urges_mod, "observe_mood",
                        lambda path, mood, **kw: mood_calls.append(float(mood)))
    monkeypatch.setattr(generation, "generate",
                        lambda *a, **k: ({"ok": True, "text": "嗯"},
                                         {"two_stage": False, "claims": []}))
    agent = _agent(tmp_path)
    agent.send("明天要面试，有点紧张")
    assert future_calls and "面试" in future_calls[-1]
    assert mood_calls


def test_gateway_narrative_tick_and_routes(tmp_path, monkeypatch):
    from agi.wechat_gateway.server import Gateway, make_handler

    gw = Gateway({
        "enabled": True, "port": 801, "dry_run": True, "admin_token": "tok",
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "narrative_enabled": True})
    called: list[dict] = []
    monkeypatch.setattr(self_narrative, "maybe_weekly",
                        lambda **kw: {"should_run": True, "week": "2026-W44"})
    monkeypatch.setattr(self_narrative, "weekly_review",
                        lambda **kw: called.append(kw) or {
                            "ok": True, "week": "2026-W44", "skipped": False,
                            "changed": True, "llm": "fake", "narrative": "n" * 80})
    retry = gw._narrative_tick(1_800_000_000.0)
    assert called and retry > 1_800_000_000.0
    assert any(e.get("kind") == "narrative_review" for e in gw.recent)

    disabled = Gateway({"enabled": True, "narrative_enabled": False,
                        "allowed_contacts": ["好友A"],
                        "monitored_contacts": ["好友A"],
                        "drafts_path": str(tmp_path / "d2.jsonl"),
                        "log_path": str(tmp_path / "g2.log")})
    monkeypatch.setattr(self_narrative, "maybe_weekly",
                        lambda **kw: called.append(kw) or {"should_run": True})
    assert disabled._narrative_tick(1_800_000_000.0) == 0.0

    handler_cls = make_handler(gw)

    def _get(path, token="tok"):
        handler = handler_cls.__new__(handler_cls)
        handler.path = path
        handler.command = "GET"
        handler.requestline = f"GET {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        handler.rfile = io.BytesIO(b"")
        handler.headers = {"Host": "127.0.0.1:801", "Content-Length": "0"}
        if token:
            handler.headers["X-Admin-Token"] = token
        handler.do_GET()
        raw = handler.wfile.getvalue().decode("utf-8")
        head, _, payload = raw.partition("\r\n\r\n")
        status = int(head.split(" ")[1]) if head else 0
        return status, json.loads(payload) if payload.strip() else None

    status, data = _get("/api/urges/status?contact=好友A")
    assert status == 200 and "items" in data and data["contact"] == "好友A"
    status, data = _get("/api/initiative/status?contact=好友A")
    assert status == 200 and "drive" in data
    status, _data = _get("/api/urges/status?contact=好友A", token=None)
    assert status == 401


def test_chat_pat_real_simple_reply_returns_ganma(tmp_path):
    """不 mock _simple_reply：确认「干嘛？」不会被 P0-26「≤2字占位」兜底改写。"""
    agent = _agent(tmp_path)
    out = agent.send('"好友A" 拍了拍我')
    assert out["reply"] == "干嘛？"

def test_local_caption_replaces_cloud_image(tmp_path, monkeypatch):
    import json as _json

    captured: dict = {}

    def fake_generate(client, system, prompt, memories, **kwargs):
        captured["prompt"] = prompt
        return {"ok": True, "text": "看着挺香"}, {"two_stage": False, "claims": []}

    monkeypatch.setattr(generation, "generate", fake_generate)
    monkeypatch.setattr(chat.vision_chat, "mode", lambda: "local_caption")
    monkeypatch.setattr(chat.vision_chat, "caption",
                        lambda image, **kw: ("一盘寿司和仙人掌", ""))

    class _Recorder(_FakeClient):
        def chat(self, messages, **kwargs):
            captured.setdefault("messages", []).append(messages)
            return {"ok": True, "text": "好啊", "usage": {}}

    agent = chat.SelfLearnAgent(tmp_path / "selflearn.sqlite3", client=_Recorder())
    out = agent.send("", image="data:image/png;base64," + PNG_1PX)
    assert out["had_image"] is True
    assert "一盘寿司和仙人掌" in captured["prompt"]          # 本地 caption 注入提示词
    blob = _json.dumps(captured.get("messages") or [], ensure_ascii=False)
    assert "image_url" not in blob                            # 云端不再收图片
    rows = memory.recent_messages(agent.path, limit=5)
    assert any("图里是一盘寿司和仙人掌" in r["text"] for r in rows)
