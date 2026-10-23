# -*- coding: utf-8 -*-
"""联系人数据访问层（微信 4.x 实现）。

原版读 pywxdump 解密的 ``MicroMsg.db``：

- ``Contact`` + ``ContactHeadImgUrl`` + ``ContactLabel`` 联表，再由 ``msg_db.get_contact``
  按最后聊天时间排序；
- ``get_contact_by_username`` 返回一行 12 列，``html_exporter`` 用第 5 列（下标 4）
  取本人昵称。

4.x 改为走 :mod:`wx4.db`（``contact/contact.db``），对外保持原方法名与**同形返回值**
（12 元组列表），GUI / exporter 无需改动。

字段映射（3.x 列 -> 4.x contact.db）::

    UserName    -> username
    Alias       -> alias
    Type        -> local_type（做一次映射，见 _to_legacy_type）
    Remark      -> remark
    NickName    -> nick_name
    PYInitial / RemarkPYInitial / smallHeadImgUrl / bigHeadImgUrl / ExTraBuf / LabelName
                -> 4.x 不提供（返回空串，GUI 不使用）
    latestTalkTime -> 0（4.x 聊天时间在 message 库，导出朋友圈不需要）
"""
from __future__ import annotations

import os.path
import threading
import traceback

from log import LOG

# 仅保留变量名兼容（4.x 不再解密到该路径）
db_path = "./app/Database/Msg/MicroMsg.db"
lock = threading.Lock()

# 微信内置 / 系统账号：3.x 里 Type 会被 GUI 过滤掉，这里统一映射成 1
_SYSTEM_USERNAMES = {
    "notifymessage", "weixin", "fmessage", "medianote", "newsapp", "filehelper",
    "qqmail", "tmessage", "qmessage", "officialaccounts", "floatbottle",
    "brandsessionholder", "brandservicesessionholder", "weixinreminder",
}


def singleton(cls):
    _instance = {}

    def inner():
        if cls not in _instance:
            _instance[cls] = cls()
        return _instance[cls]

    return inner


def is_database_exist() -> bool:
    try:
        import wx4.db  # noqa: F401  （只探测，不连接）
        return True
    except Exception:
        LOG.error("wx4.db 不可用：\n%s", traceback.format_exc())
        return False


def _to_legacy_type(local_type, username: str) -> int:
    """把 4.x ``contact.local_type`` 映射成 GUI 过滤逻辑能识别的 3.x ``Type``。

    GUI 的 ``validate_contact`` 认为 ``Type in (1, 33, 513)`` 是「其他号码」并过滤。
    实测 4.x 好友和系统号的 ``local_type`` 都是 1，若原样透传会把所有好友滤光，
    所以：系统号/公众号/群聊 -> 1（会被 GUI 滤掉），普通好友 -> 3。
    """
    user = (username or "").strip()
    if user in _SYSTEM_USERNAMES or user.startswith("gh_"):
        return 1
    if user.endswith("@chatroom") or "@openim" in user:
        return 1
    if user == "filehelper":
        return 1
    if int(local_type or 0) == 1:
        return 3
    return int(local_type or 0)


def _row_tuple(contact: dict) -> list:
    """把 wx4.db 的联系人 dict 转成原版 12 列行。"""
    username = str(contact.get("username") or "")
    return [
        username,                                   # 0 UserName
        str(contact.get("alias") or ""),            # 1 Alias
        _to_legacy_type(contact.get("local_type"), username),  # 2 Type
        str(contact.get("remark") or ""),           # 3 Remark
        str(contact.get("nick_name") or ""),        # 4 NickName
        "",                                         # 5 PYInitial
        "",                                         # 6 RemarkPYInitial
        "",                                         # 7 smallHeadImgUrl
        "",                                         # 8 bigHeadImgUrl
        "",                                         # 9 ExTraBuf
        "",                                         # 10 labelName
        0,                                          # 11 latestTalkTime
    ]


@singleton
class MicroMsg:
    def __init__(self):
        self.DB = None
        self.cursor = None
        self.open_flag = False
        self._lock = threading.RLock()
        self.init_database()

    def init_database(self):
        self.open_flag = is_database_exist()
        return self.open_flag

    def _contacts(self) -> list[dict]:
        import wx4.db

        return wx4.db.get_db().contacts()

    def get_contact(self):
        """返回全部联系人（12 列行，按备注/昵称排序，同原版用途）。"""
        if not self.init_database():
            return []
        try:
            contacts = [c for c in self._contacts() if c.get("nick_name") or c.get("remark")]
        except Exception:
            LOG.error("读取微信 4.x 联系人失败：\n%s", traceback.format_exc())
            return []
        rows = [_row_tuple(c) for c in contacts]
        rows.sort(key=lambda r: ((r[3] or r[4]), r[0]))
        return rows

    def get_contact_by_username(self, username: object):
        """按 username 返回一行 12 列（不存在返回 None）；``html_exporter`` 只用下标 4。"""
        if not self.init_database():
            return None
        user = str(username or "").strip()
        if not user:
            return None
        try:
            import wx4.db

            contact = wx4.db.get_db().contact(user)
        except Exception:
            LOG.error("读取联系人 %s 失败：\n%s", user, traceback.format_exc())
            return None
        if not contact:
            return None
        return _row_tuple(contact)

    def get_chatroom_info(self, chatroomname):
        """群聊信息（3.x ChatRoom 表）；4.x 朋友圈导出不需要，返回 None。"""
        return None

    def close(self):
        self.open_flag = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
