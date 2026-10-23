"""微信消息文本清洗（网页端展示用）。

微信 4.x 的部分消息（红包/转账/系统提示/应用消息）在数据库里存的是 XML 或 HTML 片段，
直接展示会把标签原样显示出来。这里统一做“人话化”：
* 系统提示里的 ``<img>`` / ``<wc_custom_link>`` / ``<a>`` 等标签 → 纯文本；
* 应用消息 ``<appmsg><type>``：红包/转账/链接/小程序/引用等 → 可读标题 + ``extra.app_type``；
* 无法识别的 XML → ``[消息]``，绝不把 `<...>` 原样丢给前端。
"""
from __future__ import annotations

import hashlib
import html
import re

_IMG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>", re.S)
_SPACE_RE = re.compile(r"\s+")
_APP_TYPE_RE = re.compile(r"<type>\s*(?:<!\[CDATA\[)?\s*(\d+)\s*(?:\]\]>)?\s*</type>", re.I | re.S)
_TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.I | re.S)
_DES_RE = re.compile(r"<des>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</des>", re.I | re.S)
# 语音通话（local_type=50，<voipmsg> XML）
_VOIP_MSG_RE = re.compile(r"<msg>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</msg>", re.I | re.S)
_VOIP_DURATION_RE = re.compile(r"<duration>\s*(\d+)\s*</duration>", re.I)
# 视频（local_type=43，<videomsg> XML）
_VIDEO_PLAY_RE = re.compile(r'playlength="(\d+)"', re.I)
_VIDEO_MD5_RE = re.compile(r'(?:md5|newmd5|originsourcemd5)="([0-9a-fA-F]{16,64})"', re.I)

# appmsg type → (kind 标记, 默认文案)
APP_TYPES: dict[int, tuple[str, str]] = {
    2000: ("transfer", "[微信转账]"),
    2001: ("hongbao", "🧧 微信红包"),
    2002: ("hongbao", "🧧 微信红包"),
    2003: ("hongbao", "🧧 微信红包"),
    2004: ("transfer", "[微信转账]"),
    5: ("link", "[链接]"),
    33: ("mini", "[小程序]"),
    36: ("mini", "[小程序]"),
    57: ("quote", "[引用消息]"),
    3: ("music", "[音乐]"),
}


def _strip_html(text: str) -> str:
    """把微信系统提示里的 HTML 片段转成人话。"""
    s = _IMG_RE.sub("", str(text or ""))
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = s.replace("\u2005", " ").replace("\xa0", " ")
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def db_message_id(friend: str, local_id) -> str:
    """跨来源统一的 DB 消息 id：实时监听与“同步历史”拿到的同一条消息使用同一个 id。"""
    try:
        lid = int(local_id)
    except (TypeError, ValueError):
        lid = 0
    digest = hashlib.sha1(f"{friend}|{lid}".encode("utf-8", "replace")).hexdigest()
    return "db_" + digest[:16]


def normalize_message(kind: str, text: str, extra: dict | None = None) -> tuple[str, str, dict]:
    """返回清洗后的 ``(kind, text, extra)``；不修改入参 dict。"""
    extra = dict(extra or {})
    raw = str(text or "").strip()
    if not raw:
        return kind, text, extra

    # 核心已识别的“视频”占位（实时消息只有 "[视频]" 文本，没有 XML）
    if kind in ("unknown", "") and raw in ("[视频]", "视频"):
        extra["app_type"] = "video"
        return "video", "视频", extra

    # 0) 微信视频（local_type=43，<videomsg> XML；原始 XML 不能直接展示）
    if "<videomsg" in raw.lower():
        m_play = _VIDEO_PLAY_RE.search(raw)
        duration = 0
        try:
            duration = int(m_play.group(1)) if m_play else 0
        except (TypeError, ValueError):
            duration = 0
        extra["app_type"] = "video"
        if duration:
            extra["duration"] = duration
        md5s = [m.lower() for m in _VIDEO_MD5_RE.findall(raw)]
        if md5s:
            extra["video_md5"] = md5s[0]
        return "video", (f"视频 {duration}秒" if duration else "视频"), extra

    # 0) 微信语音通话（local_type=50，<voipmsg> XML，不能把里面的数字当正文）
    if "<voipmsg" in raw.lower() or "<voipbubblemsg" in raw.lower():
        m_msg = _VOIP_MSG_RE.search(raw)
        m_dur = _VOIP_DURATION_RE.search(raw)
        msg_text = _strip_html(m_msg.group(1)) if m_msg else ""
        duration = 0
        try:
            duration = int(m_dur.group(1)) if m_dur else 0
        except (TypeError, ValueError):
            duration = 0
        if not msg_text:
            msg_text = f"通话时长 {duration}秒" if duration > 0 else "语音通话"
        extra["app_type"] = "voicecall"
        if duration:
            extra["duration"] = duration
        return "system", "📞 " + msg_text, extra

    # 1) 应用消息：从 XML 里抽类型/标题
    if "<appmsg" in raw or "<wcpayinfo" in raw:
        m_type = _APP_TYPE_RE.search(raw)
        try:
            app_type = int(m_type.group(1)) if m_type else 0
        except (TypeError, ValueError):
            app_type = 0
        title = _TITLE_RE.search(raw)
        des = _DES_RE.search(raw)
        title_text = _strip_html(title.group(1)) if title else ""
        des_text = _strip_html(des.group(1)) if des else ""
        mapped = APP_TYPES.get(app_type)
        if mapped:
            app_key, default_text = mapped
            extra["app_type"] = app_key
            extra["app_type_code"] = app_type
            if app_key in ("hongbao", "transfer"):
                # 红包/转账只展示标题/祝福语，不展示支付 XML
                msg = title_text or des_text or default_text
                if app_key == "hongbao" and "红包" not in msg:
                    msg = "🧧 " + msg
                elif app_key == "transfer" and "转账" not in msg:
                    msg = "[微信转账] " + msg
                return "system", msg, extra
            return "system", (title_text and f"{default_text} {title_text}") or default_text, extra
        # 其它 appmsg：尽量只留标题
        extra["app_type"] = "app"
        extra["app_type_code"] = app_type
        return "system", title_text or des_text or "[应用消息]", extra

    # 2) 系统提示里的 HTML（领取红包、转账提醒、拍一拍等）
    #    只对系统/未知消息清洗，普通文字消息里的 "<...>" 原样保留。
    if kind in ("system", "unknown") and "<" in raw and ">" in raw:
        cleaned = _strip_html(raw)
        if cleaned:
            lower = raw.lower()
            if "hongbao" in lower or "红包" in cleaned:
                extra["app_type"] = "hongbao_notice"
            return kind, cleaned, extra
        return kind, "[消息]", extra

    return kind, text, extra
