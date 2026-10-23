"""假后端（演示 / 自动化测试用）。

不连接微信、不截图、不做 OCR，只在后台线程里每 ``interval`` 秒往
``events`` 队列里推一条示例消息（文字 / 内联 PNG 图片 / GIF 动图表情包 /
PDF 文件卡片 / 语音提示），并周期性推送 status/friends/log 事件。

用途：
* ``WXBOX_FAKE=1`` 或 ``python app.py --fake`` 时让界面可以独立演示；
* 真实后端 import / 初始化失败时自动回退，保证界面不白屏；
* 冒烟测试里作为后端替身（测试可自行 start/stop）。
"""
from __future__ import annotations

import os
import queue
import random
import threading
import time
from pathlib import Path

from wx_core.models import (
    KIND_EMOJI,
    KIND_FILE,
    KIND_IMAGE,
    KIND_TEXT,
    KIND_VOICE,
    Friend,
    Message,
    SENDER_FRIEND,
    SENDER_SELF,
    event_friends,
    event_log,
    event_message,
    event_status,
)

ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = ROOT / "media"

# 演示用好友名单（显示名）
_DEMO_FRIENDS: list[tuple[str, str, str]] = [
    ("白云一片去悠悠", "baiyun_001", "B"),
    ("文件传输助手", "filehelper", "W"),
    ("产品-王经理", "wangjingli_88", "C"),
    ("测试-小助手", "qa_bot", "C"),
    ("张三", "zhangsan_2024", "Z"),
    ("李四", "lisi_dev", "L"),
    ("设计-陈工", "chen_designer", "C"),
    ("运维-赵工", "ops_zhao", "Y"),
    ("客户-刘总", "liu_boss", "L"),
    ("前端-周同学", "zhou_fe", "Z"),
    ("后端-吴同学", "wu_be", "W"),
    ("家人-妈妈", "mama", "J"),
]

_TEXT_FRIEND = [
    "这是一条比较长的示例消息，用来演示窗口变窄时文字会自动换行，不会横向溢出，也不会被右侧裁掉。",
    "在吗？今天的需求文档我放群里了，你看一下",
    "这个功能什么时候能上线？客户那边在催了",
    "截图里这个报错帮忙看下，谢谢！",
    "晚上一起吃饭？老地方见 🍲",
    "刚发的文件收到了吗？",
    "这个方案我改好了，你有空帮忙过一遍～",
    "明天上午十点开个短会同步一下进度",
    "哈哈哈这个表情包太贴切了",
]

_TEXT_SELF = [
    "收到，我下午三点前给你反馈 👌",
    "好的，我看下再回复你",
    "没问题，我马上处理",
    "文件已收到，稍后拉个会",
]


class DevBackend:
    """实现 wx_core.api.Backend 协议的假后端（线程安全）。"""

    def __init__(self, config: dict | None = None, interval: float | None = None):
        """创建假后端；interval 默认取 WXBOX_FAKE_INTERVAL 或 2 秒。"""
        self.config = dict(config or {})
        env_interval = os.environ.get("WXBOX_FAKE_INTERVAL", "").strip()
        try:
            base_interval = float(interval if interval is not None else env_interval or 2.0)
        except (TypeError, ValueError):
            base_interval = 2.0
        self.interval = max(0.1, base_interval)

        self.events: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tick = 0
        self._friend_index = 0
        self._rng = random.Random(20261022)

        self._state: dict = {
            "running": False,
            "wechat_found": False,
            "chat_title": "",
            "monitoring": [],
            "error": None,
            "tick": 0,
            "monitor_state": "idle",
            "last_ocr_ms": 0,
        }
        self._friends: list[Friend] = [
            Friend(name=name, wxid=wxid, letter=letter)
            for name, wxid, letter in _DEMO_FRIENDS
        ]
        self._assets: dict[str, str] = {}
        self._assets_ready = False

    # ------------------------------------------------------------------
    # Backend 协议实现
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动假后端后台线程（立即返回，不阻塞 GUI）。"""
        with self._lock:
            if self._thread is not None and self._thread.is_alive() and not self._stop.is_set():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="wxbox-dev-backend", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """请求停止后台线程（不 join，避免阻塞 GUI 线程）。"""
        self._stop.set()
        with self._lock:
            alive = self._thread is not None and self._thread.is_alive()
            self._state["running"] = False
            self._state["monitor_state"] = "idle"
        self._emit_status()
        if not alive:
            self._log("info", "假后端已停止")

    def get_state(self) -> dict:
        """返回线程安全的状态快照（含 SPEC 第3节字段）。"""
        with self._lock:
            snapshot = dict(self._state)
            snapshot["monitoring"] = list(self._state.get("monitoring") or [])
            snapshot["running"] = bool(
                self._thread is not None and self._thread.is_alive() and not self._stop.is_set()
            )
            return snapshot

    def list_friends(self, refresh: bool = False) -> list[Friend]:
        """返回好友列表；refresh=True 时重新推送一次 friends 事件。"""
        with self._lock:
            friends = [Friend(name=f.name, wxid=f.wxid, letter=f.letter) for f in self._friends]
        if refresh:
            self._emit_friends()
            self._log("info", f"假后端已重新同步好友：{len(friends)} 位")
        return friends

    def set_monitored(self, names: list[str]) -> None:
        """设置监听好友名单；空列表 = 停止监听（线程继续运行）。"""
        clean = [str(name) for name in (names or []) if str(name).strip()]
        with self._lock:
            self._state["monitoring"] = clean
            self._state["monitor_state"] = "monitoring" if clean else "idle"
        self._emit_status()
        if clean:
            self._log("info", f"假后端开始监听：{'、'.join(clean)}")
        else:
            self._log("info", "假后端取消监听（无监听好友）")

    def is_running(self) -> bool:
        """返回后台线程是否在运行。"""
        return bool(self._thread is not None and self._thread.is_alive() and not self._stop.is_set())

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------
    def _run(self) -> None:
        """后台循环：准备素材 -> 发初始事件 -> 每 interval 秒产生一条消息。"""
        try:
            self._ensure_assets()
        except Exception as exc:  # noqa: BLE001 - 演示素材失败不影响消息流
            self._log("warn", f"演示媒体素材生成失败：{exc}")
        self._emit_friends()
        with self._lock:
            self._state["running"] = True
            self._state["wechat_found"] = True
            self._state["monitor_state"] = "idle"
            self._state["error"] = None
        self._emit_status()
        self._log(
            "info",
            f"假后端（演示模式）已启动：每 {self.interval:g} 秒生成一条示例消息，不连接真实微信。",
        )
        while not self._stop.is_set():
            if self._stop.wait(self.interval):
                break
            try:
                self._produce_message()
            except Exception as exc:  # noqa: BLE001
                self._log("error", f"假后端生成消息失败：{exc}")
            self._emit_status()
        with self._lock:
            self._state["running"] = False
            self._state["monitor_state"] = "idle"
        self._emit_status()
        self._log("info", "假后端后台线程已退出")

    def _produce_message(self) -> None:
        """按固定顺序生成一条不同类型的示例消息并推送。"""
        self._tick += 1
        sequence = (KIND_TEXT, KIND_IMAGE, KIND_EMOJI, KIND_FILE, KIND_VOICE, "self_text")
        kind_key = sequence[(self._tick - 1) % len(sequence)]
        friend = self._pick_friend()
        message = self._build_message(kind_key, friend)
        self.events.put(event_message(message))
        with self._lock:
            self._state["chat_title"] = friend
            self._state["last_ocr_ms"] = self._rng.randint(90, 420)
        self._log("info", f"假后端：收到「{friend}」的一条{message.kind_label}消息")

        if self._tick % 10 == 0:
            self._log("warn", "演示：模拟一次 OCR 置信度偏低的提示（仅演示，不影响真实采集）。")

    def _pick_friend(self) -> str:
        """轮流从监听名单（或演示名单）里挑一个好友名。"""
        with self._lock:
            monitored = list(self._state.get("monitoring") or [])
        pool = monitored if monitored else [f.name for f in self._friends]
        if not pool:
            return "演示好友"
        name = pool[self._friend_index % len(pool)]
        self._friend_index += 1
        return name

    def _build_message(self, kind_key: str, friend: str) -> Message:
        """根据类型构造一条 Message（图片/GIF/文件引用已生成的演示素材）。"""
        now = time.time()
        if kind_key == "self_text":
            return Message(
                friend=friend,
                kind=KIND_TEXT,
                sender=SENDER_SELF,
                text=self._rng.choice(_TEXT_SELF),
                ts=now,
            )
        if kind_key == KIND_TEXT:
            return Message(
                friend=friend,
                kind=KIND_TEXT,
                sender=SENDER_FRIEND,
                text=_TEXT_FRIEND[(self._tick - 1) % len(_TEXT_FRIEND)],
                ts=now,
            )
        if kind_key == KIND_IMAGE:
            return Message(
                friend=friend,
                kind=KIND_IMAGE,
                sender=SENDER_FRIEND,
                text="",
                media_path=self._assets.get("image", ""),
                extra={"size_text": "216.0 KB"},
                ts=now,
            )
        if kind_key == KIND_EMOJI:
            return Message(
                friend=friend,
                kind=KIND_EMOJI,
                sender=SENDER_FRIEND,
                text="",
                media_path=self._assets.get("emoji", ""),
                extra={"animated": True, "n_frames": 10, "size_text": "48.6 KB"},
                ts=now,
            )
        if kind_key == KIND_FILE:
            return Message(
                friend=friend,
                kind=KIND_FILE,
                sender=SENDER_FRIEND,
                text="项目需求文档-v3.pdf",
                media_path=self._assets.get("pdf", ""),
                thumb_path=self._assets.get("pdf_thumb", ""),
                extra={"size_text": "1.8 MB"},
                ts=now,
            )
        return Message(
            friend=friend,
            kind=KIND_VOICE,
            sender=SENDER_FRIEND,
            text="",
            extra={"duration": self._rng.choice([3, 5, 8, 12])},
            ts=now,
        )

    # ------------------------------------------------------------------
    # 事件推送
    # ------------------------------------------------------------------
    def _emit_status(self) -> None:
        """推送一条 status 事件（形状严格按 SPEC 第3节）。"""
        with self._lock:
            data = dict(self._state)
            data["monitoring"] = list(self._state.get("monitoring") or [])
            data["tick"] = self._tick
            data["running"] = bool(
                self._thread is not None and self._thread.is_alive() and not self._stop.is_set()
            )
        self.events.put(event_status(**data))

    def _emit_friends(self) -> None:
        """推送一条 friends 事件。"""
        with self._lock:
            friends = [Friend(name=f.name, wxid=f.wxid, letter=f.letter) for f in self._friends]
        self.events.put(event_friends(len(friends), friends))

    def _log(self, level: str, text: str) -> None:
        """推送一条 log 事件。"""
        self.events.put(event_log(text, level))

    # ------------------------------------------------------------------
    # 演示媒体素材（全部生成到 media/，均为本项目自己写的文件）
    # ------------------------------------------------------------------
    def _ensure_assets(self) -> None:
        """生成演示用的 PNG / GIF / PDF / 缩略图素材（已存在则跳过）。"""
        if self._assets_ready:
            return
        MEDIA_DIR.mkdir(parents=True, exist_ok=True)
        image_path = MEDIA_DIR / "demo_image.png"
        emoji_path = MEDIA_DIR / "demo_emoji.gif"
        pdf_path = MEDIA_DIR / "demo_document.pdf"
        pdf_thumb = MEDIA_DIR / "demo_pdf_thumb.png"

        if not image_path.is_file():
            self._make_demo_image(image_path)
        if not emoji_path.is_file():
            self._make_demo_gif(emoji_path)
        if not pdf_thumb.is_file():
            self._make_demo_pdf_thumb(pdf_thumb)
        if not pdf_path.is_file():
            pdf_path.write_bytes(self._make_demo_pdf_bytes())

        self._assets = {
            "image": image_path.relative_to(ROOT).as_posix(),
            "emoji": emoji_path.relative_to(ROOT).as_posix(),
            "pdf": pdf_path.relative_to(ROOT).as_posix(),
            "pdf_thumb": pdf_thumb.relative_to(ROOT).as_posix(),
        }
        self._assets_ready = True

    @staticmethod
    def _load_font(size: int):
        """尝试加载微软雅黑；失败时回退 Pillow 默认字体。"""
        try:
            from PIL import ImageFont

            for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
                candidate = Path("C:/Windows/Fonts") / name
                if candidate.is_file():
                    return ImageFont.truetype(str(candidate), size)
        except Exception:  # noqa: BLE001
            pass
        from PIL import ImageFont

        return ImageFont.load_default()

    def _make_demo_image(self, path: Path) -> None:
        """生成一张渐变 + 文字的演示 PNG 图片。"""
        from PIL import Image, ImageDraw

        width, height = 480, 320
        image = Image.new("RGB", (width, height), "#1e1f22")
        draw = ImageDraw.Draw(image)
        for y in range(height):
            ratio = y / max(1, height - 1)
            r = int(30 + 40 * ratio)
            g = int(55 + 120 * ratio)
            b = int(70 + 60 * ratio)
            draw.line([(0, y), (width, y)], fill=(r, g, b))
        draw.rounded_rectangle((36, 36, width - 36, height - 36), radius=18, outline="#3eb575", width=2)
        font = self._load_font(34)
        small = self._load_font(20)
        draw.text((width // 2, height // 2 - 24), "示例图片", font=font, fill="#eafff3", anchor="mm")
        draw.text(
            (width // 2, height // 2 + 28),
            "WXBOX DEMO · PNG",
            font=small,
            fill="#bfe8cf",
            anchor="mm",
        )
        image.save(path, "PNG")

    def _make_demo_gif(self, path: Path) -> None:
        """生成一个 10 帧循环播放的演示 GIF 动图表情包。"""
        from PIL import Image, ImageDraw

        size = 160
        frames = []
        font = self._load_font(22)
        for index in range(10):
            image = Image.new("RGBA", (size, size), (30, 31, 34, 255))
            draw = ImageDraw.Draw(image)
            # 来回弹跳的小球
            phase = index / 10.0
            bounce = abs(1.0 - 2.0 * phase)
            top = int(20 + 70 * bounce)
            draw.ellipse((20, top, size - 20, top + 60), fill=(62, 181, 117, 255))
            eye_y = top + 24
            draw.ellipse((58, eye_y, 68, eye_y + 10), fill=(13, 26, 19, 255))
            draw.ellipse((92, eye_y, 102, eye_y + 10), fill=(13, 26, 19, 255))
            draw.text((size // 2, size - 28), "GIF", font=font, fill="#ffcb6b", anchor="mm")
            frames.append(image.convert("P", palette=Image.Palette.ADAPTIVE))
        frames[0].save(
            path,
            "GIF",
            save_all=True,
            append_images=frames[1:],
            duration=120,
            loop=0,
            disposal=2,
        )

    def _make_demo_pdf_thumb(self, path: Path) -> None:
        """生成 PDF 文件的 96×96 缩略图（纸页 + 折角）。"""
        from PIL import Image, ImageDraw

        size = 96
        image = Image.new("RGBA", (size, size), (30, 31, 34, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((18, 10, 78, 86), radius=6, fill="#f4f4f4")
        draw.polygon([(60, 10), (78, 28), (60, 28)], fill="#c9ccd2")
        draw.rectangle((28, 40, 68, 44), fill="#d23c3c")
        draw.rectangle((28, 52, 66, 55), fill="#9aa0a8")
        draw.rectangle((28, 62, 62, 65), fill="#9aa0a8")
        image.save(path, "PNG")

    @staticmethod
    def _make_demo_pdf_bytes() -> bytes:
        """构造一个最小可用的 PDF 文件内容（演示文件卡片用）。"""
        text = "WXBOX demo document"
        content = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET"
        objects = [
            "<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        ]
        body = "%PDF-1.4\n"
        offsets = []
        for index, obj in enumerate(objects, start=1):
            offsets.append(len(body.encode("latin-1")))
            body += f"{index} 0 obj\n{obj}\nendobj\n"
        xref_pos = len(body.encode("latin-1"))
        body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
        for offset in offsets:
            body += f"{offset:010d} 00000 n \n"
        body += (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        )
        return body.encode("latin-1")
