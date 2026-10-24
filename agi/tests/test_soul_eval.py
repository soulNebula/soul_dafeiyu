# -*- coding: utf-8 -*-
"""W4-C1 评测脚本单测：不加载模型（CI 可跑），只锁指标函数与 prompt 一致性。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agi.soul import evaluate

ROOT = Path(__file__).resolve().parents[2]
VAL_PATH = ROOT / "agi" / "soul" / "data" / "soul_sft_v0_val.jsonl"
MODEL_PATH = Path(r"E:\agent\models\Qwen3.5-2B")


# --------------------------------------------------------------------------- #
# 测试数据
# --------------------------------------------------------------------------- #
def _target(**overrides) -> dict:
    target = {
        "mood": 0.5, "energy": 0.4, "sharing": 0.3, "battery": 0.5, "trust": 0.5,
        "stage": "熟悉", "stance": "平稳肯定，带点默契",
        "intent": "确认收到进度，简单回应", "monologue": "先接住",
        "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "偶尔"},
    }
    target.update(overrides)
    return target


def _pred(match_target: bool = True, **overrides) -> dict:
    pred = {
        "mood": 0.5, "energy": 0.4, "sharing": 0.3, "battery": 0.5, "trust": 0.5,
        "stage": "熟悉", "stance": "平稳肯定，带点默契",
        "intent": "确认收到进度，简单回应", "monologue": "先接住",
        "behavior": {"length": "short", "ask": False, "advice": False, "emoji": "偶尔"},
    }
    if not match_target:
        pred["behavior"] = {"length": "long", "ask": True, "advice": True, "emoji": "不用"}
        pred["stance"] = "冷淡"
        pred["intent"] = "换个话题"
    pred.update(overrides)
    return pred


def _record(parsed_ok: bool, match_all: bool, fail: str = "") -> dict:
    target = _target()
    pred = _pred(match_target=match_all) if parsed_ok else None
    return {
        "id": "X", "parsed_ok": parsed_ok, "fail": fail,
        "field_match": evaluate.compare_fields(pred, target),
    }


# --------------------------------------------------------------------------- #
# G1：格式
# --------------------------------------------------------------------------- #
def test_parse_valid_json_passes_g1():
    raw = json.dumps(_pred(), ensure_ascii=False)
    out = evaluate.check_format(raw)
    assert out["parsed_ok"] is True
    assert out["fail"] == ""
    assert isinstance(out["pred"], dict)


def test_parse_invalid_json_is_not_json():
    out = evaluate.check_format("我觉得应该回得温和一点")
    assert out["parsed_ok"] is False
    assert out["fail"] == "not_json"
    assert out["fail_detail"]


def test_truncated_json_classified_as_truncated():
    out = evaluate.check_format('{"mood": 0.5, "energy": 0.4', hit_max_tokens=True)
    assert out["parsed_ok"] is False
    assert out["fail"] == "truncated"


def test_empty_output_is_empty():
    for raw in ("", "   \n"):
        out = evaluate.check_format(raw)
        assert out["parsed_ok"] is False
        assert out["fail"] == "empty"


def test_schema_invalid_field_fails_g1():
    import json as _json
    out = evaluate.check_format(_json.dumps({"mood": "很高"}))
    assert out["parsed_ok"] is False
    assert out["fail"] == "schema_invalid"

    out = evaluate.check_format(_json.dumps([1, 2, 3]))
    assert out["parsed_ok"] is False and out["fail"] == "schema_invalid"


def test_schema_accepts_english_emoji_alias():
    # G1 走 schema 严格校验；中文枚举之外的英文别名在 G1 会失败，G2 才归一（见下）
    out = evaluate.check_format(json.dumps(_pred(behavior={
        "length": "short", "ask": False, "advice": False, "emoji": "rare"})))
    assert out["parsed_ok"] is False and out["fail"] == "schema_invalid"


# --------------------------------------------------------------------------- #
# G2：字段归一 / 宽松匹配
# --------------------------------------------------------------------------- #
def test_emoji_alias_normalization():
    target = _target()
    for alias in ("偶尔", "RARE", "rare", "occasionally", "sometimes", "少量", "yes"):
        pred = _pred(behavior={"length": "short", "ask": False,
                               "advice": False, "emoji": alias})
        assert evaluate.compare_fields(pred, target)["emoji"] is True, alias
    for alias in ("不用", "none", "no", "never", "", "无"):
        target2 = _target(behavior={"length": "short", "ask": False,
                                    "advice": False, "emoji": "不用"})
        pred = _pred(behavior={"length": "short", "ask": False,
                               "advice": False, "emoji": alias})
        assert evaluate.compare_fields(pred, target2)["emoji"] is True, alias
    # 归一后不同 → False
    other = _target(behavior={"length": "short", "ask": False,
                              "advice": False, "emoji": "不用"})
    pred = _pred(behavior={"length": "short", "ask": False,
                           "advice": False, "emoji": "rare"})
    assert evaluate.compare_fields(pred, other)["emoji"] is False


def test_length_and_bool_normalization():
    target = _target()
    pred = _pred(behavior={"length": "SHORT", "ask": "false", "advice": 0,
                           "emoji": "偶尔"})
    match = evaluate.compare_fields(pred, target)
    assert match["length"] is True and match["ask"] is True and match["advice"] is True
    # 短/长 也是合法别名
    match2 = evaluate.compare_fields(
        _pred(behavior={"length": "短", "ask": "no", "advice": False, "emoji": "偶尔"}),
        target)
    assert match2["length"] is True and match2["ask"] is True
    # 布尔值非法 → 不匹配
    bad = evaluate.compare_fields(
        _pred(behavior={"length": "short", "ask": "可能吧",
                        "advice": False, "emoji": "偶尔"}), target)
    assert bad["ask"] is False


def test_stance_strict_and_loose():
    target = _target()
    # 完全一致：严格/宽松都真
    m = evaluate.compare_fields(_pred(), target)
    assert m["stance_strict"] is True and m["stance_loose"] is True
    # 关键词大量重合但文字不同：严格假、宽松真
    m2 = evaluate.compare_fields(_pred(stance="平稳，简短"), target)
    assert m2["stance_strict"] is False
    assert m2["stance_loose"] is True
    # 完全不搭：都假
    m3 = evaluate.compare_fields(_pred(stance="暴躁反问"), target)
    assert m3["stance_strict"] is False and m3["stance_loose"] is False


def test_intent_loose_matches_partial_overlap():
    target = _target()
    m = evaluate.compare_fields(_pred(intent="确认收到进度"), target)
    assert m["intent_strict"] is False and m["intent_loose"] is True
    m2 = evaluate.compare_fields(_pred(intent="提议出去吃饭"), target)
    assert m2["intent_loose"] is False


def test_loose_match_edge_cases():
    assert evaluate.loose_match("", "") is False
    assert evaluate.loose_match("平和", "") is False
    assert evaluate.loose_match("", "平和") is False
    assert evaluate.loose_match("平稳简短", "平稳") is True
    assert evaluate.loose_match("abc def", "ABC") is True


def test_empty_or_invalid_pred_has_all_false_matches():
    target = _target()
    match = evaluate.compare_fields(None, target)
    assert set(match) == set(evaluate.FIELD_RATE_KEYS)
    assert not any(match.values())
    match2 = evaluate.compare_fields({}, target)
    assert not any(match2.values())


def test_behavior_only_and_all5_definitions():
    assert evaluate.BEHAVIOR_FIELDS == ("length", "ask", "advice", "emoji")
    assert set(evaluate.ALL5_FIELDS) == {"stance_strict", "intent_strict",
                                       "length", "ask", "advice"}
    assert "emoji" not in evaluate.ALL5_FIELDS


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def test_summarize_rates_and_top_fail():
    target = _target()
    partial = {  # 只有 length 对
        "parsed_ok": True, "fail": "",
        "field_match": {k: False for k in evaluate.FIELD_RATE_KEYS} | {"length": True},
    }
    records = [
        _record(True, True),                       # 全对
        _record(True, True),                       # 全对
        partial,                                   # 只 length 对
        _record(False, False, fail="not_json"),
        _record(False, False, fail="not_json"),
        _record(False, False, fail="truncated"),
    ]
    summary = evaluate.summarize(records)
    n = 6
    assert summary["G1"]["valid_rate"] == round(3 / n, 4)
    assert summary["G1"]["valid"] == 3
    assert summary["G1"]["top_fail"] == {"not_json": 2, "truncated": 1}
    g2 = summary["G2"]
    assert set(g2["field_rates"]) == set(evaluate.FIELD_RATE_KEYS)
    assert g2["field_rates"]["length"] == round(3 / n, 4)   # 2 全对 + 1 只对 length
    assert g2["field_rates"]["stance_strict"] == round(2 / n, 4)
    assert g2["behavior_only"] == round(2 / n, 4)           # 只有 2 条 behavior 全对
    assert g2["all5"] == round(2 / n, 4)
    assert g2["all6"] == round(2 / n, 4)
    # 分母包含格式失败项：所有 rate 都不会超过 valid_rate + 0
    assert summary["G2"]["all5"] <= summary["G1"]["valid_rate"]


def test_summarize_empty():
    summary = evaluate.summarize([])
    assert summary["G1"]["valid_rate"] == 0.0
    assert summary["G1"]["top_fail"] == {}
    assert summary["G2"]["all5"] == 0.0


# --------------------------------------------------------------------------- #
# prompt 构造与 tools/train/smoke_lora.py 一致
# --------------------------------------------------------------------------- #
def _smoke_module():
    root = str(ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    pytest.importorskip("tools.train.smoke_lora")
    from tools.train import smoke_lora  # noqa: WPS433

    return smoke_lora


def _val_items(n: int = 5) -> list[dict]:
    if not VAL_PATH.exists():
        pytest.skip(f"缺 v0_val：{VAL_PATH}")
    return evaluate.load_items(VAL_PATH, n)


def test_build_messages_matches_smoke_on_real_rows():
    smoke = _smoke_module()
    items = _val_items(10)
    assert items
    for item in items:
        assert evaluate.build_messages(item) == smoke.build_messages(item), item.get("id")


def test_build_messages_matches_smoke_on_edge_cases():
    smoke = _smoke_module()
    cases = [
        {  # assistant 结尾 → 兜底改成 user
            "id": "E1", "context": [{"role": "system", "text": "SYS"},
                                    {"role": "assistant", "text": "在呢"}],
            "user_msg": "", "target": _target(),
        },
        {  # context 已含与 user_msg 相同的最后一句 → 不重复
            "id": "E2", "context": [{"role": "system", "text": "SYS"},
                                    {"role": "user", "text": "你好"}],
            "user_msg": "你好", "target": _target(),
        },
        {  # 没有 system → 用默认人格
            "id": "E3", "context": [{"role": "user", "text": "嗨"}],
            "user_msg": "嗨", "target": _target(),
        },
    ]
    for item in cases:
        assert evaluate.build_messages(item) == smoke.build_messages(item), item["id"]


def test_prompt_field_takes_precedence():
    item = {"id": "P1", "prompt": "现成的完整 prompt",
            "context": [{"role": "system", "text": "SYS"}],
            "user_msg": "hi", "target": _target()}
    msgs = evaluate.messages_for_item(item)
    assert msgs == [{"role": "user", "content": "现成的完整 prompt"}]


def test_infer_split():
    assert evaluate.infer_split("soul_sft_v1_val.jsonl", {}) == "val"
    assert evaluate.infer_split("soul_sft_v1_train.jsonl", {}) == "train"
    assert evaluate.infer_split("foo.jsonl", {"split": "test"}) == "test"
    assert evaluate.infer_split("foo.jsonl", {}) == "unknown"


def test_prompt_token_ids_match_smoke_with_tokenizer():
    pytest.importorskip("transformers")
    if not (MODEL_PATH / "config.json").exists():
        pytest.skip(f"缺本地模型目录：{MODEL_PATH}")
    from transformers import AutoTokenizer

    smoke = _smoke_module()
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL_PATH), trust_remote_code=True)
    for item in _val_items(3):
        messages, answer = evaluate.build_messages(item)
        text, ids = evaluate.build_prompt_text(tokenizer, messages)
        smoke_enc = smoke.encode_sample(tokenizer, messages, answer, max_len=512)
        # smoke 会按 max_len 截断，但 v0 样本 prompt 远小于预算 → 两者应完全一致
        assert ids == smoke_enc["input_ids"][:len(ids)], item.get("id")
        assert len(ids) == smoke_enc["prompt_tokens"], item.get("id")
        # 再确认字符串级一致（同一 tokenizer/模板）
        assert text == evaluate.apply_chat(tokenizer, messages)


# --------------------------------------------------------------------------- #
# dry-run（不加载模型权重）
# --------------------------------------------------------------------------- #
def test_dry_run_base_path(tmp_path):
    if not VAL_PATH.exists():
        pytest.skip(f"缺 v0_val：{VAL_PATH}")
    code = evaluate.main([
        "--data", str(VAL_PATH), "--limit", "2", "--dry-run",
        "--out", str(tmp_path / "dry.json"),
    ])
    assert code == 0
    assert not (tmp_path / "dry.json").exists()  # dry-run 不写结果文件


def test_dry_run_rejects_bad_adapter(tmp_path, capsys):
    if not VAL_PATH.exists():
        pytest.skip(f"缺 v0_val：{VAL_PATH}")
    code = evaluate.main([
        "--data", str(VAL_PATH), "--limit", "2", "--dry-run",
        "--adapter", str(tmp_path / "not_exist"),
        "--out", str(tmp_path / "dry.json"),
    ])
    assert code == 2
    assert "adapter" in capsys.readouterr().err


def test_missing_data_returns_2(tmp_path):
    code = evaluate.main(["--data", str(tmp_path / "nope.jsonl"), "--dry-run"])
    assert code == 2


def test_criteria_declares_behavior_as_hard_metric():
    criteria = evaluate.G2_CRITERIA
    assert criteria["hard_metric"] == "behavior_only"
    assert criteria["hard_fields"] == list(evaluate.BEHAVIOR_FIELDS)
    assert criteria["stance_intent_primary"] == "loose"
    assert criteria["stance_intent_reference"] == "strict"
    assert "emoji" not in criteria["all5_fields"]


def test_vision_defaults_to_keep_and_offload_is_opt_in():
    parser = evaluate.build_parser()
    args = parser.parse_args(["--data", "x.jsonl"])
    assert args.offload_vision is False  # 推理默认保留视觉塔（6GB 够用）
    args2 = parser.parse_args(["--data", "x.jsonl", "--offload-vision"])
    assert args2.offload_vision is True
    assert "--offload-vision" in parser.format_help()


def test_load_items_limit_zero_means_all(tmp_path):
    path = tmp_path / "rows.jsonl"
    rows = [{"id": f"X{i}", "context": [], "user_msg": "", "target": _target()}
            for i in range(4)]
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")
    assert len(evaluate.load_items(path, 0)) == 4      # 0 = 全部（v1 正式评估路径）
    assert len(evaluate.load_items(path, 2)) == 2
    assert len(evaluate.load_items(path)) == 4


def test_dry_run_limit_zero_path(tmp_path):
    """--limit 0（全量）不能在建 prompt 阶段崩；正式 v1 评估要走这条。"""
    if not VAL_PATH.exists():
        pytest.skip(f"缺 v0_val：{VAL_PATH}")
    code = evaluate.main([
        "--data", str(VAL_PATH), "--limit", "0", "--dry-run",
        "--out", str(tmp_path / "dry.json"),
    ])
    assert code == 0


# --------------------------------------------------------------------------- #
# 断点续跑 / 汇总落盘
# --------------------------------------------------------------------------- #
def test_load_resume_records_and_select_todo(tmp_path):
    out = tmp_path / "eval.json"
    assert evaluate.load_resume_records(out) == []
    payload = {"items": [{"id": "A"}, {"id": "B"}, {"no_id": True}], "n": 2}
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    recs = evaluate.load_resume_records(out)
    assert [r["id"] for r in recs if r.get("id")] == ["A", "B"]
    todo = evaluate.select_todo([{"id": "A"}, {"id": "B"}, {"id": "C"}], recs)
    assert [i["id"] for i in todo] == ["C"]
    # 没有 id 的样本一律重跑
    assert len(evaluate.select_todo([{"foo": 1}], recs)) == 1
    out.write_text("{broken", encoding="utf-8")
    assert evaluate.load_resume_records(out) == []


def test_build_payload_keys_and_resume_flag():
    class _Cuda:
        @staticmethod
        def max_memory_allocated():
            return 1024 ** 3

        @staticmethod
        def max_memory_reserved():
            return 2 * 1024 ** 3

    class _Torch:
        cuda = _Cuda

    args = evaluate.build_parser().parse_args(["--data", "x.jsonl", "--limit", "2"])
    records = [_record(True, True), _record(False, False, fail="not_json")]
    payload = evaluate.build_payload(
        records, args=args, adapter="", model_path=Path("m"),
        data_path=Path("d.jsonl"), elapsed=3.0, torch=_Torch, split="val", resumed=1)
    assert payload["n"] == 2 and payload["resumed"] == 1
    assert payload["adapter"] is None
    assert payload["peak_alloc_gb"] == 1.0 and payload["peak_reserved_gb"] == 2.0
    assert payload["per_item_s"] == 1.5
    assert set(payload) >= {"n", "adapter", "temperature", "G1", "G2",
                            "elapsed_s", "criteria", "items"}
    assert payload["G2"]["field_rates"]["length"] == 0.5
    assert payload["criteria"]["hard_metric"] == "behavior_only"
