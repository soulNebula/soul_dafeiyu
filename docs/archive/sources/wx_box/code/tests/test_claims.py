# -*- coding: utf-8 -*-
"""S6 claims.py：LLM JSON 抽取、原文证据硬校验、推断上限、落库。"""
from __future__ import annotations

import json

from agi.selflearn import claims, db


class FakeClient:
    """可注入的假 LLM：记录调用，返回固定输出。

    ``payload`` 可以是 dict/list（自动 JSON 序列化）、原始字符串，
    或一个 ``Exception`` 实例（模拟 client 抛错）。
    """

    def __init__(self, payload, *, ok: bool = True, available: bool = True):
        self.payload = payload
        self.ok = ok
        self._available = available
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self.ok:
            return {"ok": False, "text": "", "error": "fake client down"}
        if isinstance(self.payload, BaseException):
            raise self.payload
        raw = (
            json.dumps(self.payload, ensure_ascii=False)
            if isinstance(self.payload, (dict, list))
            else str(self.payload)
        )
        return {"ok": True, "text": raw}


def _item(**overrides) -> dict:
    base = {
        "subject": "user",
        "predicate": "喜欢",
        "object": "猫",
        "polarity": 1,
        "time_ref": "",
        "claim_type": "preference",
        "evidence_span": "我喜欢猫",
        "confidence": 0.9,
        "source_level": "A",
    }
    base.update(overrides)
    return base


def _count(path) -> int:
    con = db.connect(path)
    try:
        return int(con.execute("SELECT count(*) FROM claims").fetchone()[0])
    finally:
        con.close()


def test_normalize_predicate_is_stable_and_canonical():
    assert claims.normalize_predicate(" 喜欢 ") == "喜欢"
    assert claims.normalize_predicate("喜爱") == "喜欢"
    assert claims.normalize_predicate("讨厌") == "喜欢"
    assert claims.normalize_predicate("不喜欢") == "喜欢"
    assert claims.normalize_predicate("打算") == "计划"
    assert claims.normalize_predicate("居住在") == "住在"
    assert claims.normalize_predicate("Like") == "like"
    assert claims.normalize_predicate("") == ""
    assert claims.normalize_predicate(None) == ""


def test_extract_claims_parses_json_and_writes_rows(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我喜欢猫，打算周末去钓鱼。我住在杭州。"
    payload = {
        "claims": [
            _item(claim_type="preference", predicate="喜欢", object="猫",
                  evidence_span="我喜欢猫", confidence=0.92),
            _item(claim_type="plan", predicate="打算", object="周末去钓鱼",
                  evidence_span="打算周末去钓鱼", time_ref="周末", confidence=0.8),
            _item(claim_type="fact", predicate="住在", object="杭州",
                  evidence_span="我住在杭州", confidence=0.95),
        ]
    }
    client = FakeClient(payload)
    result = claims.extract_claims(path, client, text, "M-1")

    assert len(result) == 3
    assert all(isinstance(claim, claims.Claim) for claim in result)
    by_type = {claim.claim_type: claim for claim in result}
    assert set(by_type) == {"preference", "plan", "fact"}
    assert by_type["preference"].predicate == "喜欢"
    assert by_type["preference"].polarity == 1
    assert by_type["fact"].predicate == "住在"
    assert by_type["plan"].predicate == "计划"  # 打算 → 计划
    assert by_type["plan"].time_ref == "周末"
    for claim in result:
        assert claim.evidence_span in text
        assert claim.evidence_message_id == "M-1"
        assert claim.source_level == "A"
        assert claim.id.startswith("C-")

    # 调用约定：json_mode + 专属 purpose
    call = client.calls[0]
    assert call["kwargs"]["purpose"] == claims.PURPOSE
    assert call["kwargs"]["json_mode"] is True
    assert call["messages"][0]["content"] == claims.SYSTEM_PROMPT
    assert text in call["messages"][1]["content"]

    # 落库字段完整
    rows = claims.list_claims(path, limit=10)
    assert len(rows) == 3
    row = {item["claim_type"]: item for item in rows}["preference"]
    assert row["subject"] == "user"
    assert row["object"] == "猫"
    assert row["evidence_span"] == "我喜欢猫"
    assert row["evidence_message_id"] == "M-1"
    assert row["source_level"] == "A"
    assert row["status"] == "active"
    assert row["confidence"] == 0.92

    # 同一 message 重复抽取走 id 去重，不重复写库
    again = claims.extract_claims(path, FakeClient(payload), text, "M-1")
    assert [claim.id for claim in again] == [claim.id for claim in result]
    assert _count(path) == 3


def test_claims_with_evidence_not_in_text_are_dropped(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我喜欢猫。"
    payload = {
        "claims": [
            _item(evidence_span="我喜欢猫"),
            _item(predicate="养", object="狗", claim_type="fact",
                  evidence_span="我养了一只狗"),
            _item(predicate="养", object="鱼", claim_type="fact",
                  evidence_span=""),
        ]
    }
    result = claims.extract_claims(path, FakeClient(payload), text, "M-1")

    assert [claim.object for claim in result] == ["猫"]
    assert _count(path) == 1


def test_evidence_span_allows_whitespace_differences(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我 喜欢 猫"
    payload = {"claims": [_item(evidence_span="我喜欢猫")]}
    result = claims.extract_claims(path, FakeClient(payload), text, "M-1")

    assert len(result) == 1
    assert result[0].evidence_span == "我 喜欢 猫"
    assert result[0].evidence_span in text


def test_inference_claims_are_marked_c_and_capped_at_three(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我最近在学硬件，可能在考虑转行。"
    payload = {
        "claims": [
            _item(predicate="转行", object=f"方向{i}", claim_type="inference",
                  source_level="C", evidence_span="我最近在学硬件", confidence=0.5)
            for i in range(5)
        ]
    }
    result = claims.extract_claims(path, FakeClient(payload), text, "M-1")

    assert len(result) == 3
    assert all(
        claim.claim_type == "inference" and claim.source_level == "C"
        for claim in result
    )
    rows = claims.list_claims(path, limit=10)
    assert len(rows) == 3
    assert {row["source_level"] for row in rows} == {"C"}
    assert {row["claim_type"] for row in rows} == {"inference"}


def test_source_c_forces_inference_type_and_b_falls_back_to_a(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我最近在学硬件，可能想换工作。"
    payload = {
        "claims": [
            _item(predicate="换", object="工作", claim_type="fact",
                  source_level="C", evidence_span="可能想换工作"),
            _item(predicate="学", object="硬件", claim_type="plan",
                  source_level="B", evidence_span="我最近在学硬件"),
        ]
    }
    result = claims.extract_claims(path, FakeClient(payload), text, "M-1")

    by_object = {claim.object: claim for claim in result}
    assert by_object["工作"].claim_type == "inference"
    assert by_object["工作"].source_level == "C"
    assert by_object["硬件"].claim_type == "plan"
    assert by_object["硬件"].source_level == "A"
    assert all(claim.source_level in claims.SOURCE_LEVELS for claim in result)


def test_claim_type_is_restricted_to_contract_values(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我准备考试，喜欢猫，可能想换工作，住在杭州。"
    payload = {
        "claims": [
            _item(claim_type="opinion", predicate="喜欢", object="猫",
                  evidence_span="喜欢猫"),
            _item(claim_type="profile", predicate="准备", object="考试",
                  evidence_span="我准备考试"),
            _item(claim_type="mystery", predicate="换", object="工作",
                  evidence_span="可能想换工作"),
            _item(claim_type="fact", predicate="住在", object="杭州",
                  evidence_span="住在杭州", source_level="B"),
        ]
    }
    result = claims.extract_claims(path, FakeClient(payload), text, "M-1")

    assert [claim.claim_type for claim in result] == [
        "preference", "fact", "fact", "fact",
    ]
    assert all(claim.claim_type in claims.CLAIM_TYPES for claim in result)
    assert all(claim.source_level in claims.SOURCE_LEVELS for claim in result)


def test_negative_preference_shares_predicate_and_flips_polarity(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我讨厌猫。"
    payload = {
        "claims": [
            _item(predicate="讨厌", object="猫", claim_type="preference",
                  evidence_span="我讨厌猫"),
        ]
    }
    result = claims.extract_claims(path, FakeClient(payload), text, "M-1")

    assert len(result) == 1
    assert result[0].predicate == "喜欢"
    assert result[0].polarity == -1
    assert claims.list_claims(path, limit=10)[0]["polarity"] == -1


def test_broken_llm_output_does_not_crash(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    text = "我喜欢猫"

    assert claims.extract_claims(path, FakeClient("这不是 JSON"), text, "M-1") == []
    assert claims.extract_claims(path, FakeClient({"nope": []}), text, "M-1") == []
    assert claims.extract_claims(
        path, FakeClient({}, ok=False), text, "M-1") == []
    assert claims.extract_claims(
        path, FakeClient(RuntimeError("boom")), text, "M-1") == []
    assert claims.extract_claims(path, None, text, "M-1") == []

    offline = FakeClient({"claims": [_item()]}, available=False)
    assert claims.extract_claims(path, offline, text, "M-1") == []
    assert offline.calls == []

    idle = FakeClient({"claims": [_item()]})
    assert claims.extract_claims(path, idle, "   ", "M-1") == []
    assert idle.calls == []

    assert _count(path) == 0
