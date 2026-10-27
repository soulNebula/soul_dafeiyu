# -*- coding: utf-8 -*-
"""S9 profile.py：10 维画像、证据硬校验、没证据不编、旧版本留痕。"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, profile


class FakeClient:
    """可注入的假 LLM：记录调用，返回固定输出（不联网）。"""

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


def _seed_memories(path, *items) -> None:
    con = db.connect(path)
    try:
        now = time.time()
        for index, item in enumerate(items):
            con.execute(
                "INSERT OR IGNORE INTO user_memories(id,scope,kind,content,evidence,"
                "evidence_message_id,confidence,status,conflict_group,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    item["id"],
                    item.get("scope", "user"),
                    item.get("kind", "fact"),
                    item["content"],
                    item.get("evidence", item["content"]),
                    item.get("evidence_message_id"),
                    item.get("confidence", 0.8),
                    item.get("status", "active"),
                    None,
                    now + index,
                ),
            )
        con.commit()
    finally:
        con.close()


def _seed_claims(path, *items) -> None:
    con = db.connect(path)
    try:
        now = time.time()
        for index, item in enumerate(items):
            con.execute(
                "INSERT OR IGNORE INTO claims(id,subject,predicate,object,polarity,"
                "time_ref,claim_type,evidence_span,evidence_message_id,confidence,"
                "source_level,status,conflict_group,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    item["id"],
                    item.get("subject", "user"),
                    item["predicate"],
                    item["object"],
                    item.get("polarity", 1),
                    item.get("time_ref", ""),
                    item.get("claim_type", "fact"),
                    item["evidence_span"],
                    item.get("evidence_message_id"),
                    item.get("confidence", 0.7),
                    item.get("source_level", "A"),
                    item.get("status", "active"),
                    None,
                    now + index,
                ),
            )
        con.commit()
    finally:
        con.close()


def _rows(path) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(row) for row in con.execute(
            "SELECT * FROM user_profile ORDER BY id")]
    finally:
        con.close()


def _count(path) -> int:
    return len(_rows(path))


# ---------------------------------------------------------------------------
# 契约形状
# ---------------------------------------------------------------------------


def test_dimensions_are_the_contract_ten():
    assert profile.DIMENSIONS == (
        "work", "city", "family", "age", "school",
        "health", "interests", "values", "style", "goals",
    )
    assert len(set(profile.DIMENSIONS)) == 10
    assert set(profile.DIMENSION_LABELS) == set(profile.DIMENSIONS)


def test_canon_dimension_accepts_labels_and_aliases():
    assert profile.canon_dimension("work") == "work"
    assert profile.canon_dimension("职业") == "work"
    assert profile.canon_dimension(" job ") == "work"
    assert profile.canon_dimension("说话风格") == "style"
    assert profile.canon_dimension("城市维度") == "city"
    assert profile.canon_dimension("salary") == ""
    assert profile.canon_dimension(None) == ""


# ---------------------------------------------------------------------------
# 正常蒸馏
# ---------------------------------------------------------------------------


def test_distill_writes_dimensions_with_real_evidence(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_memories(
        path,
        {"id": "UM-1", "content": "我住在杭州", "confidence": 0.9},
        {"id": "UM-2", "content": "我在做后端开发", "confidence": 0.8},
        # 自人格/世界记忆不是用户信息，不能当画像证据
        {"id": "UM-SELF", "scope": "self", "kind": "insight", "content": "我喜欢独处"},
        # 非 active 的记忆不参与
        {"id": "UM-OLD", "content": "我住在北京", "status": "conflict"},
    )
    _seed_claims(
        path,
        {"id": "C-1", "predicate": "喜欢", "object": "露营",
         "claim_type": "preference", "evidence_span": "我喜欢露营",
         "confidence": 0.7, "source_level": "A"},
        # C 级是模型推断，不算证据
        {"id": "C-INF", "predicate": "想", "object": "转行",
         "claim_type": "inference", "evidence_span": "可能想转行",
         "source_level": "C"},
    )
    payload = {
        "profile": [
            {"dimension": "city", "value": "住在杭州",
             "evidence_memory_ids": ["UM-1"], "confidence": 0.9},
            {"dimension": "work", "value": "在做后端开发",
             "evidence_memory_ids": ["UM-2"], "confidence": 0.85},
            {"dimension": "interests", "value": "喜欢露营",
             "evidence_memory_ids": ["C-1"], "confidence": 0.75},
        ]
    }
    client = FakeClient(payload)
    result = profile.distill(path, client)

    assert result["ok"] is True
    assert result["written"] == 3
    assert result["evidence_count"] == 3  # UM-1 / UM-2 / C-1
    assert result["skipped"] == []
    assert set(result["dimensions"]) == {"work", "city", "interests"}

    # 调用约定：json_mode + 专属 purpose，证据 id 出现在 prompt 里
    call = client.calls[0]
    assert call["kwargs"]["purpose"] == profile.PURPOSE
    assert call["kwargs"]["json_mode"] is True
    assert call["messages"][0]["content"] == profile.SYSTEM_PROMPT
    prompt = call["messages"][1]["content"]
    assert "[UM-1]" in prompt and "[UM-2]" in prompt and "[C-1]" in prompt
    assert "UM-SELF" not in prompt and "独处" not in prompt
    assert "C-INF" not in prompt and "转行" not in prompt
    assert "我喜欢露营" in prompt

    saved = profile.get_profile(path)
    assert list(saved) == ["work", "city", "interests"]  # 按 DIMENSIONS 顺序
    assert saved["city"]["value"] == "住在杭州"
    assert saved["city"]["confidence"] == 0.9
    assert saved["city"]["status"] == "active"
    assert saved["city"]["evidence_memory_ids"] == ["UM-1"]
    assert saved["interests"]["evidence_memory_ids"] == ["C-1"]

    rows = _rows(path)
    assert len(rows) == 3
    assert {row["status"] for row in rows} == {"active"}
    by_dim = {row["dimension"]: row for row in rows}
    evidence_json = json.loads(by_dim["work"]["evidence_json"])
    assert evidence_json["memory_ids"] == ["UM-2"]
    assert evidence_json["items"][0]["source"] == "memory"
    assert evidence_json["items"][0]["text"] == "我在做后端开发"


# ---------------------------------------------------------------------------
# 没证据不编
# ---------------------------------------------------------------------------


def test_no_evidence_never_calls_llm_or_invents(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    payload = {"profile": [
        {"dimension": "city", "value": "杭州", "evidence_memory_ids": ["UM-1"]},
    ]}
    client = FakeClient(payload)
    result = profile.distill(path, client)

    assert result["ok"] is True
    assert result["written"] == 0
    assert result["evidence_count"] == 0
    assert "没有可用证据" in result["reason"]
    assert client.calls == []  # 没证据时根本不问模型
    assert profile.get_profile(path) == {}
    assert profile.render_profile(path) == ""
    assert _count(path) == 0

    # 只有自人格/世界记忆与推断 claim，同样不算证据
    _seed_memories(
        path,
        {"id": "UM-SELF", "scope": "self", "kind": "insight", "content": "我喜欢独处"},
        {"id": "UM-WORLD", "scope": "world", "kind": "insight", "content": "今天下雨"},
    )
    _seed_claims(
        path,
        {"id": "C-INF", "predicate": "想", "object": "转行",
         "claim_type": "inference", "evidence_span": "可能想转行",
         "source_level": "C"},
    )
    client2 = FakeClient(payload)
    result2 = profile.distill(path, client2)
    assert result2["ok"] is True and result2["written"] == 0
    assert client2.calls == []
    assert _count(path) == 0


# ---------------------------------------------------------------------------
# 证据硬校验
# ---------------------------------------------------------------------------


def test_items_without_valid_evidence_are_dropped(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_memories(path, {"id": "UM-1", "content": "我已婚"})
    payload = {
        "profile": [
            {"dimension": "city", "value": "杭州",
             "evidence_memory_ids": ["UM-404"], "confidence": 0.9},
            {"dimension": "work", "value": "后端", "confidence": 0.9},
            {"dimension": "salary", "value": "月薪三万",
             "evidence_memory_ids": ["UM-1"]},
            {"dimension": "family", "value": "已婚",
             "evidence_memory_ids": ["UM-1"], "confidence": 0.8},
        ]
    }
    result = profile.distill(path, FakeClient(payload))

    assert result["ok"] is True
    assert result["written"] == 1
    assert list(result["dimensions"]) == ["family"]
    reasons = {item["reason"] for item in result["skipped"]}
    assert reasons == {"no_evidence", "unknown_dimension"}
    skipped_dims = {item["dimension"] for item in result["skipped"]}
    assert skipped_dims == {"city", "work", "salary"}

    saved = profile.get_profile(path)
    assert list(saved) == ["family"]
    assert saved["family"]["evidence_memory_ids"] == ["UM-1"]
    rows = _rows(path)
    assert len(rows) == 1 and rows[0]["dimension"] == "family"


def test_uncertain_or_empty_items_are_skipped(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_memories(path, {"id": "UM-1", "content": "我在浙大读研，28 岁"})
    payload = {
        "profile": [
            {"dimension": "city", "value": "不确定",
             "evidence_memory_ids": ["UM-1"]},
            {"dimension": "work", "value": "可能在互联网",
             "evidence_memory_ids": ["UM-1"], "uncertain": True},
            {"dimension": "family", "value": "",
             "evidence_memory_ids": ["UM-1"]},
            {"dimension": "age", "value": "28 岁",
             "evidence_memory_ids": ["UM-1"], "status": "skip"},
            {"dimension": "school", "value": "在浙大读研",
             "evidence_memory_ids": ["UM-1"], "confidence": 0.85},
        ]
    }
    result = profile.distill(path, FakeClient(payload))

    assert result["ok"] is True
    assert result["written"] == 1
    assert list(result["dimensions"]) == ["school"]
    assert {item["dimension"] for item in result["skipped"]} == {
        "city", "work", "family", "age",
    }
    saved = profile.get_profile(path)
    assert list(saved) == ["school"]
    assert saved["school"]["value"] == "在浙大读研"


# ---------------------------------------------------------------------------
# 模型输出容错
# ---------------------------------------------------------------------------


def test_mapping_output_and_chinese_dimensions_are_accepted(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_memories(
        path,
        {"id": "UM-1", "content": "我住在杭州", "confidence": 0.9},
        {"id": "UM-2", "content": "周末去露营", "confidence": 0.7},
    )
    payload = {
        "dimensions": {
            "城市": {"value": "住在杭州", "evidence": ["[UM-1]"], "confidence": 0.85},
            "interests": {"value": "周末露营", "evidence_memory_ids": "UM-2"},
        }
    }
    result = profile.distill(path, FakeClient(payload))

    assert result["written"] == 2
    saved = profile.get_profile(path)
    assert list(saved) == ["city", "interests"]
    assert saved["city"]["evidence_memory_ids"] == ["UM-1"]
    assert saved["city"]["confidence"] == 0.85
    # 模型没给 confidence 时用证据置信度均值
    assert saved["interests"]["evidence_memory_ids"] == ["UM-2"]
    assert saved["interests"]["confidence"] == 0.7


def test_long_value_is_capped_and_status_is_preserved(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_memories(path, {"id": "UM-1", "content": "我说话很短"})
    payload = {"profile": [{
        "dimension": "style",
        "value": "短句" * 100,
        "evidence_memory_ids": ["UM-1"],
        "confidence": 0.66,
        "status": "pending",
    }]}
    result = profile.distill(path, FakeClient(payload))

    assert result["written"] == 1
    saved = profile.get_profile(path)["style"]
    assert len(saved["value"]) == profile.MAX_VALUE_CHARS == 120
    assert saved["confidence"] == 0.66
    assert saved["status"] == "pending"


def test_broken_llm_outputs_do_not_crash(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_memories(path, {"id": "UM-1", "content": "我住在杭州"})

    not_json = profile.distill(path, FakeClient("这不是 JSON"))
    assert not_json["ok"] is True and not_json["written"] == 0
    assert "LLM 没有给出可落库的维度" in not_json["reason"]

    no_items = profile.distill(path, FakeClient({"nope": []}))
    assert no_items["ok"] is True and no_items["written"] == 0

    failed = profile.distill(path, FakeClient({}, ok=False))
    assert failed["ok"] is False and failed["error"]

    boom = profile.distill(path, FakeClient(RuntimeError("boom")))
    assert boom["ok"] is False and "RuntimeError" in boom["error"]

    no_client = profile.distill(path, None)
    assert no_client["ok"] is False and no_client["written"] == 0

    offline = FakeClient({"profile": []}, available=False)
    offline_result = profile.distill(path, offline)
    assert offline_result["ok"] is False
    assert offline.calls == []

    assert _count(path) == 0
    assert profile.get_profile(path) == {}


# ---------------------------------------------------------------------------
# 版本与读回
# ---------------------------------------------------------------------------


def test_new_value_stales_old_version_and_repeat_is_idempotent(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    _seed_memories(path, {"id": "UM-1", "content": "我住在杭州"})

    first = profile.distill(path, FakeClient({"profile": [{
        "dimension": "city", "value": "住在杭州", "evidence_memory_ids": ["UM-1"],
    }]}))
    assert first["written"] == 1
    assert _count(path) == 1

    second = profile.distill(path, FakeClient({"profile": [{
        "dimension": "city", "value": "搬到上海", "evidence_memory_ids": ["UM-1"],
        "confidence": 0.9,
    }]}))
    assert second["written"] == 1
    rows = _rows(path)
    assert len(rows) == 2  # 旧版本留痕，不物理删除
    by_status = {row["status"]: row for row in rows}
    assert by_status["active"]["value"] == "搬到上海"
    assert by_status["stale"]["value"] == "住在杭州"

    saved = profile.get_profile(path)
    assert saved["city"]["value"] == "搬到上海"
    assert saved["city"]["status"] == "active"

    active_updated_at = by_status["active"]["updated_at"]
    third = profile.distill(path, FakeClient({"profile": [{
        "dimension": "city", "value": "搬到上海", "evidence_memory_ids": ["UM-1"],
        "confidence": 0.9,
    }]}))
    assert third["written"] == 1
    assert _count(path) == 2  # 同一结论不产生重复行
    same = [row for row in _rows(path) if row["status"] == "active"]
    assert len(same) == 1
    assert same[0]["updated_at"] >= active_updated_at


def test_get_profile_reads_legacy_evidence_json_and_skips_stale(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    con = db.connect(path)
    try:
        now = time.time()
        con.execute(
            "INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,"
            "status,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("UP-LEGACY", "city", "杭州", '["UM-1"]', 0.7, "active", now),
        )
        con.execute(
            "INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,"
            "status,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("UP-STALE", "city", "北京", '{"memory_ids":["UM-2"]}', 0.6,
             "stale", now + 100),
        )
        con.execute(
            "INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,"
            "status,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("UP-PENDING", "work", "后端",
             '{"items":[{"id":"C-1","source":"claim"}]}', 0.5, "pending", now + 50),
        )
        con.execute(
            "INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,"
            "status,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("UP-STR", "health", "睡眠一般", "UM-9", 0.6, "active", now + 10),
        )
        con.execute(
            "INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,"
            "status,updated_at) VALUES(?,?,?,?,?,?,?)",
            ("UP-FOREIGN", "salary", "三万", '["UM-3"]', 0.9, "active", now + 20),
        )
        con.commit()
    finally:
        con.close()

    saved = profile.get_profile(path)
    assert list(saved) == ["work", "city", "health"]  # 未知维度 salary 被忽略
    assert saved["city"]["value"] == "杭州"  # stale 的北京不出现
    assert saved["city"]["evidence_memory_ids"] == ["UM-1"]
    assert saved["work"]["status"] == "pending"
    assert saved["work"]["evidence_memory_ids"] == ["C-1"]
    assert saved["health"]["evidence_memory_ids"] == ["UM-9"]

    text = profile.render_profile(path)
    assert "职业/工作：后端" in text
    assert "城市/居住地：杭州" in text
