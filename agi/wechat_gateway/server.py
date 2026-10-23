# -*- coding: utf-8 -*-
"""801 微信网关：接收 → selflearn 生成 → wxauto 发送。

默认 enabled=false / dry_run=true；真实发送必须显式开启，且只发白名单。
"""
from __future__ import annotations

import argparse
import json
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

from . import config as cfg
from . import control, policy
from .receiver import WeChatReceiver
from .sender import WxAutoSender

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


def _log(entry: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


class Gateway:
    def __init__(self, cfg_obj: dict):
        self.cfg = dict(cfg_obj)
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
        self.send_times = deque()
        self.sent_today = 0
        self.sent_day = time.strftime("%Y-%m-%d")

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
        with self.lock:
            self.recent.append(entry)
        _log(entry)

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

    def _work(self) -> None:
        from agi.selflearn.chat import SelfLearnAgent
        from agi.selflearn.config import DB_PATH as DEFAULT_DB

        while not self.stop_event.is_set():
            try:
                msg = self.queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._handle_feedback(msg):
                    continue
                self._reply(msg)
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                self._record("worker_error", {"error": f"{type(exc).__name__}: {exc}"})

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

    def _reply(self, msg: dict) -> None:
        from agi.selflearn.chat import SelfLearnAgent

        contact = str(msg.get("friend") or "")
        text = str(msg.get("text") or "")
        db_path = cfg.contact_db(contact)
        agent = SelfLearnAgent(db_path)
        result = agent.send(text)
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
            out = self._send(contact, bubble)
            entry = {"friend": contact, "text": bubble, "status": out.get("status"),
                     "dry_run": bool(self.cfg.get("dry_run", True)), "result": out}
            self._record("outbound", entry)
            if out.get("ok"):
                self.sent += 1
            else:
                self.failed += 1
            if not self.cfg.get("dry_run", True):
                time.sleep(float(self.cfg.get("reply_delay") or 0))

    def _send(self, contact: str, text: str, *, force: bool = False) -> dict:
        mode = control.send_mode(self.cfg)
        if mode == "off":
            self._record("send_blocked", {"friend": contact, "reason": "send_mode_off"})
            return {"ok": False, "status": "blocked", "error": "send_mode_off"}
        if mode == "confirm" and not force:
            self._record("draft", {"friend": contact, "text": text})
            return {"ok": False, "status": "draft", "requires_confirm": True,
                    "contact": contact, "text": text}
        if self.cfg.get("stop"):
            self._record("send_blocked", {"friend": contact, "reason": "stop_switch"})
            return {"ok": False, "status": "blocked", "error": "stop_switch"}
        if self.cfg.get("dry_run", True):
            return self.sender.send(contact, text, dry_run=True)
        if not WXAUTO_PYTHON.exists():
            return {"ok": False, "status": "failed", "error": f"缺少 wxauto venv: {WXAUTO_PYTHON}"}
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.sent_day:
            self.sent_day = day
            self.sent_today = 0
        recent = sum(1 for t in self.send_times if now - t < 60)
        verdict = policy.check_send(now=now, hour=time.localtime(now).tm_hour,
                                    recent_60s=recent, today_count=self.sent_today,
                                    cfg=self.cfg)
        if not verdict.get("ok"):
            self._record("send_blocked", {"friend": contact, "reason": verdict.get("reason")})
            return {"ok": False, "status": "blocked", "error": verdict.get("reason")}
        cmd = [str(WXAUTO_PYTHON), "-m", "agi.wechat_gateway.send_cli",
               "--contact", contact, "--text", text]
        try:
            proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=90,
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
            if not line:
                return {"ok": False, "status": "failed",
                        "error": (proc.stderr or "send_cli 无输出")[-500:]}
            out = json.loads(line)
            if out.get("ok"):
                self.send_times.append(time.time())
                self.sent_today += 1
            return out
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}

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
            "admin_token_set": bool(self.cfg.get("admin_token")),
            "policy": {"stop": bool(self.cfg.get("stop")),
                       "quiet_hours": self.cfg.get("quiet_hours"),
                       "rate_per_minute": self.cfg.get("rate_per_minute"),
                       "daily_limit": self.cfg.get("daily_limit"),
                       "sent_today": self.sent_today},
            "backend": state,
            "receiver": self.receiver.status() if self.receiver else None,
            "recent": list(self.recent)[-20:],
        }


def make_handler(gateway: Gateway):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _json(self, code, data):
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/approve":
                if not control.auth_ok({k: v for k, v in self.headers.items()}, gateway.cfg):
                    self._json(401, {"ok": False, "error": "admin token required"}); return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    self._json(400, {"ok": False, "error": "bad json"}); return
                contact = str(body.get("contact") or "")
                text = str(body.get("text") or "")
                if contact not in (gateway.cfg.get("allowed_contacts") or []) or not text:
                    self._json(400, {"ok": False, "error": "contact 不在白名单或 text 为空"}); return
                out = gateway._send(contact, text, force=True)
                self._json(200, out); return
            if path == "/api/test/inbound":
                if not gateway.cfg.get("dry_run", True):
                    self._json(403, {"ok": False, "error": "测试注入接口只允许 dry_run=true"})
                    return
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    self._json(400, {"ok": False, "error": "bad json"})
                    return
                friend = str(body.get("friend") or "")
                text = str(body.get("text") or "")
                if friend not in (gateway.cfg.get("allowed_contacts") or []) or not text:
                    self._json(400, {"ok": False, "error": "friend 不在白名单或 text 为空"})
                    return
                gateway.on_inbound({"friend": friend, "text": text, "id": "TEST-" + str(time.time())})
                self._json(202, {"ok": True, "queued": True, "dry_run": True})
                return
            self._json(404, {"ok": False, "error": "not found"})

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/health", "/healthz"):
                self._json(200, {"ok": True, "service": "wechat_gateway"})
            elif path == "/status":
                self._json(200, gateway.status())
            else:
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
