# -*- coding: utf-8 -*-
"""P25：网上冲浪引擎（全 mock：不联网、不写真实数据目录）。

覆盖：骰子/因子/门槛/时长/条数公式、窗口与下次时刻、状态持久化与损坏容错、
surf_sessions 表结构、一轮冲浪（取数→my_take→兴趣记账→摘要）、worker 一轮
（跳过/执行/用户强制/engine 暂停）、gateway HTTP 路由。
"""
from __future__ import annotations

import io
import json
import random
import threading
import time
from pathlib import Path

import pytest

from agi.selflearn import surf, surf_worker
from agi.wechat_gateway.server import Gateway, make_handler


def _ts(hour: int, minute: int = 0, day: int = 27) -> float:
    """本地 2026-10-27 的某时刻（窗口边界测试用）。"""
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """隔离状态文件/日志/DB，并显式打开冲浪开关（测试不读生产数据）。"""
    monkeypatch.setattr(surf, "STATE_PATH", tmp_path / "surf_state.json")
    monkeypatch.setattr(surf, "SURF_LOG", tmp_path / "surf.log")
    monkeypatch.setattr(surf, "DB_PATH", tmp_path / "selflearn.sqlite3")
    monkeypatch.setenv("SURF_ENABLED", "1")
    yield


class FakeRng:
    """确定性骰子替身：randint(1,6)=dice，其余 randint 返回 items。"""

    def __init__(self, dice: int, items: int = 5):
        self.dice = dice
        self.items = items
        self.calls: list[tuple] = []

    def randint(self, low, high):
        self.calls.append((low, high))
        if (low, high) == (1, surf.DICE_FACES):
            return self.dice
        return min(max(self.items, low), high)

    def gauss(self, mu, sigma):
        return 0.0


# ------------------------------------------------------------------ 骰子/公式
def test_roll_dice_bounds():
    rng = random.Random(7)
    values = {surf.roll_dice(rng) for _ in range(300)}
    assert values <= set(range(1, 7)) and len(values) == 6


def test_gap_minutes_by_factor():
    assert surf.gap_minutes(0.9) == 30
    assert surf.gap_minutes(1.0) == 29
    assert surf.gap_minutes(0.2) == 38
    assert surf.gap_minutes(1.8) == 19


def test_gap_minutes_clamped():
    assert surf.gap_minutes(-5) == 45
    assert surf.gap_minutes(5) == 15
    # base_min 可配，但 gap 仍然夹紧在 15–45 分钟内
    assert surf.gap_minutes(0.9, base_min=60) == 45
    assert surf.gap_minutes(0.9, base_min=20) == 20


def test_should_execute_gate():
    assert surf.should_execute(3, 0.8) is True       # 3×0.8=2.4 恰好达标
    assert surf.should_execute(4, 0.6) is True       # 4×0.6=2.4
    assert surf.should_execute(6, 0.35) is False     # 6×0.35=2.1 <2.4
    assert surf.should_execute(2, 1.8) is False      # 点数<3 不执行
    assert surf.should_execute(3, 0.79) is False


def test_duration_minutes_clamped():
    assert surf.duration_minutes(6, 1.8) == 12
    assert surf.duration_minutes(3, 0.2) == 3
    assert surf.duration_minutes(4, 1.0) == 8
    assert surf.duration_minutes(6, 1.0) == 12


def test_items_count_bounds():
    assert 2 <= surf.items_count(6, FakeRng(6, 99)) <= 20
    assert 2 <= surf.items_count(3, FakeRng(3, 99)) <= 10
    assert surf.items_count(6, FakeRng(6, 3), cap=5) <= 5
    values = {surf.items_count(6, random.Random(seed)) for seed in range(50)}
    assert min(values) >= 2 and max(values) <= 20


def test_base_factor_mood_energy_and_clamp():
    high = surf.base_factor(1.0, 1.0, 0.0)
    low = surf.base_factor(0.0, 0.0, 0.0)
    assert high > low
    assert surf.base_factor(1.0, 1.0, 5.0) == surf.BASE_FACTOR_MAX
    assert surf.base_factor(0.0, 0.0, -5.0) == surf.BASE_FACTOR_MIN


def test_factor_decay_and_recover():
    assert surf.decay_factor(1.0, 6) == pytest.approx(0.55)
    assert surf.decay_factor(1.0, 5) == pytest.approx(0.8)
    assert surf.decay_factor(0.3, 6) == pytest.approx(surf.FACTOR_FLOOR)
    assert surf.recover_factor(1.0) == pytest.approx(1.12)
    assert surf.recover_factor(1.75) == pytest.approx(1.8)
    assert surf.boost_factor(1.0) == pytest.approx(1.1)
    assert surf.boost_factor(1.75) == pytest.approx(1.8)


def test_mood_energy_from_mood_judge(monkeypatch):
    from agi.selflearn import mood_judge

    monkeypatch.setattr(mood_judge, "load_state", lambda path=None: {
        "mood": 90.0, "dims": {"happy": 80.0, "sweet": 60.0,
                               "sad": 0.0, "worry": 0.0}})
    mood, energy = surf.mood_energy()
    assert mood == pytest.approx(0.9)
    assert energy > 0.7
    monkeypatch.setattr(mood_judge, "load_state", lambda path=None: {
        "mood": 30.0, "dims": {"happy": 0.0, "sweet": 0.0,
                               "sad": 80.0, "worry": 60.0}})
    _mood, sad_energy = surf.mood_energy()
    assert mood > _mood and sad_energy < energy


# ------------------------------------------------------------------ 窗口
def test_in_window_boundaries():
    assert surf.in_window(_ts(10, 0)) is True
    assert surf.in_window(_ts(21, 59)) is True
    assert surf.in_window(_ts(9, 59)) is False
    assert surf.in_window(_ts(22, 0)) is False
    assert surf.in_window(_ts(3, 0)) is False


def test_next_window_start():
    assert surf.next_window_start(_ts(8, 0)) == _ts(10, 0)
    assert surf.next_window_start(_ts(12, 0)) == _ts(10, 0, day=28)
    assert surf.next_window_start(_ts(23, 0)) == _ts(10, 0, day=28)


def test_next_roll_at_inside_and_after_window():
    inside = surf.next_roll_at(_ts(12, 0), 1.0)
    assert inside == _ts(12, 0) + surf.gap_minutes(1.0) * 60
    # 21:50 投骰，29 分钟后已过 22:00 → 顺延到明早 10:00
    assert surf.next_roll_at(_ts(21, 50), 1.0) == _ts(10, 0, day=28)


# ------------------------------------------------------------------ 状态文件
def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = surf.default_state()
    state.update({"factor": 0.67, "last_roll_ts": 123.0, "next_roll_ts": 456.0,
                  "last_dice": 6, "rolls_today": 3, "executed_today": 1,
                  "minutes_today": 12.0, "today_date": "2026-10-27",
                  "last_summary": "看了3条"})
    assert surf.save_state(state, path) == path
    loaded = surf.load_state(path)
    assert loaded["factor"] == pytest.approx(0.67)
    assert loaded["last_dice"] == 6 and loaded["rolls_today"] == 3
    assert loaded["last_summary"] == "看了3条"


@pytest.mark.parametrize("payload", ["{bad json", "[]", '"text"', ""])
def test_state_corrupt_falls_back_to_defaults(tmp_path, payload):
    path = tmp_path / "state.json"
    path.write_text(payload, encoding="utf-8")
    state = surf.load_state(path)
    assert state["factor"] == pytest.approx(1.0) and state["last_dice"] == 0


def test_state_type_tolerance(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"factor": 99, "last_dice": "x",
                                "rolls_today": None, "minutes_today": -5}),
                    encoding="utf-8")
    state = surf.load_state(path)
    assert state["factor"] == pytest.approx(surf.FACTOR_MAX)
    assert state["last_dice"] == 0 and state["rolls_today"] == 0
    assert state["minutes_today"] == 0.0


def test_reset_daily_counters():
    state = surf.default_state()
    state.update({"today_date": "2026-10-26", "rolls_today": 5,
                  "executed_today": 2, "minutes_today": 30})
    fresh = surf.reset_daily(state, _ts(12))
    assert fresh["rolls_today"] == 0 and fresh["executed_today"] == 0
    assert fresh["today_date"] == "2026-10-27"
    same = surf.reset_daily(fresh, _ts(13))
    assert same["rolls_today"] == 0


# ------------------------------------------------------------------ 投骰
def test_roll_executes_and_updates_state():
    state = surf.default_state()
    state["factor"] = 1.0
    plan = surf.roll(state, now=_ts(12), rng=FakeRng(6, 5))
    assert plan["executed"] is True
    assert plan["duration_min"] == 12
    assert plan["items"] == 5
    # 6 → ×0.55 = 0.55，再 +0.12 = 0.67
    assert plan["factor"] == pytest.approx(0.67)
    assert state["rolls_today"] == 1 and state["last_dice"] == 6
    assert state["next_roll_ts"] > _ts(12)


def test_roll_not_executed_only_recovers():
    state = surf.default_state()
    state["factor"] = 1.0
    plan = surf.roll(state, now=_ts(12), rng=FakeRng(2, 5))
    assert plan["executed"] is False
    assert plan["factor"] == pytest.approx(1.12)
    assert state["rolls_today"] == 1


def test_roll_user_source_boosts_factor():
    state = surf.default_state()
    state["factor"] = 1.0
    plan = surf.roll(state, now=_ts(12), rng=FakeRng(2, 5), source="user")
    assert plan["factor"] == pytest.approx(1.0 + 0.1 + 0.12)
    assert plan["source"] == "user"


def test_roll_initializes_factor_from_mood(monkeypatch):
    from agi.selflearn import mood_judge
    monkeypatch.setattr(mood_judge, "load_state", lambda path=None: {
        "mood": 100.0, "dims": {"happy": 100.0, "sweet": 100.0}})
    state = surf.default_state()
    state["factor"] = 0.0
    plan = surf.roll(state, now=_ts(12), rng=FakeRng(2, 5))
    assert plan["factor_before"] > 1.0


# ------------------------------------------------------------------ 冲浪一轮
def _fetch_stub(items_per_call=3):
    def _fetch(keyword, limit):
        count = min(int(limit), items_per_call)
        return {"ok": True, "source": "test",
                "items": [{"title": f"{keyword} 的第{i}条", "summary": "摘要",
                           "url": "", "source": "test"} for i in range(count)]}
    return _fetch


def test_run_session_persists_and_observes(tmp_path):
    db_path = tmp_path / "selflearn.sqlite3"
    observed: list[dict] = []
    session = surf.run_session(
        {"dice": 6, "factor": 0.9, "duration_min": 12, "items": 4,
         "source": "scheduled", "started_at": _ts(12)},
        now=_ts(12), db_path=db_path, fetch_fn=_fetch_stub(),
        observe_fn=lambda texts, **kw: observed.append({"texts": texts, **kw}),
        keywords=["手冲咖啡", "机械键盘"])
    assert session["ok"] is True and session["items_seen"] == 4
    assert len(session["summary"]) <= 200
    assert observed and observed[0]["source"] == "self"
    rows = surf.recent_sessions(5, db_path)
    assert len(rows) == 1
    row = rows[0]
    assert row["executed"] == 1 and row["items_seen"] == 4
    assert row["dice"] == 6 and row["duration_min"] == 12
    assert json.loads(json.dumps(row["keywords"], ensure_ascii=False)) == ["手冲咖啡", "机械键盘"]
    assert row["summary"] == session["summary"]


def test_run_session_fetch_failure_is_graceful(tmp_path):
    def _boom(keyword, limit):
        raise OSError("network down")

    session = surf.run_session({"dice": 6, "items": 3, "duration_min": 5},
                               now=_ts(12), db_path=tmp_path / "s.sqlite3",
                               fetch_fn=_boom, observe_fn=lambda *a, **k: None,
                               keywords=["x"])
    assert session["items_seen"] == 0 and session["error"]
    assert surf.recent_sessions(3, tmp_path / "s.sqlite3")[0]["items_seen"] == 0


def test_run_session_observe_failure_does_not_break(tmp_path):
    def _bad_observe(*a, **k):
        raise RuntimeError("interests boom")

    session = surf.run_session({"dice": 5, "items": 2, "duration_min": 5},
                               now=_ts(12), db_path=tmp_path / "s.sqlite3",
                               fetch_fn=_fetch_stub(), observe_fn=_bad_observe,
                               keywords=["y"])
    assert session["ok"] is True and session["items_seen"] == 2
    assert "observe" in session["error"]
    assert len(surf.recent_sessions(3, tmp_path / "s.sqlite3")) == 1


def test_summary_truncated_to_200():
    items = [{"my_take": "很长的看法" + "啊" * 80} for _ in range(8)]
    text = surf.summarize(items, ["关键词一", "关键词二", "关键词三"])
    assert len(text) <= 200 and text


def test_make_my_take_is_short_and_deterministic():
    item = {"title": "一个很长的标题" * 5}
    first = surf.make_my_take(item, "手冲咖啡")
    second = surf.make_my_take(item, "手冲咖啡")
    assert first == second and len(first) <= 60


def test_pick_keywords_falls_back(monkeypatch):
    from agi.selflearn import interests
    monkeypatch.setattr(interests, "next_search_keywords", lambda *a, **k: [])
    assert surf.pick_keywords(limit=2) == list(surf.FALLBACK_KEYWORDS)


def test_fetch_falls_back_to_bilibili(monkeypatch):
    from agi.integrations import bili_ingest
    import agi.integrations.web_lookup as web_lookup

    monkeypatch.setattr(web_lookup, "enabled", lambda: False)
    monkeypatch.setattr(bili_ingest, "search_videos",
                        lambda keyword, limit=3: [{"bvid": "BV1X", "title": "B站结果"}])
    result = surf.fetch("手冲咖啡", 3)
    assert result["ok"] is True and result["source"] == "bilibili"
    assert "bilibili.com" in result["items"][0]["url"]


def test_fetch_all_sources_fail(monkeypatch):
    from agi.integrations import bili_ingest
    import agi.integrations.web_lookup as web_lookup

    monkeypatch.setattr(web_lookup, "enabled", lambda: False)

    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(bili_ingest, "search_videos", _boom)
    result = surf.fetch("x", 3)
    assert result["ok"] is False and result["items"] == [] and result["error"]


def test_sessions_table_structure(tmp_path):
    db_path = tmp_path / "s.sqlite3"
    con = surf.connect(db_path)
    try:
        cols = {row["name"] for row in con.execute("PRAGMA table_info(surf_sessions)")}
    finally:
        con.close()
    assert {"id", "started_at", "ended_at", "dice", "factor", "executed",
            "duration_min", "items_seen", "keywords_json", "summary", "source",
            "error"} <= cols


# ------------------------------------------------------------------ worker
def _recorder():
    events: list[tuple] = []
    return events, (lambda kind, data: events.append((kind, data)))


def test_worker_outside_window_does_not_roll(tmp_path, monkeypatch):
    events, rec = _recorder()
    monkeypatch.setattr(surf, "roll", lambda *a, **k: pytest.fail("窗口外不该投骰"))
    result = surf_worker.run_once(now=_ts(3), recorder=rec,
                                  state_path=tmp_path / "state.json",
                                  db_path=tmp_path / "s.sqlite3")
    assert result["ran"] is False and result["reason"] == "outside_window"
    assert result["next_roll_ts"] == _ts(10, 0)
    assert [kind for kind, _ in events] == ["surf_skipped"]


def test_worker_waits_for_next_roll(tmp_path):
    state = surf.default_state()
    state["next_roll_ts"] = _ts(12) + 600
    surf.save_state(state, tmp_path / "state.json")
    result = surf_worker.run_once(now=_ts(12), state_path=tmp_path / "state.json",
                                  db_path=tmp_path / "s.sqlite3")
    assert result["ran"] is False and result["reason"] == "wait"


def test_worker_skipped_gate_logs_and_saves(tmp_path):
    events, rec = _recorder()
    state = surf.default_state()
    state["factor"] = 1.0
    surf.save_state(state, tmp_path / "state.json")
    result = surf_worker.run_once(now=_ts(12), rng=FakeRng(2, 5), recorder=rec,
                                  state_path=tmp_path / "state.json",
                                  db_path=tmp_path / "s.sqlite3")
    assert result["executed"] is False and result["reason"] == "gate"
    kinds = [kind for kind, _ in events]
    assert kinds == ["surf_roll", "surf_skipped"]
    saved = surf.load_state(tmp_path / "state.json")
    assert saved["factor"] == pytest.approx(1.12) and saved["rolls_today"] == 1


def test_worker_executes_and_updates_counters(tmp_path, monkeypatch):
    events, rec = _recorder()
    monkeypatch.setattr(surf, "pick_keywords", lambda *a, **k: ["测试关键词"])
    state = surf.default_state()
    state["factor"] = 1.0
    surf.save_state(state, tmp_path / "state.json")
    result = surf_worker.run_once(now=_ts(12), rng=FakeRng(6, 4), recorder=rec,
                                  state_path=tmp_path / "state.json",
                                  db_path=tmp_path / "s.sqlite3",
                                  fetch_fn=_fetch_stub(),
                                  observe_fn=lambda *a, **k: None,
                                  window_hours=(10, 22))
    assert result["executed"] is True
    kinds = [kind for kind, _ in events]
    assert "surf_roll" in kinds and "surf_executed" in kinds
    saved = surf.load_state(tmp_path / "state.json")
    assert saved["executed_today"] == 1 and saved["minutes_today"] == 12
    assert saved["last_session_id"].startswith("SURF-")
    assert len(surf.recent_sessions(3, tmp_path / "s.sqlite3")) == 1


def test_worker_user_trigger_bypasses_window_and_gate(tmp_path, monkeypatch):
    events, rec = _recorder()
    monkeypatch.setattr(surf, "pick_keywords", lambda *a, **k: ["测试关键词"])
    state = surf.default_state()
    state["factor"] = 0.2
    surf.save_state(state, tmp_path / "state.json")
    result = surf_worker.run_once(now=_ts(3), force=True, source="user",
                                  rng=FakeRng(1, 2), recorder=rec,
                                  state_path=tmp_path / "state.json",
                                  db_path=tmp_path / "s.sqlite3",
                                  fetch_fn=_fetch_stub(),
                                  observe_fn=lambda *a, **k: None)
    assert result["executed"] is True and result["plan"]["forced"] is True
    assert result["plan"]["source"] == "user"
    assert surf.recent_sessions(3, tmp_path / "s.sqlite3")[0]["source"] == "user"


def test_worker_engine_off_pauses_loop():
    stop = threading.Event()
    calls: list[int] = []
    thread = threading.Thread(target=surf_worker.loop, daemon=True, kwargs={
        "stop_event": stop, "engine_state": lambda: calls.append(1) or False,
        "poll_s": 0.01})
    thread.start()
    time.sleep(0.05)
    stop.set()
    thread.join(timeout=2)
    assert calls and not thread.is_alive()


def test_worker_loop_exits_on_stop(tmp_path):
    stop = threading.Event()
    stop.set()
    result = surf_worker.loop(stop_event=stop,
                              state_path=tmp_path / "state.json",
                              db_path=tmp_path / "s.sqlite3")
    assert result["iterations"] == 0


def test_worker_main_status_and_once(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(surf_worker, "run_once", lambda **kw: {
        "ok": True, "ran": True, "executed": False, "plan": {"dice": 2},
        "reason": "gate", "state": {}})
    assert surf_worker.main(["--status"]) == 0
    out = capsys.readouterr().out
    assert '"window"' in out and '"factor"' in out
    assert surf_worker.main(["--once", "--reason", "user"]) == 0
    out = capsys.readouterr().out
    assert '"gate"' in out


def test_status_snapshot(tmp_path):
    state = surf.default_state()
    state.update({"factor": 0.8, "next_roll_ts": _ts(12) + 1800,
                  "rolls_today": 2, "executed_today": 1, "minutes_today": 9,
                  "today_date": "2026-10-27"})
    data = surf.status(now=_ts(12), state=state, db_path=tmp_path / "s.sqlite3",
                       enabled_override=True)
    assert data["ok"] is True and data["enabled"] is True
    assert data["in_window"] is True and data["window"] == [10, 22]
    assert data["factor"] == pytest.approx(0.8)
    assert data["today"] == {"date": state["today_date"], "rolls": 2,
                             "executed": 1, "minutes": 9}
    assert data["next_roll_in_s"] == pytest.approx(1800, abs=1)
    assert isinstance(data["recent_sessions"], list)


# ------------------------------------------------------------------ HTTP 路由
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


def test_http_surf_status_route(tmp_path):
    gw = _gw(tmp_path)
    status, payload = _call(gw, "GET", "/api/surf/status")
    assert status == 200 and payload["ok"] is True
    assert payload["window"] == [10, 22]
    assert "factor" in payload and "thread_alive" in payload
    status, _ = _call(gw, "GET", "/api/surf/status", token=None)
    assert status == 401


def test_http_surf_trigger_route(tmp_path, monkeypatch):
    gw = _gw(tmp_path)
    called: list[dict] = []
    monkeypatch.setattr(surf_worker, "run_once",
                        lambda **kw: called.append(kw) or {"ok": True})
    status, payload = _call(gw, "POST", "/api/surf/trigger",
                            body=json.dumps({"reason": "user"}).encode())
    assert status == 202 and payload["queued"] is True
    assert payload["reason"] == "user"
    for _ in range(50):          # 后台线程最多等 0.5s
        if called:
            break
        time.sleep(0.01)
    assert called and called[0]["force"] is True and called[0]["source"] == "user"
    assert any(entry.get("kind") == "surf_triggered" for entry in gw.recent)
