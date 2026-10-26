# -*- coding: utf-8 -*-
"""P0-16 / P0-24 / P6-b：死区哨兵阈值与噪音抑制。

P0-16（2026-10-25）：调度间隔 ``uniform(60, 7200)``（最长 2h），固定 1800s 阈值会
**一次正常长等待就误报** → 有效阈值 = ``max(gateway_stale_alert_s,
proactive_interval_max_s*1.2, 3600)`` ⇒ 默认 **8640s（2.4h）**；30 分钟节流不变。

P0-24（2026-10-26 真机噪音）：``proactive_enabled=false`` + 夜间静默时哨兵仍报
gateway_stale（idle 14040s）→ 三种「预期不发送」状态（disabled/stopped/quiet）不判。

P6-b（2026-10-26 真机噪音）：09:01:39 报 ``idle_s=8640.1``，但网关一直活着（10:17
还发了主动消息）。原因：P0-16 把 ``proactive_schedule`` 整体排除出 ``last_event_ts``，
静默期间无真实活动 → 静默一结束就按静默前的旧 idle 判死区。修法：
- ``proactive_enabled=true`` 且非静默/无 STOP 时，``proactive_schedule`` **算活动**；
- 静默结束后的宽限期 ``grace_s = min(1800, threshold*0.2)``（reason=grace）；
- 真实死区（enabled 但 schedule 也停了）仍必须报。
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


def _grace_s(g) -> float:
    return min(1800.0, max(0.0, g.stale_threshold() * 0.2))


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
# proactive_schedule 算不算活动（P6-b）
# --------------------------------------------------------------------------- #
def test_proactive_schedule_counts_as_activity_when_enabled(tmp_path):
    """enabled + 非静默 + 无 STOP 时，schedule 证明 ticker 活着 → 刷新 idle 时钟。"""
    g = _gw(tmp_path)
    g.last_event_ts = 1000.0
    g._record("proactive_schedule", {"next_delay_s": 600.0, "source": "random"})
    assert g.last_event_ts != 1000.0 and g.last_event_ts > 1000.0

    # 它不能掩盖超过阈值的"调度也停了"死区：调度一停，idle 继续涨
    g.last_event_ts = 1000.0
    assert g._check_stale(now=1000.0 + 8640.0) is False   # 边界仍不报
    assert g._check_stale(now=1000.0 + 8640.5) is True    # >阈值且无 schedule → 报

    # 心跳 gateway_stale 仍不算活动
    g.last_event_ts = 1234.0
    g._record("gateway_stale", {"idle_s": 9000})
    assert g.last_event_ts == 1234.0
    # inbound/outbound/send_blocked 照旧算活动
    g._record("outbound", {"friend": CONTACT, "status": "sent"})
    assert g.last_event_ts != 1234.0


@pytest.mark.parametrize("state", ["disabled", "quiet", "stopped"])
def test_proactive_schedule_not_activity_when_suppressed(tmp_path, state):
    """disabled / 静默 / STOP 时 schedule **不**刷新（保持 P0-16/P0-24 旧语义）。"""
    stop = tmp_path / "STOP"
    overrides = {}
    if state == "disabled":
        overrides["proactive_enabled"] = False
    elif state == "quiet":
        overrides["quiet_hours"] = [0, 24]
    else:
        stop.write_text("stopped\n", encoding="utf-8")
    g = _gw(tmp_path, stop_path=stop if state == "stopped" else None, **overrides)

    g.last_event_ts = 1000.0
    g._record("proactive_schedule", {"next_delay_s": 600.0, "source": "random"})
    assert g.last_event_ts == 1000.0


def test_real_dead_zone_with_schedule_stopped_still_reported(tmp_path):
    """enabled=true、非静默、无 STOP，但 schedule 也停了（ticker 死）→ 仍报。"""
    g = _gw(tmp_path)
    now = 1000.0 + 10800.0
    g.last_event_ts = 1000.0
    assert g.stale_suppression(now=now) == ""
    assert g._check_stale(now=now) is True
    stale = _stale_events(g)
    assert len(stale) == 1
    assert stale[0]["idle_s"] == 10800.0


# --------------------------------------------------------------------------- #
# P6-b：静默结束后的宽限期（真机 09:01:39 idle=8640.1 事故）
# --------------------------------------------------------------------------- #
def _ts(hour, minute, second=0, day=26):
    return time.mktime((2026, 10, day, hour, minute, second, 0, 0, -1))


def test_incident_090139_quiet_end_no_false_stale(tmp_path):
    """复现真机：静默 23:00–08:00，最后活动 06:37:38.9，09:01:39 idle=8640.1。

    修法后两种都不会误报：
    ① 静默结束后 proactive_schedule 算活动（刷新 idle）；
    ② 即使没有 schedule：静默期间不算死区（从静默结束起算 idle）。
    """
    # ① 静默结束后有调度事件（ticker 活着）
    g = _gw(tmp_path, quiet_hours=[23, 8])
    last_activity = _ts(6, 37, 39) - 0.1          # 真机 idle 8640.1 的起点
    g.last_event_ts = last_activity
    assert g._check_stale(now=_ts(7, 0)) is False   # 静默期间：抑制
    assert g._check_stale(now=_ts(8, 10)) is False  # 静默刚结束：grace 宽限
    g._record("proactive_schedule", {"next_delay_s": 600.0, "source": "random",
                                     "ts": _ts(8, 12)})
    assert g.last_event_ts == _ts(8, 12)            # P6-b：schedule 算活动
    incident = _ts(9, 1, 39)
    assert incident - last_activity > g.stale_threshold()   # 旧逻辑必然误报
    assert g._check_stale(now=incident) is False
    assert _stale_events(g) == []


def test_incident_090139_quiet_period_not_counted_as_dead_time(tmp_path):
    """② 即使静默结束后没有 schedule：静默段本身不算死区 idle。"""
    g = _gw(tmp_path, quiet_hours=[23, 8])
    g.last_event_ts = _ts(6, 37, 39) - 0.1
    assert g._check_stale(now=_ts(7, 0)) is False            # 静默
    quiet_end = _ts(8, 0, 10)                                # 静默结束瞬间
    assert g._check_stale(now=quiet_end) is False            # grace
    # 08:00→09:01:39 没有任何活动：idle 从静默结束算只有 ~3690s < 8640 → 不报
    assert g._check_stale(now=_ts(9, 1, 39)) is False
    assert _stale_events(g) == []
    # 但静默结束后持续无活动超过阈值 = 真实死区，必须报
    dead = quiet_end + g.stale_threshold() + 1
    assert g._check_stale(now=dead) is True
    assert len(_stale_events(g)) == 1


def test_quiet_end_grace_visible_in_status_and_expires(tmp_path):
    """静默刚结束：/status reason=grace；宽限过后按"静默结束"起算，真死区照报。"""
    g = _gw(tmp_path, quiet_hours=[0, 24])
    g.last_event_ts = time.time() - 10800.0
    assert g.stale_suppression() == "quiet"

    g.cfg["quiet_hours"] = [0, 0]
    st = g.status()
    assert st["stale_suppressed"] is True
    assert st["stale_suppressed_reason"] == "grace"
    grace_s = _grace_s(g)
    assert grace_s == min(1800.0, g.stale_threshold() * 0.2)
    assert g._check_stale(now=g._stale_grace_until - 1) is False     # 宽限内

    # 宽限过期但静默段不计死区：idle 从静默结束起算，还没到阈值 → 不报
    expired = g._stale_grace_until + 1.0
    assert g.stale_suppression(now=expired) == ""
    assert g._check_stale(now=expired) is False
    # 静默结束后持续无活动 > 阈值 → 真实死区必须报
    dead = g._stale_quiet_end_ts + g.stale_threshold() + 1
    assert g._check_stale(now=dead) is True


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

    assert g.stale_suppression(now=now) == "quiet"
    assert g._check_stale(now=now) is False
    assert _stale_events(g) == []

    # 静默结束：[0,0] = 历史语义"不静默" → 先进入 grace 宽限，不能立刻判死区
    g.cfg["quiet_hours"] = [0, 0]
    quiet_end = now
    grace_s = _grace_s(g)
    assert g.stale_suppression(now=quiet_end) == "grace"
    assert g._check_stale(now=quiet_end) is False
    assert g._check_stale(now=quiet_end + grace_s - 1) is False
    # 宽限过后：静默段不计死区（idle 从静默结束起算），还没到阈值 → 不报
    assert g.stale_suppression(now=quiet_end + grace_s + 1) == ""
    assert g._check_stale(now=quiet_end + grace_s + 1) is False
    # 静默结束后持续无活动 > 阈值 → 真实死区照报
    dead = quiet_end + g.stale_threshold() + 1
    assert g._check_stale(now=dead) is True
    assert len(_stale_events(g)) == 1


def test_night_quiet_hours_real_clock_window(tmp_path):
    """真实 [23,8] 跨零点区间：23:30 静默；12:00 已过宽限 → 恢复判定。"""
    g = _gw(tmp_path, quiet_hours=[23, 8])
    night = time.mktime((2026, 10, 25, 23, 30, 0, 0, 0, -1))
    day = time.mktime((2026, 10, 25, 12, 0, 0, 0, 0, -1))

    _idle_since(g, night)
    assert g.stale_suppression(now=night) == "quiet"
    assert g._check_stale(now=night) is False

    # 静默刚结束 → grace；超过宽限后（12:00 早过了）才是空
    _idle_since(g, day)
    assert g.stale_suppression(now=day) == "grace"
    assert g._check_stale(now=day) is False
    grace_s = _grace_s(g)
    assert g.stale_suppression(now=day + grace_s + 1) == ""
    # 静默段不计死区：idle 从静默结束起算；真正超阈值后仍要报
    _idle_since(g, day + grace_s + 1)
    assert g._check_stale(now=day + grace_s + 1) is False
    dead = day + g.stale_threshold() + 1
    assert g._check_stale(now=dead) is True


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


# 优先级：disabled → stopped → quiet → grace（/status 只展示一个原因） --------- #
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
    # 静默刚结束：先 grace；宽限过后才是空
    assert g.stale_suppression() == "grace"
    assert g.stale_suppression(now=g._stale_grace_until + 1) == ""

