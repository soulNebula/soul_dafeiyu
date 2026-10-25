# -*- coding: utf-8 -*-
"""P1-a：记忆去重合并 + 重要性/访问衰减（防"越聊越脏、越聊越贵"）。

现状：每轮用户消息都抽一次记忆且没有去重/合并 → 近义记忆不断堆积；
检索只看最近 200 条 active → 超过 200 条后旧记忆静默检索不到。
本文件锁住：写入合并、importance 初始化、日衰减/休眠、检索打分与访问更新。
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from agi.selflearn import db, memory

DAY = 86400.0


class MemClient:
    """固定返回 items 的记忆抽取 client（不联网）。"""

    def __init__(self, items: list[dict]):
        self.items = items
        self.calls = 0

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
        return {"ok": True, "text": json.dumps({"items": self.items})}


def _item(kind: str, content: str, evidence: str, *, scope: str = "user",
          confidence: float = 0.8, importance: float | None = None) -> dict:
    item = {"scope": scope, "kind": kind, "content": content,
            "evidence": evidence, "confidence": confidence}
    if importance is not None:
        item["importance"] = importance
    return item


def _extract(path, items: list[dict], text: str, mid: str = "M-1"):
    return memory.extract_user_memories(path, MemClient(items), text, mid)


def _rows(path) -> list[dict]:
    return memory.list_user_memories(path, limit=100)


# --------------------------------------------------------------------------- #
# 迁移（旧库 ALTER 幂等）
# --------------------------------------------------------------------------- #
OLD_SCHEMA = """
CREATE TABLE user_memories(
  id TEXT PRIMARY KEY, scope TEXT NOT NULL DEFAULT 'user', kind TEXT NOT NULL,
  content TEXT NOT NULL, evidence TEXT NOT NULL,
  evidence_message_id TEXT, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  conflict_group TEXT, created_at REAL NOT NULL, UNIQUE(content)
);
"""


def test_migration_adds_columns_and_backfills(tmp_path):
    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    con.execute("INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("UM-old", "user", "preference", "用户喜欢猫", "我喜欢猫", 0.8, "active", 100.0))
    con.commit()
    con.close()

    con = db.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(user_memories)").fetchall()}
    assert {"importance", "access_count", "last_accessed", "evidence_history"} <= cols
    row = dict(con.execute("SELECT * FROM user_memories WHERE id='UM-old'").fetchone())
    assert row["content"] == "用户喜欢猫"            # 原数据保留
    assert row["importance"] == pytest.approx(0.7)   # 按 kind 回填（preference=0.7）
    assert row["access_count"] == 0 and row["last_accessed"] == 0.0
    assert json.loads(row["evidence_history"]) == ["我喜欢猫"]
    con.close()

    # 幂等：再次 connect 不重复加列、不改值
    con = db.connect(path)
    cols2 = {r[1] for r in con.execute("PRAGMA table_info(user_memories)").fetchall()}
    assert cols == cols2
    row2 = dict(con.execute("SELECT * FROM user_memories WHERE id='UM-old'").fetchone())
    assert row2 == row
    con.close()


def test_migration_very_old_table_without_scope(tmp_path):
    """极早期库没有 scope 列也要能迁移（先把 scope 补齐再建索引）。"""
    path = tmp_path / "ancient.sqlite3"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE user_memories("
                "id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL, "
                "evidence TEXT NOT NULL, evidence_message_id TEXT, confidence REAL NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL, UNIQUE(content))")
    con.commit()
    con.close()
    con = db.connect(path)
    cols = {r[1] for r in con.execute("PRAGMA table_info(user_memories)").fetchall()}
    assert "scope" in cols and "importance" in cols
    con.close()


# --------------------------------------------------------------------------- #
# 写入去重合并
# --------------------------------------------------------------------------- #
def test_same_content_merges_into_earliest_row(tmp_path):
    path = tmp_path / "merge.sqlite3"
    _extract(path, [_item("preference", "用户喜欢猫", "我喜欢猫", confidence=0.7)],
             "我喜欢猫，晚上也喜欢猫", "M-1")
    _extract(path, [_item("preference", "用户喜欢猫", "真的很喜欢猫", confidence=0.9)],
             "我真的很喜欢猫", "M-2")

    rows = _rows(path)
    assert len(rows) == 1, rows                        # 只留一行
    row = rows[0]
    assert row["content"] == "用户喜欢猫"
    assert row["confidence"] == pytest.approx(0.9)     # confidence=max
    assert row["access_count"] == 1                    # 合并算一次访问
    assert row["importance"] == pytest.approx(0.75)    # 0.7 + 0.05
    history = json.loads(row["evidence_history"])
    assert len(history) == 2 and history == ["我喜欢猫", "真的很喜欢猫"]


def test_similar_content_merges_by_jaccard(tmp_path):
    content_a = "用户喜欢晚上写东西"
    content_b = "用户喜欢晚上写东西啊"
    assert memory._jaccard(content_a, content_b) >= memory.MERGE_JACCARD
    path = tmp_path / "merge2.sqlite3"
    _extract(path, [_item("preference", content_a, "我喜欢晚上写东西")],
             "我喜欢晚上写东西", "M-1")
    _extract(path, [_item("preference", content_b, "我喜欢晚上写东西啊")],
             "我喜欢晚上写东西啊", "M-2")
    rows = _rows(path)
    assert len(rows) == 1
    # 保留最早的 content（新内容不比旧的长？两者长度 9/10，旧 <20 且新更长 → 允许替换）
    # 规则：旧 content <20 字且新更长时用新 content（更具体）
    assert rows[0]["content"] in (content_a, content_b)


def test_unrelated_content_does_not_merge(tmp_path):
    path = tmp_path / "nomerge.sqlite3"
    _extract(path, [_item("preference", "用户喜欢猫", "我喜欢猫")], "我喜欢猫", "M-1")
    _extract(path, [_item("preference", "用户养了一只狗", "我养了一只狗")],
             "我养了一只狗", "M-2")
    rows = _rows(path)
    assert len(rows) == 2


def test_dormant_rows_are_not_merge_targets(tmp_path):
    path = tmp_path / "dormant_merge.sqlite3"
    _extract(path, [_item("preference", "用户喜欢晚上写东西", "我喜欢晚上写东西")],
             "我喜欢晚上写东西", "M-1")
    con = db.connect(path)
    con.execute("UPDATE user_memories SET status='dormant', importance=0.1")
    con.commit()
    con.close()
    _extract(path, [_item("preference", "用户喜欢晚上写东西啊", "我喜欢晚上写东西啊")],
             "我喜欢晚上写东西啊", "M-2")
    rows = _rows(path)
    assert len(rows) == 2
    statuses = {r["status"] for r in rows}
    assert statuses == {"dormant", "active"}
    dormant = [r for r in rows if r["status"] == "dormant"][0]
    assert dormant["content"] == "用户喜欢晚上写东西"   # 休眠行原封不动


# --------------------------------------------------------------------------- #
# importance 初始化
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind,expected", [
    ("value", 0.8), ("plan", 0.8), ("preference", 0.7),
    ("insight", 0.6), ("fact", 0.5), ("event", 0.4),
])
def test_kind_importance_initialization(tmp_path, kind, expected):
    path = tmp_path / f"kind_{kind}.sqlite3"
    _extract(path, [_item(kind, f"用户{kind}内容", f"我{kind}内容")], f"我{kind}内容", "M-1")
    rows = _rows(path)
    assert rows and rows[0]["importance"] == pytest.approx(expected)


def test_llm_importance_wins_over_kind_default(tmp_path):
    path = tmp_path / "llm_imp.sqlite3"
    _extract(path, [_item("fact", "用户住在杭州", "我住在杭州", importance=0.93)],
             "我住在杭州", "M-1")
    assert _rows(path)[0]["importance"] == pytest.approx(0.93)


# --------------------------------------------------------------------------- #
# 衰减 / 休眠
# --------------------------------------------------------------------------- #
def _insert(path, mid, *, kind="fact", content, importance=0.5,
            last_accessed=0.0, created_at=0.0, status="active"):
    con = db.connect(path)
    con.execute(
        "INSERT INTO user_memories(id,scope,kind,content,evidence,confidence,status,"
        "created_at,importance,access_count,last_accessed,evidence_history) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, "user", kind, content, content, 0.8, status, created_at,
         importance, 0, last_accessed, json.dumps([content], ensure_ascii=False)))
    con.commit()
    con.close()


def test_decay_floor_and_dormant(tmp_path):
    path = tmp_path / "decay.sqlite3"
    now = 1_000_000_000.0
    _insert(path, "UM-old", content="旧记忆", importance=0.5,
            last_accessed=now - 200 * DAY, created_at=now - 200 * DAY)
    _insert(path, "UM-fresh", content="新记忆", importance=0.9,
            last_accessed=now - 1 * DAY, created_at=now - 200 * DAY)

    result = memory.decay(path, now=now, days=180)
    assert result["ok"] is True and result["dormant"] == 1
    rows = {r["id"]: r for r in _rows(path)}
    assert rows["UM-old"]["status"] == "dormant"             # 低重要 + 180 天未访问
    assert rows["UM-old"]["importance"] >= memory.IMPORTANCE_FLOOR
    assert rows["UM-old"]["importance"] == pytest.approx(memory.IMPORTANCE_FLOOR)
    assert rows["UM-fresh"]["status"] == "active"            # 最近访问过 → 不休眠
    assert rows["UM-fresh"]["importance"] >= memory.IMPORTANCE_FLOOR

    # 幂等：days=0 不再变化
    before = {r["id"]: r["importance"] for r in _rows(path)}
    memory.decay(path, now=now + 10, days=0)
    after = {r["id"]: r["importance"] for r in _rows(path)}
    assert before == after


def test_decay_without_override_uses_state_clock(tmp_path):
    path = tmp_path / "decay_clock.sqlite3"
    now = 1_000_000_000.0
    _insert(path, "UM-1", content="记忆", importance=0.8, last_accessed=now, created_at=now)
    memory.decay(path, now=now, days=0)          # 只写 state 时钟（不衰减）
    memory.decay(path, now=now + DAY)            # 经过 1 天 → ×0.98
    rows = _rows(path)
    assert rows[0]["importance"] == pytest.approx(0.8 * 0.98, rel=1e-3)


# --------------------------------------------------------------------------- #
# 检索打分 + 访问更新
# --------------------------------------------------------------------------- #
def test_retrieve_prefers_high_importance_with_same_overlap(tmp_path):
    path = tmp_path / "score.sqlite3"
    now = time.time()
    # 同一 query 下 overlap 相同（都命中「喜欢」「欢猫」等），importance 不同
    _insert(path, "UM-low", kind="fact", content="用户喜欢猫", importance=0.2,
            last_accessed=now, created_at=now)
    _insert(path, "UM-high", kind="fact", content="用户很喜欢猫", importance=0.95,
            last_accessed=now, created_at=now + 1)
    assert memory._jaccard("用户喜欢猫", "用户很喜欢猫") < memory.MERGE_JACCARD

    mems = memory.retrieve(path, "喜欢猫", k=5)
    assert [m["id"] for m in mems][:2] == ["UM-high", "UM-low"]

    # 命中后访问统计更新
    rows = {r["id"]: r for r in _rows(path)}
    assert rows["UM-high"]["access_count"] == 1
    assert rows["UM-low"]["access_count"] == 1
    assert rows["UM-high"]["last_accessed"] > 0


def test_retrieve_excludes_dormant(tmp_path):
    path = tmp_path / "dormant_retrieve.sqlite3"
    now = time.time()
    _insert(path, "UM-dormant", kind="fact", content="用户喜欢猫", importance=0.1,
            last_accessed=now, created_at=now, status="dormant")
    _insert(path, "UM-active", kind="fact", content="用户喜欢狗", importance=0.6,
            last_accessed=now, created_at=now)
    ids = [m["id"] for m in memory.retrieve(path, "喜欢", k=5)]
    assert ids == ["UM-active"]


# --------------------------------------------------------------------------- #
# 兼容性：forget / correct / export_brain
# --------------------------------------------------------------------------- #
def test_forget_and_correct_keep_working_with_new_columns(tmp_path):
    path = tmp_path / "compat.sqlite3"
    _extract(path, [_item("fact", "用户住在杭州", "我住在杭州")], "我住在杭州", "M-1")
    row = _rows(path)[0]
    corrected = memory.correct(path, row["id"], "用户住在上海", "我搬到上海了")
    assert corrected["ok"] is True
    rows = _rows(path)
    assert len(rows) == 2
    new = [r for r in rows if r["id"] == corrected["id"]][0]
    assert new["status"] == "active" and new["evidence_history"]
    old = [r for r in rows if r["id"] == row["id"]][0]
    assert old["status"] == "invalid"

    assert memory.forget(path, corrected["id"])["ok"] is True
    exported = memory.export_brain(path)
    assert "user_memories" in exported["tables"]
    keys = exported["tables"]["user_memories"][0].keys()
    assert "importance" in keys and "evidence_history" in keys
