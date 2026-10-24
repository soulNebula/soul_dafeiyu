# -*- coding: utf-8 -*-
"""W4-A 灵魂蒸馏数据集单测：mock 生成 / schema 字段 / 断点续跑 / 匿名扫描 /
behavior 归一化 / state_profile 变体 / 合成模板唯一性 / train-val 切分。

只跑 mock：不联网、不花钱、不写 ``agi/data``（产物全在 ``tmp_path``）。
"""
from __future__ import annotations

import itertools
import json

import pytest

from agi.inner import schema
from agi.soul import dataset

REQUIRED_KEYS = {"id", "source", "state_profile", "context", "user_msg",
                 "target", "teacher", "ts"}
MAX_MONOLOGUE = schema.MAX_MONOLOGUE


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


@pytest.fixture
def fake_golden(tmp_path):
    rows = [
        {"id": "G-001", "bucket": "work",
         "context": [{"role": "user", "text": "脚本传上去了"}],
         "user_msg": "你看看"},
        {"id": "G-002", "bucket": "low_mood",
         "context": [], "user_msg": "今天很累"},
    ]
    path = tmp_path / "golden.jsonl"
    _write_jsonl(path, rows)
    return path


@pytest.fixture
def fake_log(tmp_path):
    """构造一段真实格式的聊天日志：含昵称、手机号、微信号、本地路径、X工 称呼。"""
    rows = [
        {"friend": "阿珍", "kind": "text", "sender": "friend", "text": "你好呀", "ts": 1},
        {"friend": "阿珍", "kind": "text", "sender": "self", "text": "在的，什么事", "ts": 2},
        {"friend": "阿珍", "kind": "text", "sender": "friend",
         "text": "我手机号 13812345678，微信 wxid_abc123，"
                 "文件在 C:\\Users\\Administrator\\Desktop\\a.txt", "ts": 3},
        {"friend": "阿珍", "kind": "text", "sender": "friend", "text": "刘工那边怎么说", "ts": 4},
        {"friend": "阿珍", "kind": "text", "sender": "self", "text": "我晚点问问他", "ts": 5},
        {"friend": "阿珍", "kind": "text", "sender": "friend", "text": "记得把资料发我", "ts": 6},
        {"friend": "阿珍", "kind": "text", "sender": "self", "text": "好", "ts": 7},
        {"friend": "阿珍", "kind": "text", "sender": "friend", "text": "那我先下班了", "ts": 8},
    ]
    path = tmp_path / "messages.jsonl"
    _write_jsonl(path, rows)
    return path


def _build(tmp_path, golden, log, limit):
    return dataset.build_items(limit, golden_path=golden, message_log=log)


# ---------------------------------------------------------------- mock 生成


def test_mock_generates_valid_inner_state(tmp_path):
    out = tmp_path / "soul.jsonl"
    assert dataset.main(["--mock", "--limit", "5", "--out", str(out)]) == 0
    rows = _read_jsonl(out)
    assert len(rows) == 5
    for row in rows:
        assert set(row) == REQUIRED_KEYS
        assert row["teacher"] == "mock"
        assert row["source"] in ("golden", "messages", "synthetic")
        assert row["state_profile"] in dataset.PROFILE_NAMES
        assert isinstance(row["user_msg"], str) and row["user_msg"]
        assert row["context"] and row["context"][0]["role"] == "system"
        target = row["target"]
        assert set(target) == set(dataset.TARGET_FIELDS)
        assert set(target["behavior"]) == set(dataset.BEHAVIOR_FIELDS)
        # 落盘 behavior 必须就是 schema 当前枚举（中文偶尔/不用）
        assert target["behavior"]["emoji"] in schema.EMOJI
        assert target["behavior"]["length"] in schema.LENGTHS
        state = schema.validate(target)  # 非法会抛 ValueError
        assert len(state.monologue) <= MAX_MONOLOGUE
        assert 0.0 <= state.mood <= 1.0
        assert 0.0 <= state.trust <= 1.0
        assert isinstance(state.behavior["ask"], bool)


# ---------------------------------------------------------------- 断点续跑


def test_resume_does_not_duplicate(tmp_path):
    out = tmp_path / "soul.jsonl"
    args = ["--mock", "--limit", "8", "--out", str(out)]
    assert dataset.main(args) == 0
    first = _read_jsonl(out)
    assert len(first) == 8
    # 同一条命令再跑：已完成的 id 全部跳过，文件不变长、不重复
    assert dataset.main(args) == 0
    second = _read_jsonl(out)
    assert len(second) == len(first)
    assert [r["id"] for r in second] == [r["id"] for r in first]
    assert len({r["id"] for r in second}) == len(second)


def test_resume_skips_failed_ledger(tmp_path, fake_golden, fake_log):
    class BadTeacher:
        model = "bad"

        def __init__(self):
            self.calls = 0

        def available(self):
            return True

        def chat(self, messages, **_kwargs):
            self.calls += 1
            return {"ok": True, "text": "这不是 JSON", "usage": {}}

    out = tmp_path / "soul.jsonl"
    items = _build(tmp_path, fake_golden, fake_log, 2)
    bad = BadTeacher()
    result = dataset.generate(items, bad, out)
    assert result["written"] == 0
    assert result["failed"] == 2
    assert bad.calls == 4  # 每条失败后重试 1 次
    failures = _read_jsonl(dataset.failures_path_for(out))
    assert len(failures) == 2
    assert all(f["attempts"] == 2 for f in failures)

    # 第二次跑同一批：失败台账里的 id 默认跳过，不再花钱
    again = BadTeacher()
    dataset.generate(items, again, out)
    assert again.calls == 0


# ---------------------------------------------------------------- 匿名扫描


def test_anonymized_windows_have_no_private_data(tmp_path, fake_golden, fake_log):
    out = tmp_path / "soul.jsonl"
    items = _build(tmp_path, fake_golden, fake_log, 4)
    assert any(item["source"] == "messages" for item in items)
    dataset.generate(items, dataset.MockTeacher(), out)

    text = out.read_text(encoding="utf-8")
    assert dataset.privacy_violations(text) == []
    for leaked in ("阿珍", "刘工", "wxid_", "13812345678", "Administrator"):
        assert leaked not in text
    assert any(ph in text for ph in ("[号码]", "[微信号]", "[本地路径]", "[用户]"))
    assert all(len(r["target"]["monologue"]) <= MAX_MONOLOGUE for r in _read_jsonl(out))


# ---------------------------------------------------------------- behavior 归一化


def test_normalize_behavior_maps_to_schema_enum():
    assert dataset.normalize_behavior({"emoji": "不用"})["emoji"] == "不用"
    assert dataset.normalize_behavior({"emoji": "偶尔"})["emoji"] == "偶尔"
    # 英文三值（未来 schema 迁移用）也要能收敛到当前 schema 枚举
    assert dataset.normalize_behavior({"emoji": "none"})["emoji"] == "不用"
    assert dataset.normalize_behavior({"emoji": "rare"})["emoji"] == "偶尔"
    assert dataset.normalize_behavior({"emoji": "normal"})["emoji"] == "偶尔"
    # 未知值回退默认（不用）
    assert dataset.normalize_behavior({"emoji": "随便"})["emoji"] == "不用"
    assert dataset.normalize_behavior({})["emoji"] == "不用"
    # length：近义/非法值修正
    assert dataset.normalize_behavior({"length": "short"})["length"] == "short"
    assert dataset.normalize_behavior({"length": "短"})["length"] == "short"
    assert dataset.normalize_behavior({"length": "medium"})["length"] == "normal"
    assert dataset.normalize_behavior({"length": "whatever"})["length"] == "normal"
    assert dataset.normalize_behavior("not a dict")["length"] == "normal"


def test_coerce_target_corrects_bad_behavior():
    base = {
        "mood": 0.5, "energy": 0.6, "sharing": 0.4, "battery": 0.7, "trust": 0.5,
        "stage": "熟悉", "stance": "平和", "intent": "自然接话",
        "monologue": "先听他说完",
        "behavior": {"length": "invalid", "ask": "yes", "advice": "no",
                     "emoji": "rare"},
    }
    target = dataset.coerce_target(base)
    assert target["behavior"] == {"length": "normal", "ask": True,
                                  "advice": False, "emoji": "偶尔"}
    schema.validate(target)  # 100% 符合 schema

    with pytest.raises(ValueError):
        dataset.coerce_target({**base, "monologue": "字" * (MAX_MONOLOGUE + 1)})
    with pytest.raises(ValueError):
        dataset.coerce_target({**base, "monologue": "你必须马上回我"})
    with pytest.raises(ValueError):
        dataset.coerce_target({k: v for k, v in base.items() if k != "trust"})


def test_normalize_dataset_writes_norm_file(tmp_path):
    # 模拟 v0：中文 behavior 合法；混一条英文三值，归一化后必须全部过 schema
    rows = []
    for index in range(3):
        rows.append({
            "id": f"S-G-work-{index + 1:04d}", "source": "golden",
            "context": [{"role": "system", "text": "角色设定"}],
            "user_msg": "你看看",
            "target": {
                "mood": 0.5, "energy": 0.6, "sharing": 0.4, "battery": 0.7,
                "trust": 0.5, "stage": "熟悉", "stance": "平和", "intent": "自然接话",
                "monologue": "先听他说完",
                "behavior": {"length": "short", "ask": True, "advice": False,
                             "emoji": ("不用", "偶尔", "rare")[index]},
            },
            "teacher": "deepseek-flash", "ts": "2026-10-25T00:00:00+08:00",
        })
    source = tmp_path / "soul_sft_v0.jsonl"
    _write_jsonl(source, rows)
    before = source.read_bytes()

    result = dataset.normalize_dataset(source)
    norm = tmp_path / "soul_sft_v0_norm.jsonl"
    assert norm.exists()
    assert result["written"] == 3 and result["skipped"] == 0
    assert source.read_bytes() == before  # 原文件不被覆盖
    out_rows = _read_jsonl(norm)
    assert len(out_rows) == 3
    for row in out_rows:
        schema.validate(row["target"])  # 100% 符合 schema
        assert row["target"]["behavior"]["emoji"] in schema.EMOJI
        assert row["target"]["behavior"]["length"] in schema.LENGTHS


# ---------------------------------------------------------------- 配额 / 变体


def test_plan_counts_respects_synthetic_cap():
    plan = dataset.plan_counts(2000, 174)
    assert plan["real"] + plan["synthetic"] == 2000
    assert plan["synthetic"] <= dataset.SYNTHETIC_MAX
    assert plan["synthetic"] <= 2000 // 2
    small = dataset.plan_counts(20, 174)
    assert small["real"] == 20 and small["synthetic"] == 0


def test_state_profile_variants_multiply_samples(tmp_path):
    out = tmp_path / "soul.jsonl"
    assert dataset.main(["--mock", "--limit", "400", "--out", str(out)]) == 0
    rows = _read_jsonl(out)
    assert len(rows) == 400
    profiles = {r["state_profile"] for r in rows}
    assert profiles == set(dataset.PROFILE_NAMES)
    # 同一 (user_msg, context) 至少有一条带多个 state_profile 的变体
    variants = {}
    for row in rows:
        key = (row["user_msg"], json.dumps(row["context"][1:], ensure_ascii=False))
        variants.setdefault(key, set()).add(row["state_profile"])
    assert any(len(v) > 1 for v in variants.values())
    # 去重键 (user_msg, context 摘要, state_profile) 全局唯一
    keys = [(r["user_msg"], json.dumps(r["context"][1:], ensure_ascii=False),
             r["state_profile"]) for r in rows]
    assert len(keys) == len(set(keys))


def test_synthetic_templates_are_diverse():
    specs = dataset.SYNTHETIC_TEMPLATE_SPECS
    assert set(specs) == set(dataset.BUCKETS)
    for bucket, spec in specs.items():
        combos = (len(spec["A"]) * len(spec["B"])
                  * len(spec["C"]) * len(spec["patterns"]))
        assert combos >= 2000, f"{bucket} 组合数不足: {combos}"
        assert all(dataset.privacy_violations(msg) == []
                   for msg in itertools.chain(spec["A"], spec["B"], spec["C"]))
    # 合成池前 2000 条里 unique user_msg ≥1800（个别短句模板会重复，
    # 真正入数据集时 builder 还会按 user_msg 再去重）
    seeds = list(itertools.islice(dataset.synthetic_seeds(), 2000))
    assert len(seeds) == 2000
    messages = [seed["user_msg"] for seed in seeds]
    assert len(set(messages)) >= 1800
    assert {seed["bucket"] for seed in seeds} == set(dataset.BUCKETS)


# ---------------------------------------------------------------- split


def test_split_keeps_sessions_together_and_is_reproducible(tmp_path):
    out = tmp_path / "soul.jsonl"
    assert dataset.main(["--mock", "--limit", "120", "--out", str(out), "--split"]) == 0
    rows = _read_jsonl(out)
    assert len(rows) == 120
    train_path = out.with_name(out.stem + "_train.jsonl")
    val_path = out.with_name(out.stem + "_val.jsonl")
    train = _read_jsonl(train_path)
    val = _read_jsonl(val_path)
    assert len(train) + len(val) == len(rows)
    assert {r["id"] for r in train}.isdisjoint({r["id"] for r in val})
    assert {r["id"] for r in train} | {r["id"] for r in val} == {r["id"] for r in rows}
    assert 0 < len(val) < len(rows)
    # 同一会话（id 前缀分组）不跨集
    train_groups = {dataset.split_group_id(r["id"]) for r in train}
    val_groups = {dataset.split_group_id(r["id"]) for r in val}
    assert train_groups.isdisjoint(val_groups)
    # 再切一次结果 byte 级一致
    first_train = train_path.read_bytes()
    first_val = val_path.read_bytes()
    dataset.write_split(out)
    assert train_path.read_bytes() == first_train
    assert val_path.read_bytes() == first_val
