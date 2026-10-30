# -*- coding: utf-8 -*-
"""04 proposals.py：提案状态机、JSON 证据、终态与列表过滤。"""
from __future__ import annotations

import json
import re

import pytest

from agi.selflearn import db, persona_layers, proposals


def _raw(path, proposal_id: str) -> dict:
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT * FROM learning_proposals WHERE id=?", (proposal_id,)).fetchone()
    finally:
        con.close()
    assert row is not None
    return dict(row)


def _count(path) -> int:
    con = db.connect(path)
    try:
        return int(con.execute(
            "SELECT count(*) FROM learning_proposals").fetchone()[0])
    finally:
        con.close()


def _create(path, **overrides) -> dict:
    data = {
        "kind": "new_variant",
        "target": "S-007/高焦虑",
        "payload": {"hint": "先处理羞耻感，再谈下一步", "reason": "A 组 vs B 组差异"},
        "evidence": ["T-8812", "T-8830"],
    }
    data.update(overrides)
    return proposals.create(path, **data)


def test_create_persists_draft_with_json_evidence(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    result = _create(path)

    assert result["ok"] is True
    assert result["status"] == "draft"
    assert result["terminal"] is False
    assert result["next"] == ["gray", "rejected"]
    assert re.fullmatch(r"P-\d{8}-\d{3}", result["id"])
    assert result["kind"] == "new_variant"
    assert result["target"] == "S-007/高焦虑"
    assert result["payload"] == {
        "hint": "先处理羞耻感，再谈下一步",
        "reason": "A 组 vs B 组差异",
    }
    assert result["evidence"] == ["T-8812", "T-8830"]
    assert result["created_at"] > 0
    assert result["created_at"] == result["updated_at"]

    raw = _raw(path, result["id"])
    assert raw["status"] == "draft"
    assert json.loads(raw["payload_json"]) == result["payload"]
    assert json.loads(raw["evidence_json"]) == ["T-8812", "T-8830"]
    assert _count(path) == 1


def test_create_normalises_json_string_payload_and_evidence(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    as_json = _create(
        path,
        payload='{"hint": "短一点", "reason": "太长"}',
        evidence='["T-1", "T-2"]',
    )
    assert as_json["ok"] is True
    assert as_json["payload"] == {"hint": "短一点", "reason": "太长"}
    assert as_json["evidence"] == ["T-1", "T-2"]

    plain_text = _create(path, evidence="T-9")
    assert plain_text["evidence"] == ["T-9"]

    as_object = _create(
        path, evidence={"turns": ["T-1"], "note": "人工复核"})
    assert as_object["evidence"] == {"turns": ["T-1"], "note": "人工复核"}

    default = _create(path, evidence=None)
    assert default["evidence"] == []
    assert json.loads(_raw(path, default["id"])["evidence_json"]) == []


@pytest.mark.parametrize("overrides", [
    {"kind": ""},
    {"kind": "   "},
    {"target": ""},
    {"target": " \t "},
    {"payload": None},
    {"payload": "not-json"},
    {"payload": [1, 2, 3]},
    {"payload": {"bad": object()}},
    {"evidence": object()},
])
def test_create_rejects_invalid_input_without_writing(tmp_path, overrides):
    path = tmp_path / "selflearn.sqlite3"

    result = _create(path, **overrides)

    assert result["ok"] is False
    assert result["error"]
    assert _count(path) == 0


def test_create_ids_are_unique_and_well_formed(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    ids = [_create(path, target=f"S-{i}")["id"] for i in range(3)]

    assert len(set(ids)) == 3
    assert all(re.fullmatch(r"P-\d{8}-\d{3}", pid) for pid in ids)
    assert _count(path) == 3


def test_legal_transitions_draft_gray_merged(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    created = _create(path)

    gray = proposals.transition(path, created["id"], "gray")
    assert gray["ok"] is True
    assert gray["status"] == "gray"
    assert gray["previous_status"] == "draft"
    assert gray["action"] == "gray"
    assert gray["terminal"] is False
    assert gray["next"] == ["merged", "rejected"]
    assert gray["updated_at"] > created["updated_at"]

    merged = proposals.transition(path, created["id"], "merged")
    assert merged["ok"] is True
    assert merged["status"] == "merged"
    assert merged["previous_status"] == "gray"
    assert merged["terminal"] is True
    assert merged["next"] == []
    assert merged["updated_at"] > gray["updated_at"]

    stored = proposals.get(path, created["id"])
    assert stored is not None
    assert stored["status"] == "merged"
    assert _raw(path, created["id"])["updated_at"] == merged["updated_at"]


def test_reject_from_draft_and_from_gray(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    from_draft = _create(path)
    rejected = proposals.transition(path, from_draft["id"], "reject")
    assert rejected["ok"] is True
    assert rejected["status"] == "rejected"
    assert rejected["previous_status"] == "draft"
    assert rejected["terminal"] is True
    assert rejected["next"] == []

    from_gray = _create(path, target="S-008")
    assert proposals.transition(path, from_gray["id"], "gray")["status"] == "gray"
    rejected_gray = proposals.transition(path, from_gray["id"], "deny")
    assert rejected_gray["ok"] is True
    assert rejected_gray["status"] == "rejected"
    assert rejected_gray["previous_status"] == "gray"


def test_action_aliases_map_to_contract_states(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    submitted = _create(path)
    assert proposals.transition(path, submitted["id"], "submit")["status"] == "gray"
    assert proposals.transition(path, submitted["id"], "approve")["status"] == "merged"

    denied = _create(path, target="S-009")
    assert proposals.transition(path, denied["id"], "DENY")["status"] == "rejected"


def test_illegal_transitions_are_refused_and_keep_state(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    created = _create(path)
    stamp = created["updated_at"]

    for action in ("merged", "merge", "approve"):
        result = proposals.transition(path, created["id"], action)
        assert result["ok"] is False
        assert result["status"] == "draft"
        assert result["allowed"] == ["gray", "rejected"]
        assert "不允许 draft→merged" in result["error"]
    assert _raw(path, created["id"])["updated_at"] == stamp

    assert proposals.transition(path, created["id"], "gray")["ok"] is True
    gray_stamp = _raw(path, created["id"])["updated_at"]
    for action in ("gray", "submit"):
        result = proposals.transition(path, created["id"], action)
        assert result["ok"] is False
        assert result["status"] == "gray"
        assert result["allowed"] == ["merged", "rejected"]
    assert _raw(path, created["id"])["updated_at"] == gray_stamp

    unknown_action = proposals.transition(path, created["id"], "publish")
    assert unknown_action["ok"] is False
    assert "未知动作" in unknown_action["error"]

    missing = proposals.transition(path, "P-19990101-001", "gray")
    assert missing["ok"] is False
    assert "不存在" in missing["error"]

    assert proposals.transition(path, "", "gray")["ok"] is False
    assert proposals.transition(path, created["id"], "")["ok"] is False


def test_merged_and_rejected_are_terminal(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    for terminal_action, expected in (("merge", "merged"), ("reject", "rejected")):
        created = _create(path, target=f"S-{expected}")
        if expected == "merged":
            assert proposals.transition(path, created["id"], "gray")["status"] == "gray"
        done = proposals.transition(path, created["id"], terminal_action)
        assert done["ok"] is True
        assert done["status"] == expected
        stamp = _raw(path, created["id"])["updated_at"]

        for action in ("gray", "merged", "merge", "approve", "rejected", "reject", "deny"):
            result = proposals.transition(path, created["id"], action)
            assert result["ok"] is False
            assert "终态" in result["error"]
            assert result["status"] == expected
            assert result["allowed"] == []
        assert _raw(path, created["id"])["updated_at"] == stamp
        assert _raw(path, created["id"])["status"] == expected


def test_list_proposals_filters_by_status(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    draft = _create(path, target="S-001")
    gray = _create(path, target="S-002")
    rejected = _create(path, target="S-003")
    merged = _create(path, target="S-004")
    proposals.transition(path, gray["id"], "gray")
    proposals.transition(path, rejected["id"], "reject")
    proposals.transition(path, merged["id"], "gray")
    proposals.transition(path, merged["id"], "merge")

    everything = proposals.list_proposals(path)
    assert {row["id"] for row in everything} == {
        draft["id"], gray["id"], rejected["id"], merged["id"]}

    assert [row["id"] for row in proposals.list_proposals(path, "draft")] == [draft["id"]]
    assert [row["id"] for row in proposals.list_proposals(path, "gray")] == [gray["id"]]
    assert [row["id"] for row in proposals.list_proposals(path, "rejected")] == [rejected["id"]]
    assert [row["id"] for row in proposals.list_proposals(path, "merged")] == [merged["id"]]

    # 大小写不敏感 + 多状态过滤
    assert [row["id"] for row in proposals.list_proposals(path, "DRAFT")] == [draft["id"]]
    owned = {row["id"] for row in proposals.list_proposals(path, ["draft", "merged"])}
    assert owned == {draft["id"], merged["id"]}
    assert len(proposals.list_proposals(path, "")) == 4

    assert proposals.list_proposals(path, "draft")[0]["status"] == "draft"


def test_list_proposals_empty_and_invalid_filter(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    assert proposals.list_proposals(path) == []
    assert proposals.list_proposals(path, "draft") == []

    with pytest.raises(ValueError):
        proposals.list_proposals(path, "unknown")
    with pytest.raises(ValueError):
        proposals.list_proposals(path, ["draft", "unknown"])
    with pytest.raises(ValueError):
        proposals.list_proposals(path, [])


def test_get_and_can_transition_helpers(tmp_path):
    path = tmp_path / "selflearn.sqlite3"

    assert proposals.get(path, "P-19990101-001") is None
    assert proposals.get(path, "") is None

    created = _create(path)
    assert proposals.get(path, created["id"])["status"] == "draft"

    assert proposals.can_transition("draft", "gray") is True
    assert proposals.can_transition("draft", "reject") is True
    assert proposals.can_transition("draft", "merged") is False
    assert proposals.can_transition("gray", "merge") is True
    assert proposals.can_transition("merged", "gray") is False
    assert proposals.can_transition("rejected", "reject") is False
    assert proposals.can_transition("bogus", "gray") is False


def test_proposals_never_touch_persona_skills_or_variants(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    tables = ("persona", "skills", "variants")

    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) "
            "VALUES('PS-1','tone','quiet','[]',1,'active',1.0)")
        con.execute(
            "INSERT INTO skills(id,name,description,level,alpha,beta,status) "
            "VALUES('S-CONV-001','日常聊天','x',0,1,1,'active')")
        con.execute(
            "INSERT INTO variants(id,skill_id,hint,alpha,beta,status) "
            "VALUES('V-BASE','S-CONV-001','自然回应',1,1,'base')")
        con.commit()
        before = {
            table: [dict(row) for row in con.execute(
                f"SELECT * FROM {table} ORDER BY id")] for table in tables
        }
    finally:
        con.close()

    merged = _create(path, kind="persona_patch", target="core")
    proposals.transition(path, merged["id"], "gray")
    proposals.transition(path, merged["id"], "merged")
    rejected = _create(path, kind="knob_adjust", target="length_budget")
    proposals.transition(path, rejected["id"], "reject")

    con = db.connect(path)
    try:
        after = {
            table: [dict(row) for row in con.execute(
                f"SELECT * FROM {table} ORDER BY id")] for table in tables
        }
    finally:
        con.close()

    assert after == before
    assert _count(path) == 2


# --------------------------------------------------------------------------- #
# C2：merged 后的 apply 点（persona_trait → core 人格层）
# --------------------------------------------------------------------------- #
def _create_trait(path, key: str = "tone", value: str = "quiet") -> dict:
    return proposals.create(
        path, "persona_trait", key, {"value": value}, ["T-1001", "T-1002"])


def _core_rows(path) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM persona_layers WHERE layer='core' AND status='active' "
            "ORDER BY created_at, id").fetchall()]
    finally:
        con.close()


def test_merged_persona_trait_applies_to_core_layer(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    created = _create_trait(path, "tone", "quiet")

    assert proposals.transition(path, created["id"], "gray")["ok"] is True
    merged = proposals.transition(path, created["id"], "merge")

    assert merged["ok"] is True
    assert merged["status"] == "merged"
    assert merged["applied"] is True
    assert "apply_error" not in merged
    assert merged["apply"]["already_applied"] is False

    rows = _core_rows(path)
    assert len(rows) == 1
    assert rows[0]["key"] == "tone"
    assert rows[0]["value"] == "quiet"
    assert rows[0]["approved_by"] == f"proposal:{created['id']}"

    ctx = persona_layers.context(path)
    assert "core:" in ctx and "tone=quiet" in ctx
    logs = persona_layers.changelog(path, limit=50)
    assert any(row["key"] == "tone" and row["action"] == "approve"
               for row in logs)


def test_apply_merged_is_idempotent(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    created = _create_trait(path, "pace", "慢一点")
    proposals.transition(path, created["id"], "gray")
    proposals.transition(path, created["id"], "merge")

    first = proposals.apply_merged(path, created["id"])
    assert first["ok"] is True
    assert first["applied"] is False
    assert first["already_applied"] is True

    second = proposals.apply_merged(path, created["id"])
    assert second["ok"] is True
    assert second["already_applied"] is True

    rows = [row for row in _core_rows(path) if row["key"] == "pace"]
    assert len(rows) == 1
    assert rows[0]["value"] == "慢一点"
    # 幂等不重复写 changelog 的 apply 记录
    logs = [row for row in persona_layers.changelog(path, limit=100)
            if row["key"] == "pace"]
    assert len(logs) == 1


def test_apply_merged_rejects_non_merged_proposal(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    draft = _create_trait(path)

    result = proposals.apply_merged(path, draft["id"])
    assert result["ok"] is False
    assert result["code"] == "not_merged"
    assert _core_rows(path) == []

    missing = proposals.apply_merged(path, "P-19990101-001")
    assert missing["ok"] is False and missing["code"] == "not_found"


def test_merged_unknown_kind_reports_unsupported(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    created = proposals.create(
        path, "knob_adjust", "length_budget", {"value": 120}, ["T-1"])
    proposals.transition(path, created["id"], "gray")
    merged = proposals.transition(path, created["id"], "merge")

    assert merged["ok"] is True
    assert merged["status"] == "merged"
    assert merged["applied"] is False
    assert merged["apply"]["code"] == "unsupported_kind"
    assert "unknown proposal kind" in merged["apply_error"]

    direct = proposals.apply_merged(path, created["id"])
    assert direct["ok"] is False
    assert direct["code"] == "unsupported_kind"
    assert direct["kind"] == "knob_adjust"


def test_transition_surfaces_apply_error(tmp_path, monkeypatch):
    path = tmp_path / "selflearn.sqlite3"
    created = _create_trait(path)
    proposals.transition(path, created["id"], "gray")

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(persona_layers, "apply_core", boom)
    merged = proposals.transition(path, created["id"], "merge")

    # 状态迁移本身成功，但 apply 失败必须显式可见，不能静默
    assert merged["ok"] is True
    assert merged["status"] == "merged"
    assert merged["applied"] is False
    assert "disk full" in merged["apply_error"]
    assert merged["apply"]["code"] == "apply_failed"
    assert _core_rows(path) == []
