"""只读跟随模式后端：不启动任何采集，只跟随桌面版写入的日志文件。

用途：当桌面版（``app.py``）或另一个程序正在监听微信时，网页端**不能**再起一份
采集线程（会争抢窗口焦点、重复读写媒体、干扰对方）。本模块实现一个只读后端：

* 从 ``config.json`` 读取好友缓存与监听名单（只读，不写回）；
* 从 ``log/messages.jsonl`` 读取历史消息，并周期性只读 tail 新追加的行；
* 实现 :class:`wx_core.api.Backend` 协议，事件形状与真实后端一致；
* ``set_monitored`` / ``start`` / ``stop`` 都不操作微信、不写任何文件。

这样网页端可以做「只读跟随」：桌面版收到新消息 → 写 ``messages.jsonl`` → 网页
1 秒内出现同一条消息，同时朋友圈/大模型分析照常可用。
"""
from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path

from wx_core.models import Friend, event_friends, event_log, event_status

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG = ROOT / "log" / "messages.jsonl"


class ReplayBackend:
    """只读跟随 ``log/messages.jsonl`` 的假后端（实现 Backend 协议）。"""

    def __init__(self, config: dict | None = None, *, log_path: str | Path | None = None,
                 interval: float = 1.0, history_limit: int = 3000):
        self.config = dict(config or {})
        self.log_path = Path(log_path) if log_path else DEFAULT_LOG
        self.interval = max(0.2, float(interval or 1.0))
        self.history_limit = max(1, int(history_limit or 3000))

        self.events: "queue.Queue[dict]" = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._tick = 0
        self._chat_title = "跟随桌面版日志"
        self._monitoring = [str(n) for n in (self.config.get("monitored") or []) if str(n).strip()]
        self._friends = [
            Friend(name=str(n)) for n in (self.config.get("friends_cache") or []) if str(n).strip()
        ]
        self._state = {
            "running": False,
            "wechat_found": False,
            "chat_title": self._chat_title,
            "monitoring": list(self._monitoring),
            "error": None,
            "tick": 0,
            "monitor_state": "following",
            "last_ocr_ms": 0,
            "friends_count": len(self._friends),
        }

    # ------------------------------------------------------------------
    # Backend 协议
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动日志跟随线程（幂等）。不会启动采集、不会操作微信。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._state["running"] = True
            self._thread = threading.Thread(target=self._run, name="wxbox-replay-follow", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """停止跟随线程（不操作微信、不写文件）。"""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        with self._lock:
            self._state["running"] = False
        self._emit_status()

    def get_state(self) -> dict:
        with self._lock:
            state = dict(self._state)
            state["monitoring"] = list(self._monitoring)
            state["running"] = bool(self._thread is not None and self._thread.is_alive())
            return state

    def list_friends(self, refresh: bool = False) -> list[Friend]:
        if refresh:
            self._log("跟随模式：好友名单来自桌面版 config.json 缓存，本页不能主动重新枚举")
        with self._lock:
            return [Friend(name=f.name, wxid=f.wxid, letter=f.letter) for f in self._friends]

    def set_monitored(self, names: list[str]) -> None:
        """只记录到本页状态，**不写 config.json、不操作桌面版**。"""
        clean = [str(n).strip() for n in (names or []) if str(n).strip()]
        with self._lock:
            self._monitoring = clean
            self._state["monitoring"] = list(clean)
        self._log(
            "跟随模式：本页只读，监听名单由桌面版决定；勾选只影响本页的浏览筛选。",
            "warn",
        )
        self._emit_status()

    def is_running(self) -> bool:
        return bool(self._thread is not None and self._thread.is_alive())

    # ------------------------------------------------------------------
    # 日志跟随
    # ------------------------------------------------------------------
    def _run(self) -> None:
        self._emit_friends()
        self._emit_status()
        self._log(
            f"只读跟随模式：正在读取 {self._tail_name()}；不会启动采集、不会操作微信，"
            "新消息由桌面版写入日志后 1 秒内同步到本页。",
        )
        pos = 0
        buffer = b""
        last_size = -1
        while not self._stop.is_set():
            try:
                path = self.log_path
                if path.is_file():
                    size = path.stat().st_size
                    if size < pos:  # 日志被截断/轮转：从头重读
                        pos, buffer, last_size = 0, b"", -1
                    if size != last_size:
                        with open(path, "rb") as handle:
                            handle.seek(pos)
                            chunk = handle.read()
                            pos = handle.tell()
                        last_size = size
                        buffer += chunk
                        lines = buffer.split(b"\n")
                        buffer = lines.pop() if lines else b""
                        self._emit_lines(lines)
                with self._lock:
                    self._tick += 1
                    self._state["tick"] = self._tick
                self._emit_status()
            except Exception as exc:  # noqa: BLE001 - 日志读取失败不能让线程退出
                self._log(f"读取消息日志失败：{exc}", "warn")
            self._stop.wait(self.interval)

    def _emit_lines(self, lines: list[bytes]) -> None:
        """把日志行解析成 message 事件；坏行跳过（不影响后续行）。"""
        emitted = 0
        for raw in lines:
            text = raw.decode("utf-8", "replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict) or not data.get("id"):
                continue
            if not data.get("kind_label"):
                from wx_core.models import KIND_LABEL, SENDER_LABEL

                data.setdefault("kind_label", KIND_LABEL.get(str(data.get("kind")), str(data.get("kind") or "")))
                data.setdefault("sender_label", SENDER_LABEL.get(str(data.get("sender")), str(data.get("sender") or "")))
            data.setdefault("time_text", time.strftime("%H:%M:%S", time.localtime(float(data.get("ts") or time.time()))))
            self.events.put({"type": "message", "data": data})
            emitted += 1
            if data.get("friend"):
                with self._lock:
                    self._chat_title = str(data["friend"])
                    self._state["chat_title"] = self._chat_title
            if emitted >= self.history_limit:
                return

    def _tail_name(self) -> str:
        try:
            return str(self.log_path.relative_to(ROOT)).replace("\\", "/")
        except Exception:  # noqa: BLE001
            return str(self.log_path)

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------
    def _emit_status(self) -> None:
        with self._lock:
            data = dict(self._state)
            data["monitoring"] = list(self._monitoring)
            data["running"] = bool(self._thread is not None and self._thread.is_alive())
            data["tick"] = self._tick
            data["friends_count"] = len(self._friends)
        self.events.put(event_status(**data))

    def _emit_friends(self) -> None:
        with self._lock:
            friends = [Friend(name=f.name, wxid=f.wxid, letter=f.letter) for f in self._friends]
        self.events.put(event_friends(len(friends), friends))

    def _log(self, text: str, level: str = "info") -> None:
        self.events.put(event_log(text, level))
