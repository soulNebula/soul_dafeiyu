"""数据模型（Lead 维护，其他人只读）。

所有跨模块传递的数据都必须是本文件里的 dataclass 或普通 dict，
方便 GUI / 日志 / 测试互相对接。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field

# ---- 消息类型 ----
KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_EMOJI = "emoji"
KIND_FILE = "file"
KIND_SYSTEM = "system"
KIND_UNKNOWN = "unknown"
KIND_VOICE = "voice"  # 语音：只提示、不接收
KIND_REDPACKET = "redpacket"  # 微信红包
KIND_TRANSFER = "transfer"    # 微信转账
KIND_CALL = "call"            # 语音/视频通话记录

KINDS = (KIND_TEXT, KIND_IMAGE, KIND_EMOJI, KIND_FILE, KIND_SYSTEM, KIND_UNKNOWN,
         KIND_VOICE, KIND_REDPACKET, KIND_TRANSFER, KIND_CALL)

KIND_LABEL = {
    KIND_TEXT: "文字",
    KIND_IMAGE: "图片",
    KIND_EMOJI: "表情包",
    KIND_FILE: "文件",
    KIND_SYSTEM: "系统",
    KIND_UNKNOWN: "未知",
    KIND_VOICE: "语音",
    KIND_REDPACKET: "红包",
    KIND_TRANSFER: "转账",
    KIND_CALL: "通话",
}

SENDER_FRIEND = "friend"
SENDER_SELF = "self"

SENDER_LABEL = {SENDER_FRIEND: "对方", SENDER_SELF: "我"}


def stable_id(*parts: object) -> str:
    """由若干字段生成稳定短哈希，用于消息去重。"""
    h = hashlib.sha1()
    for p in parts:
        h.update(repr(p).encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()[:16]


@dataclass
class Friend:
    """一个微信好友（以会话显示名为准）。"""

    name: str
    wxid: str = ""
    letter: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Friend":
        return Friend(name=str(d.get("name", "")), wxid=str(d.get("wxid", "")), letter=str(d.get("letter", "")))


@dataclass
class Message:
    """一条被监听会话里的消息。"""

    friend: str
    kind: str
    sender: str = SENDER_FRIEND
    text: str = ""
    media_path: str = ""  # 相对项目根目录的媒体路径（图片/表情包/GIF/文件缩略图）
    thumb_path: str = ""
    extra: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            self.kind = KIND_UNKNOWN
        if not self.id:
            self.id = stable_id(self.friend, self.sender, self.kind, self.text,
                                self.extra.get("visual_hash", ""), int(self.ts))

    # ---- 展示辅助 ----
    @property
    def kind_label(self) -> str:
        return KIND_LABEL.get(self.kind, self.kind)

    @property
    def sender_label(self) -> str:
        return SENDER_LABEL.get(self.sender, self.sender)

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.ts))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["kind_label"] = self.kind_label
        d["sender_label"] = self.sender_label
        d["time_text"] = self.time_text
        return d

    @staticmethod
    def from_dict(d: dict) -> "Message":
        return Message(
            friend=d.get("friend", ""),
            kind=d.get("kind", KIND_UNKNOWN),
            sender=d.get("sender", SENDER_FRIEND),
            text=d.get("text", ""),
            media_path=d.get("media_path", ""),
            thumb_path=d.get("thumb_path", ""),
            extra=dict(d.get("extra") or {}),
            ts=float(d.get("ts") or 0.0),
            id=d.get("id", ""),
        )


@dataclass
class OcrLine:
    """一行 OCR 结果（图像坐标，原点左上）。"""

    text: str
    box: tuple[int, int, int, int]  # (left, top, right, bottom)
    words: list = field(default_factory=list)  # [{"text":str,"box":(l,t,r,b),...}]

    def to_dict(self) -> dict:
        return {"text": self.text, "box": list(self.box), "words": self.words}

    @staticmethod
    def from_dict(d: dict) -> "OcrLine":
        box = tuple(int(v) for v in d.get("box", (0, 0, 0, 0)))
        return OcrLine(text=str(d.get("text", "")), box=box, words=list(d.get("words") or []))


# ---- 事件构造器（backend -> GUI） ----
def event_status(**data) -> dict:
    base = {"running": False, "wechat_found": False, "chat_title": "", "monitoring": [], "error": None, "tick": 0}
    base.update(data)
    return {"type": "status", "data": base}


def event_message(msg: Message) -> dict:
    return {"type": "message", "data": msg.to_dict()}


def event_friends(total: int, friends: list[Friend]) -> dict:
    return {"type": "friends", "data": {"total": total, "friends": [f.to_dict() for f in friends]}}


def event_log(text: str, level: str = "info") -> dict:
    return {"type": "log", "data": {"level": level, "text": text}}
