# -*- coding: utf-8 -*-
"""801 微信网关：接收 → selflearn 生成 → wxauto 发送。

默认 enabled=false / dry_run=true；真实发送必须显式开启，且只发白名单。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config as cfg
from .receiver import WeChatReceiver
from .sender import WxAutoSender

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
                self._reply(msg)
            except Exception as exc:  # noqa: BLE001
                self.failed += 1
                self._record("worker_error", {"error": f"{type(exc).__name__}: {exc}"})

    def _reply(self, msg: dict) -> None:
        from agi.selflearn.chat import SelfLearnAgent

        contact = str(msg.get("friend") or "")
        text = str(msg.get("text") or "")
        db_path = cfg.contact_db(contact)
        agent = SelfLearnAgent(db_path)
        result = agent.send(text)
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

    def _send(self, contact: str, text: str) -> dict:
        if self.cfg.get("dry_run", True):
            return self.sender.send(contact, text, dry_run=True)
        if not WXAUTO_PYTHON.exists():
            return {"ok": False, "status": "failed", "error": f"缺少 wxauto venv: {WXAUTO_PYTHON}"}
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
            return json.loads(line)
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
