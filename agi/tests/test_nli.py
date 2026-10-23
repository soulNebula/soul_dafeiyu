# -*- coding: utf-8 -*-
"""NLI 接口测试（fake client）。"""
from __future__ import annotations

from agi.selflearn import nli


class C:
    def available(self): return True
    def chat(self, messages, **kwargs):
        return {"ok": True, "text": '{"claims":[{"index":0,"supported":true,"reason":"ok"},{"index":1,"supported":false,"reason":"no"}]}'}


def test_nli_verify_fake():
    r = nli.verify("咖啡不错。你家橘猫生病了。", ["我喜欢咖啡"], C())
    assert r["ok"] and r["total"] == 2 and r["unsupported"] == 1


def test_nli_no_client():
    assert nli.verify("x", [], None)["ok"] is False
