# -*- coding: utf-8 -*-
"""V1 PR5：联系人快照（wxid）与白名单双写。"""
from __future__ import annotations

from agi.wechat_gateway import contacts


def _reader():
    return [
        {"wxid": "wxid_a", "nickname": "好友A", "local_type": 1},
        {"wxid": "wxid_b", "nickname": "好友B", "local_type": 1},
        {"wxid": "123@chatroom", "nickname": "群一", "local_type": 2},
    ]


def test_refresh_and_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "db_path", lambda: tmp_path / "contacts.sqlite3")
    result = contacts.refresh(_reader)
    assert result["ok"] is True and result["count"] == 3
    assert contacts.wxid_for("好友A") == "wxid_a"
    assert contacts.wxid_for("不存在") == ""
    names = [item["nickname"] for item in contacts.list_contacts()]
    assert names[:3] == ["好友A", "好友B", "群一"] or set(names) == {"好友A", "好友B", "群一"}


def test_whitelist_by_name_or_wxid(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "db_path", lambda: tmp_path / "contacts.sqlite3")
    contacts.refresh(_reader)
    assert contacts.is_allowed("好友A", {"allowed_contacts": ["好友A"]}) is True
    assert contacts.is_allowed("好友A", {"allowed_contacts": ["别人"],
                                         "allowed_wxids": ["wxid_a"]}) is True
    assert contacts.is_allowed("好友A", {"allowed_contacts": ["别人"]}) is False
    assert contacts.is_allowed("陌生人", {"allowed_wxids": ["wxid_a"]}) is False


def test_refresh_empty_reader_reports_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "db_path", lambda: tmp_path / "contacts.sqlite3")
    result = contacts.refresh(lambda: [])
    assert result["ok"] is False
    assert result["error"] == "contact_reader_unavailable"
