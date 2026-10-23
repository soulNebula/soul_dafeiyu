"""联系人（好友/群聊）列表 + “是否有历史聊天记录”过滤 + 拼音首字母。

用户要求：网页左栏只保留**和自己有历史聊天记录**的好友/群；没有记录的不要显示。
数据来源：
* ``contact.db``：好友与群聊（``username``、``remark``、``nick_name``、``local_type``、
  ``delete_flag``）；
* ``session/session.db`` 的 ``SessionTable``：有 ``last_msg_locald_id>0`` 或
  ``last_timestamp>0`` 且 ``summary`` 非空，即视为“有历史聊天记录”。

只读、结果仅内存 TTL 缓存（默认 60 秒），不写任何文件。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable

from webui.account import detect_active_account
from webui.history import HistoryService
from webui.pinyin import pinyin_initial

CONTACTS_TTL = 60.0


class ContactsService:
    """当前账号的联系人摘要（好友/群聊/历史记录标记/头像首字母）。"""

    def __init__(self, config: dict | None = None, *, log: Callable[[str], None] | None = None,
                 ttl: float = CONTACTS_TTL):
        self._history = HistoryService(config, log=log)
        self._log = log or (lambda *_a, **_k: None)
        self._ttl = max(5.0, float(ttl or CONTACTS_TTL))
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._payload: dict = {}
        self._last_wxid = ""

    # ------------------------------------------------------------------
    def clear_cache(self) -> None:
        """清空联系人缓存（账号切换/手动清空列表时用）。"""
        with self._lock:
            self._payload = {}
            self._cached_at = 0.0
            self._last_wxid = ""

    def list(self, refresh: bool = False, include_all: bool = False) -> dict:
        """返回好友/群聊列表。

        :param refresh: 强制重新读库（否则 60 秒缓存）。
        :param include_all: True 时返回全部联系人（``has_history`` 标记是否有记录）；
                            False（默认）只返回有历史聊天记录的联系人。
        """
        # 当前登录账号变了：即使缓存没过期也必须重读（切号问题的关键）
        try:
            current_wxid = detect_active_account()[1]
        except Exception:  # noqa: BLE001
            current_wxid = ""
        with self._lock:
            now = time.time()
            cached_account = str((self._payload or {}).get("account_wxid") or "")
            if current_wxid and cached_account and current_wxid != cached_account:
                refresh = True
            if (not refresh and self._payload
                    and now - self._cached_at < self._ttl
                    and bool(self._payload.get("include_all")) == bool(include_all)):
                cached = dict(self._payload)
                cached["cached"] = True
                return cached
            reader = None
            try:
                acc, keys, wxid = self._history._account()
                from wx_core import wcdb

                reader = wcdb.SessionReader(acc, keys, wxid)
                contact_path = reader._materialize("db_storage/contact/contact.db", "_contact_path")
                session_path = reader._materialize("db_storage/session/session.db", "_session_path")
                if contact_path is None:
                    raise RuntimeError("无法读取联系人数据库（微信可能未登录或密钥不可用）")
                contacts = _query_contacts(contact_path)
                history_names = _query_history_names(session_path) if session_path else set()
                friends, groups = _build_lists(contacts, history_names, include_all=include_all)
                # 头像首字母给“全部联系人”（即使被过滤掉，页面里残留的历史消息也要正确显示字母）
                initials: dict[str, str] = {}
                for username, nick, remark, _local_type in contacts:
                    name = _display_name(username, nick, remark)
                    if name:
                        initials.setdefault(name, pinyin_initial(name))
                account_changed = bool(self._last_wxid and self._last_wxid != wxid)
                payload = {
                    "ok": True,
                    "include_all": bool(include_all),
                    "friends": friends,
                    "groups": groups,
                    "count": len(friends) + len(groups),
                    "friend_count": len(friends),
                    "group_count": len(groups),
                    "initials": initials,
                    "history_names": sorted(history_names),
                    "cached": False,
                    "account_wxid": wxid,
                    "account": {"wxid": wxid, "dir": acc.name},
                    "account_changed": account_changed,
                }
                self._payload = payload
                self._cached_at = now
                self._last_wxid = wxid
                self._log(
                    f"[contacts] 账号 {wxid}：好友 {len(friends)} / 群聊 {len(groups)}"
                    f"（{'全部' if include_all else '仅有聊天记录'}）"
                )
                result = dict(payload)
                return result
            except Exception as exc:  # noqa: BLE001 - 失败不影响好友/聊天主流程
                self._log(f"[contacts] 读取联系人失败：{exc}")
                return {"ok": False, "error": f"读取联系人失败：{exc}", "friends": [],
                        "groups": [], "count": 0, "initials": {}, "history_names": [],
                        "account_wxid": "", "account_changed": False}
            finally:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:  # noqa: BLE001
                        pass

    def has_history(self, name: str) -> bool:
        """某个显示名是否有历史聊天记录（必要时刷新缓存）。"""
        target = str(name or "").strip()
        if not target:
            return False
        payload = self.list()
        return target in set(payload.get("history_names") or [])

    def close(self) -> None:
        self._history.close()


def _query_contacts(contact_db_path) -> list[tuple[str, str, str, int]]:
    """返回 ``[(username, nick_name, remark, local_type)]``（含好友与群聊）。"""
    con = sqlite3.connect(f"file:{contact_db_path}?mode=ro", uri=True)
    try:
        cols = {str(row[1]) for row in con.execute("PRAGMA table_info(contact)")}
        where = ["1=1"]
        if "delete_flag" in cols:
            where.append("COALESCE(delete_flag, 0) = 0")
        sql = (
            "SELECT username, COALESCE(nick_name, ''), COALESCE(remark, ''), "
            "COALESCE(local_type, 0) FROM contact WHERE " + " AND ".join(where)
        )
        return [(str(u or ""), str(n or ""), str(r or ""), int(t or 0))
                for u, n, r, t in con.execute(sql)]
    finally:
        con.close()


def _query_history_names(session_db_path) -> set[str]:
    """从 SessionTable 里取“有历史消息”的会话 username 集合。"""
    con = sqlite3.connect(f"file:{session_db_path}?mode=ro", uri=True)
    try:
        cols = {str(row[1]) for row in con.execute("PRAGMA table_info(SessionTable)")}
        if "username" not in cols:
            return set()
        last_id = "COALESCE(last_msg_locald_id, 0)" if "last_msg_locald_id" in cols else "0"
        last_ts = "COALESCE(last_timestamp, 0)" if "last_timestamp" in cols else "0"
        summary = "COALESCE(summary, '')" if "summary" in cols else "''"
        sql = (
            f"SELECT username FROM SessionTable WHERE {last_id} > 0 "
            f"OR ({last_ts} > 0 AND {summary} <> '')"
        )
        return {str(u) for (u,) in con.execute(sql) if u}
    finally:
        con.close()


def _is_group_username(username: str) -> bool:
    return str(username or "").lower().endswith("@chatroom")


def _display_name(username: str, nick: str, remark: str) -> str:
    """联系人显示名：备注优先，其次昵称；内置账号使用中文名。"""
    username = str(username or "")
    if username in ("filehelper", "weixin", "brandsessionholder"):
        return remark or nick or {"filehelper": "文件传输助手", "weixin": "微信团队"}.get(username, username)
    return (str(remark or "").strip() or str(nick or "").strip())


def _build_lists(contacts: list[tuple[str, str, str, int]], history_names: set[str],
                 *, include_all: bool = False) -> tuple[list[dict], list[dict]]:
    """把 contact.db 行转成好友/群聊列表（按显示名去重、按需过滤无记录项）。"""
    friends: list[dict] = []
    groups: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for username, nick, remark, _local_type in contacts:
        if not username:
            continue
        if username.startswith("gh_") or username.startswith("@"):
            continue  # 公众号/系统账号
        name = _display_name(username, nick, remark)
        if not name:
            continue
        is_group = _is_group_username(username)
        kind = "group" if is_group else "friend"
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        has_history = username in history_names
        if not include_all and not has_history:
            continue
        item = {
            "name": name,
            "username": username,
            "kind": kind,
            "initial": pinyin_initial(name),
            "has_history": has_history,
        }
        (groups if is_group else friends).append(item)
    friends.sort(key=lambda item: (str(item.get("name") or "")))
    groups.sort(key=lambda item: (str(item.get("name") or "")))
    return friends, groups
