# -*- coding: utf-8 -*-
"""P25：网上冲浪 worker（线程循环 + CLI；只读不阻塞聊天）。

职责：窗口判断 → 到点投骰 → 执行/跳过 → 调 ``surf`` 取数沉淀 → 写状态/日志。
**默认不主动发消息**：这里只沉淀兴趣与审计，不碰 gateway 的发送通道。

观测日志（``gateway._record`` 或独立 ``surf.log``）：
``surf_roll`` / ``surf_executed`` / ``surf_skipped`` / ``surf_error``。

CLI::

    python -m agi.selflearn.surf_worker --status
    python -m agi.selflearn.surf_worker --once --reason user   # 立即冲浪（用户让她看）
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import surf

#: 线程最长睡多久再回头看一次（保证 engine 暂停/STOP 能及时生效）
MAX_SLEEP_S = 60.0
#: 最短睡多久（避免忙等）
MIN_SLEEP_S = 5.0


def _record(recorder: Callable[[str, dict], Any] | None, kind: str,
            data: dict[str, Any]) -> None:
    """写一条观测日志：优先 gateway._record，否则追加到 surf.log。"""
    payload = {"ts": time.time(), "kind": kind, **data}
    if callable(recorder):
        try:
            recorder(kind, data)
            return
        except Exception:  # noqa: BLE001 - 记录失败不影响冲浪
            pass
    try:
        target = Path(surf.SURF_LOG)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def _recorder_for(gateway, recorder):
    if callable(recorder):
        return recorder
    rec = getattr(gateway, "_record", None) if gateway is not None else None
    return rec if callable(rec) else None


def engine_on(gateway=None, engine_state: Callable[[], bool] | None = None) -> bool:
    """引擎软开关：engine_state() 优先，其次 gateway.engine_enabled。"""
    if callable(engine_state):
        try:
            return bool(engine_state())
        except Exception:  # noqa: BLE001
            return True
    if gateway is not None:
        return bool(getattr(gateway, "engine_enabled", True))
    return True


def run_once(*, now: float | None = None, force: bool = False,
             source: str = "scheduled", state_path: Path | str | None = None,
             db_path: Path | str | None = None, gateway=None,
             recorder: Callable[[str, dict], Any] | None = None,
             rng=None, fetch_fn=None, observe_fn=None,
             engine_state: Callable[[], bool] | None = None,
             window_hours=None) -> dict[str, Any]:
    """跑一轮：窗口/等待判断 → 投骰 → （执行时）冲浪 + 落库；返回结果 dict。"""
    now = time.time() if now is None else float(now)
    rec = _recorder_for(gateway, recorder)
    window_hours = window_hours if window_hours is not None else surf.window()
    state = surf.reset_daily(surf.load_state(state_path), now)

    if not force and not surf.in_window(now, window_hours):
        state["next_roll_ts"] = surf.next_window_start(now, window_hours)
        surf.save_state(state, state_path)
        _record(rec, "surf_skipped", {
            "reason": "outside_window", "next_roll_ts": state["next_roll_ts"],
            "window": list(window_hours)})
        return {"ok": True, "ran": False, "reason": "outside_window",
                "next_roll_ts": state["next_roll_ts"], "state": state}

    next_roll = float(state.get("next_roll_ts") or 0.0)
    if not force and next_roll and now < next_roll:
        return {"ok": True, "ran": False, "reason": "wait",
                "next_roll_ts": next_roll, "state": state}

    plan = surf.roll(state, now=now, rng=rng, db_path=db_path,
                     source=("user" if force and source == "scheduled" else source))
    plan["window"] = list(window_hours)
    if force:
        # 用户明确让她看：绕过窗口与门槛，立即冲（dice/factor 仍只影响时长/条数）
        plan["executed"] = True
        plan["forced"] = True
    _record(rec, "surf_roll", {
        "dice": plan["dice"], "factor": plan["factor_before"],
        "factor_after": plan["factor"], "effective": plan["effective"],
        "executed": plan["executed"], "gap_min": plan["gap_min"],
        "duration_min": plan["duration_min"], "items": plan["items"],
        "next_roll_ts": plan["next_roll_ts"], "source": plan["source"]})

    if not plan["executed"]:
        surf.save_state(state, state_path)
        _record(rec, "surf_skipped", {
            "reason": "gate", "dice": plan["dice"], "factor": plan["factor_before"],
            "effective": plan["effective"], "gap_min": plan["gap_min"]})
        return {"ok": True, "ran": True, "executed": False, "plan": plan,
                "reason": "gate", "state": state}

    _record(rec, "surf_executed", {
        "dice": plan["dice"], "factor": plan["factor_before"],
        "duration_min": plan["duration_min"], "items": plan["items"]})
    result: dict[str, Any] = {}
    try:
        result = surf.run_session(plan, now=now, db_path=db_path,
                                  fetch_fn=fetch_fn, observe_fn=observe_fn)
        state["executed_today"] = int(state.get("executed_today") or 0) + 1
        state["minutes_today"] = round(
            float(state.get("minutes_today") or 0.0) + float(plan["duration_min"]), 1)
        state["last_session_id"] = str(result.get("id") or "")
        state["last_summary"] = str(result.get("summary") or "")[:200]
        plan["summary"] = state["last_summary"]
    except Exception as exc:  # noqa: BLE001 - 单轮失败不拖垮调度
        plan["error"] = f"{type(exc).__name__}: {exc}"
        _record(rec, "surf_error", {"error": plan["error"][:200]})
    surf.save_state(state, state_path)
    return {"ok": True, "ran": True, "executed": True, "plan": plan,
            "session": result, "state": state}


def _sleep_seconds(now: float, next_roll: float) -> float:
    try:
        delay = float(next_roll) - float(now)
    except (TypeError, ValueError):
        delay = MAX_SLEEP_S
    return max(MIN_SLEEP_S, min(MAX_SLEEP_S, delay))


def loop(*, stop_event: threading.Event | None = None, gateway=None,
         engine_state: Callable[[], bool] | None = None,
         state_path: Path | str | None = None,
         db_path: Path | str | None = None, poll_s: float = MAX_SLEEP_S,
         fetch_fn=None, observe_fn=None, window_hours=None) -> dict[str, Any]:
    """常驻调度：窗口外休眠、到点投骰；engine_enabled=false 时暂停（不退出）。"""
    stop = stop_event or threading.Event()
    state = surf.load_state(state_path)
    last_result: dict[str, Any] = {}
    iterations = 0
    while not stop.is_set():
        iterations += 1
        if not surf.enabled():
            stop.wait(poll_s)
            continue
        if not engine_on(gateway, engine_state):
            stop.wait(min(poll_s, 30.0))
            continue
        try:
            last_result = run_once(now=time.time(), state_path=state_path,
                                   db_path=db_path, gateway=gateway,
                                   fetch_fn=fetch_fn, observe_fn=observe_fn,
                                   window_hours=window_hours)
        except Exception as exc:  # noqa: BLE001
            _record(_recorder_for(gateway, None), "surf_error",
                    {"error": f"{type(exc).__name__}: {exc}"[:200]})
            stop.wait(poll_s)
            continue
        state = last_result.get("state") or state
        delay = _sleep_seconds(time.time(), float(state.get("next_roll_ts") or 0.0))
        stop.wait(min(float(poll_s), delay))
    return {"ok": True, "iterations": iterations, "last": last_result}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.selflearn.surf_worker")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出")
    parser.add_argument("--status", action="store_true", help="打印状态 JSON")
    parser.add_argument("--reason", default="scheduled",
                        help="触发来源：scheduled / user / manual")
    parser.add_argument("--force", action="store_true", help="绕过窗口与门槛")
    args = parser.parse_args(argv)
    if args.status:
        print(json.dumps(surf.status(), ensure_ascii=False, indent=2, default=str))
        return 0
    if args.once:
        result = run_once(force=bool(args.force or args.reason == "user"),
                          source=str(args.reason or "scheduled"))
        print(json.dumps(result, ensure_ascii=False, default=str)[:4000])
        return 0 if result.get("ok") else 1
    loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
