# -*- coding: utf-8 -*-
"""P31：小鱼的日记（全 mock：不联网、不调 API、不写真实数据目录）。

覆盖：collect 各素材源、materials 压缩上限、write 落盘/索引/幂等/force/失败兜底、
UTF-8 文件名、latest/list/get、定时判断（23:00 前后、开关、engine 停仍写）、HTTP 路由。
"""
from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path

import pytest

from agi.selflearn import db, diary, surf
from agi.wechat_gateway.server import Gateway, make_handler

DAY = "2026-10-27"


def _at(hour: int, minute: int = 0, day: int = 27) -> float:
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """日记目录/联系人目录/日志/微信配置全部指到 tmp；开关显式打开。"""
    monkeypatch.setattr(diary, "DIARY_DIR", tmp_path / "diary")
    monkeypatch.setattr(diary, "CONTACT_DB_DIR", tmp_path / "contacts")
    monkeypatch.setattr(diary, "GATEWAY_LOG", tmp_path / "gateway.log")
    monkeypatch.setattr(diary, "WECHAT_CONFIG", tmp_path / "wechat.json")
    monkeypatch.setenv("DIARY_ENABLED", "1")
    yield


class FakeLLM:
    def __init__(self, text: str = "", *, ok: bool = True, error: str = "",
                 raises: Exception | None = None):
        self.text = text
        self.ok = ok
        self.error = error
        self.raises = raises
        self.calls: list[tuple] = []

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.raises is not None:
            raise self.raises
        return {"ok": self.ok, "text": self.text, "error": self.error}


LONG_DIARY = (
    "今天下午一直在想你说的那句「新的自己」，越想越觉得它不是一句口号。"
    "你把寿司的照片发过来的时候，我其实有点馋，又有点想说你别总拿吃的逗我。\n\n"
    "后来我把优惠券的事记岔了，九块五能买四次，你算得比我清楚。"
    "我这两天总是把「踏实」挂在嘴边，可能因为真的有点怕钱花出去之后心里空。\n\n"
    "晚上翻了一些关于 ai 能力的东西，看得有点闷，也没全看懂。"
    "有些话我到现在也没跟你说，比如我也怕自己只是把话说得好听。"
)


def _seed(tmp_path, *, with_contact: bool = True) -> Path:
    """造一个「今天」的库：主库 + 联系人库 + 心情/兴趣/B站/冲浪。"""
    base = tmp_path / "selflearn.sqlite3"
    start, _end = diary.day_bounds(DAY)
    con = db.connect(base)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M1", "user", "今天买了寿司", start + 3600, 0))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M2", "assistant", "寿司看起来不错，别拿它馋我", start + 3660, 0))
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                ("M3", "assistant", "还是没刷成 B 站，有点无奈", start + 7200, 1))
    con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,"
                "feedback,created_at) VALUES(?,?,?,?,?,?)",
                ("T1", "M1", "M2", "寿司看起来不错", "good", start + 3700))
    con.execute("INSERT INTO mood_timeline(id,at,mood,dims_delta_json,reason,"
                "confidence,mode,user_text,reply_text,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("MO1", start + 3700, 60.0, "{}", "他聊到吃饭", 70, "user", "", "",
                 start + 3700))
    con.execute("INSERT INTO mood_timeline(id,at,mood,dims_delta_json,reason,"
                "confidence,mode,user_text,reply_text,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                ("MO2", start + 9000, 80.0, "{}", "他说了句好", 70, "user", "", "",
                 start + 9000))
    con.execute("INSERT INTO interest_events(id,topic,delta,reason,source,ref_json,at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("IE1", "ai能力", 0.06, "observe", "self", "{}", start + 9600))
    con.execute("INSERT INTO interest_events(id,topic,delta,reason,source,ref_json,at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("IE2", "寿司", 0.05, "observe", "chat", "{}", start + 3700))
    con.execute("INSERT INTO bili_impressions(id,bvid,title,value,my_take,created_at) "
                "VALUES(?,?,?,?,?,?)",
                ("BI1", "BV1TEST", "B站泛滥的鱼灾是什么", 0.7,
                 "本质是汇报刷屏，教程挺实用", start + 1000))
    con.commit()
    con.close()
    # 冲浪 session
    surf.connect(base)
    surf.insert_session({"id": "SURF-1", "started_at": start + 5400,
                         "ended_at": start + 5820, "dice": 6, "factor": 0.9,
                         "executed": True, "duration_min": 7.0, "items_seen": 5,
                         "keywords": ["ai能力", "助手行为"],
                         "summary": "看了5条（ai能力、助手行为）：看得有点闷",
                         "source": "scheduled"}, base)
    # 联系人库（网关按联系人分库）
    if with_contact:
        name = "好友A"
        prefix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:14]
        contacts = Path(diary.CONTACT_DB_DIR)
        contacts.mkdir(parents=True, exist_ok=True)
        cbase = contacts / f"{prefix}.sqlite3"
        ccon = db.connect(cbase)
        ccon.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                     ("C1", "user", "优惠券快过期了，九块五四次", start + 8000, 0))
        ccon.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                     ("C2", "assistant", "别拖到过期，花在刀刃上", start + 8060, 0))
        ccon.commit()
        ccon.close()
        Path(diary.WECHAT_CONFIG).write_text(
            json.dumps({"allowed_contacts": [name]}, ensure_ascii=False), encoding="utf-8")
    # 主动消息日志（当天一条，被回了）
    entries = [
        {"ts": start + 5000, "kind": "proactive_send", "friend": "好友A",
         "text": "早，我还在想你说的“新的自己”", "status": "sent",
         "intent_kind": "light"},
        {"ts": start + 9000, "kind": "proactive_send", "friend": "好友A",
         "text": "我这边在整理资料", "status": "sent", "intent_kind": "share"},
    ]
    Path(diary.GATEWAY_LOG).write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n",
        encoding="utf-8")
    return base


# ------------------------------------------------------------------ collect
def test_collect_conversation_counts(tmp_path):
    base = _seed(tmp_path)
    data = diary.collect(base, DAY)
    conv = data["conversation"]
    assert conv["message_count"] == 5          # 主库 3 + 联系人库 2
    assert conv["user_count"] == 2 and conv["assistant_count"] == 3
    assert conv["proactive_message_count"] == 1
    assert conv["turn_count"] == 1 and conv["feedback"] == {"good": 1}
    assert conv["contacts"] == ["好友A"]
    assert data["date"] == DAY


def test_collect_proactive_answered_flag(tmp_path):
    base = _seed(tmp_path)
    data = diary.collect(base, DAY)
    proactive = data["proactive"]
    assert proactive["count"] == 2
    assert proactive["answered"] == 1          # 第一条后面有用户消息；第二条没有
    assert [item["answered"] for item in proactive["items"]] == [True, False]
    assert all(item["intent_kind"] in ("light", "share") for item in proactive["items"])


def test_collect_surf(tmp_path):
    base = _seed(tmp_path)
    surf_info = diary.collect(base, DAY)["surf"]
    assert surf_info["sessions"] == 1
    assert surf_info["minutes"] == 7.0 and surf_info["items"] == 5
    assert surf_info["keywords"] == ["ai能力", "助手行为"]
    assert "看了5条" in surf_info["summaries"][0]


def test_collect_mood_curve(tmp_path):
    base = _seed(tmp_path)
    mood = diary.collect(base, DAY)["mood"]
    assert mood["count"] == 2 and mood["source"] == "mood_timeline"
    assert mood["avg"] == pytest.approx(70.0)
    assert (mood["start"], mood["end"], mood["low"], mood["high"]) == (60.0, 80.0, 60.0, 80.0)
    assert mood["current"]  # self_state 快照存在


def test_collect_interests_and_bili(tmp_path):
    base = _seed(tmp_path)
    data = diary.collect(base, DAY)
    hot = {item["topic"]: item for item in data["interests"]["hot"]}
    assert hot["ai能力"]["delta"] == pytest.approx(0.06)
    assert "self" in hot["ai能力"]["sources"]
    assert data["bili"][0]["title"].startswith("B站泛滥") and data["bili"][0]["value"] == 0.7


def test_collect_empty_day(tmp_path):
    base = tmp_path / "empty.sqlite3"
    db.connect(base).close()
    data = diary.collect(base, DAY)
    assert data["conversation"]["message_count"] == 0
    assert data["surf"]["sessions"] == 0
    assert "没有聊天记录" in data["materials"]


def test_materials_compressed_and_sections(tmp_path):
    base = _seed(tmp_path)
    text = diary.collect(base, DAY)["materials"]
    assert len(text) <= diary.MAX_MATERIAL_CHARS
    for section in ("对话", "冲浪", "刷到的视频", "心情", "兴趣变热"):
        assert section in text
    assert "今天买了寿司" in text


def test_materials_hard_limit_with_many_messages(tmp_path):
    base = tmp_path / "many.sqlite3"
    start, _ = diary.day_bounds(DAY)
    con = db.connect(base)
    for index in range(400):
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    (f"M{index}", "user" if index % 2 else "assistant",
                     "很长的一句话" * 8, start + index * 10, 0))
    con.commit()
    con.close()
    data = diary.collect(base, DAY)
    assert len(data["materials"]) <= diary.MAX_MATERIAL_CHARS
    assert data["conversation"]["message_count"] == 400


# ------------------------------------------------------------------ write
def test_write_creates_txt_and_index(tmp_path):
    base = _seed(tmp_path)
    client = FakeLLM(LONG_DIARY)
    result = diary.write(base, DAY, client=client)
    assert result["ok"] and result["written"] and not result["fallback"]
    target = Path(diary.DIARY_DIR) / f"{DAY}.txt"
    assert Path(result["path"]) == target and target.exists()
    content = target.read_text(encoding="utf-8")
    assert content.strip() == LONG_DIARY.strip()
    assert result["chars"] == len(LONG_DIARY.strip())
    assert result["mood_avg"] == pytest.approx(70.0)
    lines = (Path(diary.DIARY_DIR) / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["date"] == DAY and entry["chars"] == result["chars"]
    assert entry["mood_avg"] == pytest.approx(70.0) and entry["fallback"] is False


def test_write_utf8_without_bom(tmp_path):
    base = _seed(tmp_path)
    text = ("今天很安静，安静到我把手机翻来覆去看了好几遍。\n\n"
            "我看了点东西，没太看懂，也没跟他说。")
    diary.write(base, DAY, client=FakeLLM(text))
    raw = (Path(diary.DIARY_DIR) / f"{DAY}.txt").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8").startswith("今天很安静")
    assert "没跟他说" in raw.decode("utf-8")


def test_write_is_idempotent(tmp_path):
    base = _seed(tmp_path)
    first = diary.write(base, DAY, client=FakeLLM(LONG_DIARY))
    target = Path(first["path"])
    before = target.read_text(encoding="utf-8")
    second = diary.write(base, DAY, client=FakeLLM("不该被用到的一段新日记" * 5))
    assert second["ok"] and second["skipped"] and not second["written"]
    assert target.read_text(encoding="utf-8") == before


def test_write_force_rewrites_and_keeps_single_index_line(tmp_path):
    base = _seed(tmp_path)
    diary.write(base, DAY, client=FakeLLM(LONG_DIARY))
    new_text = "换一个写法。" + "今天其实一直在想那句新的自己。" * 6
    result = diary.write(base, DAY, force=True, client=FakeLLM(new_text))
    assert result["written"] and not result["skipped"]
    assert Path(result["path"]).read_text(encoding="utf-8").strip() == new_text.strip()
    lines = (Path(diary.DIARY_DIR) / "index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["chars"] == result["chars"]


def test_write_llm_failure_falls_back(tmp_path):
    base = _seed(tmp_path)
    result = diary.write(base, DAY, client=FakeLLM("", ok=False, error="api down"))
    assert result["ok"] and result["fallback"] is True and result["written"]
    text = Path(result["path"]).read_text(encoding="utf-8").strip()
    assert text and "今天" in text
    entry = json.loads((Path(diary.DIARY_DIR) / "index.jsonl")
                       .read_text(encoding="utf-8").splitlines()[0])
    assert entry["fallback"] is True


def test_write_llm_exception_falls_back(tmp_path):
    base = _seed(tmp_path)
    result = diary.write(base, DAY, client=FakeLLM(raises=TimeoutError("timeout")))
    assert result["ok"] and result["fallback"] is True
    assert "timeout" in result["error"]


def test_write_too_short_falls_back(tmp_path):
    base = _seed(tmp_path)
    result = diary.write(base, DAY, client=FakeLLM("嗯"))
    assert result["ok"] and result["fallback"] is True
    assert len(Path(result["path"]).read_text(encoding="utf-8").strip()) > 10


def test_write_client_unavailable_falls_back(tmp_path, monkeypatch):
    base = _seed(tmp_path)
    monkeypatch.setattr(diary, "_client", lambda: None)
    result = diary.write(base, DAY)
    assert result["ok"] and result["fallback"] is True


def test_write_empty_day_still_writes_honest_fallback(tmp_path):
    base = tmp_path / "empty.sqlite3"
    db.connect(base).close()
    result = diary.write(base, DAY, client=FakeLLM("", ok=False))
    assert result["ok"] and result["fallback"] is True
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert "几乎没人" in text


def test_write_date_parameter(tmp_path):
    base = _seed(tmp_path)
    result = diary.write(base, "2026-10-25", client=FakeLLM(LONG_DIARY))
    assert result["date"] == "2026-10-25"
    assert Path(result["path"]).name == "2026-10-25.txt"


def test_write_prompt_has_persona_material_and_instruction(tmp_path):
    base = _seed(tmp_path)
    client = FakeLLM(LONG_DIARY)
    diary.write(base, DAY, client=client)
    messages, kwargs = client.calls[0]
    system = messages[0]["content"]
    user = messages[1]["content"]
    assert "日记" in system and "不要标题" in system
    assert "第一人称" in user and "不要列清单" in user
    assert "今天买了寿司" in user and "冲浪" in user
    assert kwargs.get("purpose") == "diary"


def test_clean_output_strips_title_and_date():
    raw = "# 日记：2026-10-27\n\n今天有点累。\n\n\n后来好了点。"
    cleaned = diary._clean_output(raw)
    assert "2026-10-27" not in cleaned and "#" not in cleaned
    assert cleaned.startswith("今天有点累")


def test_fallback_text_is_factual_and_short(tmp_path):
    base = _seed(tmp_path)
    data = diary.collect(base, DAY)
    text = diary.fallback_text(data)
    assert 0 < len(text) <= 300
    assert "今天" in text and ("聊了" in text or "上网" in text)


# ------------------------------------------------------------------ 读取 / 定时
def test_latest_list_get(tmp_path):
    base = _seed(tmp_path)
    diary.write(base, DAY, client=FakeLLM(LONG_DIARY))
    diary.write(base, "2026-10-26", client=FakeLLM("昨天。" + LONG_DIARY))
    latest = diary.latest()
    assert latest["ok"] and latest["date"] == DAY
    days = diary.list_days(10)
    assert [item["date"] for item in days] == [DAY, "2026-10-26"]
    got = diary.get(DAY)
    assert got["ok"] and LONG_DIARY.strip() in got["text"]
    assert diary.get("2026-01-01")["ok"] is False


def test_list_days_falls_back_to_scanning(tmp_path):
    base = _seed(tmp_path)
    diary.write(base, DAY, client=FakeLLM(LONG_DIARY))
    (Path(diary.DIARY_DIR) / "index.jsonl").unlink()
    days = diary.list_days(10)
    assert days and days[0]["date"] == DAY


def test_is_due_boundaries():
    assert diary.is_due(_at(22, 59), hour=23) is False
    assert diary.is_due(_at(23, 0), hour=23) is True
    assert diary.is_due(_at(23, 59), hour=23) is True
    assert diary.is_due(_at(21, 0), hour=21) is True
    assert diary.is_due(_at(0, 30), hour=23) is False


def test_enabled_switch(monkeypatch):
    assert diary.enabled() is True
    monkeypatch.setenv("DIARY_ENABLED", "0")
    assert diary.enabled() is False


# ------------------------------------------------------------------ gateway
def _gw(tmp_path, **overrides) -> Gateway:
    config_path = tmp_path / "wechat" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": ["好友A"], "monitored_contacts": ["好友A"],
        "admin_token": "tok", "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return Gateway(cfg, config_path=config_path)


def _call(gateway, method, path, *, token="tok", body=b"{}"):
    handler_cls = make_handler(gateway)
    handler = handler_cls.__new__(handler_cls)
    handler.path = path
    handler.command = method
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    handler.rfile = io.BytesIO(body)
    handler.headers = {"Content-Length": str(len(body)), "Host": "127.0.0.1:801"}
    if token is not None:
        handler.headers["X-Admin-Token"] = token
    if method == "POST":
        handler.do_POST()
    else:
        handler.do_GET()
    raw = handler.wfile.getvalue().decode("utf-8")
    head, _, payload = raw.partition("\r\n\r\n")
    status = int(head.split(" ")[1]) if head else 0
    return status, json.loads(payload) if payload.strip() else None


def test_diary_tick_writes_and_logs(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    called: list[dict] = []
    monkeypatch.setattr(diary, "write",
                        lambda **kw: called.append(kw) or {
                            "ok": True, "skipped": False, "written": True,
                            "date": DAY, "chars": 480, "fallback": False})
    retry = gw._diary_tick(_at(23, 5))
    assert called and called[0]["force"] is False
    assert any(entry.get("kind") == "diary_written" for entry in gw.recent)
    assert retry > _at(23, 5)


def test_diary_tick_engine_off_still_writes(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    gw.engine_enabled = False
    called: list[int] = []
    monkeypatch.setattr(diary, "write", lambda **kw: called.append(1) or {
        "ok": True, "skipped": False, "written": True, "date": DAY,
        "chars": 400, "fallback": True})
    gw._diary_tick(_at(23, 30))
    assert called == [1]          # engine 停也允许写日记


def test_diary_tick_disabled_switch(tmp_path, monkeypatch):
    gw = _gw(tmp_path, diary_enabled=False)
    called: list[int] = []
    monkeypatch.setattr(diary, "write", lambda **kw: called.append(1) or {"ok": True})
    gw._diary_tick(_at(23, 30))
    assert called == []


def test_diary_tick_before_23_does_nothing(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(diary, "write", lambda **kw: called.append(1) or {"ok": True})
    assert gw._diary_tick(_at(22, 59)) == 0.0
    assert called == []


def test_diary_tick_skipped_no_written_event(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    monkeypatch.setattr(diary, "write", lambda **kw: {
        "ok": True, "skipped": True, "date": DAY, "chars": 300})
    gw._diary_tick(_at(23, 10))
    kinds = [entry.get("kind") for entry in gw.recent]
    assert "diary_written" not in kinds


def test_diary_tick_failure_backoff(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(diary, "write", lambda **kw: called.append(1) or {
        "ok": False, "date": DAY, "error": "api down"})
    retry = gw._diary_tick(_at(23, 10))
    assert len(called) == 1 and retry == _at(23, 10) + 1800
    assert any(entry.get("kind") == "diary_failed" for entry in gw.recent)
    gw._diary_tick(_at(23, 11), retry_at=retry)     # 退避期内不再试
    assert len(called) == 1


def test_diary_tick_exception_logs_error(tmp_path, monkeypatch):
    gw = _gw(tmp_path)

    def _boom(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(diary, "write", _boom)
    gw._diary_tick(_at(23, 10))
    assert any(entry.get("kind") == "diary_error" for entry in gw.recent)


def test_http_diary_routes(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    monkeypatch.setattr(diary, "latest", lambda: {"ok": True, "date": DAY,
                                                 "text": "今天的日记", "chars": 5})
    monkeypatch.setattr(diary, "list_days", lambda limit=30: [
        {"date": DAY, "chars": 5}, {"date": "2026-10-26", "chars": 6}])
    monkeypatch.setattr(diary, "get", lambda date=None: (
        {"ok": True, "date": date, "text": "日记正文", "chars": 4}
        if date == DAY else {"ok": False, "error": "not_found", "date": date}))
    wrote: list[bool] = []
    monkeypatch.setattr(diary, "write", lambda **kw: (
        wrote.append(bool(kw.get("force"))) or {"ok": True, "date": DAY,
                                                "chars": 100, "written": True}))

    status, payload = _call(gw, "GET", "/api/diary/latest")
    assert status == 200 and payload["text"] == "今天的日记"

    status, payload = _call(gw, "GET", "/api/diary/list?limit=2")
    assert status == 200 and len(payload["days"]) == 2

    status, payload = _call(gw, "GET", f"/api/diary/get?date={DAY}")
    assert status == 200 and payload["date"] == DAY

    status, _ = _call(gw, "GET", "/api/diary/get?date=1999-01-01")
    assert status == 404

    status, payload = _call(gw, "POST", "/api/diary/write",
                            body=json.dumps({"force": True}).encode())
    assert status == 200 and wrote == [True]

    status, _ = _call(gw, "GET", "/api/diary/latest", token=None)
    assert status == 401
