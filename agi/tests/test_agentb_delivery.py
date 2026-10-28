# -*- coding: utf-8 -*-
"""task-1（Agent B）：先落库后发送的补偿 —— 送达状态 / 读侧过滤 / SQLite 加固。

覆盖验收项：
3) db.connect 加固：WAL + busy_timeout=5000 + synchronous=NORMAL；两线程各写 50 条
   不出现 ``database is locked``
5) turns.delivery_status / messages.delivered 幂等迁移 + chat.mark_turn_delivery +
   memory.recent_messages 过滤 delivered=0 的 assistant 行；白名单拦下时 turn=blocked

向后兼容红线：没被标记的消息（旧库迁移后 delivered 默认 1）必须照旧全部可见。
"""
from __future__ import annotations

import random
import sqlite3
import threading
import time

import pytest

from agi.selflearn import chat, db, memory
from agi.wechat_gateway.server import Gateway


# --------------------------------------------------------------------------- #
# 3. db.connect 加固
# --------------------------------------------------------------------------- #
def test_connect_enables_wal_busy_timeout_and_normal_sync(tmp_path):
    path = tmp_path / "c.sqlite3"
    con = db.connect(path)
    try:
        assert str(con.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
        assert int(con.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        # synchronous: 0=OFF 1=NORMAL 2=FULL
        assert int(con.execute("PRAGMA synchronous").fetchone()[0]) == 1
    finally:
        con.close()
    # memory_connect / persona_connect 走同样的加固
    for factory in (db.memory_connect, db.persona_connect):
        raw = factory(path)
        try:
            assert str(raw.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
            assert int(raw.execute("PRAGMA busy_timeout").fetchone()[0]) == 5000
        finally:
            raw.close()


def test_two_threads_write_50_rows_each_without_locked(tmp_path):
    path = tmp_path / "c.sqlite3"
    db.connect(path).close()
    errors: list[BaseException] = []

    def writer(tag: str):
        try:
            con = db.connect(path)
            try:
                for index in range(50):
                    con.execute(
                        "INSERT INTO messages(id,role,text,ts) VALUES(?,?,?,?)",
                        (f"{tag}-{index}", "user", f"write-{index}", time.time()))
                    con.commit()
            finally:
                con.close()
        except BaseException as exc:  # noqa: BLE001 - 收集起来断言
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(tag,), daemon=True)
               for tag in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == [], f"并发写出现异常：{errors}"
    con = db.connect(path)
    try:
        assert int(con.execute("SELECT count(*) FROM messages").fetchone()[0]) == 100
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 5a. 幂等迁移：旧库（无新列）connect 两次不报错、旧数据默认可见
# --------------------------------------------------------------------------- #
_OLD_MESSAGES = """
CREATE TABLE messages(
  id TEXT PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL,
  proactive INTEGER NOT NULL DEFAULT 0, archived_at REAL
);
INSERT INTO messages(id,role,text,ts) VALUES('M-old','assistant','老回复',1.0);
INSERT INTO messages(id,role,text,ts) VALUES('M-user','user','老问题',0.5);
"""
_OLD_TURNS = """
CREATE TABLE turns(
  id TEXT PRIMARY KEY, user_message_id TEXT NOT NULL,
  assistant_message_id TEXT NOT NULL, reply TEXT NOT NULL, skill_id TEXT,
  variant_id TEXT, feedback TEXT, score REAL, latency_ms REAL,
  created_at REAL NOT NULL
);
INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at)
VALUES('T-old','M-user','M-old','老回复',2.0);
"""


def test_old_split_db_migrates_columns_idempotently(tmp_path):
    base = tmp_path / "c.sqlite3"
    mem = tmp_path / "c.memory.sqlite3"
    con = sqlite3.connect(mem)
    con.executescript(_OLD_MESSAGES + _OLD_TURNS)
    con.commit()
    con.close()
    sqlite3.connect(tmp_path / "c.persona.sqlite3").close()   # 存在即不触发 legacy 迁移

    for _ in range(2):                # 连续 connect 两次：不重复 ALTER、不报错
        connection = db.connect(base)
        try:
            mcols = {r[1] for r in connection.execute("PRAGMA table_info(messages)")}
            tcols = {r[1] for r in connection.execute("PRAGMA table_info(turns)")}
            assert "delivered" in mcols and "delivery_status" in tcols
            # 旧库迁移后：旧数据必须仍然全部可见（向后兼容红线）
            assert int(connection.execute(
                "SELECT delivered FROM messages WHERE id='M-old'").fetchone()[0]) == 1
            assert str(connection.execute(
                "SELECT delivery_status FROM turns WHERE id='T-old'").fetchone()[0]) == ""
        finally:
            connection.close()

    rows = memory.recent_messages(base, limit=10)
    assert {row["id"] for row in rows} == {"M-old", "M-user"}


def test_legacy_single_db_split_still_works(tmp_path):
    """旧单库（*.sqlite3）走完整拆分迁移后，新列也在、旧数据可见。"""
    legacy = tmp_path / "legacy.sqlite3"
    con = sqlite3.connect(legacy)
    con.executescript(_OLD_MESSAGES + _OLD_TURNS)
    con.commit()
    con.close()

    connection = db.connect(legacy)
    try:
        mcols = {r[1] for r in connection.execute("PRAGMA table_info(messages)")}
        assert "delivered" in mcols
        assert int(connection.execute("SELECT count(*) FROM messages").fetchone()[0]) == 2
    finally:
        connection.close()
    assert {row["id"] for row in memory.recent_messages(legacy, limit=10)} == {
        "M-old", "M-user"}


# --------------------------------------------------------------------------- #
# 5b. mark_turn_delivery
# --------------------------------------------------------------------------- #
def _seed_turn(path):
    user_id = memory.store_message(path, "user", "在吗")
    assistant_id = memory.store_message(path, "assistant", "在的")
    turn_id = "T-test-1"
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO turns(id,user_message_id,assistant_message_id,reply,"
            "created_at) VALUES(?,?,?,?,?)",
            (turn_id, user_id, assistant_id, "在的", time.time()))
        con.commit()
    finally:
        con.close()
    return turn_id, user_id, assistant_id


def test_mark_turn_delivery_blocked_hides_assistant_message(tmp_path):
    path = tmp_path / "c.sqlite3"
    turn_id, user_id, assistant_id = _seed_turn(path)
    assert any(row["id"] == assistant_id for row in memory.recent_messages(path, 10))

    result = chat.mark_turn_delivery(path, turn_id, "blocked", detail="not_allowed")
    assert result["ok"] is True and result["messages_marked"] == 1

    con = db.connect(path)
    try:
        assert str(con.execute("SELECT delivery_status FROM turns WHERE id=?",
                               (turn_id,)).fetchone()[0]) == "blocked"
        assert int(con.execute("SELECT delivered FROM messages WHERE id=?",
                               (assistant_id,)).fetchone()[0]) == 0
        events = con.execute("SELECT * FROM raw_events WHERE kind='delivery'").fetchall()
        assert len(events) == 1
        assert "blocked" in str(events[0]["text"])
    finally:
        con.close()

    rows = memory.recent_messages(path, limit=10)
    ids = {row["id"] for row in rows}
    assert assistant_id not in ids, "没发出去的 assistant 消息不能当既成事实"
    assert user_id in ids, "用户消息照旧可见"


def test_mark_turn_delivery_sent_keeps_message_visible(tmp_path):
    path = tmp_path / "c.sqlite3"
    turn_id, _user_id, assistant_id = _seed_turn(path)
    assert chat.mark_turn_delivery(path, turn_id, "sent")["ok"] is True
    con = db.connect(path)
    try:
        assert str(con.execute("SELECT delivery_status FROM turns WHERE id=?",
                               (turn_id,)).fetchone()[0]) == "sent"
        assert int(con.execute("SELECT delivered FROM messages WHERE id=?",
                               (assistant_id,)).fetchone()[0]) == 1
    finally:
        con.close()
    assert any(row["id"] == assistant_id for row in memory.recent_messages(path, 10))


def test_mark_turn_delivery_default_empty_status_changes_nothing(tmp_path):
    """不传 status 的旧调用方：只写状态字段，不动 messages.delivered（默认可见）。"""
    path = tmp_path / "c.sqlite3"
    turn_id, _user_id, assistant_id = _seed_turn(path)
    result = chat.mark_turn_delivery(path, turn_id, "")
    assert result["ok"] is True and result["messages_marked"] == 0
    con = db.connect(path)
    try:
        assert int(con.execute("SELECT delivered FROM messages WHERE id=?",
                               (assistant_id,)).fetchone()[0]) == 1
    finally:
        con.close()
    assert any(row["id"] == assistant_id for row in memory.recent_messages(path, 10))


def test_mark_turn_delivery_tolerates_unknown_turn_and_empty_id(tmp_path):
    path = tmp_path / "c.sqlite3"
    assert chat.mark_turn_delivery(path, "T-missing", "blocked")["ok"] is True
    empty = chat.mark_turn_delivery(path, "", "blocked")
    assert empty["ok"] is False and empty["messages_marked"] == 0


# --------------------------------------------------------------------------- #
# 5c. 读侧：chat 的 history 走 recent_messages（prompt/history 同一个读口）
# --------------------------------------------------------------------------- #
class _FakeClient:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "用户记忆抽取器" in text:
            return {"ok": True, "text": "[]"}
        if "自学习对话体自己的行动和结果" in text:
            return {"ok": True, "text": "{\"traits\": []}"}
        return {"ok": True, "text": "好呀，在的。"}


def test_chat_history_excludes_undelivered_assistant_turn(tmp_path):
    path = tmp_path / "c.sqlite3"
    agent = chat.SelfLearnAgent(path, client=_FakeClient(), rng=random.Random(1))
    first = agent.send("第一条消息")
    assert first["ok"] and first["turn_id"]
    con = db.connect(path)
    try:
        first_assistant_id = str(con.execute(
            "SELECT assistant_message_id FROM turns WHERE id=?",
            (first["turn_id"],)).fetchone()[0])
    finally:
        con.close()

    # 第一次回复被判"没发出去"
    assert chat.mark_turn_delivery(path, first["turn_id"], "blocked")["ok"] is True

    second = agent.send("第二条消息")
    history = second["history"]
    history_ids = {str(row.get("id") or "") for row in history}
    assert first_assistant_id not in history_ids
    # 用户两条消息照旧在（对话轮次不会被抹掉）
    user_texts = [str(row.get("text") or "") for row in history
                  if row.get("role") == "user"]
    assert any("第一条消息" in text for text in user_texts)
    assert any("第二条消息" in text for text in user_texts)
    # 第二条回复（正常送达）在历史里
    second_assistant = [str(row.get("text") or "") for row in history
                        if row.get("role") == "assistant"]
    assert second_assistant == [str(second["bubbles"][0])]


# --------------------------------------------------------------------------- #
# 5d. 网关集成：白名单拦下 → turn=blocked 且 assistant 消息不进 recent_messages
# --------------------------------------------------------------------------- #
def _gw(tmp_path, **over):
    cfg = {
        "enabled": True, "port": 801, "dry_run": False, "send_mode": "auto",
        "allowed_contacts": ["好友B"], "monitored_contacts": ["好友B"],
        "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0], "min_interval_s": 0,
    }
    cfg.update(over)
    return Gateway(cfg, stop_path=tmp_path / "STOP",
                   config_path=tmp_path / "config.json")


def _install_offline_agent(monkeypatch):
    """把 chat.SelfLearnAgent 换成"真落库 + 假 LLM"的包装（绝不联网）。"""
    import agi.selflearn.chat as chat_mod

    real = chat_mod.SelfLearnAgent

    class _OfflineAgent:
        def __init__(self, db_path):
            self._impl = real(db_path, client=_FakeClient(),
                              rng=random.Random(7))

        def send(self, text, image=None):
            return self._impl.send(text, image=image)

    monkeypatch.setattr(chat_mod, "SelfLearnAgent", _OfflineAgent)


def test_gateway_whitelist_block_marks_turn_blocked(tmp_path, monkeypatch):
    from agi.wechat_gateway import config as gw_config

    _install_offline_agent(monkeypatch)
    gw = _gw(tmp_path)                     # 白名单只有"好友B" → 回复被 not_allowed 拦
    gw._reply({"friend": "好友A", "text": "你在吗", "kind": "text"})

    turn_id = gw.last_turns.get("好友A")
    assert turn_id, "agent 没产出 turn"
    db_path = gw_config.contact_db("好友A")

    con = db.connect(db_path)
    try:
        status = str(con.execute("SELECT delivery_status FROM turns WHERE id=?",
                                 (turn_id,)).fetchone()[0])
        assert status == "blocked"
        rows = con.execute("SELECT delivered FROM messages WHERE role='assistant'"
                           ).fetchall()
        assert rows and all(int(row[0]) == 0 for row in rows)
    finally:
        con.close()

    history = memory.recent_messages(db_path, limit=20)
    assert all(row["role"] != "assistant" for row in history)
    assert any(row["role"] == "user" for row in history)
    assert any(event.get("kind") == "delivery_marked" for event in gw.recent)


def test_gateway_dry_run_keeps_turn_planned_and_visible(tmp_path, monkeypatch):
    """dry-run 只是"计划发送"：自动回复默认行为不变，消息仍然留在历史里。"""
    from agi.wechat_gateway import config as gw_config

    _install_offline_agent(monkeypatch)
    gw = _gw(tmp_path, dry_run=True, allowed_contacts=["好友A"])
    gw._reply({"friend": "好友A", "text": "你在吗", "kind": "text"})

    turn_id = gw.last_turns.get("好友A")
    db_path = gw_config.contact_db("好友A")
    con = db.connect(db_path)
    try:
        assert str(con.execute("SELECT delivery_status FROM turns WHERE id=?",
                               (turn_id,)).fetchone()[0]) == "planned"
        assert all(int(row[0]) == 1 for row in con.execute(
            "SELECT delivered FROM messages WHERE role='assistant'").fetchall())
    finally:
        con.close()
    assert any(row["role"] == "assistant"
               for row in memory.recent_messages(db_path, limit=20))