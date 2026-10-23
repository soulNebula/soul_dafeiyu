"""历史聊天记录加载（只读、临时、不落盘）。

网页端新需求：同步**所选好友**的历史聊天记录，可选最近 5/10/20/50/100 条或全部
（= 本机微信缓存到的全部）。数据只回传给网页进程内存，不进 ``log/``、不写任何文件；
``wx_core.wcdb.SessionReader`` 解密的临时库副本在每次调用结束后立即清理。

数据来源与实时监听完全一致：只读当前登录账号的 WCDB ``message_*.db``，
不发送消息、不读微信进程内存以外的内容（密钥提取为项目原有逻辑）。
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

from webui.pinyin import pinyin_initial
from webui.wechat_text import db_message_id, normalize_message

from wx_core import config as cfg_mod
from wx_core import media_cache, wcdb
from wx_core.models import KIND_EMOJI, KIND_FILE, KIND_IMAGE, KIND_SYSTEM, KIND_TEXT, KIND_UNKNOWN, KIND_VOICE
from wx_core.models import SENDER_FRIEND, SENDER_SELF, Message

try:  # Agent A 新增的红包/转账类型（旧版核心没有时用字符串兜底）
    from wx_core.models import KIND_REDPACKET, KIND_TRANSFER
except ImportError:  # pragma: no cover - 兼容旧核心
    KIND_REDPACKET, KIND_TRANSFER = "redpacket", "transfer"

ALL_LIMITS = (0, 5, 10, 20, 50, 100)
ALL_MESSAGES_LIMIT = 100000

# 微信特有的“虚拟联系人”显示名 → username（contact.db 里查不到）
_BUILTIN_USERS = {
    "文件传输助手": "filehelper",
    "微信团队": "weixin",
}

_TITLE_RE = re.compile(r"<title>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", re.I | re.S)
_TYPE_RE = re.compile(r"<type>\s*(?:<!\[CDATA\[)?\s*(\d+)\s*(?:\]\]>)?\s*</type>", re.I | re.S)
_TOTAL_LEN_RE = re.compile(r'totallen="(\d+)"')
_VOICE_MS_RE = re.compile(r'voicelength="(\d+)"')
_MD5_RE = re.compile(r'md5="([0-9a-fA-F]+)"')
# 群聊消息的 message_content 常带 "wxid_xxx:\n正文" / "群昵称/别名:\n正文" 的发送人前缀
_GROUP_PREFIX_RE = re.compile(r"^(?:wxid_[A-Za-z0-9]+|filehelper|weixin):\s*", re.I)
_GROUP_ALIAS_PREFIX_RE = re.compile(r"^[^\s:]{1,64}:\n\s*")


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    try:
        return bytes(content).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _fmt_seconds(ms: int) -> str:
    try:
        sec = int((int(ms) + 999) // 1000)
    except Exception:  # noqa: BLE001
        sec = 0
    return f"{max(1, sec)}秒"


def _row_to_message(friend: str, row: dict, *, is_group: bool = False,
                    sender_names: dict[str, str] | None = None) -> Message | None:
    """把数据库行快速转成 Message（历史回放不做图片/表情包缓存解密，保持速度）。

    群聊时额外写入 ``extra["is_group"]=True`` 与 ``extra["sender_name"]``（群成员显示名），
    前端据此在气泡上方显示发送人。
    """
    try:
        local_id = int(row.get("local_id") or 0)
        ltype = int(row.get("local_type") or 0) & 0xFFFFFFFF
        content = row.get("content")
        text = _text_of(content)
        sender = SENDER_SELF if row.get("self") else SENDER_FRIEND
        ts = float(row.get("create_time") or time.time())
    except Exception:  # noqa: BLE001
        return None

    extra: dict = {"db_local_id": local_id, "history": True}
    if is_group:
        extra["is_group"] = True
        sender_username = str(row.get("sender") or "").strip()
        if sender_username and not row.get("self"):
            sender_name = (sender_names or {}).get(sender_username) or sender_username
            extra["sender_name"] = sender_name
            extra["sender_initial"] = pinyin_initial(sender_name)
            extra["sender_key"] = sender_username
    kind = KIND_UNKNOWN
    msg_text = ""

    if ltype in (1, 10000, 10002) or (
        isinstance(content, str) and ltype < 40 and ltype not in (3, 34, 43, 47, 49)
    ):
        kind = KIND_SYSTEM if ltype in (10000, 10002) else KIND_TEXT
        msg_text = text.strip()
    elif ltype == 3:
        kind = KIND_IMAGE
    elif ltype == 47:
        kind = KIND_EMOJI
        md5 = _MD5_RE.search(text)
        if md5:
            extra["md5"] = md5.group(1).lower()
    elif ltype == 43:
        # 小视频：本机历史里只内联展示时长；播放走 /api/video 读取微信 msg/video 缓存
        kind = KIND_UNKNOWN
        m = re.search(r'playlength="(\d+)"', text)
        duration = 0
        if m:
            duration = max(1, int(m.group(1)))
            extra["duration"] = duration
        extra["app_type"] = "video"
        for md5 in re.findall(r'(?:md5|newmd5|originsourcemd5)="([0-9a-fA-F]{16,64})"', text):
            if md5 and md5 != "0" * len(md5):
                extra["video_md5"] = md5.lower()
                break
        msg_text = f"视频 {duration}秒" if duration else "视频"
    elif ltype == 42:
        kind = KIND_UNKNOWN
        msg_text = "[名片]"
    elif ltype == 48:
        kind = KIND_UNKNOWN
        msg_text = "[位置]"
    elif ltype == 34:
        kind = KIND_VOICE
        m = _VOICE_MS_RE.search(text)
        if m:
            sec = int(m.group(1))
            extra["duration_ms"] = sec
            extra["duration"] = max(1, (sec + 999) // 1000)
            msg_text = f"[语音] {_fmt_seconds(sec)}"
        else:
            msg_text = "[语音]"
    elif ltype == 49:
        m_type = _TYPE_RE.search(text)
        sub = m_type.group(1) if m_type else ""
        title = _TITLE_RE.search(text)
        title_text = (title.group(1).strip() if title else "")
        if sub == "6":
            kind = KIND_FILE
            msg_text = title_text or "文件"
            extra["original_name"] = msg_text
            m_len = _TOTAL_LEN_RE.search(text)
            if m_len:
                extra["size_bytes"] = int(m_len.group(1))
            extra["size_text"] = _human_size(extra.get("size_bytes"))
        elif sub in ("3", "4", "5"):
            kind = KIND_SYSTEM
            msg_text = title_text or "[链接/音乐]"
        else:
            kind = KIND_SYSTEM
            msg_text = title_text or "[小程序/链接]"
    else:
        kind = KIND_UNKNOWN
        msg_text = text.strip()
        # 未知类型不要展示原始 XML（视频号/小程序等可能很长），统一占位
        if msg_text.lstrip().startswith("<"):
            msg_text = "[消息]"

    # 群聊消息的 message_content 常带 "wxid_xxx:" / "别名:\n" 的发送人前缀
    if is_group and msg_text:
        msg_text = _GROUP_PREFIX_RE.sub("", msg_text)
        msg_text = _GROUP_ALIAS_PREFIX_RE.sub("", msg_text).strip()

    # 系统提示/应用消息（红包、转账、拍一拍、HTML 提示）尽量转成人话
    if kind in (KIND_SYSTEM, KIND_UNKNOWN) and isinstance(text, str) and "<" in text:
        kind, msg_text, extra = normalize_message(kind, text, extra)
    # 微信红包/转账用独立类型（核心 Agent A 已支持；历史里根据 appmsg 类型映射）
    if extra.get("app_type") == "hongbao":
        kind = KIND_REDPACKET
    elif extra.get("app_type") == "transfer":
        kind = KIND_TRANSFER

    msg = Message(
        friend=friend,
        kind=kind,
        sender=sender,
        text=msg_text,
        media_path="",
        thumb_path="",
        extra=extra,
        ts=ts,
        id=db_message_id(friend, local_id),
    )
    # Message 的 __post_init__ 会把未知 kind 归一成 unknown；视频用独立标签展示
    if extra.get("app_type") == "video":
        msg.kind = "video"
    return msg


def _human_size(size: object) -> str:
    try:
        value = float(size)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


class HistoryService:
    """按好友读取本机历史聊天记录（只读、内存返回）。"""

    def __init__(self, config: dict | None = None, *, log: Callable[[str], None] | None = None):
        self._base_config = dict(config or {})
        self._log = log or (lambda *_a, **_k: None)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _account(self) -> tuple[object, dict, str]:
        """定位当前活跃账号目录、密钥、wxid。"""
        config = dict(self._base_config)
        try:
            config.update(cfg_mod.load())
        except Exception:  # noqa: BLE001
            pass
        from webui.account import detect_active_account

        acc, wxid = detect_active_account(config)
        if acc is None:
            raise RuntimeError("找不到微信数据目录，请确认微信电脑版已登录")
        if not wxid:
            configured = str(config.get("image_wxid") or "").strip()
            derived = acc.name.rsplit("_", 1)[0] if acc.name.count("_") >= 2 else acc.name
            wxid = derived or configured
        if not wxid:
            raise RuntimeError("无法确定当前登录账号")
        config["image_wxid"] = wxid
        keys = wcdb.ensure_keys(acc, config, log=self._log)
        if not keys:
            raise RuntimeError("数据库密钥不可用（请确认微信已登录并保持运行）")
        try:  # 缓存按账号的数据库密钥，避免每次刷新都重新扫内存
            cfg_mod.save(config)
        except Exception:  # noqa: BLE001
            pass
        return acc, keys, wxid

    # ------------------------------------------------------------------
    def load(self, friend: str, limit: int) -> dict:
        """读取好友最近 ``limit`` 条历史消息；``limit=0`` 表示全部。

        返回结构见 ``webui/CONTRACT.md`` V2-3。失败时返回
        ``{"ok": False, "error": "中文原因"}``（由 API 层转 503）。
        """
        friend = str(friend or "").strip()
        if not friend:
            return {"ok": False, "error": "请先选择一位好友", "code": "bad_request"}
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit not in ALL_LIMITS:
            return {"ok": False, "error": "同步条数只支持 5/10/20/50/100 或全部", "code": "bad_request"}

        with self._lock:
            reader = None
            try:
                acc, keys, wxid = self._account()
                reader = wcdb.SessionReader(acc, keys, wxid)
                username = self._resolve_username(reader, friend)
                if not username:
                    return {
                        "ok": False,
                        "error": f"联系人库里找不到「{friend}」（可能是群聊或未同步到本机）",
                        "code": "not_found",
                    }
                rows = reader.read_messages(username, 0, limit=ALL_MESSAGES_LIMIT)
                available = len(rows)
                selected = rows if limit == 0 else rows[-limit:]
                is_group = username.endswith("@chatroom")
                sender_names: dict[str, str] = {}
                if is_group:
                    wanted = {
                        str(row.get("sender") or "").strip()
                        for row in selected
                        if row.get("sender") and not row.get("self")
                    }
                    sender_names = _sender_display_names(reader, wanted)
                items = []
                seen = set()
                for row in selected:
                    msg = _row_to_message(friend, row, is_group=is_group,
                                          sender_names=sender_names)
                    if msg is None or msg.id in seen:
                        continue
                    seen.add(msg.id)
                    items.append(msg.to_dict())
                items.sort(key=lambda m: float(m.get("ts") or 0.0))
                loaded = len(items)
                notice = None
                if limit == 0:
                    if loaded == 0:
                        notice = f"「{friend}」在本机没有缓存到聊天记录"
                elif available < limit:
                    if available == 0:
                        notice = f"「{friend}」在本机没有缓存到聊天记录，无法同步最近 {limit} 条"
                    else:
                        notice = (
                            f"「{friend}」在本机只缓存了 {available} 条聊天记录，"
                            f"不足 {limit} 条，已同步全部 {available} 条"
                        )
                self._log(
                    f"[history] {friend}: requested={limit or 'all'} "
                    f"available={available} loaded={loaded}"
                )
                return {
                    "ok": True,
                    "friend": friend,
                    "requested": "all" if limit == 0 else limit,
                    "loaded": loaded,
                    "available": available,
                    "notice": notice,
                    "items": items,
                }
            except Exception as exc:  # noqa: BLE001 - 全部转成可读错误
                return {"ok": False, "error": f"读取历史聊天记录失败：{exc}", "code": "history_error"}
            finally:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:  # noqa: BLE001
                        pass

    # ------------------------------------------------------------------
    @staticmethod
    def _resolve_username(reader, friend: str) -> str:
        builtin = _BUILTIN_USERS.get(friend)
        if builtin:
            return builtin
        try:
            return reader.resolve_username(friend) or ""
        except Exception:  # noqa: BLE001
            return ""

    def close(self) -> None:
        """当前实现每次调用都自建/销毁 reader，无需额外清理。"""


def _sender_display_names(reader, usernames: set[str]) -> dict[str, str]:
    """批量把群成员 username 映射成显示名（备注优先，其次昵称）。只读 contact.db。"""
    if not usernames:
        return {}
    try:
        path = reader._materialize("db_storage/contact/contact.db", "_contact_path")
        if path is None:
            return {}
        import sqlite3

        placeholders = ",".join("?" for _ in usernames)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT username, COALESCE(NULLIF(remark, ''), nick_name) FROM contact "
                f"WHERE username IN ({placeholders})",
                tuple(usernames),
            ).fetchall()
        finally:
            con.close()
        return {str(u): str(n or u) for u, n in rows if u}
    except Exception:  # noqa: BLE001 - 成员名解析失败不影响消息
        return {}


def _pick_account_dir(default, wxid: str):
    """在多个账号目录中优先选与 wxid 匹配的那个。"""
    try:
        for d in media_cache.account_dirs():
            if d.name == wxid or d.name.startswith(wxid + "_"):
                return d
    except Exception:  # noqa: BLE001
        pass
    return None
