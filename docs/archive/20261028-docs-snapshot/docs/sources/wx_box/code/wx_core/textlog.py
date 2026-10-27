"""纯文本聊天记录 + Emoji 中文名规范化。

输出文件：``log/聊天记录.txt``（UTF-8），每行一条，格式与用户要求一致::

    好友名字: 测试语句1
    我: 测试语句1

Emoji 一律转成微信中文名，例如 ``😊`` → ``[微笑]``。
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from . import models

ROOT = Path(__file__).resolve().parent.parent
CHAT_LOG = ROOT / "log" / "聊天记录.txt"

_lock = threading.Lock()

# 常见 Unicode Emoji → 微信中文表情名（按需扩充；找不到的保留原字符）
EMOJI_NAMES = {
    "😊": "微笑", "😄": "呲牙", "😁": "呲牙", "😆": "呲牙", "😃": "微笑",
    "😀": "微笑", "🙂": "微笑", "😉": "眨眼", "😍": "色", "😘": "飞吻",
    "😜": "调皮", "😝": "吐舌", "😋": "馋", "😎": "得意", "😏": "坏笑", "😒": "难过",
    "😞": "难过", "😔": "难过", "😟": "难过", "😕": "困惑", "🙁": "难过", "☹️": "难过",
    "😣": "难过", "😖": "难过", "😫": "抓狂", "😩": "抓狂", "😤": "发怒", "😠": "发怒",
    "😡": "发怒", "😶": "无语", "😐": "无语", "😑": "无语", "😯": "惊讶", "😮": "惊讶",
    "😲": "惊讶", "😱": "惊恐", "😨": "惊恐", "😰": "流汗", "😥": "流汗", "😢": "流泪",
    "😭": "大哭", "😂": "笑哭", "🤣": "笑哭", "😳": "脸红", "😵": "晕", "😴": "睡",
    "🤔": "思考", "🤗": "拥抱", "🤭": "捂嘴", "🤫": "嘘", "😬": "尴尬", "🙄": "白眼",
    "😪": "困", "🤤": "流口水", "😷": "口罩", "🤒": "生病", "🤕": "受伤", "🤢": "恶心",
    "🤮": "呕吐", "🥵": "热", "🥶": "冷", "😈": "恶魔", "👿": "恶魔", "💀": "骷髅",
    "☠️": "骷髅", "💩": "便便", "🤡": "小丑", "👻": "幽灵", "👽": "外星人",
    "👍": "强", "👎": "弱", "👌": "OK", "✌️": "胜利", "🤞": "祈祷", "🤝": "握手",
    "🙏": "合十", "👏": "鼓掌", "🙌": "举手", "🤲": "拥吻", "💪": "加油", "👊": "拳头",
    "✊": "拳头", "🤛": "拳头", "🤜": "拳头", "👋": "挥手", "🖐️": "手掌", "✋": "举手",
    "👌": "OK", "🤙": "call", "👈": "左", "👉": "右", "👆": "上", "👇": "下",
    "❤️": "爱心", "🧡": "爱心", "💛": "爱心", "💚": "爱心", "💙": "爱心", "💜": "爱心",
    "🖤": "爱心", "🤍": "爱心", "🤎": "爱心", "💔": "心碎", "💕": "喜欢", "💞": "喜欢",
    "💓": "喜欢", "💗": "喜欢", "💖": "喜欢", "💘": "喜欢", "💝": "喜欢", "🌹": "玫瑰",
    "🌸": "樱花", "🌺": "花", "🌻": "向日葵", "🌷": "郁金香", "🍀": "四叶草",
    "🎉": "庆祝", "🎊": "庆祝", "🎁": "礼物", "🎂": "蛋糕", "🍰": "蛋糕", "☕": "咖啡",
    "🍺": "啤酒", "🍻": "干杯", "🍷": "红酒", "🍎": "苹果", "🍉": "西瓜", "🍓": "草莓",
    "🍔": "汉堡", "🍟": "薯条", "🍚": "饭", "🍜": "面", "🐶": "狗", "🐱": "猫",
    "🐭": "鼠", "🐷": "猪", "🐔": "鸡", "🐟": "鱼", "🌙": "月亮", "☀️": "太阳",
    "⭐": "星星", "🌟": "星星", "🔥": "火", "💧": "水滴", "⚡": "闪电", "❄️": "雪花",
    "💰": "钱", "💴": "钱", "🧧": "红包", "📱": "手机", "💻": "电脑", "🚗": "车",
    "✈️": "飞机", "🏠": "房子", "⏰": "闹钟", "💤": "睡", "❓": "问号", "❗": "感叹号",
    "✅": "对勾", "❌": "叉", "🎵": "音乐", "🎶": "音乐",
}

# Unicode emoji 区间（粗略覆盖，避免误伤中文/数字）
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE0F\U0000200D]+"
)


def emoji_to_wechat_name(ch: str) -> str:
    """单个 emoji 转微信中文名；未知返回空串。"""
    if ch in EMOJI_NAMES:
        return EMOJI_NAMES[ch]
    # 去掉变体选择符后再试
    core = ch.replace("\ufe0f", "")
    return EMOJI_NAMES.get(core, "")


def normalize_emoji(text: str) -> str:
    """把文本中的 Unicode emoji 转成 ``[中文名]``；已经是 [微笑] 的保持不变。"""
    if not text:
        return ""
    out: list[str] = []
    last = 0
    for m in _EMOJI_RE.finditer(text):
        out.append(text[last:m.start()])
        seq = m.group()
        names = []
        i = 0
        while i < len(seq):
            # 优先两字符（含 VS16 / ZWJ 组合）
            two = seq[i:i + 2]
            name = emoji_to_wechat_name(two) if len(two) == 2 else ""
            if not name:
                name = emoji_to_wechat_name(seq[i])
            if name:
                names.append(f"[{name}]")
            else:
                names.append(seq[i])
            i += 2 if (len(two) == 2 and emoji_to_wechat_name(two)) else 1
        out.append("".join(names))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def sender_label(msg) -> str:
    """聊天记录里的说话人：好友名 / 我。"""
    if msg.sender == models.SENDER_SELF:
        return "我"
    return msg.friend or "对方"


def line_for(msg) -> str:
    """一条消息 → 纯文本一行。"""
    label = sender_label(msg)
    text = normalize_emoji(msg.text or "")
    if msg.kind == models.KIND_TEXT:
        return f"{label}: {text}" if text else f"{label}: "
    if msg.kind == models.KIND_FILE:
        name = msg.text or (Path(msg.media_path).name if msg.media_path else "")
        return f"{label}: [文件] {name}".rstrip()
    if msg.kind == models.KIND_IMAGE:
        return f"{label}: [图片] {Path(msg.media_path).name if msg.media_path else ''}".rstrip()
    if msg.kind == models.KIND_EMOJI:
        return f"{label}: [表情包] {Path(msg.media_path).name if msg.media_path else ''}".rstrip()
    if msg.kind == models.KIND_VOICE:
        dur = msg.extra.get("duration", "")
        return f"{label}: [语音] {dur}".rstrip()
    return f"{label}: {text}".rstrip()


def append(msg) -> None:
    """把一条消息追加到 log/聊天记录.txt。"""
    line = line_for(msg)
    if not line:
        return
    with _lock:
        try:
            CHAT_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(CHAT_LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def append_friend_header(friend: str) -> None:
    """保留接口：严格逐行格式下不写分隔头（文件每行都是 `名字: 内容`）。"""
    return
