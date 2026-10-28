# -*- coding: utf-8 -*-
"""S1 PII/清洗测试。"""
from __future__ import annotations

from agi.selflearn import pii


def test_scrub_pii():
    text = "我手机 13812345678，身份证 110101199001011234，密码: abc123，住在朝阳区阳光花园3栋2单元101室"
    clean, hits = pii.scrub_pii(text)
    for token in ("13812345678", "110101199001011234", "abc123"):
        assert token not in clean
    assert "阳光花园" not in clean and "101室" not in clean
    kinds = {h["kind"] for h in hits}
    assert {"phone", "id", "password", "address"} <= kinds


def test_fuzz_location_and_merge():
    assert pii.fuzz_location("杭州市西湖区文三路 123 号") == "杭州市街道路段"
    assert pii.fuzz_location("") == ""
    items = [{"sender": "u", "kind": "text", "text": "在吗", "ts": 1},
             {"sender": "u", "kind": "text", "text": "出来", "ts": 10},
             {"sender": "u", "kind": "text", "text": "不出来算了", "ts": 300}]
    merged = pii.merge_short_bursts(items)
    assert len(merged) == 2 and merged[0]["text"] == "在吗 出来"
