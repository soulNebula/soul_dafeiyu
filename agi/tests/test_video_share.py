# -*- coding: utf-8 -*-
"""P12-C：主动消息分享刷到的视频（文字 + 封面图）。

全 mock：印象库走 ``bili_ingest.recent_impressions`` 替身，封面下载走
``simple_tick._fetch_bytes`` 替身，不联网、不写真实数据目录。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agi.integrations import bili_ingest
from agi.selflearn import db, proactive, simple_tick, trust
from agi.wechat_gateway import proactive_worker

T0 = time.mktime((2026, 10, 27, 10, 0, 0, 0, 0, -1))
JPEG = b"\xff\xd8\xff\xe0" + b"cover-bytes" * 4


class FakeClient:
    def __init__(self, queue: list[str] | None = None, text: str = ""):
        self.queue = list(queue or [])
        self.text = text
        self.prompts: list[str] = []
        self.purposes: list[str] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.prompts.append(str(messages[-1].get("content") or ""))
        self.purposes.append(str(kwargs.get("purpose") or ""))
        if self.queue:
            return {"ok": True, "text": self.queue.pop(0)}
        return {"ok": True, "text": self.text}


def _seed(tmp_path, name="s.sqlite3"):
    path = tmp_path / name
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "今天有点累", T0 - 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}), T0))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    return path


def _impression(**over):
    row = {
        "bvid": "BV1TEST001", "title": "手冲咖啡水温实验",
        "cover_url": "https://i0.hdslb.com/bfs/cover.jpg",
        "summary": "对比 88 度和 92 度水温的差别",
        "my_take": "我觉得水温比手法更重要", "value": 0.82,
        "share_line": "试了下不同水温，差别比想象中大",
        "shared": 0, "created_at": T0 - 3600,
    }
    row.update(over)
    return row


CARE = {"kind": "care", "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": "", "topic": ""}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("PROACTIVE_STYLE", "simple")
    monkeypatch.setenv("PROACTIVE_VIDEO_SHARE", "1")
    monkeypatch.setattr(simple_tick, "GATEWAY_LOG", tmp_path / "gateway.log")
    monkeypatch.setattr(simple_tick, "VIDEO_MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [dict(CARE)])
    monkeypatch.setattr(proactive.initiative, "decide", lambda *a, **k: {})
    monkeypatch.setattr(bili_ingest, "recent_impressions", lambda *a, **k: [])
    yield


@pytest.fixture
def impressions(monkeypatch):
    def _set(rows):
        monkeypatch.setattr(bili_ingest, "recent_impressions",
                            lambda *a, **k: list(rows))
    return _set


def _tick(path, client=None, *, now=T0):
    return simple_tick.tick_simple(
        path, client=client if client is not None else FakeClient(text="看到一个说法，挺有意思"),
        now=now, cooldown=0, min_score=0, respect_state=False)


def _write_log(path: Path, entries: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in entries) + "\n",
                    encoding="utf-8")


def _send_log(ts: float, *, kind: str = "share_video") -> dict:
    return {"ts": ts, "kind": "proactive_send", "friend": "好友A",
            "intent_kind": kind, "text": "试了下不同水温"}


# ------------------------------------------------------------------ 选中与结构
def test_video_share_selected_and_structure(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression()])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)  # 概率命中
    monkeypatch.setattr(simple_tick, "_fetch_bytes", lambda *a, **k: JPEG)

    row = _tick(path, FakeClient(text="不该走到这里"))

    assert row["available"] is True
    assert row["kind"] == "share_video"
    assert row["style"] == "simple"
    video = row["video_share"]
    assert video["bvid"] == "BV1TEST001"
    assert video["value"] == pytest.approx(0.82)
    assert video["share_line"] == "试了下不同水温，差别比想象中大"
    assert video["cover_sent"] is True
    assert row["messages"][0] == "试了下不同水温，差别比想象中大"
    assert 1 <= len(row["messages"]) <= 2
    assert all(len(item) <= 45 for item in row["messages"])
    assert len(row["messages"][-1]) <= 12
    bubbles = row["media"]["bubbles"]
    assert len(bubbles) == 1 and bubbles[0]["type"] == "image"
    cover = Path(bubbles[0]["path"])
    assert cover.exists() and cover.read_bytes() == JPEG
    assert row["message_id"] and row["turn_id"]
    # proactive 表记了 share_video
    con = db.connect(path)
    try:
        kinds = [r[0] for r in con.execute("SELECT kind FROM proactive").fetchall()]
    finally:
        con.close()
    assert kinds == ["share_video"]


def test_no_candidate_does_not_participate(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    row = _tick(path)
    assert row["available"] is True
    assert row["kind"] == "care"
    assert "video_share" not in row
    assert row["message"] == "看到一个说法，挺有意思"


def test_probability_miss_keeps_normal_kind(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression()])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.99)  # 未命中
    monkeypatch.setattr(simple_tick, "_fetch_bytes", lambda *a, **k: JPEG)
    row = _tick(path)
    assert row["kind"] == "care"


def test_switch_off_keeps_behavior_and_skips_db_query(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    calls: list[int] = []

    def _spy(*a, **k):
        calls.append(1)
        return [_impression()]

    monkeypatch.setenv("PROACTIVE_VIDEO_SHARE", "0")
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    monkeypatch.setattr(bili_ingest, "recent_impressions", _spy)
    row = _tick(path)
    assert calls == []                      # 开关关：连印象库都不查
    assert row["kind"] == "care"
    assert "video_share" not in row


def test_switch_reads_config_file(tmp_path, monkeypatch):
    cfg = tmp_path / "wechat.json"
    monkeypatch.delenv("PROACTIVE_VIDEO_SHARE", raising=False)
    monkeypatch.setattr(simple_tick, "WECHAT_CONFIG", cfg)
    cfg.write_text(json.dumps({"proactive_video_share": False}), encoding="utf-8")
    assert simple_tick.video_share_enabled() is False
    cfg.write_text(json.dumps({"proactive_video_share": True}), encoding="utf-8")
    assert simple_tick.video_share_enabled() is True
    cfg.write_text(json.dumps({}), encoding="utf-8")
    assert simple_tick.video_share_enabled() is True   # 默认 true


# ------------------------------------------------------------------ 频率限制
def test_video_quota_daily_limit(tmp_path, monkeypatch):
    _write_log(Path(simple_tick.GATEWAY_LOG), [_send_log(T0 - 60), _send_log(T0 - 7200)])
    assert simple_tick.video_quota(T0) == (False, "daily_limit")


def test_video_quota_gap(tmp_path, monkeypatch):
    _write_log(Path(simple_tick.GATEWAY_LOG), [_send_log(T0 - 3600)])
    assert simple_tick.video_quota(T0) == (False, "gap")
    _write_log(Path(simple_tick.GATEWAY_LOG), [_send_log(T0 - 4 * 3600)])
    assert simple_tick.video_quota(T0) == (True, "ok")


def test_video_quota_ignores_other_kinds(tmp_path):
    _write_log(Path(simple_tick.GATEWAY_LOG), [
        _send_log(T0 - 60, kind="care"), _send_log(T0 - 120, kind="followup"),
        {"ts": T0 - 30, "kind": "proactive_skipped", "intent_kind": "share_video"},
    ])
    assert simple_tick.video_quota(T0) == (True, "ok")
    assert simple_tick._video_send_times(T0) == []


def test_daily_limit_blocks_video_but_allows_normal(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression()])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    _write_log(Path(simple_tick.GATEWAY_LOG), [_send_log(T0 - 60), _send_log(T0 - 7200)])
    row = _tick(path)
    assert row["kind"] == "care"


# ------------------------------------------------------------------ 文案与硬检查
def test_meta_topic_regenerated_then_used(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression(share_line="我刷到一条视频，太有意思了")])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    monkeypatch.setattr(simple_tick, "_fetch_bytes", lambda *a, **k: JPEG)
    client = FakeClient(text="这个想法值得试试看")
    row = _tick(path, client)
    assert row["available"] is True and row["kind"] == "share_video"
    assert "video_meta_topic" in row["regen_reason"]
    assert row["message"] == "这个想法值得试试看"


def test_meta_topic_twice_discards(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression(share_line="我刷到一条视频，太有意思了")])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    monkeypatch.setattr(simple_tick, "_fetch_bytes", lambda *a, **k: JPEG)
    client = FakeClient(text="还是刷到的一个视频")
    row = _tick(path, client)
    assert row["available"] is False
    assert row["reason"] == "discarded"
    assert "video_meta_topic" in row["discard_reason"]
    assert row["kind"] == "share_video"
    assert row["media"] is None
    assert row["video_share"]["cover_sent"] is False


def test_share_line_missing_uses_share_copy_purpose(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression(share_line="")])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    monkeypatch.setattr(simple_tick, "_fetch_bytes", lambda *a, **k: JPEG)
    client = FakeClient(text="水温这事比我想的有意思")
    row = _tick(path, client)
    assert "share_copy" in client.purposes
    assert row["available"] is True and row["kind"] == "share_video"
    assert row["message"] == "水温这事比我想的有意思"


def test_share_line_too_long_is_replaced(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    long_line = "这句话特别特别长" + "啊" * 50
    impressions([_impression(share_line=long_line)])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    monkeypatch.setattr(simple_tick, "_fetch_bytes", lambda *a, **k: JPEG)
    row = _tick(path, FakeClient(text="这个思路挺妙的"))
    assert row["available"] is True
    assert len(row["message"]) <= 45


def test_similarity_rule_prefers_clean_share_line():
    assert simple_tick._text_similarity("完全不同的一句话", "试了下不同水温，差别比想象中大") < 0.2
    assert simple_tick._prefer_share_line(
        "完全不同的一句话", "试了下不同水温，差别比想象中大", []) == "试了下不同水温，差别比想象中大"
    close = "试了下不同水温，差别比想象中大一点"
    assert simple_tick._prefer_share_line(close, "试了下不同水温，差别比想象中大", []) == close
    # share_line 自己违规时不拿它兜底
    assert simple_tick._prefer_share_line(
        "完全不同的一句话", "我刷到一条视频", []) == "完全不同的一句话"


# ------------------------------------------------------------------ 封面下载
def test_cover_download_success_and_cache(tmp_path, monkeypatch):
    calls: list[int] = []

    def _fetch(*a, **k):
        calls.append(1)
        return JPEG

    monkeypatch.setattr(simple_tick, "_fetch_bytes", _fetch)
    first = simple_tick.download_cover(_impression(), T0)
    assert first is not None and first.exists()
    assert first.suffix == ".jpg" and first.read_bytes() == JPEG
    second = simple_tick.download_cover(_impression(), T0)
    assert second == first
    assert len(calls) == 1          # 已下载则复用，不重复请求


@pytest.mark.parametrize("payload", [None, b"", b"<html>404</html>"],
                         ids=["network_error", "empty", "html"])
def test_cover_download_failures(tmp_path, monkeypatch, payload):
    def _fetch(*a, **k):
        if payload is None:
            raise OSError("network down")
        return payload

    monkeypatch.setattr(simple_tick, "_fetch_bytes", _fetch)
    assert simple_tick.download_cover(_impression(), T0) is None


def test_cover_download_rejects_oversize(tmp_path, monkeypatch):
    monkeypatch.setattr(simple_tick, "_fetch_bytes",
                        lambda *a, **k: b"x" * (simple_tick.VIDEO_MAX_BYTES + 1))
    assert simple_tick.download_cover(_impression(), T0) is None


def test_tick_without_cover_sends_text_only(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression()])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)

    def _boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(simple_tick, "_fetch_bytes", _boom)
    row = _tick(path)
    assert row["available"] is True and row["kind"] == "share_video"
    assert row["media"] is None
    assert row["video_share"]["cover_sent"] is False


def test_no_url_cover_skips_download(tmp_path, monkeypatch, impressions):
    path = _seed(tmp_path)
    impressions([_impression(cover_url="")])
    monkeypatch.setattr(simple_tick.random, "random", lambda: 0.0)
    called: list[int] = []
    monkeypatch.setattr(simple_tick, "_fetch_bytes", lambda *a, **k: called.append(1) or JPEG)
    row = _tick(path)
    assert called == [] and row["media"] is None


# ------------------------------------------------------------------ worker 发放
class _FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {"proactive_enabled": True, "allowed_contacts": ["好友A"],
                    "proactive_contacts": [], **cfg}
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}
        self.media_calls: list[tuple] = []

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        return {"ok": True, "status": "sent"}

    def _record(self, kind, data):
        self.records.append((kind, data))

    def send_bubbles(self, contact, items, proactive=False):
        self.media_calls.append((contact, items, proactive))
        return [{"ok": True, "status": "sent", "type": item.get("type")} for item in items]


def _video_pro(cover_path: Path | None):
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {
                "ok": True, "available": True, "style": "simple",
                "kind": "share_video",
                "messages": ["试了下不同水温，差别比想象中大"],
                "message": "试了下不同水温，差别比想象中大",
                "turn_id": "T-1",
                "reason": "B站印象 value=0.82",
                "score": 0.82,
                "video_share": {"bvid": "BV1TEST001", "title": "手冲咖啡水温实验",
                                "value": 0.82,
                                "share_line": "试了下不同水温，差别比想象中大",
                                "cover_sent": cover_path is not None},
                "media": ({"bubbles": [{"type": "image", "path": str(cover_path)}],
                           "reason": "video_cover"} if cover_path else None)}}


def test_worker_sends_cover_logs_and_marks_shared(tmp_path, monkeypatch):
    from agi.selflearn import ticker

    cover = tmp_path / "media" / "video_cover_BV1TEST001.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(JPEG)
    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _video_pro(cover))
    marked: list[str] = []
    monkeypatch.setattr(bili_ingest, "mark_shared",
                        lambda bvid, **k: marked.append(bvid) or True)

    g = _FakeGateway(media_enabled=True)
    out = proactive_worker.run_cycle(g, client=object(), now=T0)

    assert out["sent"] == 1 and out["media_sent"] == 1
    assert [c[1][0]["type"] for c in g.media_calls] == ["image"]
    assert marked == ["BV1TEST001"]
    logs = [data for kind, data in g.records if kind == "proactive_video_share"]
    assert len(logs) == 1
    assert logs[0]["bvid"] == "BV1TEST001"
    assert logs[0]["value"] == pytest.approx(0.82)
    assert logs[0]["cover_sent"] is True
    assert logs[0]["media_sent"] == 1
    assert logs[0]["delivered"] is True and logs[0]["marked_shared"] is True
    # 频控读的就是这条日志
    assert any(kind == "proactive_send" and data.get("intent_kind") == "share_video"
               for kind, data in g.records)


def test_worker_text_only_when_media_disabled(tmp_path, monkeypatch):
    from agi.selflearn import ticker

    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _video_pro(None))
    marked: list[str] = []
    monkeypatch.setattr(bili_ingest, "mark_shared",
                        lambda bvid, **k: marked.append(bvid) or True)
    g = _FakeGateway(media_enabled=False)
    out = proactive_worker.run_cycle(g, client=object(), now=T0)
    assert out["sent"] == 1 and out["media_sent"] == 0
    assert g.media_calls == []
    assert marked == ["BV1TEST001"]


def test_worker_failed_text_does_not_mark_shared(tmp_path, monkeypatch):
    from agi.selflearn import ticker

    monkeypatch.setattr(ticker, "run_once", lambda *a, **k: _video_pro(None))
    marked: list[str] = []
    monkeypatch.setattr(bili_ingest, "mark_shared",
                        lambda bvid, **k: marked.append(bvid) or True)

    class FailGateway(_FakeGateway):
        def _send(self, contact, text, **kw):
            self.sent.append((contact, text, kw))
            return {"ok": False, "status": "failed", "error": "wxauto boom"}

    g = FailGateway(media_enabled=True)
    out = proactive_worker.run_cycle(g, client=object(), now=T0)
    assert out["sent"] == 0
    assert marked == []
    logs = [data for kind, data in g.records if kind == "proactive_video_share"]
    assert logs and logs[0]["delivered"] is False and logs[0]["marked_shared"] is False


# ------------------------------------------------------------------ 隔离/兼容
def test_legacy_style_ignores_video(tmp_path, monkeypatch):
    path = _seed(tmp_path)
    monkeypatch.setenv("PROACTIVE_STYLE", "legacy")
    monkeypatch.delenv("PROACTIVE_VIDEO_SHARE", raising=False)
    monkeypatch.setattr(bili_ingest, "recent_impressions", lambda *a, **k: [_impression()])
    row = proactive.tick(path, client=FakeClient(text="看到一个说法，挺有意思"),
                         now=T0, cooldown=0, min_score=0, respect_state=False)
    assert row.get("style") != "simple"
    assert "video_share" not in row


def test_pytest_default_is_off_without_env(tmp_path, monkeypatch):
    """没有 env、且读的是生产 config 路径时默认关（避免测试误读生产库）。"""
    monkeypatch.delenv("PROACTIVE_VIDEO_SHARE", raising=False)
    monkeypatch.setattr(simple_tick, "WECHAT_CONFIG", simple_tick.DEFAULT_WECHAT_CONFIG)
    assert simple_tick.video_share_enabled() is False


def test_video_candidates_filter_and_order(tmp_path, monkeypatch):
    rows = [
        _impression(bvid="BV_OLD", value=0.9, created_at=T0 - 8 * 86400),
        _impression(bvid="BV_LOW", value=0.3, created_at=T0 - 60),
        _impression(bvid="BV_SHARED", value=0.9, created_at=T0 - 60, shared=1),
        _impression(bvid="BV_GOOD", value=0.7, created_at=T0 - 3600),
        _impression(bvid="BV_BEST", value=0.9, created_at=T0 - 7200),
    ]
    monkeypatch.setattr(bili_ingest, "recent_impressions", lambda *a, **k: rows)
    picked = simple_tick.recent_video_candidates(T0)
    assert [item["bvid"] for item in picked] == ["BV_BEST", "BV_GOOD"]
