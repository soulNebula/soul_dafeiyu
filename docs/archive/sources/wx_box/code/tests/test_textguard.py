"""内部 ID 出站消毒（agi/textguard）单元测试。"""
from __future__ import annotations

from agi import textguard

#: 2026-10-24 事故里真实发出去的那串（脱敏）
LEAKED = "wxid_0v0g2mpf37u322:1792801924481:1:27970b6973f85f8a8396b4ead353794f::0"


def test_strip_removes_composite_id():
    text = "对方引用了这条消息：" + LEAKED
    out = textguard.strip_internal_ids(text)
    assert "wxid_" not in out
    assert "1792801924481" not in out
    assert "对方引用了这条消息" in out


def test_strip_keeps_human_text():
    assert textguard.strip_internal_ids("你几点开始学来着？") == "你几点开始学来着？"


def test_strip_placeholder_variant():
    out = textguard.strip_internal_ids("引用：" + LEAKED, placeholder=textguard.PLACEHOLDER)
    assert "wxid_" not in out
    assert textguard.PLACEHOLDER in out


def test_bare_composite_and_hash_suffix():
    bare = "1792801924481:1:27970b6973f85f8a8396b4ead353794f"
    assert textguard.strip_internal_ids(bare) == ""
    # 纯 md5（不带 ::N）不应被误伤
    md5 = "d8a0d235d854ab3fbe7694add4631b25"
    assert textguard.strip_internal_ids(md5) == md5
    assert textguard.strip_internal_ids(md5 + "::0") == ""


def test_looks_like_internal_id_flag():
    assert textguard.looks_like_internal_id(LEAKED) is True
    assert textguard.looks_like_internal_id("普通一句话") is False
    assert textguard.looks_like_internal_id("") is False
