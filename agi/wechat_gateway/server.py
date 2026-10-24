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
from . import control, drafts, policy
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


def _log(entry: dict, path: Path | None = None) -> None:
    target = Path(path) if path else LOG_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


class Gateway:
    def __init__(self, cfg_obj: dict):
        self.cfg = dict(cfg_obj)
        self.draft_path = self.cfg.get("drafts_path") or drafts.DEFAULT_PATH
        self.log_path = Path(self.cfg.get("log_path") or LOG_PATH)
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
        entry = {"ts": time.time(), "kind": kind, **data}
        if kind != "gateway_stale":
            # 死区心跳自己不算"有活动"，否则哨兵会自我续命
            self.last_event_ts = entry["ts"]
        with self.lock:
            self.recent.append(entry)
        _log(entry, self.log_path)

    def _check_stale(self, now: float | None = None) -> bool:
        """P0-2 死区哨兵：超过 gateway_stale_alert_s 没事件就写 gateway_stale。

        复用 `_work` 现有事件循环（0.2s 转一圈），不新起线程；同一告警 30 分钟内
        最多写一次，避免自己刷屏。
        """
        now = time.time() if now is None else float(now)
        try:
            alert_s = float(self.cfg.get("gateway_stale_alert_s") or 1800.0)
        except (TypeError, ValueError):
            alert_s = 1800.0
        last_event = float(getattr(self, "last_event_ts", 0.0) or 0.0)
        if not last_event or now - last_event <= alert_s:
            return False
        if now - float(getattr(self, "_last_stale_ts", 0.0) or 0.0) < 1800.0:
            return False
        self._last_stale_ts = now
        self._record("gateway_stale", {
            "last_event_ts": round(last_event, 1),
            "idle_s": round(now - last_event, 1),
            "threshold_s": alert_s,
        })
        return True

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
        """M5：常驻主动调度（LLM 自己决定下次间隔，60–7200s = 1–120 分钟；带抖动）。"""
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
            self._record("proactive_schedule", {"next_delay_s": delay})
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
                                    cfg=self.cfg, reactive=reactive and not proactive,
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
                          "interval_s": {
                              "min": self.cfg.get("proactive_interval_min_s"),
                              "max": self.cfg.get("proactive_interval_max_s")},
                          "probability": {
                              "min": self.cfg.get("proactive_probability_min"),
                              "max": self.cfg.get("proactive_probability_max")},
                          "last": self.last_proactive},
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
