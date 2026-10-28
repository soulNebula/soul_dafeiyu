# -*- coding: utf-8 -*-
"""citation：记忆引用生成与校验测试（03 防幻觉）。"""
from __future__ import annotations

import time

from agi.selflearn import db, memory
from agi.selflearn.citation import CITATION, format_memory_context, validate_citations


def _mem(**over) -> dict:
    base = {
        "id": "UM-abc", "scope": "user", "kind": "fact",
        "content": "我住在杭州", "evidence": "我住在杭州",
        "evidence_message_id": None, "confidence": 0.8, "status": "active",
    }
    base.update(over)
    return base


def _message(path, mid: str = "M-1", text: str = "我住在杭州", role: str = "user") -> str:
    con = db.connect(path)
    try:
        con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,?)",
                    (mid, role, text, time.time(), 0))
        con.commit()
    finally:
        con.close()
    return mid


def _memory(path, mid: str = "UM-1", content: str = "我住在杭州", *,
            message_id: str | None = None, status: str = "active") -> str:
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO user_memories(id,scope,kind,content,evidence,"
            "evidence_message_id,confidence,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (mid, "user", "fact", content, content, message_id, 0.8, status, time.time()))
        con.commit()
    finally:
        con.close()
    return mid


# ---------------------------------------------------------------- prompt 片段

def test_format_prefers_evidence_message_id():
    mems = [
        _mem(evidence_message_id="M-msg1"),
        _mem(id="UM-mem2", content="我养了猫", evidence="我养了猫"),
    ]
    ctx = format_memory_context(mems)
    assert "[M-msg1] [user/fact] 我住在杭州" in ctx
    assert "[M-UM-mem2] [user/fact] 我养了猫" in ctx
    # 有真实消息证据时不再暴露内部 memory id
    assert "[M-UM-abc]" not in ctx


def test_format_is_stable_and_human_readable():
    mems = [_mem(evidence_message_id="M-msg1")]
    first = format_memory_context(mems)
    assert first == format_memory_context(mems)
    assert first.startswith("- [M-msg1]")
    assert "（依据：我住在杭州）" not in first  # 依据与内容相同不重复


def test_format_empty_and_no_id():
    assert format_memory_context([]) == "（暂无长期记忆）"
    assert format_memory_context(None) == "（暂无长期记忆）"
    assert format_memory_context([{"id": "UM-x", "content": "   "}]) == "（暂无长期记忆）"
    orphan = format_memory_context([{"content": "没有 id 的记忆"}])
    assert "（不可引用）" in orphan
    assert CITATION.search(orphan) is None


# ---------------------------------------------------------------- 引用校验

def test_no_citation_passes_through_verbatim(tmp_path):
    p = tmp_path / "c1.sqlite3"
    raw = " 嗯，我在。\n\n你呢？ "
    out = validate_citations(raw, p)
    assert out == {"ok": True, "reply": raw, "removed": []}
    empty = validate_citations("", p)
    assert empty == {"ok": True, "reply": "", "removed": []}


def test_valid_citations_kept(tmp_path):
    p = tmp_path / "c2.sqlite3"
    _message(p, "M-1")
    _memory(p, "UM-1", message_id="M-1")
    out = validate_citations("你住在杭州。[M-1] 我记着呢。[M-UM-1]", p)
    assert out["ok"] is True
    assert out["removed"] == []
    assert "[M-1]" in out["reply"] and "[M-UM-1]" in out["reply"]


def test_unknown_citation_removed(tmp_path):
    p = tmp_path / "c3.sqlite3"
    _message(p, "M-1")
    out = validate_citations("你上次说面试在周三。[M-1] 你还说在搬家。[M-nope]", p)
    assert out["ok"] is False
    assert out["removed"] == ["M-nope"]
    assert "[M-nope]" not in out["reply"]
    assert "[M-1]" in out["reply"]
    assert "搬家" in out["reply"]  # 只删引用本身，不删整句


def test_inactive_memory_citation_removed(tmp_path):
    p = tmp_path / "c4.sqlite3"
    _memory(p, "UM-old", status="invalid")
    out = validate_citations("你以前说过这个。[M-UM-old]", p)
    assert out["ok"] is False
    assert out["removed"] == ["M-UM-old"]
    assert "[M-UM-old]" not in out["reply"]


def test_duplicate_citations(tmp_path):
    p = tmp_path / "c5.sqlite3"
    _message(p, "M-1")
    # 重复的合法引用：不算错误，原样保留
    keep = validate_citations("你住在杭州。[M-1] 我记得很清楚。[M-1]", p)
    assert keep["ok"] is True and keep["removed"] == []
    assert keep["reply"].count("[M-1]") == 2
    # 重复的坏引用：只报告一次，但所有出现都删掉
    drop = validate_citations("他说过。[M-nope] 真的。[M-nope]", p)
    assert drop["ok"] is False
    assert drop["removed"] == ["M-nope"]
    assert "[M-nope]" not in drop["reply"]


def test_multiple_unknown_removed_in_order_and_whitespace_cleaned(tmp_path):
    p = tmp_path / "c6.sqlite3"
    _message(p, "M-1")
    out = validate_citations("你好 [M-a] 在吗 [M-1] 别担心 [M-b]", p)
    assert out["removed"] == ["M-a", "M-b"]
    assert "  " not in out["reply"]
    assert out["reply"] == "你好 在吗 [M-1] 别担心"


# ------------------------------------------------ format → validate 真实回路

def test_format_then_validate_roundtrip(tmp_path):
    p = tmp_path / "c7.sqlite3"
    mid = _message(p, "M-msg1")
    _memory(p, "UM-mem1", message_id=mid)
    ctx = format_memory_context([
        _mem(id="UM-mem1", evidence_message_id=mid),
    ])
    token = CITATION.search(ctx).group(1)
    assert token == mid
    out = validate_citations(f"你说过住在杭州。[{token}]", p)
    assert out["ok"] is True
    assert out["removed"] == []


def test_memory_only_citation_roundtrip(tmp_path):
    p = tmp_path / "c8.sqlite3"
    _memory(p, "UM-mem2", content="我养了猫")
    ctx = format_memory_context([
        _mem(id="UM-mem2", content="我养了猫", evidence="我养了猫"),
    ])
    token = CITATION.search(ctx).group(1)
    assert token == "M-UM-mem2"
    out = validate_citations(f"你说过养猫。[{token}]", p)
    assert out["ok"] is True
    assert out["removed"] == []


def test_retrieved_memory_citation_resolves(tmp_path):
    p = tmp_path / "c9.sqlite3"
    mid = memory.store_message(p, "user", "我喜欢猫")
    saved = memory.extract_user_memories(p, None, "我喜欢猫", mid)
    assert saved, "规则抽取应写入一条偏好记忆"
    mems = memory.retrieve(p, "猫", k=5)
    assert mems
    ctx = format_memory_context(mems)
    token = CITATION.search(ctx).group(1)
    assert token == mid  # 引用优先落在真实消息上
    out = validate_citations(f"你说过喜欢猫。[{token}]", p)
    assert out["ok"] is True
    assert out["removed"] == []
