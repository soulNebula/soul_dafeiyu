# -*- coding: utf-8 -*-
"""引用消息解析：<title> 里的实际回复不能丢。"""
from __future__ import annotations

from wx_core import db_source


def _bridge():
    return object.__new__(db_source.DbBridge)


def test_quote_message_keeps_reply_title():
    xml = (
        "<msg><appmsg><title>都是推荐的？还是你想刷的</title><type>57</type>"
        "<refermsg><type>1</type><displayname>小鱼</displayname>"
        "<content>视频画面还是空的</content></refermsg></appmsg></msg>"
    )
    kind, text, media = _bridge()._resolve_appmsg(xml, 0)
    assert media == ""
    assert kind == db_source.KIND_TEXT
    assert "对方引用了这条消息" in text
    assert "视频画面还是空的" in text
    assert "都是推荐的？还是你想刷的" in text
    assert text.endswith("都是推荐的？还是你想刷的")


def test_quote_message_without_title_only_context():
    xml = (
        "<msg><appmsg><title></title><type>57</type>"
        "<refermsg><type>47</type><displayname>小鱼</displayname>"
        "<content>wxid_abc:1:1:ff</content></refermsg></appmsg></msg>"
    )
    _kind, text, _media = _bridge()._resolve_appmsg(xml, 0)
    assert "[引用]" in text and "表情包" in text
    assert "wxid_" not in text
