# -*- coding: utf-8 -*-
"""上下文预算/注入测试。"""
from __future__ import annotations

from agi.selflearn import context
from agi.selflearn.chat import SelfLearnAgent


def test_estimate_and_fit_reduce_long_context():
    assert context.estimate_tokens("你好") > 0
    long = "长期记忆" * 5000
    fitted = context.fit(long, max_tokens=500)
    assert context.estimate_tokens(fitted) <= 800
    assert "上下文过长" in fitted


def test_prompt_includes_profile_retrieval_summary():
    p = SelfLearnAgent._prompt(
        "今天怎么样", [{"scope": "user", "kind": "fact", "content": "用户喜欢咖啡", "evidence": "我喜欢咖啡"}],
        "自画像", {"hint": "自然回应"}, [{"role": "user", "text": "在吗"}],
        st={"emotion": {"label": "平静"}, "relationship": {"stage": "初识"}},
        lessons="", conflict_q="未解决冲突X", sum_ctx="阶段摘要Y",
        retrieval_ctx="检索片段Z", profile_ctx="画像A")
    for token in ("用户画像", "画像A", "阶段摘要", "阶段摘要Y", "相关片段", "检索片段Z", "未解决冲突X"):
        assert token in p
