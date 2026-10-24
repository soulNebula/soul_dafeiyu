# -*- coding: utf-8 -*-
"""W3-B：反馈幂等 / 原因标签 / pairwise 采集接口。

全部用 tmp_path 临时库，不联网；web 接口在进程内直接调 Handler.do_GET/do_POST，
不监听端口。
"""
from __future__ import annotations

import io
import json
import time

from agi.selflearn import db, skills, web


# ------------------------------------------------------------------ 夹具
def _seed_turn(path, turn_id: str = "T-1", variant_id: str = skills.BASELINE_ID) -> str:
    """建库并插入一条带变体的 turn；返回 turn_id。"""
    skills.ensure_defaults(path)
    now = time.time()
    con = db.connect(path)
    try:
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    (f"{turn_id}-U", "user", "今天有点累", now - 2, 0))
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    (f"{turn_id}-A", "assistant", "那就先歇会儿。", now - 1, 0))
        con.execute(
            "INSERT INTO turns(id,user_message_id,assistant_message_id,reply,"
            "skill_id,variant_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (turn_id, f"{turn_id}-U", f"{turn_id}-A", "那就先歇会儿。",
             skills.SKILL_ID, variant_id, now),
        )
        con.commit()
    finally:
        con.close()
    return turn_id


def _scalar(path, sql, params=()):
    con = db.connect(path)
    try:
        return con.execute(sql, params).fetchone()[0]
    finally:
        con.close()


def _rows(path, sql, params=()):
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


class _HandlerHarness:
    """进程内调用 web.Handler，不绑端口。"""

    @staticmethod
    def call(method: str, path: str, body=None):
        handler = web.Handler.__new__(web.Handler)
        handler.path = path
        handler.command = method
        handler.requestline = f"{method} {path} HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.close_connection = True
        handler.wfile = io.BytesIO()
        handler._headers_buffer = []
        raw = b"" if body is None else json.dumps(body, ensure_ascii=False).encode()
        handler.rfile = io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw))}
        getattr(handler, f"do_{method}")()
        raw_out = handler.wfile.getvalue().decode("utf-8")
        head, _, payload = raw_out.partition("\r\n\r\n")
        status = int(head.split(" ")[1]) if head else 0
        data = json.loads(payload) if payload.strip() else None
        return status, data


# ------------------------------------------------------------------ B1 反馈幂等
def test_same_turn_feedback_good_then_bad_keeps_single_row(tmp_path):
    path = tmp_path / "feedback.sqlite3"
    turn_id = _seed_turn(path)

    first = skills.record_feedback(path, turn_id, "good")
    assert first["ok"] is True
    # 返回结构保持兼容
    assert {"ok", "turn_id", "reaction", "score", "variant", "stats", "skill"} <= set(first)

    second = skills.record_feedback(path, turn_id, "bad", "太冷")
    assert second["ok"] is True and second["reaction"] == "bad"

    rows = _rows(path, "SELECT * FROM feedback WHERE turn_id=?", (turn_id,))
    assert len(rows) == 1
    assert rows[0]["reaction"] == "bad"
    assert rows[0]["note"] == "太冷"
    assert _scalar(path, "SELECT feedback FROM turns WHERE id=?", (turn_id,)) == "bad"
    assert _scalar(path, "SELECT score FROM turns WHERE id=?", (turn_id,)) == 0.0
    # 弱验证只保留一条（同一 turn 不重复计数）
    assert _scalar(path, "SELECT count(*) FROM weak_checks WHERE skill_id=?",
                   (skills.SKILL_ID,)) == 1
    # 变体账本按最终评价：good 的贡献被撤回后记一次 bad → alpha=1, beta=2
    variant = _rows(path, "SELECT alpha,beta FROM variants WHERE id=?", (skills.BASELINE_ID,))[0]
    assert variant["alpha"] == 1.0 and variant["beta"] == 2.0


def test_repeat_same_reaction_does_not_duplicate_or_inflate(tmp_path):
    path = tmp_path / "repeat.sqlite3"
    turn_id = _seed_turn(path)
    for _ in range(3):
        assert skills.record_feedback(path, turn_id, "good")["ok"] is True

    assert _scalar(path, "SELECT count(*) FROM feedback") == 1
    assert _scalar(path, "SELECT count(*) FROM weak_checks") == 1
    variant = _rows(path, "SELECT alpha,beta FROM variants WHERE id=?", (skills.BASELINE_ID,))[0]
    assert variant["alpha"] == 2.0 and variant["beta"] == 1.0


def test_legacy_duplicate_rows_are_collapsed(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    turn_id = _seed_turn(path)
    now = time.time()
    con = db.connect(path)
    try:
        for i, reaction in enumerate(("good", "ok")):
            con.execute(
                "INSERT INTO feedback(id,turn_id,reaction,score,note,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (f"FB-LEGACY-{i}", turn_id, reaction, 1.0, "", now + i),
            )
        con.commit()
    finally:
        con.close()
    assert _scalar(path, "SELECT count(*) FROM feedback") == 2

    result = skills.record_feedback(path, turn_id, "bad", "说错了")
    assert result["ok"] is True and result["reaction"] == "bad"
    rows = _rows(path, "SELECT reaction FROM feedback WHERE turn_id=?", (turn_id,))
    assert len(rows) == 1 and rows[0]["reaction"] == "bad"
    assert _scalar(path, "SELECT feedback FROM turns WHERE id=?", (turn_id,)) == "bad"


def test_invalid_reaction_is_rejected_without_writing(tmp_path):
    path = tmp_path / "invalid.sqlite3"
    turn_id = _seed_turn(path)
    result = skills.record_feedback(path, turn_id, "great")
    assert result["ok"] is False
    assert _scalar(path, "SELECT count(*) FROM feedback") == 0
    missing = skills.record_feedback(path, "T-NOT-EXIST", "good")
    assert missing["ok"] is False and "不存在" in missing["error"]


# ------------------------------------------------------------------ B2 原因标签（页面）
def test_page_has_reason_tags_and_sends_note():
    page = web.PAGE
    for tag in ("太长", "太冷", "说错了", "很好"):
        assert tag in page
    # 标签点击走同一条 /api/feedback，note 随请求提交
    assert "note:note||''" in page
    assert "fb(turnId,r,tag)" in page
    # 原有 good/ok/bad 语义没变
    assert "['good','ok','bad'].forEach" in page


def test_api_feedback_note_and_idempotent(monkeypatch, tmp_path):
    """走 /api/feedback 的真实链路（web → SelfLearnAgent → skills）。"""
    db_path = tmp_path / "api_feedback.sqlite3"
    monkeypatch.setattr(web, "DB_PATH", db_path)
    monkeypatch.setattr(web, "_AGENT", None)
    turn_id = _seed_turn(db_path)

    status, first = _HandlerHarness.call(
        "POST", "/api/feedback", {"turn_id": turn_id, "reaction": "good", "note": "太长"})
    assert status == 200 and first["ok"] is True

    status, second = _HandlerHarness.call(
        "POST", "/api/feedback", {"turn_id": turn_id, "reaction": "bad", "note": "太冷"})
    assert status == 200 and second["ok"] is True

    rows = _rows(db_path, "SELECT * FROM feedback WHERE turn_id=?", (turn_id,))
    assert len(rows) == 1
    assert rows[0]["reaction"] == "bad" and rows[0]["note"] == "太冷"
    assert _scalar(db_path, "SELECT feedback FROM turns WHERE id=?", (turn_id,)) == "bad"

    # 非法 reaction 仍返回 400，且不新增行
    status, _ = _HandlerHarness.call(
        "POST", "/api/feedback", {"turn_id": turn_id, "reaction": "great"})
    assert status == 400
    assert _scalar(db_path, "SELECT count(*) FROM feedback") == 1


# ------------------------------------------------------------------ B3 pairwise
def _valid_pair_payload(**overrides):
    payload = {
        "turn_a": "T-A",
        "turn_b": "T-B",
        "reply_a": "那就先歇会儿，别硬撑。",
        "reply_b": "你要学会管理时间。",
        "model_a": "inner-rule",
        "model_b": "baseline",
        "choice": "a",
        "note": "B 太说教",
        "context": {"user_msg": "今天有点累"},
    }
    payload.update(overrides)
    return payload


def test_pairwise_requires_fields_and_choice(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "pairwise.sqlite3")

    for field in ("turn_a", "turn_b", "reply_a", "reply_b"):
        payload = _valid_pair_payload()
        payload.pop(field)
        status, data = _HandlerHarness.call("POST", "/api/pairwise", payload)
        assert status == 400, (field, status, data)
        assert data["ok"] is False and field in data["error"]

    for choice in ("", "c", "A ", "both"):
        status, data = _HandlerHarness.call(
            "POST", "/api/pairwise", _valid_pair_payload(choice=choice))
        # "A " 会被 strip/lower 成合法 a，其余非法
        if choice.strip().lower() == "a":
            assert status == 200
        else:
            assert status == 400, (choice, status, data)
            assert "choice" in data["error"]

    assert _scalar(web.DB_PATH, "SELECT count(*) FROM pairs") == 1  # 只有 "A " 那条合法


def test_pairwise_write_and_stats(monkeypatch, tmp_path):
    db_path = tmp_path / "pairwise.sqlite3"
    monkeypatch.setattr(web, "DB_PATH", db_path)

    status, data = _HandlerHarness.call("POST", "/api/pairwise", _valid_pair_payload())
    assert status == 200 and data["ok"] is True
    assert data["id"].startswith("PR-")

    status, data = _HandlerHarness.call(
        "POST", "/api/pairwise",
        _valid_pair_payload(turn_b="T-C", choice="tie", model_a=None, model_b=None, note=""))
    assert status == 200

    rows = _rows(db_path, "SELECT * FROM pairs ORDER BY created_at")
    assert len(rows) == 2
    assert rows[0]["choice"] == "a" and rows[0]["model_a"] == "inner-rule"
    assert json.loads(rows[0]["context_json"]) == {"user_msg": "今天有点累"}
    assert rows[1]["choice"] == "tie" and rows[1]["model_a"] is None

    status, stats = _HandlerHarness.call("GET", "/api/pairwise/stats")
    assert status == 200 and stats["ok"] is True
    assert stats["total"] == 2
    assert stats["choices"] == {"a": 1, "b": 0, "tie": 1}
    assert len(stats["recent"]) == 2
    assert stats["recent"][0]["turn_b"] == "T-C"  # 最近的在前
    assert stats["recent"][0]["context"] == {"user_msg": "今天有点累"}


def test_pair_stats_empty_db(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "empty_pairwise.sqlite3")
    status, stats = _HandlerHarness.call("GET", "/api/pairwise/stats")
    assert status == 200
    assert stats == {"ok": True, "total": 0,
                     "choices": {"a": 0, "b": 0, "tie": 0}, "recent": []}


def test_pairwise_body_must_be_object(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "notobject.sqlite3")
    status, data = _HandlerHarness.call("POST", "/api/pairwise", ["不是", "对象"])
    assert status == 400
    assert data["ok"] is False and "JSON object" in data["error"]


def test_pairwise_bad_json_returns_400(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "DB_PATH", tmp_path / "badjson.sqlite3")
    handler = web.Handler.__new__(web.Handler)
    handler.path = "/api/pairwise"
    handler.command = "POST"
    handler.requestline = "POST /api/pairwise HTTP/1.1"
    handler.request_version = "HTTP/1.1"
    handler.close_connection = True
    handler.wfile = io.BytesIO()
    handler._headers_buffer = []
    raw = b"{not json"
    handler.rfile = io.BytesIO(raw)
    handler.headers = {"Content-Length": str(len(raw))}
    handler.do_POST()
    out = handler.wfile.getvalue().decode("utf-8")
    head, _, payload = out.partition("\r\n\r\n")
    assert int(head.split(" ")[1]) == 400
    assert json.loads(payload)["error"] == "bad json"
