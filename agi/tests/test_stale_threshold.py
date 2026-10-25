# -*- coding: utf-8 -*-
"""P0-16：死区哨兵阈值动态化（避免 proactive 长间隔误报）。

真机最终窗口（2026-10-25 20:00 起）出现 1 条 `gateway_stale` 误报：
调度间隔已经是 ``uniform(60, 7200)``（最长 2h），但哨兵阈值固定 1800s，
**一次正常的长等待就必然触发**。本文件锁住：

- 有效阈值 = ``max(gateway_stale_alert_s, proactive_interval_max_s * 1.2, 3600)``
  → 默认 1800 + 上限 7200 ⇒ **8640s（2.4h）**
- ``proactive_schedule`` 是内部调度事件，**不刷新** ``last_event_ts``
  （只有 inbound/outbound/send/queue 等真实活动才算）
- 30 分钟节流不变

P0-24（2026-10-26 真机噪音）：``proactive_enabled=false`` + 夜间静默时，
哨兵仍每 30 分钟报一条 gateway_stale（idle 已 14040s）——主动关闭后本来就
"预期不发送"，报死区是纯噪音。本文件同时锁住三种抑制条件
（disabled / stopped / quiet）与"真实死区照旧告警"。
"""
from __future__ import annotations

import time

import pytest

from agi.wechat_gateway.server import Gateway

CONTACT = "好友A"


def _gw(tmp_path, *, stop_path=None, **overrides) -> Gateway:
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": [CONTACT], "send_mode": "auto",
        # P0-24：默认把主动消息开着 = "真实死区"场景；抑制相关测试显式覆盖
        "proactive_enabled": True,
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg, stop_path=stop_path)


def _stale_events(g) -> list[dict]:
    return [e for e in g.recent if e["kind"] == "gateway_stale"]


# --------------------------------------------------------------------------- #
# 阈值公式
# --------------------------------------------------------------------------- #
def test_effective_threshold_defaults_to_8640(tmp_path):
    """默认配置：1800 但间隔上限 7200 → 8640s（2.4h）。"""
    g = _gw(tmp_path)
    assert g.stale_threshold() == 8640.0


def test_threshold_formula_variants(tmp_path):
    # 间隔上限 300 → 360 被下限 3600 兜底
    assert _gw(tmp_path, proactive_interval_max_s=300).stale_threshold() == 3600.0
    # 显式基础阈值更大 → 用基础阈值
    assert _gw(tmp_path, gateway_stale_alert_s=10000).stale_threshold() == 10000.0
    # slack 可配
    assert _gw(tmp_path, proactive_interval_max_s=7200,
               gateway_stale_slack=1.0).stale_threshold() == 7200.0
    # 非法值回退默认
    assert _gw(tmp_path, proactive_interval_max_s="bad",
               gateway_stale_alert_s="bad").stale_threshold() == 8640.0


# --------------------------------------------------------------------------- #
# 误报消除：2h 不报，3h 才报
# --------------------------------------------------------------------------- #
def test_two_hours_idle_is_not_reported(tmp_path):
    g = _gw(tmp_path)                  # 上限 7200 → 阈值 8640
    g.last_event_ts = 1000.0
    # 一次正常的最长间隔（2h）→ 距离阈值 8640 还差 1440s，不应报
    assert g._check_stale(now=1000.0 + 7200) is False
    assert g._check_stale(now=1000.0 + 8640) is False   # 恰好等于阈值：边界不报
    assert _stale_events(g) == []


def test_three_hours_idle_reported_and_throttled(tmp_path):
    g = _gw(tmp_path)
    g.last_event_ts = 1000.0
    now = 1000.0 + 10800.0             # 3h > 8640
    assert g._check_stale(now=now) is True
    stale = _stale_events(g)
    assert len(stale) == 1
    assert stale[0]["last_event_ts"] == 1000.0
    assert stale[0]["idle_s"] == 10800.0
    assert stale[0]["threshold_s"] == 8640.0
    assert stale[0]["base_s"] == 1800.0
    assert stale[0]["interval_max_s"] == 7200.0

    # 30 分钟节流：10 分钟后不再写
    assert g._check_stale(now=now + 600) is False
    assert len(_stale_events(g)) == 1
    # 满 30 分钟后（仍未恢复活动）再报一次
    assert g._check_stale(now=now + 1801) is True
    assert len(_stale_events(g)) == 2


def test_inbound_activity_resets_idle_clock(tmp_path):
    g = _gw(tmp_path)
    g.last_event_ts = 1000.0
    assert g._check_stale(now=1000.0 + 5000) is False   # 还没到 8640
    g._record("inbound", {"friend": CONTACT, "text": "hi"})
    assert g._check_stale(now=1000.0 + 9000) is False   # inbound 刷新了时钟


# --------------------------------------------------------------------------- #
# proactive_schedule 不算活动
# --------------------------------------------------------------------------- #
def test_proactive_schedule_does_not_refresh_last_event_ts(tmp_path):
    g = _gw(tmp_path)
    g.last_event_ts = 1234.0
    g._record("proactive_schedule", {"next_delay_s": 600.0, "source": "random"})
    assert g.last_event_ts == 1234.0
    # 真正活动才会刷新
    g._record("outbound", {"friend": CONTACT, "status": "sent"})
    assert g.last_event_ts != 1234.0
    g.last_event_ts = 1234.0
    g._record("send_blocked", {"friend": CONTACT, "reason": "x"})
    assert g.last_event_ts != 1234.0
    g.last_event_ts = 1234.0
    g._record("gateway_stale", {"idle_s": 9000})        # 心跳同样不算
    assert g.last_event_ts == 1234.0


def test_schedule_event_does_not_mask_dead_zone(tmp_path):
    """只有 proactive_schedule 在动（内部调度）→ 2.5h 后仍应报死区。"""
    g = _gw(tmp_path)
    g.last_event_ts = 1000.0
    now = 1000.0 + 9000.0
    g._record("proactive_schedule", {"next_delay_s": 7200.0, "source": "random"})
    assert g.last_event_ts == 1000.0
    assert g._check_stale(now=now) is True


# --------------------------------------------------------------------------- #
# P0-24：三种"预期不发送"状态不报 gateway_stale；真实死区照旧
# --------------------------------------------------------------------------- #
def _idle_since(g, now: float, idle_s: float = 10800.0) -> None:
    """把"最后一次真实活动"摆到 now - idle_s（3h > 阈值 8640）。"""
    g.last_event_ts = now - idle_s


# ① proactive_enabled=false -------------------------------------------------- #
def test_proactive_disabled_suppresses_stale_and_status(tmp_path):
    g = _gw(tmp_path, proactive_enabled=False)
    now = 1000.0 + 10800.0
    _idle_since(g, now)

    assert g._check_stale(now=now) is False          # 不报
    assert _stale_events(g) == []
    assert g.status()["stale_suppressed"] is True
    assert g.status()["stale_suppressed_reason"] == "disabled"

    # 重新开启主动消息（同一 cfg 对象，等价于热加载后的最新配置）→ 恢复判定
    g.cfg["proactive_enabled"] = True
    assert g.status()["stale_suppressed"] is False
    assert g.status()["stale_suppressed_reason"] is None
    assert g._check_stale(now=now) is True
    assert len(_stale_events(g)) == 1


def test_suppression_does_not_consume_throttle_window(tmp_path):
    """抑制期间不占用 30 分钟节流窗口：恢复后立刻能报。"""
    g = _gw(tmp_path, proactive_enabled=False)
    now = 1000.0 + 10800.0
    _idle_since(g, now)
    assert g._check_stale(now=now) is False
    assert float(getattr(g, "_last_stale_ts", 0.0) or 0.0) == 0.0
    g.cfg["proactive_enabled"] = True
    assert g._check_stale(now=now) is True


# ② STOP 文件存在 ------------------------------------------------------------ #
def test_stop_file_suppresses_stale_then_resumes(tmp_path):
    stop = tmp_path / "STOP"
    g = _gw(tmp_path, stop_path=stop)
    now = 1000.0 + 10800.0
    _idle_since(g, now)

    stop.write_text("stopped\n", encoding="utf-8")
    assert g._check_stale(now=now) is False
    assert _stale_events(g) == []
    assert g.status()["stale_suppressed"] is True
    assert g.status()["stale_suppressed_reason"] == "stopped"

    stop.unlink()
    assert g.status()["stale_suppressed"] is False
    assert g._check_stale(now=now) is True
    assert len(_stale_events(g)) == 1


# ③ 夜间静默 ---------------------------------------------------------------- #
def test_quiet_hours_suppresses_stale_and_status(tmp_path):
    # [0,24] = 全天静默：与测试运行的具体时刻无关，断言稳定
    g = _gw(tmp_path, quiet_hours=[0, 24])
    now = 1000.0 + 10800.0
    _idle_since(g, now)

    assert g._check_stale(now=now) is False
    assert _stale_events(g) == []
    assert g.status()["stale_suppressed"] is True
    assert g.status()["stale_suppressed_reason"] == "quiet"

    # 静默结束（[0,0] = 历史语义"不静默"）→ 恢复真实死区告警
    g.cfg["quiet_hours"] = [0, 0]
    assert g.status()["stale_suppressed"] is False
    assert g._check_stale(now=now) is True
    assert len(_stale_events(g)) == 1


def test_night_quiet_hours_real_clock_window(tmp_path):
    """真实 [23,8] 跨零点区间：23:30 抑制、12:00 恢复（用固定时间戳，不依赖当前时刻）。"""
    g = _gw(tmp_path, quiet_hours=[23, 8])
    night = time.mktime((2026, 10, 25, 23, 30, 0, 0, 0, -1))
    day = time.mktime((2026, 10, 25, 12, 0, 0, 0, 0, -1))

    _idle_since(g, night)
    assert g.stale_suppression(now=night) == "quiet"
    assert g._check_stale(now=night) is False

    _idle_since(g, day)
    assert g.stale_suppression(now=day) == ""
    assert g._check_stale(now=day) is True


def test_quiet_override_keeps_stale_alert(tmp_path):
    """24h override 放行 = 预期仍可发送 → 不该被夜间静默误抑制。"""
    g = _gw(tmp_path, quiet_hours=[0, 24], proactive_override_quiet=True)
    now = 1000.0 + 10800.0
    _idle_since(g, now)

    assert g.stale_suppression() == ""       # override 生效 → 不抑制
    assert g._check_stale(now=now) is True


# 真实故障不受影响 ----------------------------------------------------------- #
def test_real_dead_zone_still_reported(tmp_path):
    """enabled=true、非静默、无 STOP、超阈值 → 必须报，且 /status 显示未抑制。"""
    g = _gw(tmp_path)                        # proactive 开、无 quiet_hours、无 STOP
    now = 1000.0 + 10800.0
    _idle_since(g, now)

    assert g.stale_suppression(now=now) == ""
    assert g._check_stale(now=now) is True
    stale = _stale_events(g)
    assert len(stale) == 1 and stale[0]["idle_s"] == 10800.0
    assert g.status()["stale_suppressed"] is False
    assert g.status()["stale_suppressed_reason"] is None


# 优先级：disabled → stopped → quiet（/status 只展示一个原因） --------------- #
def test_suppression_reason_precedence(tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("stopped\n", encoding="utf-8")
    g = _gw(tmp_path, stop_path=stop, proactive_enabled=False, quiet_hours=[0, 24])

    assert g.stale_suppression() == "disabled"
    g.cfg["proactive_enabled"] = True
    assert g.stale_suppression() == "stopped"
    stop.unlink()
    assert g.stale_suppression() == "quiet"
    g.cfg["quiet_hours"] = [0, 0]
    assert g.stale_suppression() == ""

