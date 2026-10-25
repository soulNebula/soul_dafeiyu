# -*- coding: utf-8 -*-
"""801 微信网关：接收 → selflearn 生成 → wxauto 发送。

默认 enabled=false / dry_run=true；真实发送必须显式开启，且只发白名单。
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import config as cfg
from . import control, drafts, media as media_mod, policy
from .receiver import WeChatReceiver
from .sender import WxAutoSender
from .. import textguard

_REACTION_WORDS = {
    "good": "good", "好": "good", "好评": "good", "不错": "good", "赞": "good", "满意": "good",
    "ok": "ok", "一般": "ok", "还行": "ok", "中": "ok",
    "bad": "bad", "差": "bad", "差评": "bad", "不好": "bad", "不满意": "bad",
}


def parse_feedback(text: str) -> str | None:
    """只认显式反馈：反馈 好 / 评价：一般 / feedback bad。"""
    value = str(text or "").strip()
    m = re.fullmatch(r"(?:反馈|评价|feedback)\s*[:：]?\s*(\S+)", value, re.I)
    if not m:
        return None
    return _REACTION_WORDS.get(m.group(1).strip().lower())

ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = ROOT / "agi" / "data" / "wechat" / "gateway.log"
WXAUTO_PYTHON = ROOT / "agi" / "data" / "wechat" / ".venv" / "Scripts" / "python.exe"

# P0-16 死区哨兵阈值：主动调度间隔是 uniform(60, 7200)，最长一次正常等待 2h，
# 固定 1800s 阈值会把它误报成"死区"。有效阈值 = max(配置值, 间隔上限×slack, 下限)。
STALE_MIN_S = 3600.0        # 至少 1h，避免短间隔配置把阈值压回误报区
STALE_SLACK = 1.2           # 间隔上限的余量：7200 × 1.2 = 8640s（2.4h）
STALE_REPEAT_S = 1800.0     # 同一告警 30 分钟内最多写一次（防刷屏）
#: 内部调度/心跳事件不是"真实活动"，不刷新 last_event_ts（避免调度喂饱哨兵）
STALE_IGNORED_KINDS = frozenset({"gateway_stale", "proactive_schedule"})

# P0-22：紧急停止日志节流 / 静默覆盖有效期
STOP_LOG_INTERVAL_S = 600.0        # proactive_stopped 每 10 分钟最多 1 条
QUIET_OVERRIDE_S = 86400.0         # proactive_override_quiet 24h 后自动失效

# P0-24：死区哨兵只在"应该有事发生"时才有意义。下面三种状态都是
# **预期不发送**（本来就不会有活动），此时报 gateway_stale 是纯噪音：
STALE_SUPPRESS_DISABLED = "disabled"   # proactive_enabled=false
STALE_SUPPRESS_STOPPED = "stopped"     # STOP 紧急停止文件存在
STALE_SUPPRESS_QUIET = "quiet"         # 夜间静默时段（且无 24h override）


def _log(entry: dict, path: Path | None = None) -> None:
    target = Path(path) if path else LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


class Gateway:
    def __init__(self, cfg_obj: dict, *, stop_path: Path | str | None = None):
        self.cfg = dict(cfg_obj)
        self.draft_path = self.cfg.get("drafts_path") or drafts.DEFAULT_PATH
        self.log_path = Path(self.cfg.get("log_path") or LOG_PATH)
        # P0-22b：STOP 路径显式注入（None = 走 config.stop_path()，可被测试隔离）
        self._stop_path = Path(stop_path) if stop_path is not None else None
        self.receiver = None
        self.backend = None
        self.sender = WxAutoSender()
        self.queue: "queue.Queue[dict]" = queue.Queue(maxsize=200)
        self.worker = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.recent = deque(maxlen=50)
        self.ready = False
        self.start_error = ""
        self.sent = 0
        self.failed = 0
        self.last_turns = {}
        self.last_proactive = {}
        self.proactive_thread = None
        self.pending: dict[str, dict] = {}
        self.last_inbound: dict[str, float] = {}
        self.send_times = deque()
        self.sent_today = 0
        self.sent_day = time.strftime("%Y-%m-%d")
        # P0-2 死区哨兵：任何事件都会刷新 last_event_ts；长时间没有事件就写 gateway_stale
        self.last_event_ts = self.started_at
        self._last_stale_ts = 0.0
        # P0-22：配置热加载 / 紧急停止 / 夜间静默硬保护
        self._config_hot_reload = True       # run_cycle 每轮按 mtime 重读 config.json
        self._last_stop_log_ts = 0.0         # proactive_stopped 日志节流（10 分钟）
        self._override_started_ts: float | None = None
        self._override_expired_logged = False
        self._quiet_warned_value: str | None = None

    def start(self) -> None:
        if not self.cfg.get("enabled"):
            self.start_error = "enabled=false"
            return
        allowed = [str(x) for x in (self.cfg.get("allowed_contacts") or []) if str(x).strip()]
        monitored = [str(x) for x in (self.cfg.get("monitored_contacts") or []) if str(x).strip()]
        if not allowed or not monitored:
            self.start_error = "allowed_contacts/monitored_contacts 为空"
            return
        try:
            from app import load_backend, load_config as load_wx_config
            wx_cfg = load_wx_config()
            wx_cfg["monitored"] = monitored
            backend, reason, fake = load_backend(wx_cfg, force_fake=False)
            if fake:
                self.start_error = f"真实后端不可用：{reason}"
                return
            self.backend = backend
            self.backend.set_monitored(monitored)
            self.backend.start()
        except Exception as exc:  # noqa: BLE001
            self.start_error = f"{type(exc).__name__}: {exc}"
            return
        self.receiver = WeChatReceiver(backend, allowed, self.on_inbound, since=time.time())
        self.receiver.start()
        self.worker = threading.Thread(target=self._work, name="gateway-worker", daemon=True)
        self.worker.start()
        if self.cfg.get("proactive_enabled"):
            self.proactive_thread = threading.Thread(
                target=self._proactive_loop, name="gateway-proactive", daemon=True)
            self.proactive_thread.start()
        self.ready = True
        self._record("gateway_started", {"allowed": allowed, "monitored": monitored,
                                          "dry_run": bool(self.cfg.get("dry_run", True))})

    def stop(self) -> None:
        self.stop_event.set()
        if self.receiver:
            self.receiver.stop()
        if self.backend:
            try:
                self.backend.stop()
            except Exception:  # noqa: BLE001
                pass
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=2)

    def _record(self, kind: str, data: dict) -> None:
        entry = {"ts": time.time(), **data}
        # kind 以事件名为准，防止 payload 里的同名字段把它覆盖掉（P0-7.1 实测踩到）
        entry["kind"] = kind
        if kind not in STALE_IGNORED_KINDS:
            # 死区心跳/内部调度不算"有活动"：否则哨兵会自我续命或被调度喂饱
            self.last_event_ts = entry["ts"]
        with self.lock:
            self.recent.append(entry)
        _log(entry, self.log_path)

    def stale_threshold(self) -> float:
        """P0-16 动态死区阈值（秒）：max(配置值, 间隔上限×1.2, 3600)。"""
        try:
            base = float(self.cfg.get("gateway_stale_alert_s") or 1800.0)
        except (TypeError, ValueError):
            base = 1800.0
        try:
            interval_max = float(self.cfg.get("proactive_interval_max_s") or 7200.0)
        except (TypeError, ValueError):
            interval_max = 7200.0
        try:
            slack = float(self.cfg.get("gateway_stale_slack") or STALE_SLACK)
        except (TypeError, ValueError):
            slack = STALE_SLACK
        return max(base, interval_max * max(0.0, slack), STALE_MIN_S)

    def _quiet_active(self, now: float) -> bool:
        """P0-24：**只读**判断当前是否夜间静默（供观察用，不写事件/不改状态）。

        与 :meth:`_quiet_blocked` 的判定保持一致（含 ``quiet_hours=[]`` 回退默认、
        24h ``proactive_override_quiet`` 放行/过期语义），但 /status 与死区哨兵
        每 0.2s 都可能调用它，**不能**像发送路径那样记 ``quiet_override`` 事件或
        推进 override 窗口（否则会刷新 ``last_event_ts``、把真实死区掩盖掉）。
        """
        raw = self.cfg.get("quiet_hours")
        if raw is None:
            return False                     # 未配置：不做静默硬保护（旧 cfg 兼容）
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            try:
                start, end = int(raw[0]), int(raw[1])
            except (TypeError, ValueError):
                start = end = 0
            if start == end:
                return False                 # [0,0] / 相等 = 历史语义"不静默"
            quiet = [start, end]
        else:
            # 非法值（含 []）→ 与发送路径一致：回退默认 [23,8]
            quiet = list(cfg.DEFAULTS.get("quiet_hours") or [23, 8])
        try:
            start, end = int(quiet[0]), int(quiet[1])
        except (TypeError, ValueError):
            return False
        if not policy.in_quiet_hours(time.localtime(now).tm_hour, start, end):
            return False
        if bool(self.cfg.get("proactive_override_quiet")):
            started = self._override_started_ts
            if started is None:
                return False                 # override 请求了 = 视为放行（窗口未起算）
            try:
                if now - float(started) < QUIET_OVERRIDE_S:
                    return False             # 24h 内放行
            except (TypeError, ValueError):
                pass
        return True

    def stale_suppression(self, now: float | None = None) -> str:
        """P0-24：当前是否处于「预期不发送」状态。

        返回抑制原因（``disabled`` / ``stopped`` / ``quiet``），空串表示哨兵正常工作。
        这三种状态下本来就不会有活动，报 ``gateway_stale`` 是纯噪音，会淹没真实死区。

        判断读的是 ``self.cfg``（与 proactive 循环热加载更新的是同一个对象），
        所以 ``proactive_enabled`` / ``quiet_hours`` / ``proactive_override_quiet``
        变化后下一轮检查立即生效。优先级：disabled → stopped → quiet。
        **只读**：不写日志事件、不推进 override 窗口（观测不能反过来制造"活动"）。
        """
        now = time.time() if now is None else float(now)
        # ① 主动消息关闭：没有"应该发生的事"，也就没有死区
        if not bool(self.cfg.get("proactive_enabled")):
            return STALE_SUPPRESS_DISABLED
        # ② STOP 文件：紧急停止，预期不发送
        try:
            if self.stop_present():
                return STALE_SUPPRESS_STOPPED
        except Exception:  # noqa: BLE001 - 探测失败不能反过来打断哨兵
            pass
        # ③ 夜间静默（含 24h override 放行语义）：发送被硬拦，同样预期无活动
        try:
            if self._quiet_active(now):
                return STALE_SUPPRESS_QUIET
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _check_stale(self, now: float | None = None) -> bool:
        """P0-2 死区哨兵：超过动态阈值没有真实活动就写 gateway_stale。

        复用 `_work` 现有事件循环（0.2s 转一圈），不新起线程；同一告警 30 分钟内
        最多写一次，避免自己刷屏。阈值见 :meth:`stale_threshold`（P0-16：必须
        覆盖 proactive 最长 2h 的随机间隔，否则正常长等待会误报）。

        P0-24：``proactive_enabled=false`` / STOP 文件 / 夜间静默这三种
        「预期不发送」状态直接不判定（不写 gateway_stale、不占用 30 分钟节流窗口），
        状态恢复后按真实空闲时长立刻重新判定；真实死区照旧告警。
        """
        now = time.time() if now is None else float(now)
        if self.stale_suppression(now):
            return False
        alert_s = self.stale_threshold()
        last_event = float(getattr(self, "last_event_ts", 0.0) or 0.0)
        if not last_event or now - last_event <= alert_s:
            return False
        if now - float(getattr(self, "_last_stale_ts", 0.0) or 0.0) < STALE_REPEAT_S:
            return False
        self._last_stale_ts = now
        self._record("gateway_stale", {
            "last_event_ts": round(last_event, 1),
            "idle_s": round(now - last_event, 1),
            "threshold_s": round(alert_s, 1),
            "base_s": round(float(self.cfg.get("gateway_stale_alert_s") or 1800.0), 1),
            "interval_max_s": round(float(self.cfg.get("proactive_interval_max_s") or 7200.0), 1),
        })
        return True

    # ------------------------------------------------------------------ #
    # P0-22：紧急停止 / 夜间静默硬保护 / 热加载辅助
    # ------------------------------------------------------------------ #
    def _stop_file(self) -> Path:
        """STOP 文件路径：显式注入 > ``config.stop_path()``（测试可隔离）。

        每次都重新解析而不是在 ``__init__`` 缓存，这样 Gateway 创建之后再重定向
        ``config.STOP_PATH``（或 conftest autouse fixture）也能生效。
        """
        if self._stop_path is not None:
            return self._stop_path
        return cfg.stop_path()

    def stop_present(self) -> bool:
        """STOP 文件存在 = 立即停止一切外发（删除即恢复）。"""
        try:
            return bool(self._stop_file().exists())
        except Exception:  # noqa: BLE001
            return False

    def _log_stopped(self, now: float | None = None) -> None:
        """proactive_stopped 日志节流：每 10 分钟最多 1 条（首次必写）。"""
        now = time.time() if now is None else float(now)
        last = float(getattr(self, "_last_stop_log_ts", 0.0) or 0.0)
        if last and now - last < STOP_LOG_INTERVAL_S:
            return
        self._last_stop_log_ts = now
        self._record("proactive_stopped", {"reason": "stop_file",
                                          "path": str(self._stop_file())})

    def set_stop(self, stopped: bool, *, now: float | None = None) -> dict:
        """创建/删除 STOP 文件（幂等）；返回 {ok, stopped}。"""
        now = time.time() if now is None else float(now)
        target = self._stop_file()
        wanted = bool(stopped)
        try:
            if wanted:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_text("stopped\n", encoding="utf-8")
            else:
                target.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stopped": self.stop_present()}
        self._record("proactive_stop_toggled", {"stopped": wanted, "path": str(target), "ts": now})
        return {"ok": True, "stopped": wanted}

    def _quiet_hours_value(self) -> list[int]:
        """夜间静默配置：``quiet_hours=[]`` 视为非法 → 回退默认 [23,8] 并告警一次。"""
        raw = self.cfg.get("quiet_hours")
        if raw is None:
            return []  # 未配置：不做硬保护（旧 cfg/测试兼容）
        quiet: list[int] = []
        if isinstance(raw, (list, tuple)) and len(raw) == 2:
            try:
                quiet = [int(raw[0]), int(raw[1])]
            except (TypeError, ValueError):
                quiet = []
        if not quiet:
            fallback = list(cfg.DEFAULTS.get("quiet_hours") or [23, 8])
            marker = f"{raw!r}"
            if self._quiet_warned_value != marker:
                self._quiet_warned_value = marker
                self._record("quiet_hours_invalid", {"value": raw, "fallback": fallback})
            return fallback
        return quiet

    def _quiet_override_active(self, now: float) -> tuple[bool, bool]:
        """返回 (是否处于 24h override 有效期内, 本次是否刚激活)。"""
        if not bool(self.cfg.get("proactive_override_quiet")):
            self._override_started_ts = None
            self._override_expired_logged = False
            return False, False
        if self._override_started_ts is None:
            self._override_started_ts = now
            self._record("quiet_override", {"started_at": now, "expires_in_s": QUIET_OVERRIDE_S})
            return True, True
        if now - self._override_started_ts < QUIET_OVERRIDE_S:
            return True, False
        if not self._override_expired_logged:
            self._override_expired_logged = True
            self._record("quiet_override_expired", {
                "started_at": self._override_started_ts, "ttl_s": QUIET_OVERRIDE_S})
        return False, False

    def _quiet_blocked(self, now: float | None = None) -> bool:
        """夜间静默硬保护：发送前最后一道，quiet_hours=[] 也不得绕过。

        允许两种情况放行：① 显式 ``proactive_override_quiet=true`` 且 24h 内
        （记 quiet_override 日志，过期自动失效）；② 显式 ``[0,0]`` 历史语义。
        """
        now = time.time() if now is None else float(now)
        quiet = self._quiet_hours_value()
        if not quiet or len(quiet) != 2:
            return False
        start, end = quiet
        if int(start) == int(end):
            return False
        hour = time.localtime(now).tm_hour
        if not policy.in_quiet_hours(hour, start, end):
            return False
        active, just = self._quiet_override_active(now)
        return not active

    def on_inbound(self, msg: dict) -> None:
        self._record("inbound", {"friend": msg.get("friend"), "text": msg.get("text"),
                                  "id": msg.get("id")})
        if self.cfg.get("complaint_stop", True) and control.is_complaint(str(msg.get("text") or "")):
            self.cfg["stop"] = True
            self._record("complaint_circuit", {"friend": msg.get("friend"),
                                               "text": str(msg.get("text") or "")[:120], "stop": True})
            return
        if not self.cfg.get("auto_reply"):
            return
        try:
            self.queue.put_nowait(msg)
        except queue.Full:
            self.failed += 1
            self._record("queue_full", {"friend": msg.get("friend")})

    def _proactive_loop(self) -> None:
        """M5：常驻主动调度（间隔 60–7200s 均匀随机 = 1–120 分钟，P0-12）。"""
        while not self.stop_event.is_set() and self.cfg.get("proactive_enabled"):
            outcome = {}
            try:
                outcome = self._proactive_cycle()
            except Exception as exc:  # noqa: BLE001
                self._record("proactive_error",
                             {"error": f"{type(exc).__name__}: {exc}"[:200]})
            try:
                from . import proactive_worker
                delay = proactive_worker.next_interval(self, last_outcome=outcome)
            except Exception as exc:  # noqa: BLE001
                self._record("proactive_schedule_error",
                             {"error": f"{type(exc).__name__}: {exc}"[:200]})
                # 兜底：1–120 分钟量级，不能再退回旧的 5 分钟密度
                delay = 3600.0
            try:
                self.last_proactive["next_delay_s"] = delay
            except Exception:  # noqa: BLE001
                pass
            # P0-12：source=random 便于真机窗口按日志验收调度分布（不再是 LLM 决定）
            self._record("proactive_schedule", {"next_delay_s": delay, "source": "random"})
            self.stop_event.wait(delay)

    def _proactive_cycle(self) -> dict:
        from . import proactive_worker
        return proactive_worker.run_cycle(self)

    def _work(self) -> None:
        while not self.stop_event.is_set():
            msg = None
            try:
                msg = self.queue.get(timeout=0.2)
            except queue.Empty:
                msg = None
            if msg is not None:
                try:
                    if self._handle_feedback(msg):
                        continue
                    self._buffer_inbound(msg)
                except Exception as exc:  # noqa: BLE001
                    self.failed += 1
                    self._record("worker_error",
                                 {"error": f"{type(exc).__name__}: {exc}"})
            try:
                self._flush_pending()
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                self._record("flush_error", {"error": f"{type(exc).__name__}: {exc}"})
            try:
                self._check_stale()
            except Exception:  # noqa: BLE001 - 哨兵失败不能影响主循环
                pass
        self._flush_pending(force=True)

    def _buffer_inbound(self, msg: dict) -> None:
        """「等他把话说完」：连续几条文字先攒着，debounce 到点一次性回。

        图片/表情/系统消息不攒（有时效），直接回。
        """
        contact = str(msg.get("friend") or "")
        self.last_inbound[contact] = time.time()
        # 收到消息 → 给上一条主动消息标反应（正/负/中性），用于"说错了就收敛"
        try:
            from . import proactive_worker
            proactive_worker.mark_response(cfg.contact_db(contact),
                                           str(msg.get("text") or ""))
        except Exception:  # noqa: BLE001
            pass
        debounce_ms = int(self.cfg.get("reply_debounce_ms") or 0)
        kind = str(msg.get("kind") or "text")
        if debounce_ms <= 0 or kind != "text":
            self._reply(msg)
            return
        now = time.time()
        buf = self.pending.get(contact)
        text = str(msg.get("text") or "").strip()
        if not buf:
            self.pending[contact] = {"msg": msg, "texts": [text],
                                     "deadline": now + debounce_ms / 1000.0}
            return
        limit = int(self.cfg.get("max_chars") or 500)
        merged = chr(10).join(x for x in (buf["texts"] + [text]) if x)
        if len(merged) > limit:
            # 已经攒太多了，立刻回，别再等
            self._reply({"friend": contact, "text": merged[:limit], "kind": "text"})
            self.pending.pop(contact, None)
            return
        buf["texts"].append(text)

    def _flush_pending(self, *, force: bool = False) -> None:
        now = time.time()
        for contact in list(self.pending):
            buf = self.pending[contact]
            if not force and now < float(buf.get("deadline") or 0):
                continue
            self.pending.pop(contact, None)
            texts = [x for x in (buf.get("texts") or []) if x]
            if not texts:
                continue
            msg = dict(buf.get("msg") or {})
            msg.update({"friend": contact, "text": chr(10).join(texts),
                        "kind": "text"})
            try:
                self._reply(msg)
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                self._record("worker_error",
                             {"error": f"{type(exc).__name__}: {exc}"})

    def _handle_feedback(self, msg: dict) -> bool:
        contact = str(msg.get("friend") or "")
        reaction = parse_feedback(str(msg.get("text") or ""))
        if not reaction:
            return False
        turn_id = self.last_turns.get(contact)
        if not turn_id:
            self._record("feedback_ignored", {"friend": contact, "reason": "no_last_turn"})
            return True
        try:
            from agi.selflearn.chat import SelfLearnAgent
            result = SelfLearnAgent(cfg.contact_db(contact)).feedback(turn_id, reaction, "wechat_explicit")
        except Exception as exc:  # noqa: BLE001
            self._record("feedback_error", {"friend": contact, "error": f"{type(exc).__name__}: {exc}"})
            return True
        self._record("feedback", {"friend": contact, "turn_id": turn_id,
                                  "reaction": reaction, "result": result})
        return True

    def _image_data_url(self, media_path: str) -> str:
        if not media_path:
            return ""
        try:
            p = Path(str(media_path))
            if not p.is_absolute():
                p = ROOT / p
            if not p.is_file() or p.stat().st_size > 12_000_000:
                return ""
            mime = mimetypes.guess_type(str(p))[0] or "image/png"
            if not mime.startswith("image/"):
                return ""
            return "data:" + mime + ";base64," + base64.b64encode(p.read_bytes()).decode()
        except Exception:  # noqa: BLE001
            return ""

    def _reply(self, msg: dict) -> None:
        from agi.selflearn.chat import SelfLearnAgent

        contact = str(msg.get("friend") or "")
        text = str(msg.get("text") or "")
        # 上行也消毒：引用内容里可能是内部 ID（上游已按类型解析，这里兜底）
        text = textguard.strip_internal_ids(text)
        kind = str(msg.get("kind") or "text")
        db_path = cfg.contact_db(contact)
        agent = SelfLearnAgent(db_path)
        if kind == "system":
            if "撤回" in text:
                text = "[对方撤回了一条消息]"
            elif "引用消息" in text:
                text = text.replace("[引用消息]", "对方引用了这条消息：", 1)
        image_data = ""
        if kind in ("image", "emoji"):
            image_data = self._image_data_url(str(msg.get("media_path") or ""))
            if not text.strip():
                text = "发了个表情包" if kind == "emoji" else "发了张图片"
        result = agent.send(text, image=image_data or None)
        if result.get("turn_id"):
            self.last_turns[contact] = str(result["turn_id"])
        if result.get("skipped"):
            self._record("skipped", {"friend": contact, "reason": result.get("skip_reason")})
            return
        bubbles = result.get("bubbles") or ([result.get("reply")] if result.get("reply") else [])
        for bubble in bubbles:
            bubble = str(bubble or "").strip()
            if not bubble:
                continue
            out = self._send(contact, bubble, reactive=True)
            entry = {"friend": contact, "text": bubble, "status": out.get("status"),
                     "dry_run": bool(self.cfg.get("dry_run", True)), "result": out}
            self._record("outbound", entry)
            if out.get("ok"):
                self.sent += 1
            else:
                self.failed += 1
            if not self.cfg.get("dry_run", True):
                time.sleep(float(self.cfg.get("reply_delay") or 0))

    def _send(self, contact: str, text: str, *, force: bool = False,
              reactive: bool = False, proactive: bool = False) -> dict:
        # P0-22：STOP 文件是最高优先级，先于任何发送判断（含 reactive/force）
        if self.stop_present():
            self._log_stopped()
            self._record("send_blocked", {"friend": contact, "reason": "stop_file"})
            return {"ok": False, "status": "blocked", "error": "stop_file"}
        # 纵深防御：出站文本里的微信内部 ID 一律摘掉（2026-10-24 引用事故）
        original = str(text or "")
        text = textguard.strip_internal_ids(original)
        if not text:
            self._record("send_blocked", {"friend": contact, "reason": "internal_id_only"})
            return {"ok": False, "status": "blocked", "error": "internal_id_only"}
        if text != original:
            self._record("send_sanitized", {"friend": contact,
                                            "removed_chars": len(original) - len(text)})
        content = policy.check_content(text, self.cfg)
        if not content.get("ok"):
            self._record("send_blocked", {"friend": contact,
                                          "reason": content.get("reason"),
                                          "detail": content.get("detail", ""),
                                          "chars": content.get("chars")})
            return {"ok": False, "status": "blocked",
                    "error": str(content.get("reason") or "content_rejected")}
        mode = control.send_mode(self.cfg, contact)
        if mode == "off":
            self._record("send_blocked", {"friend": contact, "reason": "send_mode_off"})
            return {"ok": False, "status": "blocked", "error": "send_mode_off"}
        if mode == "confirm" and not force:
            item = drafts.add(contact, text, "confirm", path=self.draft_path)
            self._record("draft", {"friend": contact, "text": text, "draft_id": item["id"]})
            return {"ok": False, "status": "draft", "requires_confirm": True,
                    "draft_id": item["id"], "contact": contact, "text": text}
        if self.cfg.get("stop"):
            self._record("send_blocked", {"friend": contact, "reason": "stop_switch"})
            return {"ok": False, "status": "blocked", "error": "stop_switch"}
        now = time.time()
        # P0-22：夜间静默硬保护（发送前最后一道；quiet_hours=[] 会回退默认 [23,8]，
        # 只有显式 proactive_override_quiet=true 且 24h 内才能放行）
        # P0-25：静默只约束主动消息——用户自己发消息过来（reactive / force 手动发送）必须回，
        # 否则凌晨聊天会被静默闸全部吞掉（2026-10-26 真机事故）。
        override_active, _just = self._quiet_override_active(now)
        quiet_blocks = (not reactive) and (not force)
        if quiet_blocks and self._quiet_blocked(now):
            self._record("send_blocked", {"friend": contact, "reason": "quiet_hours"})
            return {"ok": False, "status": "blocked", "error": "quiet_hours"}
        policy_cfg = self.cfg
        if override_active or reactive or force:
            # 覆盖期/被动回复/手动发送：把 policy 的静默临时置空（静默只拦主动）
            policy_cfg = dict(self.cfg)
            policy_cfg["quiet_hours"] = [0, 0]
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.sent_day:
            self.sent_day = day
            self.sent_today = 0
        recent = sum(1 for t in self.send_times if now - t < 60)
        # 每小时窗口：顺手清掉一小时前的记录，避免列表无限增长
        self.send_times = [t for t in self.send_times if now - t < 3600]
        hourly = len(self.send_times)
        last_sent_at = max(self.send_times) if self.send_times else 0.0
        verdict = policy.check_send(now=now, hour=time.localtime(now).tm_hour,
                                    recent_60s=recent, today_count=self.sent_today,
                                    cfg=policy_cfg, reactive=reactive and not proactive,
                                    text=text, last_sent_at=last_sent_at,
                                    hourly_count=hourly)
        if not verdict.get("ok"):
            self._record("send_blocked", {"friend": contact, "reason": verdict.get("reason")})
            return {"ok": False, "status": "blocked", "error": verdict.get("reason")}
        if self.cfg.get("dry_run", True):
            out = self.sender.send(contact, text, dry_run=True)
            if not out.get("ok") and out.get("status") == "failed":
                self._archive_failed_draft(contact, text,
                                           str(out.get("error") or "failed"))
            return out
        if not WXAUTO_PYTHON.exists():
            return self._on_send_failure(contact, text, f"缺少 wxauto venv: {WXAUTO_PYTHON}")
        cmd = [str(WXAUTO_PYTHON), "-m", "agi.wechat_gateway.send_cli",
               "--contact", contact, "--text", text]
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=90,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                       "WECHAT_SEND_PROBE_ENABLED":
                                           "1" if self.cfg.get("send_probe_enabled", True) else "0",
                                       "WECHAT_SEND_RETRY_MAX":
                                           str(self.cfg.get("send_retry_max", 1)),
                                       "WECHAT_GATEWAY_LOG": str(self.log_path)})
            line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            if not line:
                return self._on_send_failure(
                    contact, text, (proc.stderr or "send_cli 无输出")[-500:])
            out = json.loads(line)
            if out.get("ok"):
                self.send_times.append(time.time())
                self.sent_today += 1
            else:
                return self._on_send_failure(
                    contact, text,
                    str(out.get("error") or out.get("status") or "failed"),
                    error_key=str(out.get("error_key") or ""),
                    stage=str(out.get("stage") or ""))
            return out
        except Exception as exc:  # noqa: BLE001
            return self._on_send_failure(contact, text, f"{type(exc).__name__}: {exc}")


    # ------------------------------------------------------------------ #
    # P5-a：多媒体多气泡发送（STOP/静默/白名单/干跑与文本发送同一套闸门）
    # ------------------------------------------------------------------ #
    def _media_env(self) -> dict:
        """子进程媒体配置：与文本发送同一套 env 下发方式（send_cli 读取）。"""
        media_dirs = [str(x) for x in (self.cfg.get("media_dirs") or [])]
        return {
            "WECHAT_MEDIA_ENABLED": "1" if media_mod.media_enabled(self.cfg) else "0",
            "WECHAT_MEDIA_DIRS": json.dumps(media_dirs, ensure_ascii=False),
            "WECHAT_MEDIA_MAX_MB": str(self.cfg.get("media_max_mb", 10)),
            "WECHAT_MEDIA_INTERVAL_MS": str(self.cfg.get("media_send_interval_ms", 120)),
            "WECHAT_STICKER_FALLBACK_EMOJI": str(
                self.cfg.get("sticker_fallback_emoji") or "😄"),
            # 审计统一由 Gateway._record 写，子进程那层关掉避免重复
            "WECHAT_MEDIA_AUDIT": "0",
        }

    def _media_blocked(self, contact: str, reason: str,
                       items: list[dict] | None = None) -> dict:
        self._record("send_blocked", {"friend": contact, "reason": reason})
        # 逐条返回 blocked 结果（和成功路径同 shape，决策层按 item["ok"] 统计即可）
        results = [WxAutoSender._bubble_result(item, False, "blocked", reason)
                   for item in (items or []) if isinstance(item, dict)]
        return {"ok": False, "status": "blocked", "error": reason,
                "items": results, "sent": 0, "failed": 0, "draft_ids": []}

    def send_media_batch(self, contact: str, items: list[dict] | tuple[dict, ...], *,
                         proactive: bool = False, reactive: bool = False,
                         force: bool = False) -> dict:
        """发送一组多媒体气泡（批量版，返回含统计/草稿的 dict）。

        items 规范见 :mod:`agi.wechat_gateway.media`：
        ``{type: text|emoji_text|image|gif|sticker, text?, path?, name?}``。
        与 :meth:`_send` 共用闸门：STOP > media_enabled > 白名单 > 内容 > send_mode >
        stop 开关 > 夜间静默 > 频率/预算；真实发送在 wxauto4 venv 子进程里执行。
        返回 ``{ok, status, items, sent, failed, draft_ids}``；失败的**文本**气泡
        会落草稿（不丢内容），media 失败只记审计、不影响同批其它气泡。
        """
        contact_name = str(contact or "").strip()
        raw_items = [dict(item) for item in (items or []) if isinstance(item, dict)]
        if not contact_name or not raw_items:
            return {"ok": False, "status": "failed", "error": "empty_contact_or_items",
                    "items": [], "sent": 0, "failed": 0, "draft_ids": []}

        # ① STOP 最高优先级（含 reactive/force）
        if self.stop_present():
            self._log_stopped()
            return self._media_blocked(contact_name, "stop_file", raw_items)
        # ② 媒体开关（默认 false：上线前显式开启）
        if not media_mod.media_enabled(self.cfg):
            return self._media_blocked(contact_name, "media_disabled", raw_items)
        # ③ 白名单
        allowed = [str(x) for x in (self.cfg.get("allowed_contacts") or [])
                   if str(x).strip()]
        if allowed and contact_name not in allowed:
            return self._media_blocked(contact_name, "not_allowed", raw_items)
        # ④ 文本消毒/内容闸门 + 逐条校验（坏条目只失败自己，不拖累整批）
        results: list[dict | None] = [None] * len(raw_items)
        cleaned: list[tuple[int, dict]] = []
        for index, item in enumerate(raw_items):
            kind = str(item.get("type") or "").strip().lower()
            if kind in media_mod.TEXT_TYPES:
                original = str(item.get("text") or "")
                text = textguard.strip_internal_ids(original)
                if not text:
                    results[index] = {"ok": False, "status": "failed", "type": kind,
                                      "error": "internal_id_only", "fallback_used": [],
                                      "used": None}
                    continue
                if text != original:
                    self._record("send_sanitized", {"friend": contact_name,
                                                    "removed_chars": len(original) - len(text)})
                    item = dict(item)
                    item["text"] = text
                content = policy.check_content(text, self.cfg)
                if not content.get("ok"):
                    results[index] = {
                        "ok": False, "status": "failed", "type": kind,
                        "error": str(content.get("reason") or "content_rejected"),
                        "fallback_used": [], "used": None}
                    continue
            cleaned.append((index, item))
        if not cleaned:
            return {"ok": False, "status": "failed", "error": "all_items_invalid",
                    "items": [r for r in results if r is not None],
                    "sent": 0, "failed": len(raw_items), "draft_ids": []}
        # ⑤ send_mode：off 拦住；confirm 只给文本落确认草稿（media 无法草稿化）
        mode = control.send_mode(self.cfg, contact_name)
        if mode == "off":
            return self._media_blocked(contact_name, "send_mode_off", raw_items)
        if mode == "confirm" and not force:
            draft_ids: list[str] = []
            for index, item in cleaned:
                kind = str(item.get("type") or "")
                if kind in media_mod.TEXT_TYPES:
                    draft = drafts.add(contact_name, str(item.get("text") or ""),
                                       "confirm", path=self.draft_path)
                    draft_ids.append(draft["id"])
                    self._record("draft", {"friend": contact_name,
                                           "text": str(item.get("text") or ""),
                                           "draft_id": draft["id"]})
                    results[index] = {"ok": False, "status": "draft", "type": kind,
                                      "draft_id": draft["id"], "fallback_used": [],
                                      "used": None}
                else:
                    results[index] = {"ok": False, "status": "skipped", "type": kind,
                                      "error": "media_requires_auto_mode",
                                      "fallback_used": [], "used": None}
            return {"ok": False, "status": "draft", "requires_confirm": True,
                    "items": [r for r in results if r is not None],
                    "sent": 0, "failed": 0, "draft_ids": draft_ids}
        if self.cfg.get("stop"):
            return self._media_blocked(contact_name, "stop_switch", raw_items)
        now = time.time()
        # ⑥ 夜间静默：与 _send 一致（只拦主动；reactive/force 必须回）
        override_active, _just = self._quiet_override_active(now)
        quiet_blocks = (not reactive) and (not force)
        if quiet_blocks and self._quiet_blocked(now):
            return self._media_blocked(contact_name, "quiet_hours", raw_items)
        policy_cfg = self.cfg
        if override_active or reactive or force:
            policy_cfg = dict(self.cfg)
            policy_cfg["quiet_hours"] = [0, 0]
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.sent_day:
            self.sent_day = day
            self.sent_today = 0
        recent = sum(1 for t in self.send_times if now - t < 60)
        self.send_times = [t for t in self.send_times if now - t < 3600]
        hourly = len(self.send_times)
        last_sent_at = max(self.send_times) if self.send_times else 0.0
        batch_text = " ".join(str(item.get("text") or "") for _, item in cleaned)[:200]
        verdict = policy.check_send(now=now, hour=time.localtime(now).tm_hour,
                                    recent_60s=recent, today_count=self.sent_today,
                                    cfg=policy_cfg, reactive=reactive and not proactive,
                                    text=batch_text, last_sent_at=last_sent_at,
                                    hourly_count=hourly)
        if not verdict.get("ok"):
            return self._media_blocked(contact_name,
                                       str(verdict.get("reason") or "policy_blocked"),
                                       raw_items)
        # ⑦ dry_run：不导入 wxauto，只返回计划
        if self.cfg.get("dry_run", True):
            for index, item in cleaned:
                results[index] = {"ok": True, "status": "planned",
                                  "type": str(item.get("type") or ""),
                                  "fallback_used": [], "used": None}
                self._record("send_media", {
                    "friend": contact_name, "type": str(item.get("type") or ""),
                    "name": media_mod.item_label(item), "status": "planned",
                    "fallback": [], "used": None, "error": "", "error_key": ""})
            return {"ok": True, "status": "planned",
                    "items": [r for r in results if r is not None],
                    "sent": 0, "failed": 0, "draft_ids": []}

        # ⑧ 真发：wxauto4 venv 子进程（与文本发送同一条链路）
        if not WXAUTO_PYTHON.exists():
            payload_items = []
            error = f"缺少 wxauto venv: {WXAUTO_PYTHON}"
        else:
            payload = [item for _, item in cleaned]
            cmd = [str(WXAUTO_PYTHON), "-m", "agi.wechat_gateway.send_cli",
                   "--contact", contact_name,
                   "--items", json.dumps(payload, ensure_ascii=False)]
            error = ""
            payload_items = []
            try:
                proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=120,
                                      env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                           "WECHAT_SEND_PROBE_ENABLED":
                                               "1" if self.cfg.get("send_probe_enabled", True) else "0",
                                           "WECHAT_SEND_RETRY_MAX":
                                               str(self.cfg.get("send_retry_max", 1)),
                                           "WECHAT_GATEWAY_LOG": str(self.log_path),
                                           **self._media_env()})
                stdout = (proc.stdout or "").strip()
                line = stdout.splitlines()[-1] if stdout else ""
                if not line:
                    error = (proc.stderr or "send_cli 无输出")[-500:]
                else:
                    parsed = json.loads(line)
                    payload_items = parsed.get("items") or []
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"

        # ⑨ 汇总：结果回填、计数、文本失败落草稿、逐条 send_media 审计
        sent = 0
        failed = 0
        draft_ids: list[str] = []
        for order, (index, item) in enumerate(cleaned):
            result = payload_items[order] if order < len(payload_items) else None
            if not isinstance(result, dict):
                result = {"ok": False, "status": "failed", "type": str(item.get("type") or ""),
                          "error": error or "missing_result", "fallback_used": [], "used": None}
            result = dict(result)
            result.setdefault("type", str(item.get("type") or ""))
            results[index] = result
            self._record("send_media", {
                "friend": contact_name, "type": str(result.get("type") or ""),
                "name": media_mod.item_label(item),
                "status": str(result.get("status") or ""),
                "fallback": result.get("fallback_used") or [],
                "used": result.get("used"),
                "error": str(result.get("error") or "")[:200],
                "error_key": (str(result.get("error_key") or "")
                              or self._normalize_error_key(str(result.get("error") or "")))})
            if result.get("ok"):
                sent += 1
                self.send_times.append(now)
                self.sent_today += 1
                continue
            failed += 1
            # 文本气泡失败：落草稿，内容不丢（media 无草稿形态，只记审计）
            kind = str(item.get("type") or "").strip().lower()
            if kind in media_mod.TEXT_TYPES:
                draft = self._archive_failed_draft(
                    contact_name, str(item.get("text") or ""),
                    str(result.get("error") or error or "failed"),
                    reason="send_failed",
                    error_key=str(result.get("error_key") or ""),
                    stage="media")
                if draft is not None:
                    draft_ids.append(draft["id"])
        self.sent += sent
        self.failed += failed
        all_ok = bool(sent) and failed == 0
        if all_ok:
            status = "sent"
        elif sent:
            status = "partial"
        elif draft_ids:
            status = "draft"
        else:
            status = "failed"
        return {"ok": all_ok, "status": status,
                "items": [r for r in results if r is not None],
                "sent": sent, "failed": failed, "draft_ids": draft_ids}

    def send_bubbles(self, contact: str, items: list[dict] | tuple[dict, ...], *,
                     proactive: bool = False, reactive: bool = False,
                     force: bool = False) -> list[dict]:
        """发送一组多媒体气泡，返回**逐条结果 list**（与 WxAutoSender.send_bubbles 同契约）。

        决策层（task-60）直接调用本方法：``results[i]`` = {ok, status, type, error,
        fallback_used, used}；批量统计/草稿详情见 :meth:`send_media_batch`。
        """
        batch = self.send_media_batch(contact, items, proactive=proactive,
                                      reactive=reactive, force=force)
        return batch.get("items") or []

    def _on_send_failure(self, contact: str, text: str, error: str, *,
                         error_key: str = "", stage: str = "") -> dict:
        """真实发送失败：先试窗口级兜底，再落草稿。

        P0-4：落草稿时返回 ``status=draft`` + ``draft_id``，让调用方知道内容已保住、
        等人工决定（不再只返回一个丢失上下文的 failed）。
        """
        fallback = self._fallback_send(contact, text, error)
        if fallback and fallback.get("ok") and fallback.get("status") == "sent":
            now = time.time()
            self.send_times.append(now)
            self.sent_today += 1
            fallback["trigger_error"] = str(error)[:200]
            fallback["error_key"] = error_key or self._normalize_error_key(error)
            return fallback
        if fallback and fallback.get("ok") and fallback.get("status") == "clipboard":
            # P0-7：clipboard 原样透传给调用方（ok=True 但**未送达**，等人手动粘贴）。
            # 这里只归档草稿，绝不能改写成 sent/draft —— 主动链据此不计 sent、不重置熔断。
            self._archive_failed_draft(
                contact, text,
                "fallback_clipboard: " + str(fallback.get("warning") or ""),
                reason="fallback_clipboard")
            return fallback
        key = error_key or self._normalize_error_key(error)
        item = self._archive_failed_draft(contact, text, error,
                                          error_key=key, stage=stage)
        result = {"ok": False, "status": "failed", "error": str(error)[:200]}
        if item is not None:
            # 发送确实失败了，只是内容落成了草稿：send_failed 供熔断/统计识别
            # （status=draft 本身在 task-16 语义里属于"非故障状态"）
            result.update({"status": "draft", "draft_id": item["id"],
                           "requires_confirm": True, "send_failed": True})
        if key:
            result["error_key"] = key
        if stage:
            result["stage"] = stage
        return result

    @staticmethod
    def _normalize_error_key(error: str) -> str:
        """与 sender/proactive_worker 同口径：0x… → 0xADDR，压空白，取前 60 字。"""
        import re as _re
        value = _re.sub(r"0x[0-9a-fA-F]+", "0xADDR", str(error or ""))
        value = _re.sub(r"\s+", " ", value).strip()
        return value[:60]

    def _fallback_send(self, contact: str, text: str, error: str) -> dict | None:
        """窗口级兜底（默认关闭；默认只做剪贴板协助，不自动盲发）。"""
        if not self.cfg.get("fallback_enabled"):
            return None
        try:
            from . import win_send
            out = win_send.send_via_window(text, self.cfg)
        except Exception as exc:  # noqa: BLE001
            out = {"ok": False, "status": "failed",
                   "error": f"{type(exc).__name__}: {exc}"}
        self._record("fallback", {"friend": contact, "status": out.get("status"),
                                  "ok": bool(out.get("ok")),
                                  "warning": str(out.get("warning") or "")[:200],
                                  "error": str(out.get("error") or "")[:200],
                                  "trigger": str(error)[:120]})
        return out

    def _archive_failed_draft(self, contact: str, text: str, error: str,
                              reason: str = "send_failed", *,
                              error_key: str = "", stage: str = "") -> dict | None:
        """发送失败不丢内容：落成草稿，等人工决定重发或丢弃。"""
        try:
            item = drafts.add(contact, text, reason, path=self.draft_path)
            event = {"friend": contact, "text": text,
                     "draft_id": item["id"], "reason": "send_failed",
                     "error": str(error)[:200]}
            key = error_key or self._normalize_error_key(error)
            if key:
                event["error_key"] = key
            if stage:
                event["stage"] = stage
            self._record("draft", event)
            return item
        except Exception:  # noqa: BLE001
            return None

    def status(self) -> dict:
        state = {}
        try:
            if self.backend is not None:
                state = self.backend.get_state()
        except Exception:  # noqa: BLE001
            state = {}
        # P3-a：内状态（本地灵魂模型）观测；import 失败不影响 /status
        try:
            from ..inner import inject as inner_inject
            inner_status = inner_inject.stats()
        except Exception:  # noqa: BLE001
            inner_status = {"mode": "off", "last_latency_ms": None,
                            "fallbacks_24h": 0}
        # P0-24：死区哨兵抑制状态（预期不发送时不报 gateway_stale）
        try:
            stale_reason = self.stale_suppression()
        except Exception:  # noqa: BLE001 - 观测字段失败不影响 /status
            stale_reason = ""
        return {
            "ok": True,
            "service": "wechat_gateway",
            "port": int(self.cfg.get("port") or 801),
            "started_at": self.started_at,
            "ready": self.ready,
            "start_error": self.start_error,
            "enabled": bool(self.cfg.get("enabled")),
            "auto_reply": bool(self.cfg.get("auto_reply")),
            "dry_run": bool(self.cfg.get("dry_run", True)),
            "allowed_contacts": self.cfg.get("allowed_contacts") or [],
            "monitored_contacts": self.cfg.get("monitored_contacts") or [],
            "queue": self.queue.qsize(),
            "sent": self.sent,
            "failed": self.failed,
            "send_mode": control.send_mode(self.cfg),
            "drafts_pending": len(drafts.list_all(path=self.draft_path, limit=200)),
            "admin_token_set": bool(self.cfg.get("admin_token")),
            "policy": {"stop": bool(self.cfg.get("stop")),
                       "quiet_hours": self.cfg.get("quiet_hours"),
                       "rate_per_minute": self.cfg.get("rate_per_minute"),
                       "min_interval_s": self.cfg.get("min_interval_s"),
                       "hourly_limit": self.cfg.get("hourly_limit"),
                       "daily_limit": self.cfg.get("daily_limit"),
                       "sent_today": self.sent_today,
                       "sent_last_hour": len([t for t in self.send_times
                                              if time.time() - t < 3600])},
            "fallback": {"enabled": bool(self.cfg.get("fallback_enabled")),
                         "mode": str(self.cfg.get("fallback_mode") or "manual")},
            "proactive": {"enabled": bool(self.cfg.get("proactive_enabled")),
                          "stopped": self.stop_present(),
                          "interval_s": {
                              "min": self.cfg.get("proactive_interval_min_s"),
                              "max": self.cfg.get("proactive_interval_max_s")},
                          "probability": {
                              "min": self.cfg.get("proactive_probability_min"),
                              "max": self.cfg.get("proactive_probability_max")},
                          "last": self.last_proactive},
            "inner": inner_status,
            # P0-24：stale_suppressed=true 表示当前处于「预期不发送」状态，
            # 哨兵主动静默；reason ∈ {disabled, stopped, quiet} 或 null。
            "stale_suppressed": bool(stale_reason),
            "stale_suppressed_reason": stale_reason or None,
            "backend": state,
            "receiver": self.receiver.status() if self.receiver else None,
            "recent": list(self.recent)[-20:],
        }


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        # ------------------------------------------------------------ helpers
        def _json(self, code, data):
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _port(self) -> int:
            try:
                return int(self.server.server_address[1])
            except Exception:  # noqa: BLE001
                return int(gateway.cfg.get("port") or 801)

        def _query_token(self) -> str:
            try:
                return (parse_qs(urlsplit(self.path).query).get("token") or [""])[0]
            except Exception:  # noqa: BLE001
                return ""

        def _guard(self) -> bool:
            """Host 回环校验 + token 鉴权；任一不过直接回错误。"""
            if not control.host_ok(self.headers.get("Host") or "", self._port()):
                self._json(403, {"ok": False, "error": "host not allowed"})
                return False
            headers = {k: v for k, v in self.headers.items()}
            if not control.auth_ok(headers, gateway.cfg, self._query_token()):
                self._json(401, {"ok": False, "error": "admin token required"})
                return False
            return True

        def _read_json(self, limit: int = 1_048_576) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                n = 0
            if n > limit:
                raise ValueError("body too large")
            raw = self.rfile.read(n) if n > 0 else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError as exc:
                raise ValueError("bad json") from exc
            if not isinstance(body, dict):
                raise ValueError("bad json")
            return body

        # ------------------------------------------------------------ routes
        def do_POST(self):
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
            except ValueError as exc:
                self._json(413 if "large" in str(exc) else 400,
                           {"ok": False, "error": str(exc)})
                return

            if path == "/api/approve":
                contact = str(body.get("contact") or "")
                text = str(body.get("text") or "")
                if contact not in (gateway.cfg.get("allowed_contacts") or []) or not text:
                    self._json(400, {"ok": False, "error": "contact 不在白名单或 text 为空"})
                    return
                self._json(200, gateway._send(contact, text, force=True))
                return

            if path == "/api/draft/approve":
                draft_id = str(body.get("id") or "").strip()
                item = drafts.discard(draft_id, path=gateway.draft_path)
                if item is None:
                    self._json(404, {"ok": False, "error": "草稿不存在"})
                    return
                out = gateway._send(str(item.get("contact") or ""),
                                    str(item.get("text") or ""), force=True)
                gateway._record("draft_approved", {"draft_id": draft_id,
                                                   "friend": item.get("contact"),
                                                   "status": out.get("status")})
                self._json(200, {**out, "draft": item})
                return

            if path == "/api/draft/reject":
                draft_id = str(body.get("id") or "").strip()
                item = drafts.discard(draft_id, path=gateway.draft_path)
                if item is None:
                    self._json(404, {"ok": False, "error": "草稿不存在"})
                    return
                gateway._record("draft_rejected", {"draft_id": draft_id,
                                                   "friend": item.get("contact")})
                self._json(200, {"ok": True, "rejected": draft_id})
                return

            if path in ("/api/proactive/stop", "/api/proactive/resume"):
                # P0-22：紧急停止开关（admin_token 已由 _guard 校验；幂等）
                stopped = path.endswith("/stop")
                result = gateway.set_stop(stopped)
                self._json(200 if result.get("ok") else 500, result)
                return

            if path in ("/api/media/send", "/api/send/media"):
                # P5-a 媒体发送 HTTP 入口（控制台/调试用）：{contact, items:[{type,...}], proactive?}
                # 鉴权/_guard 已校验；contact 必须在白名单；items 为 media.py 契约。
                contact = str(body.get("contact") or "")
                items = body.get("items")
                if not contact or contact not in (gateway.cfg.get("allowed_contacts") or []):
                    self._json(400, {"ok": False, "error": "contact 不在白名单"})
                    return
                if not isinstance(items, list) or not items:
                    self._json(400, {"ok": False, "error": "items 必须是非空列表"})
                    return
                if len(items) > 10:
                    self._json(400, {"ok": False, "error": "单次最多 10 条气泡"})
                    return
                result = gateway.send_media_batch(
                    contact, items,
                    proactive=bool(body.get("proactive")),
                    reactive=bool(body.get("reactive")),
                    force=bool(body.get("force")))
                self._json(200 if result.get("ok") or result.get("sent") else 502, result)
                return

            if path == "/api/test/inbound":
                if not gateway.cfg.get("dry_run", True):
                    self._json(403, {"ok": False, "error": "测试注入接口只允许 dry_run=true"})
                    return
                friend = str(body.get("friend") or "")
                text = str(body.get("text") or "")
                if friend not in (gateway.cfg.get("allowed_contacts") or []) or not text:
                    self._json(400, {"ok": False, "error": "friend 不在白名单或 text 为空"})
                    return
                gateway.on_inbound({"friend": friend, "text": text,
                                    "id": "TEST-" + str(time.time())})
                self._json(202, {"ok": True, "queued": True, "dry_run": True})
                return

            self._json(404, {"ok": False, "error": "not found"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/healthz"):
                self._json(200, {"ok": True, "service": "wechat_gateway"})
                return
            if not self._guard():
                return
            if path == "/status":
                self._json(200, gateway.status())
                return
            if path == "/api/drafts":
                self._json(200, {"ok": True,
                                 "drafts": drafts.list_all(path=gateway.draft_path)})
                return
            self._json(404, {"ok": False, "error": "not found"})

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.wechat_gateway.server")
    parser.add_argument("--config", default=str(cfg.CONFIG_PATH))
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    cfg_obj = cfg.load()
    if args.port:
        cfg_obj["port"] = args.port
    token, generated = control.ensure_admin_token(cfg_obj)
    if generated:
        try:
            cfg.save(cfg_obj)
        except Exception as exc:  # noqa: BLE001
            print(f"admin_token 生成后写回配置失败：{exc}", flush=True)
        print(f"[gateway] 已生成管理 token（X-Admin-Token）：{token}", flush=True)
    gateway = Gateway(cfg_obj)
    gateway.start()
    port = int(cfg_obj.get("port") or 801)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(gateway))
    print(f"wechat_gateway http://127.0.0.1:{port}/  ready={gateway.ready} "
          f"error={gateway.start_error}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        gateway.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
