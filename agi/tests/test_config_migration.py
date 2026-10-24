# -*- coding: utf-8 -*-
"""P0-5：运行时配置迁移 + per-contact 概率闸门 + autonomy 静音对齐 + 残留清理。

背景：``agi/data/wechat/config.json``（未跟踪的运行时数据）会把 DEFAULTS 覆盖回
事故值（间隔 30/300 秒、``quiet_hours: []``），导致 task-15/16/17 的节奏修复在本机
不生效。这里锁住迁移规则：幂等、原子写回、只升旧事故值、用户显式新值不动。
"""
from __future__ import annotations

import datetime
import json
import os
import queue

import pytest

from agi.selflearn import autonomy, ticker
from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway import proactive_worker
from agi.wechat_gateway.server import Gateway

CONTACTS = ["好友A", "好友B", "好友C"]


# --------------------------------------------------------------------------- #
# 夹具：把 CONFIG_PATH 指到 tmp，测试绝不碰真实 agi/data/wechat/config.json
# --------------------------------------------------------------------------- #
@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "wechat" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(gw_config, "CONFIG_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _contact_db(tmp_path, monkeypatch):
    """contact_db 固定到 tmp（run_cycle 会写 proactive_events）。"""
    path = tmp_path / "contacts.sqlite3"
    monkeypatch.setattr("agi.wechat_gateway.config.contact_db", lambda contact: path)
    return path


def _write(path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _migration_logs(tmp_path) -> list[dict]:
    log = tmp_path / "wechat" / "gateway.log"
    if not log.exists():
        return []
    entries = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("kind") == "config_migrated":
            entries.append(entry)
    return entries


# --------------------------------------------------------------------------- #
# 配置迁移
# --------------------------------------------------------------------------- #
def test_old_accident_config_is_migrated_and_written_back(config_path, tmp_path):
    old = {
        "enabled": True,
        "port": 801,
        "proactive_interval_min_s": 30,
        "proactive_interval_max_s": 300,
        "quiet_hours": [],
        "custom_key": "keep-me",
    }
    _write(config_path, old)

    cfg = gw_config.load()
    assert cfg["proactive_interval_min_s"] == 60
    assert cfg["proactive_interval_max_s"] == 7200
    assert cfg["quiet_hours"] == [23, 8]
    # 其他键保留
    assert cfg["enabled"] is True and cfg["custom_key"] == "keep-me"
    # 缺 probability_min/max → 不写文件，内存里由 DEFAULTS 提供
    assert cfg["proactive_probability_min"] == 0.3
    assert cfg["proactive_probability_max"] == 0.9
    on_disk = _read(config_path)
    assert on_disk["proactive_interval_min_s"] == 60
    assert on_disk["proactive_interval_max_s"] == 7200
    assert on_disk["quiet_hours"] == [23, 8]
    assert "proactive_probability_min" not in on_disk
    assert "proactive_probability_max" not in on_disk
    # config_migrated 日志（改了哪些键、旧值→新值）
    logs = _migration_logs(tmp_path)
    assert len(logs) == 1
    changes = logs[0]["changes"]
    assert changes["proactive_interval_min_s"] == {"old": 30, "new": 60}
    assert changes["proactive_interval_max_s"] == {"old": 300, "new": 7200}
    assert changes["quiet_hours"]["new"] == [23, 8]
    assert str(config_path) in logs[0]["path"]


def test_migration_is_idempotent_and_does_not_touch_file_again(config_path, tmp_path):
    _write(config_path, {"proactive_interval_min_s": 30,
                         "proactive_interval_max_s": 300, "quiet_hours": []})
    gw_config.load()
    mtime = os.stat(config_path).st_mtime_ns
    logs_before = len(_migration_logs(tmp_path))

    cfg = gw_config.load()
    assert cfg["proactive_interval_min_s"] == 60
    assert os.stat(config_path).st_mtime_ns == mtime      # 第二次没写盘
    assert len(_migration_logs(tmp_path)) == logs_before  # 也没有新日志


def test_explicit_new_values_are_not_migrated(config_path, tmp_path):
    new = {"proactive_interval_min_s": 3600, "proactive_interval_max_s": 7200,
           "quiet_hours": [1, 5]}
    _write(config_path, new)
    mtime = os.stat(config_path).st_mtime_ns
    cfg = gw_config.load()
    assert cfg["proactive_interval_min_s"] == 3600
    assert cfg["proactive_interval_max_s"] == 7200
    assert cfg["quiet_hours"] == [1, 5]
    assert os.stat(config_path).st_mtime_ns == mtime
    assert _migration_logs(tmp_path) == []


def test_old_quiet_hours_0_7_is_migrated(config_path, tmp_path):
    _write(config_path, {"proactive_interval_min_s": 1800,
                         "proactive_interval_max_s": 5400,
                         "quiet_hours": [0, 7]})
    cfg = gw_config.load()
    assert cfg["quiet_hours"] == [23, 8]
    # 1800/5400 是 task-15 的过渡值，按新规则不算事故值（>=120 / >=600）
    assert cfg["proactive_interval_min_s"] == 1800
    assert cfg["proactive_interval_max_s"] == 5400


def test_corrupt_json_keeps_defaults_and_file_untouched(config_path, tmp_path):
    config_path.write_text("{not json", encoding="utf-8")
    before = config_path.read_text(encoding="utf-8")
    cfg = gw_config.load()
    assert cfg["proactive_interval_min_s"] == 60
    assert config_path.read_text(encoding="utf-8") == before
    assert _migration_logs(tmp_path) == []


def test_missing_file_loads_defaults(config_path):
    assert not config_path.exists()
    cfg = gw_config.load()
    assert cfg["proactive_interval_min_s"] == 60
    assert cfg["quiet_hours"] == [23, 8]
    assert not config_path.exists()  # 不因为「缺文件」而新建


def test_old_idle_600_is_migrated_to_900(config_path, tmp_path):
    """旧默认 proactive_idle_s=600（10 分钟）→ 900（15 分钟），否则运行时覆盖 DEFAULTS。"""
    _write(config_path, {"proactive_idle_s": 600})
    cfg = gw_config.load()
    assert cfg["proactive_idle_s"] == 900
    assert _read(config_path)["proactive_idle_s"] == 900
    logs = _migration_logs(tmp_path)
    assert logs and logs[-1]["changes"]["proactive_idle_s"] == {"old": 600, "new": 900}


def test_explicit_idle_value_is_not_migrated(config_path, tmp_path):
    _write(config_path, {"proactive_idle_s": 1200})
    mtime = os.stat(config_path).st_mtime_ns
    cfg = gw_config.load()
    assert cfg["proactive_idle_s"] == 1200
    assert os.stat(config_path).st_mtime_ns == mtime


def test_dead_config_and_idle_cleanup():
    assert "proactive_interval_s" not in gw_config.DEFAULTS   # 死配置已删
    assert gw_config.DEFAULTS["proactive_idle_s"] == 900     # 600 → 900（15 分钟）
    assert gw_config.DEFAULTS["proactive_interval_min_s"] == 60
    assert gw_config.DEFAULTS["proactive_interval_max_s"] == 7200
    assert gw_config.DEFAULTS["proactive_probability_min"] == 0.3
    assert gw_config.DEFAULTS["proactive_probability_max"] == 0.9


def test_status_reads_new_interval_keys(tmp_path):
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": [CONTACTS[0]], "send_mode": "auto",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "proactive_interval_min_s": 60, "proactive_interval_max_s": 7200,
        "proactive_probability_min": 0.3, "proactive_probability_max": 0.9,
    }
    status = Gateway(cfg).status()
    proactive = status["proactive"]
    assert proactive["interval_s"] == {"min": 60, "max": 7200}
    assert proactive["probability"] == {"min": 0.3, "max": 0.9}


# --------------------------------------------------------------------------- #
# per-contact 概率闸门
# --------------------------------------------------------------------------- #
class FakeGateway:
    def __init__(self, **cfg):
        self.cfg = {
            "proactive_enabled": True,
            "proactive_probability_min": 0.3,
            "proactive_probability_max": 0.9,
            "allowed_contacts": list(CONTACTS),
            "proactive_contacts": [],
            **cfg,
        }
        self.sent: list[tuple] = []
        self.records: list[tuple] = []
        self.last_turns: dict = {}
        self.last_proactive: dict = {}

    def _send(self, contact, text, **kw):
        self.sent.append((contact, text, kw))
        return {"ok": True, "status": "sent"}

    def _record(self, kind, data):
        self.records.append((kind, data))


def _tick(turn_id="T-PRO-1"):
    return {"ok": True, "ran": True, "gate": {"allowed": True},
            "proactive": {"ok": True, "available": True,
                          "messages": ["在吗"], "message": "在吗",
                          "turn_id": turn_id}}


def test_probability_gate_is_per_contact(monkeypatch):
    calls: list[str] = []

    def fake_tick(path, client, now=None):
        calls.append(str(path))
        return _tick()

    monkeypatch.setattr(ticker, "run_once", fake_tick)
    monkeypatch.setattr(proactive_worker.random, "uniform", lambda lo, hi: 0.6)
    rolls = iter([0.1, 0.5, 0.95])   # 前两个 <0.6 命中，第三个跳过
    monkeypatch.setattr(proactive_worker.random, "random", lambda: next(rolls))

    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=1000.0)

    # 每个联系人独立掷骰：A/B 发，C 跳过
    assert [s[0] for s in g.sent] == ["好友A", "好友B"]
    assert len(calls) == 2                       # 被跳过的 C 不调 LLM
    skipped = [s for s in out["skipped"] if s.get("reason") == "probability"]
    assert [s["contact"] for s in skipped] == ["好友C"]
    assert skipped[0]["probability"] == pytest.approx(0.6)
    logs = [d for k, d in g.records if k == "proactive_skipped"]
    assert len(logs) == 1 and logs[0]["contact"] == "好友C"
    assert logs[0]["probability"] == pytest.approx(0.6)
    assert 0.3 <= logs[0]["probability"] <= 0.9
    assert out["sent"] == 2


def test_all_contacts_skipped_keeps_legacy_return_shape(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: calls.append(1) or _tick())
    monkeypatch.setattr(proactive_worker.random, "uniform", lambda lo, hi: 0.6)
    monkeypatch.setattr(proactive_worker.random, "random", lambda: 0.99)

    g = FakeGateway()
    out = proactive_worker.run_cycle(g, client=object(), now=1000.0)
    assert calls == [] and g.sent == []
    assert out["ran"] is False and out["reason"] == "probability_skip"
    assert out["probability"] == pytest.approx(0.6)
    assert out["roll"] == pytest.approx(0.99)
    assert len([1 for k, _ in g.records if k == "proactive_skipped"]) == len(CONTACTS)


def test_probability_samples_stay_in_configured_range():
    g = FakeGateway()
    samples = [proactive_worker._probability(g) for _ in range(100)]
    assert all(0.3 <= p <= 0.9 for p in samples)
    assert len({round(p, 3) for p in samples}) > 10


# --------------------------------------------------------------------------- #
# autonomy 静音对齐 + 跨零点 gate
# --------------------------------------------------------------------------- #
def _ts(hour: int) -> float:
    return datetime.datetime(2026, 10, 25, hour, 0, 0).timestamp()


def test_autonomy_default_quiet_hours_matches_gateway():
    assert autonomy.DEFAULT["quiet_hours"] == gw_config.DEFAULTS["quiet_hours"] == [23, 8]


@pytest.mark.parametrize("hour,allowed", [
    (23, False), (0, False), (7, False),   # 夜间静音段（跨零点）
    (8, True), (12, True), (22, True),     # 白天/边界解除
])
def test_autonomy_quiet_hours_cross_midnight(tmp_path, hour, allowed):
    path = tmp_path / "autonomy.sqlite3"
    gate = autonomy.gate(path, now=_ts(hour))
    if allowed:
        assert gate["allowed"] is True, gate
    else:
        assert gate["allowed"] is False
        assert gate["reason"] == "quiet_hours"
