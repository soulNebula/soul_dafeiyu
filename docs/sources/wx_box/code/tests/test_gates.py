# -*- coding: utf-8 -*-
"""S7 认知闸门测试。

Claim 全部在本地构造（契约字段），不调用 LLM，也不依赖 `claims.py`
是否已经写好；闸门只做 duck typing。
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from agi.selflearn import gates
from agi.selflearn.gates import gate_claim

RESULTS = {"accept", "reject", "pending", "merge", "conflict"}


@dataclass
class Claim:
    """与 BATCH1_CONTRACT.md 的 Claim 字段一致，测试自带一份。"""

    id: str = "c1"
    subject: str = "用户"
    predicate: str = "喜欢"
    object: str = "咖啡"
    polarity: int = 1
    time_ref: str = ""
    claim_type: str = "preference"
    evidence_span: str = "我平时喜欢喝咖啡"
    evidence_message_id: str = "m1"
    confidence: float = 0.9
    source_level: str = "A"


def make_claim(**overrides) -> Claim:
    return replace(Claim(), **overrides)


def make_existing(**overrides) -> dict:
    row = {
        "id": "old1",
        "subject": "用户",
        "predicate": "喜欢",
        "object": "咖啡",
        "polarity": 1,
        "time_ref": "",
        "claim_type": "preference",
        "evidence_span": "我喜欢喝咖啡",
        "evidence_message_id": "m0",
        "confidence": 0.8,
        "source_level": "A",
        "status": "active",
    }
    row.update(overrides)
    return row


def assert_shape(result) -> None:
    """契约三键必须一直在，且类型合法。"""
    assert set(result) >= {"result", "reason", "conflict_with"}
    assert result["result"] in RESULTS
    assert isinstance(result["reason"], str) and result["reason"].strip()
    assert result["conflict_with"] is None or isinstance(result["conflict_with"], str)


class RowLike:
    """模拟 sqlite3.Row：没有属性访问，只有下标访问。"""

    def __init__(self, data):
        self._data = dict(data)

    def keys(self):
        return list(self._data)

    def __getitem__(self, key):
        return self._data[key]


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------

def test_accept_clean_user_claim():
    result = gate_claim(make_claim(), [])
    assert result["result"] == "accept"
    assert result["reason_code"] == "accepted"
    assert result["conflict_with"] is None
    assert result["match_id"] is None
    assert_shape(result)


def test_accept_dict_claim():
    payload = {
        "id": "c-dict", "subject": "用户", "predicate": "住在", "object": "杭州",
        "polarity": 1, "claim_type": "fact", "evidence_span": "我住在杭州",
        "evidence_message_id": "m2", "confidence": 0.95, "source_level": "A",
    }
    result = gate_claim(payload)
    assert result["result"] == "accept"
    assert_shape(result)


def test_accept_city_level_location_is_not_precise_address():
    """只到城市不算精确住址，不能被敏感规则误杀。"""
    claim = make_claim(predicate="住在", object="杭州", claim_type="fact",
                       evidence_span="我住在杭州")
    assert gate_claim(claim)["result"] == "accept"


def test_accept_evidence_at_confidence_boundary():
    assert gate_claim(make_claim(confidence=0.6))["result"] == "accept"


def test_accept_with_source_text_substring():
    claim = make_claim(evidence_span="我平时喜欢喝咖啡")
    result = gate_claim(claim, [], source_text="嗯，我平时喜欢喝咖啡，一天两杯。")
    assert result["result"] == "accept"
    assert_shape(result)


# ---------------------------------------------------------------------------
# reject：结构与字段
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,code", [
    ("subject", "missing_subject"),
    ("predicate", "missing_predicate"),
    ("object", "missing_object"),
])
def test_reject_incomplete_claim(field, code):
    result = gate_claim(make_claim(**{field: "   "}))
    assert result["result"] == "reject"
    assert result["reason_code"] == code


@pytest.mark.parametrize("overrides,code", [
    ({"polarity": 0}, "invalid_polarity"),
    ({"polarity": 2}, "invalid_polarity"),
    ({"claim_type": "gossip"}, "invalid_claim_type"),
    ({"confidence": "high"}, "invalid_confidence"),
    ({"confidence": 1.5}, "invalid_confidence"),
])
def test_reject_invalid_fields(overrides, code):
    result = gate_claim(make_claim(**overrides))
    assert result["result"] == "reject"
    assert result["reason_code"] == code


def test_reject_non_claim():
    for bad in (None, "用户喜欢咖啡"):
        result = gate_claim(bad)
        assert result["result"] == "reject"
        assert result["reason_code"] == "invalid_claim"


# ---------------------------------------------------------------------------
# reject：证据
# ---------------------------------------------------------------------------

def test_reject_missing_evidence():
    result = gate_claim(make_claim(evidence_span=""))
    assert result["result"] == "reject"
    assert result["reason_code"] == "evidence_missing"


@pytest.mark.parametrize("span", ["……", "...", "无", "n/a"])
def test_reject_placeholder_evidence(span):
    result = gate_claim(make_claim(evidence_span=span))
    assert result["result"] == "reject"
    assert result["reason_code"] == "evidence_placeholder"


def test_reject_evidence_not_matching_object():
    result = gate_claim(make_claim(object="咖啡", evidence_span="我昨天去书店买了两本书"))
    assert result["result"] == "reject"
    assert result["reason_code"] == "evidence_span_mismatch"


def test_reject_span_not_in_source_text():
    claim = make_claim(evidence_span="我平时喜欢喝咖啡")
    result = gate_claim(claim, [], source_text="我最近在健身，晚上早睡。")
    assert result["result"] == "reject"
    assert result["reason_code"] == "evidence_span_not_in_source"
    assert_shape(result)


# ---------------------------------------------------------------------------
# reject：敏感信息
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("overrides,code", [
    ({"predicate": "身份证号", "claim_type": "fact", "object": "11010519491231002X",
      "evidence_span": "我的身份证是11010519491231002X"}, "sensitive_id_card"),
    ({"predicate": "银行卡号", "claim_type": "fact", "object": "6222 0212 3456 7890",
      "evidence_span": "卡号是6222 0212 3456 7890"}, "sensitive_bank_card"),
    ({"predicate": "手机号", "claim_type": "fact", "object": "13812345678",
      "evidence_span": "我手机13812345678"}, "sensitive_phone"),
    ({"predicate": "密码", "claim_type": "fact", "object": "abc123456",
      "evidence_span": "我的密码是abc123456"}, "sensitive_password"),
    ({"predicate": "住在", "claim_type": "fact", "object": "杭州市西湖区文一西路100号",
      "evidence_span": "我住在杭州市西湖区文一西路100号"}, "sensitive_address"),
])
def test_reject_sensitive_information(overrides, code):
    result = gate_claim(make_claim(**overrides))
    assert result["result"] == "reject"
    assert result["reason_code"] == code
    assert result["conflict_with"] is None
    assert_shape(result)


def test_sensitive_wins_over_pending():
    """C 级推断 + 手机号：隐私优先，必须 reject 而不是 pending。"""
    claim = make_claim(predicate="手机号", claim_type="fact", object="13812345678",
                       evidence_span="我手机13812345678", source_level="C", confidence=0.3)
    result = gate_claim(claim)
    assert result["result"] == "reject"
    assert result["reason_code"] == "sensitive_phone"


# ---------------------------------------------------------------------------
# pending
# ---------------------------------------------------------------------------

def test_pending_source_level_c():
    claim = make_claim(claim_type="inference", source_level="C",
                       evidence_span="你可能喜欢喝咖啡", confidence=0.9)
    result = gate_claim(claim)
    assert result["result"] == "pending"
    assert result["reason_code"] == "source_level_c"
    assert result["conflict_with"] is None
    assert_shape(result)


def test_pending_inference_type_even_if_level_a():
    claim = make_claim(claim_type="inference", source_level="A",
                       evidence_span="我可能喜欢喝咖啡")
    result = gate_claim(claim)
    assert result["result"] == "pending"
    assert result["reason_code"] == "inference_type"


def test_pending_low_confidence():
    result = gate_claim(make_claim(confidence=0.59))
    assert result["result"] == "pending"
    assert result["reason_code"] == "low_confidence"


def test_pending_unknown_source_level():
    result = gate_claim(make_claim(source_level="Z"))
    assert result["result"] == "pending"
    assert result["reason_code"] == "unknown_source_level"


def test_pending_beats_merge():
    """C 级推断即使和已有事实重复，也只挂起，不合并进事实层。"""
    claim = make_claim(source_level="C", claim_type="inference",
                       evidence_span="我可能喜欢喝咖啡")
    result = gate_claim(claim, [make_existing()])
    assert result["result"] == "pending"
    assert result["conflict_with"] is None


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------

def test_merge_same_subject_predicate_object():
    result = gate_claim(make_claim(), [make_existing()])
    assert result["result"] == "merge"
    assert result["match_id"] == "old1"
    assert result["conflict_with"] is None
    assert "old1" in result["reason"]
    assert_shape(result)


def test_merge_prefers_exact_object_over_other_object_conflict():
    rows = [
        make_existing(id="old-tea", object="茶"),
        make_existing(id="old1"),
    ]
    result = gate_claim(make_claim(), rows)
    assert result["result"] == "merge"
    assert result["match_id"] == "old1"


@pytest.mark.parametrize("overrides", [
    {"status": "rejected"},
    {"status": "superseded"},
    {"source_level": "C"},
    {"claim_type": "inference"},
    {"confidence": 0.3},
])
def test_merge_ignores_non_fact_existing(overrides):
    """C 级、推断、低置信度、非 active 的旧 Claim 都不算事实层。"""
    result = gate_claim(make_claim(), [make_existing(**overrides)])
    assert result["result"] == "accept"


def test_merge_accepts_row_like_existing():
    result = gate_claim(make_claim(), [RowLike(make_existing())])
    assert result["result"] == "merge"
    assert result["match_id"] == "old1"


def test_single_row_like_existing_is_treated_as_one_claim():
    result = gate_claim(make_claim(), RowLike(make_existing()))
    assert result["result"] == "merge"
    assert result["match_id"] == "old1"


def test_merge_by_predicate_alias_without_claims_module(monkeypatch):
    """claims.py 不可用时，likes / like 应归一到同一谓词。"""
    monkeypatch.setattr(gates, "_EXTERNAL_NORMALIZER", None)
    claim = make_claim(predicate="likes", object="咖啡",
                       evidence_span="我喜欢咖啡")
    result = gate_claim(claim, [make_existing(predicate="like")])
    assert result["result"] == "merge"


# ---------------------------------------------------------------------------
# conflict
# ---------------------------------------------------------------------------

def test_conflict_same_subject_predicate_different_object():
    claim = make_claim(predicate="住在", object="杭州", claim_type="fact",
                       evidence_span="我住在杭州")
    result = gate_claim(claim, [make_existing(id="old2", predicate="住在", object="上海",
                                              claim_type="fact")])
    assert result["result"] == "conflict"
    assert result["reason_code"] == "conflict_object"
    assert result["conflict_with"] == "old2"
    assert result["match_id"] == "old2"
    assert_shape(result)


def test_conflict_polarity_flip_same_object():
    claim = make_claim(polarity=1)
    result = gate_claim(claim, [make_existing(polarity=-1)])
    assert result["result"] == "conflict"
    assert result["reason_code"] == "conflict_polarity"
    assert result["conflict_with"] == "old1"


def test_conflict_polarity_flip_different_object():
    """偏好正负相反（对象也不同）同样算冲突。"""
    claim = make_claim(object="咖啡", polarity=1)
    result = gate_claim(claim, [make_existing(id="old3", object="茶", polarity=-1)])
    assert result["result"] == "conflict"
    assert result["conflict_with"] == "old3"


def test_conflict_does_not_leak_across_predicates():
    claim = make_claim(predicate="养", object="猫", evidence_span="我养了一只猫")
    result = gate_claim(claim, [make_existing(id="old4", predicate="喜欢", object="猫")])
    assert result["result"] == "accept"


def test_merge_treats_user_subject_alias():
    """抽取值默认 subject="user"，手工 Claim 常写“用户”，要能对上。"""
    result = gate_claim(make_claim(subject="用户"), [make_existing(subject="user")])
    assert result["result"] == "merge"


def test_no_merge_for_different_subject():
    result = gate_claim(make_claim(subject="用户"), [make_existing(subject="小明")])
    assert result["result"] == "accept"


def test_gate_accepts_real_claim_dataclass():
    """claims.py 可用时，用它的 Claim 数据类再验证一次（不可用则跳过）。"""
    try:
        from agi.selflearn import claims as claims_mod
    except Exception:  # noqa: BLE001 - 另一个 Agent 的文件，缺失/写坏都不该拖垮本文件
        pytest.skip("claims.py 暂不可用")
    claim = claims_mod.Claim(
        id="c-real", subject="user", predicate="喜欢", object="咖啡",
        polarity=1, claim_type="preference", evidence_span="我喜欢喝咖啡",
        evidence_message_id="m9", confidence=0.9, source_level="A",
    )
    result = gate_claim(claim)
    assert result["result"] == "accept"
    assert_shape(result)


# ---------------------------------------------------------------------------
# 形状不变量
# ---------------------------------------------------------------------------

def test_every_branch_has_contract_shape():
    cases = [
        gate_claim(make_claim()),
        gate_claim(make_claim(object="")),
        gate_claim(make_claim(evidence_span="")),
        gate_claim(make_claim(source_level="C", claim_type="inference")),
        gate_claim(make_claim(confidence=0.2)),
        gate_claim(make_claim(), [make_existing()]),
        gate_claim(make_claim(predicate="住在", object="杭州", claim_type="fact",
                              evidence_span="我住在杭州"),
                  [make_existing(predicate="住在", object="上海", claim_type="fact")]),
        gate_claim(make_claim(predicate="手机号", claim_type="fact", object="13812345678",
                              evidence_span="我手机13812345678")),
    ]
    for result in cases:
        assert_shape(result)
