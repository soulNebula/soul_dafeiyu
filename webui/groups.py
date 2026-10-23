"""群聊列表（只读、内存缓存、不落盘）。

网页端新需求：左栏除好友外还要能看/选/监听**群聊**。群聊在微信 4.x 的
``contact.db`` 里是 ``username LIKE '%@chatroom'`` 的联系人；显示名取 ``remark``
优先、其次 ``nick_name``。这里直接复用 ``HistoryService`` 的账号/密钥定位逻辑，
每次只读 ``contact.db``，结果放内存 TTL 缓存（默认 60 秒），不写任何文件。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from typing import Callable

from webui.history import HistoryService

GROUP_TTL = 60.0


class GroupsService:
    """当前登录账号的群聊列表（线程安全、内存缓存）。"""

    def __init__(self, config: dict | None = None, *, log: Callable[[str], None] | None = None,
                 ttl: float = GROUP_TTL):
        self._history = HistoryService(config, log=log)
        self._log = log or (lambda *_a, **_k: None)
        self._ttl = max(5.0, float(ttl or GROUP_TTL))
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._groups: list[dict] = []
        self._names: set[str] = set()
        self._last_wxid = ""

    # ------------------------------------------------------------------
    def clear_cache(self) -> None:
        """清空群聊缓存（账号切换/手动清空列表时用）。"""
        with self._lock:
            self._groups = []
            self._names = set()
            self._cached_at = 0.0
            self._last_wxid = ""

    def list(self, refresh: bool = False) -> dict:
        """返回 ``{"ok": True, "count": N, "groups": [...]}``；失败返回可读错误。"""
        try:
            from webui.account import detect_active_account

            current_wxid = detect_active_account()[1]
        except Exception:  # noqa: BLE001
            current_wxid = ""
        with self._lock:
            now = time.time()
            if current_wxid and self._last_wxid and current_wxid != self._last_wxid:
                refresh = True
            if not refresh and self._groups and now - self._cached_at < self._ttl:
                return {
                    "ok": True,
                    "count": len(self._groups),
                    "groups": [dict(g) for g in self._groups],
                    "cached": True,
                }
            reader = None
            try:
                acc, keys, wxid = self._history._account()  # 复用统一账号定位（只读）
                from wx_core import wcdb

                reader = wcdb.SessionReader(acc, keys, wxid)
                path = reader._materialize("db_storage/contact/contact.db", "_contact_path")
                if path is None:
                    raise RuntimeError("无法读取联系人数据库（微信可能未登录或密钥不可用）")
                rows = _query_groups(path)
                groups: list[dict] = []
                seen_names: set[str] = set()
                for username, nick, remark in rows:
                    name = (remark or "").strip() or (nick or "").strip()
                    username = str(username or "").strip()
                    if not name or not username:
                        continue
                    if name in seen_names:
                        continue
                    seen_names.add(name)
                    groups.append({
                        "name": name,
                        "username": username,
                        "kind": "group",
                        "member_hint": "",
                    })
                groups.sort(key=lambda g: str(g.get("name") or ""))
                self._groups = groups
                self._names = set(seen_names)
                self._cached_at = now
                self._last_wxid = wxid
                self._log(f"[groups] 已加载 {len(groups)} 个群聊")
                return {
                    "ok": True,
                    "count": len(groups),
                    "groups": [dict(g) for g in groups],
                    "cached": False,
                }
            except Exception as exc:  # noqa: BLE001 - 群聊失败不影响好友/聊天
                self._log(f"[groups] 加载群聊失败：{exc}")
                return {"ok": False, "error": f"加载群聊失败：{exc}", "count": 0, "groups": []}
            finally:
                if reader is not None:
                    try:
                        reader.close()
                    except Exception:  # noqa: BLE001
                        pass

    def is_group(self, name: str) -> bool:
        """判断显示名是否属于群聊（必要时刷新缓存）。"""
        target = str(name or "").strip()
        if not target:
            return False
        with self._lock:
            if time.time() - self._cached_at >= self._ttl or not self._groups:
                need_refresh = True
            else:
                need_refresh = False
        if need_refresh:
            self.list(refresh=True)
        with self._lock:
            return target in self._names

    def close(self) -> None:
        """群聊只读、无后台线程，无需额外清理。"""
        self._history.close()


def _query_groups(contact_db_path) -> list[tuple]:
    """只读查询 contact.db 里的群聊（username/nick_name/remark）。"""
    con = sqlite3.connect(f"file:{contact_db_path}?mode=ro", uri=True)
    try:
        cols = {str(row[1]) for row in con.execute("PRAGMA table_info(contact)")}
        where = ["username LIKE '%@chatroom'"]
        if "delete_flag" in cols:
            where.append("COALESCE(delete_flag, 0) = 0")
        if "local_type" in cols:
            where.append("local_type = 2")
        sql = (
            "SELECT username, nick_name, remark FROM contact "
            "WHERE " + " AND ".join(where) + " ORDER BY username"
        )
        return list(con.execute(sql).fetchall())
    finally:
        con.close()
