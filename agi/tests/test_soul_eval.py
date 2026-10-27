# -*- coding: utf-8 -*-
"""W4-C1 评测脚本单测：不加载模型（CI 可跑），只锁指标函数与 prompt 一致性。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
import sys as _sys
from pathlib import Path as _Path
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))
from agi.paths import agent_path


import pytest

from agi.soul import evaluate

ROOT = Path(__file__).resolve().parents[2]
VAL_PATH = ROOT / "agi" / "soul" / "data" / "soul_sft_v0_val.jsonl"
MODEL_PATH = agent_path(r"models\Qwen3.5-2B")


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
        "target": target, "pred": pred,
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
    # 新增主口径 balanced：结构齐全（这个 fixture 的 target 只有单一类别，均值有值即可）
    assert "balanced" in g2 and g2["balanced"]["n"] == n
    assert set(g2["balanced"]) >= {"ask_bal", "emoji_bal", "length_minority_recall",
                                   "advice_minority_recall", "mean", "majority_baselines"}
    assert g2["balanced"]["mean"] is not None
    # 分母包含格式失败项：所有 rate 都不会超过 valid_rate + 0
    assert summary["G2"]["all5"] <= summary["G1"]["valid_rate"]


def test_summarize_empty():
    summary = evaluate.summarize([])
    assert summary["G1"]["valid_rate"] == 0.0
    assert summary["G1"]["top_fail"] == {}
    assert summary["G2"]["all5"] == 0.0
    assert summary["G2"]["balanced"]["mean"] is None


# --------------------------------------------------------------------------- #
# balanced 主口径（balanced accuracy，类别不平衡）
# --------------------------------------------------------------------------- #
MAJORITY_BEHAVIOR = {"length": "short", "advice": False, "ask": True, "emoji": "偶尔"}
MINORITY_BEHAVIOR = {"length": "normal", "advice": True, "ask": False, "emoji": "不用"}


def _bal_record(target_behavior: dict, pred_behavior: dict | None) -> dict:
    target = _target(behavior=target_behavior)
    pred = _pred(behavior=pred_behavior) if pred_behavior is not None else None
    return {"id": "B", "parsed_ok": pred_behavior is not None, "fail": "",
            "target": target, "pred": pred,
            "field_match": evaluate.compare_fields(pred, target)}


def _imbalanced_records() -> list[dict]:
    """10 条：8 条四字段全是多数类、2 条四字段全是少数类；模型永远预测多数类。

    这样 behavior_only 很高（0.8），但每个字段的 balanced accuracy 都是 0.5。
    """
    records = [_bal_record(dict(MAJORITY_BEHAVIOR), dict(MAJORITY_BEHAVIOR))
               for _ in range(8)]
    records += [_bal_record(dict(MINORITY_BEHAVIOR), dict(MAJORITY_BEHAVIOR))
                for _ in range(2)]
    return records


def test_behavior_balanced_penalises_majority_collapse():
    records = _imbalanced_records()
    balanced = evaluate.behavior_balanced(records)
    # 每个字段：多数类 recall 1.0、少数类 recall 0 → balanced accuracy 0.5
    for key in ("length_bal", "advice_bal", "ask_bal", "emoji_bal"):
        assert balanced[key] == 0.5, key
    assert balanced["mean"] == 0.5
    # 少数类 recall 仍作为附加信息可见
    assert balanced["length_minority_recall"] == 0.0
    assert balanced["advice_minority_recall"] == 0.0
    assert balanced["majority_baselines"] == {"length": 0.8, "advice": 0.8,
                                              "ask": 0.8, "emoji": 0.8}
    summary = evaluate.summarize(records)["G2"]
    # 关键：全预测多数类时 behavior_only=0.8，但 balanced 只有 0.5
    assert summary["behavior_only"] == 0.8
    assert summary["balanced"]["mean"] < summary["behavior_only"]


def test_behavior_balanced_always_minority_is_not_perfect():
    """永远输出少数类：少数类 recall 会是 1.0，但 balanced accuracy 仍只有 0.5。"""
    records = [_bal_record(dict(MAJORITY_BEHAVIOR), dict(MINORITY_BEHAVIOR))
               for _ in range(8)]
    records += [_bal_record(dict(MINORITY_BEHAVIOR), dict(MINORITY_BEHAVIOR))
                for _ in range(2)]
    balanced = evaluate.behavior_balanced(records)
    # 旧口径（少数类 recall）会给 1.0；新口径必须公平地给 0.5
    assert balanced["length_minority_recall"] == 1.0
    assert balanced["advice_minority_recall"] == 1.0
    assert balanced["length_bal"] == 0.5 and balanced["advice_bal"] == 0.5
    assert balanced["ask_bal"] == 0.5 and balanced["emoji_bal"] == 0.5
    assert balanced["mean"] == 0.5
    assert evaluate.summarize(records)["G2"]["behavior_only"] == 0.2


def test_behavior_balanced_perfect_and_invalid():
    records = [_bal_record(dict(MAJORITY_BEHAVIOR), dict(MAJORITY_BEHAVIOR)),
               _bal_record(dict(MINORITY_BEHAVIOR), dict(MINORITY_BEHAVIOR))]
    perfect = evaluate.behavior_balanced(records)
    assert perfect["mean"] == 1.0
    assert perfect["length_bal"] == 1.0 and perfect["advice_bal"] == 1.0
    assert perfect["length_minority_recall"] == 1.0
    # 全解析失败：所有 recall 记为 0（不是 None）
    broken = evaluate.behavior_balanced([_bal_record(dict(MINORITY_BEHAVIOR), None)])
    assert broken["ask_bal"] == 0.0 and broken["emoji_bal"] == 0.0
    assert broken["length_bal"] == 0.0 and broken["advice_bal"] == 0.0
    assert broken["mean"] == 0.0


def test_behavior_balanced_missing_class():
    # target 里只有 length=short / advice=false：各字段只有一类，balanced=该类 recall
    rec = _bal_record({"length": "short", "ask": True, "advice": False,
                       "emoji": "偶尔"},
                      {"length": "short", "ask": True, "advice": False,
                       "emoji": "偶尔"})
    rec2 = _bal_record({"length": "short", "ask": False, "advice": False,
                        "emoji": "不用"},
                       {"length": "short", "ask": True, "advice": False,
                        "emoji": "不用"})
    balanced = evaluate.behavior_balanced([rec, rec2])
    assert balanced["length_bal"] == 1.0 and balanced["advice_bal"] == 1.0
    assert balanced["length_minority_recall"] is None
    assert balanced["advice_minority_recall"] is None
    # ask: true 1/1、false 0/1 → 0.5；emoji: 1.0
    assert balanced["ask_bal"] == 0.5 and balanced["emoji_bal"] == 1.0
    # mean = (1.0 + 1.0 + 0.5 + 1.0)/4
    assert balanced["mean"] == 0.875
    assert balanced["class_recalls"]["ask"] == {"True": 1.0, "False": 0.0}


# --------------------------------------------------------------------------- #
# 规则基线（--rule-baseline，用 agi/inner/render.render_rule）
# --------------------------------------------------------------------------- #
def _rule_item(state_text: str, target: dict | None = None, uid: str = "R") -> dict:
    ctx = [{"role": "system", "text": state_text},
           {"role": "user", "text": "你最近怎么样"}]
    return {"id": uid, "context": ctx, "user_msg": "还行",
            "target": target or _target()}


STATE_TEXT = ("你是微信里的一个 AI 朋友。\n"
              "当前状态：mood=0.30 energy=0.40 sharing=0.20 battery=0.50 "
              "trust=0.30 stage=初识 crisis=false")


def test_rule_prediction_matches_render_rule():
    from agi.inner import render, schema

    item = _rule_item(STATE_TEXT)
    expected_state = schema.from_state(
        {"self": {"mood": 0.30, "energy": 0.40, "sharing": 0.20, "battery": 0.50,
                  "trust": 0.30},
         "relationship": {"trust": 0.30}},
        0.30)
    expected = dict(render.render_rule(expected_state).behavior)
    got = evaluate.rule_prediction_behavior(item)
    assert got == expected
    # 没有状态文本 → None
    assert evaluate.rule_prediction_behavior(
        {"id": "x", "context": [{"role": "user", "text": "hi"}]}) is None


def test_rule_baseline_structure_and_skip():
    items = [_rule_item(STATE_TEXT, uid="R1"),
             {"id": "R2", "context": [{"role": "user", "text": "没有状态"}],
              "user_msg": "hi", "target": _target()}]
    summary = evaluate.rule_baseline(items)
    assert summary["n"] == 1 and summary["skipped"] == 1
    assert set(summary) >= {"length_bal", "advice_bal", "ask_bal", "emoji_bal",
                            "mean", "class_recalls", "majority_baselines"}
    assert 0.0 <= summary["mean"] <= 1.0


def test_rule_baseline_does_not_crash_on_render_error(monkeypatch):
    from agi.inner import render

    def boom(_state):
        raise RuntimeError("render_rule boom")

    monkeypatch.setattr(render, "render_rule", boom)
    summary = evaluate.rule_baseline([_rule_item(STATE_TEXT, uid="R1")])
    assert summary["n"] == 0 and summary["skipped"] == 1
    assert summary["mean"] is None


def test_rule_baseline_flag_adds_payload_field(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, [_rule_item(STATE_TEXT, uid="R1")])
    fake = _FakeLlamaClient(replies=[json.dumps(_pred(), ensure_ascii=False)])
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake))
    out = tmp_path / "eval.json"
    code = evaluate.main(["--data", str(data), "--backend", "llama",
                          "--rule-baseline", "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["rule_baseline"] is not None
    assert payload["rule_baseline"]["n"] == 1
    assert payload["rule_baseline"]["mean"] is not None


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


def test_criteria_declares_balanced_as_primary_metric():
    criteria = evaluate.G2_CRITERIA
    # 主口径改为 balanced accuracy（每字段各类 recall 平均，再四字段平均）
    assert criteria["primary_metric"] == "balanced"
    assert "balanced accuracy" in criteria["primary_note"]
    assert "多数类" in criteria["primary_note"]
    assert "behavior_only" in criteria["legacy_metric"]
    assert "参考" in criteria["legacy_note"]
    assert "render_rule" in criteria["rule_baseline_note"]
    assert criteria["stance_intent_primary"] == "loose"
    assert criteria["stance_intent_reference"] == "strict"
    assert "emoji" not in criteria["all5_fields"]

    # 字段级 balanced 也进 summary，且全预测多数类时 mean=0.5（不是 0.25/0.8）
    summary = evaluate.summarize(_imbalanced_records())["G2"]
    assert summary["balanced"]["mean"] == 0.5
    assert summary["balanced"]["majority_baselines"]["length"] == 0.8
    assert summary["balanced"]["class_recalls"]["length"] == {"short": 1.0,
                                                              "normal": 0.0}


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
    assert payload["backend"] == "hf" and payload["server_url"] is None
    assert payload["adapter"] is None
    assert payload["peak_alloc_gb"] == 1.0 and payload["peak_reserved_gb"] == 2.0
    assert payload["per_item_s"] == 1.5
    assert set(payload) >= {"n", "adapter", "temperature", "G1", "G2",
                            "elapsed_s", "criteria", "items", "rule_baseline"}
    assert payload["rule_baseline"] is None  # 默认不算规则基线
    assert payload["G2"]["field_rates"]["length"] == 0.5
    assert payload["criteria"]["primary_metric"] == "balanced"
    assert "balanced" in payload["G2"]


# --------------------------------------------------------------------------- #
# llama 后端（不连真实 server；任务描述要求 mock HTTP/客户端）
# --------------------------------------------------------------------------- #
class _FakeLlamaClient:
    """记录调用参数的假 client，接口与 task-12 的 llama_client 对齐。"""

    def __init__(self, replies=None, usage=None, fail_times=0, raise_error=False):
        self.replies = list(replies or [])
        self.usage = usage
        self.fail_times = int(fail_times)
        self.raise_error = bool(raise_error)
        self.calls: list[dict] = []
        self.calls_made = 0

    def chat(self, messages, *, purpose="", max_tokens=300, temperature=0.0,
             json_mode=False):
        self.calls.append({"messages": messages, "purpose": purpose,
                           "max_tokens": max_tokens, "temperature": temperature,
                           "json_mode": json_mode})
        self.calls_made += 1
        if self.raise_error:
            raise TimeoutError("fake timeout")
        if self.calls_made <= self.fail_times:
            return {"ok": False, "error": "server busy"}
        text = self.replies[(self.calls_made - 1) % len(self.replies)] \
            if self.replies else "{}"
        result = {"ok": True, "text": text}
        if self.usage is not None:
            result["usage"] = dict(self.usage)
        return result


def _llama_rows(n: int = 2) -> list[dict]:
    return [{"id": f"L{i}", "source": "synthetic",
             "context": [{"role": "system", "text": "SYS"},
                         {"role": "user", "text": "之前的话"}],
             "user_msg": f"用户第 {i} 句", "target": _target()} for i in range(n)]


def _write_rows(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "rows.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
                    encoding="utf-8")
    return path


def _fake_factory(client, seen=None):
    def factory(base_url, timeout_s):
        if seen is not None:
            seen["base_url"] = base_url
            seen["timeout_s"] = timeout_s
        return client
    return factory


def test_llama_backend_end_to_end_with_fake_client(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(2))
    fake = _FakeLlamaClient(replies=[json.dumps(_pred(), ensure_ascii=False)],
                            usage={"prompt_tokens": 123, "completion_tokens": 45})
    seen: dict = {}
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake, seen))
    out = tmp_path / "eval.json"
    code = evaluate.main([
        "--data", str(data), "--backend", "llama",
        "--base-url", "http://127.0.0.1:8123", "--limit", "0", "--out", str(out),
    ])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["backend"] == "llama"
    assert payload["server_url"] == "http://127.0.0.1:8123"
    assert payload["G1"]["valid_rate"] == 1.0
    assert payload["G2"]["behavior_only"] == 1.0
    assert payload["prompt_tokens_avg"] == 123.0
    assert payload["peak_alloc_gb"] is None and payload["peak_reserved_gb"] is None
    assert payload["n"] == 2
    assert seen == {"base_url": "http://127.0.0.1:8123", "timeout_s": 30.0}
    # 核心不变量：交给 client 的 messages 必须就是 build_messages 的结果（不本地套模板）
    rows = _llama_rows(2)
    for i, call in enumerate(fake.calls):
        assert call["messages"] == evaluate.build_messages(rows[i])[0]
        assert call["json_mode"] is True
        assert call["max_tokens"] == 300
        assert call["temperature"] == 0.0
        assert call["purpose"] == "soul_eval"
    # 无 error 字段
    assert all("error" not in item for item in payload["items"])


def test_llama_backend_retries_then_succeeds(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(1))
    fake = _FakeLlamaClient(replies=[json.dumps(_pred(), ensure_ascii=False)],
                            fail_times=1)
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake))
    out = tmp_path / "eval.json"
    code = evaluate.main(["--data", str(data), "--backend", "llama",
                          "--retries", "1", "--retry-delay-s", "0",
                          "--out", str(out)])
    assert code == 0
    assert fake.calls_made == 2  # 第一次失败 + 重试成功
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["G1"]["valid_rate"] == 1.0


def test_llama_backend_client_error_is_recorded_not_fatal(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(2))
    fake = _FakeLlamaClient(fail_times=99)
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake))
    out = tmp_path / "eval.json"
    code = evaluate.main(["--data", str(data), "--backend", "llama",
                          "--retries", "0", "--out", str(out)])
    assert code == 0  # 单条失败不中断全量
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["n"] == 2
    assert payload["G1"]["valid_rate"] == 0.0
    assert payload["G1"]["top_fail"] == {"client_error": 2}
    assert all(item["fail"] == "client_error" for item in payload["items"])
    assert all(item["parsed_ok"] is False for item in payload["items"])


def test_llama_backend_raises_timeout_maps_to_client_error(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(1))
    fake = _FakeLlamaClient(raise_error=True)
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake))
    out = tmp_path / "eval.json"
    code = evaluate.main(["--data", str(data), "--backend", "llama",
                          "--retries", "0", "--out", str(out)])
    assert code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    item = payload["items"][0]
    assert item["fail"] == "client_error"
    assert "TimeoutError" in item["error"]


def test_generate_via_client_usage_and_errors():
    messages = [{"role": "user", "content": "hi"}]
    ok_client = _FakeLlamaClient(replies=['{"mood": 0.5}'],
                                 usage={"prompt_tokens": 11, "completion_tokens": 7})
    out = evaluate.generate_via_client(ok_client, messages, max_tokens=300,
                                       temperature=0.0, retries=0)
    assert out["raw_output"] == '{"mood": 0.5}'
    assert out["prompt_tokens"] == 11 and out["generated_tokens"] == 7
    assert out["hit_max_tokens"] is False
    assert ok_client.calls[0]["json_mode"] is True

    bad = evaluate.generate_via_client(_FakeLlamaClient(fail_times=99), messages,
                                       retries=0)
    assert bad["raw_output"] == ""
    assert bad["error"].startswith("client_error:")

    # client 返回非 dict →
    class _Weird:
        def chat(self, *a, **k):
            return "not a dict"
    weird = evaluate.generate_via_client(_Weird(), messages, retries=0)
    assert weird["error"].startswith("client_error:")


def test_make_llama_client_missing_module_has_clear_error(monkeypatch):
    import importlib
    import sys as _sys

    # 全量套件里其它用例可能已把该模块导入并缓存到父包属性上；
    # 这里先清一遍缓存，保证"模块缺失"的语义可复现。
    _sys.modules.pop("agi.soul.llama_client", None)
    try:
        import agi.soul as _soul
        monkeypatch.delattr(_soul, "llama_client", raising=False)
    except Exception:  # noqa: BLE001
        pass
    importlib.invalidate_caches()
    monkeypatch.setitem(_sys.modules, "agi.soul.llama_client", None)
    with pytest.raises(RuntimeError) as exc:
        evaluate._make_llama_client("http://127.0.0.1:8123", 30.0)
    assert "llama_client" in str(exc.value)


def test_make_llama_client_matches_delivered_signature():
    """task-12 已交付 llama_client.make_client(base_url, timeout_s=30)。"""
    pytest.importorskip("agi.soul.llama_client")
    client = evaluate._make_llama_client("http://127.0.0.1:8123", 7.5)
    assert client.base_url == "http://127.0.0.1:8123"
    assert client.timeout_s == 7.5
    assert hasattr(client, "available") and hasattr(client, "chat")


def test_llama_backend_unhealthy_server_exits_3(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(1))

    class _DownClient(_FakeLlamaClient):
        def available(self):
            return False

    fake = _DownClient(replies=["{}"])
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake))
    code = evaluate.main(["--data", str(data), "--backend", "llama",
                          "--out", str(tmp_path / "eval.json")])
    assert code == 3
    assert fake.calls_made == 0  # 不可用就不发请求


def test_llama_dry_run_does_not_need_server(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(3))
    out = tmp_path / "dry.json"
    code = evaluate.main(["--data", str(data), "--backend", "llama",
                          "--limit", "0", "--dry-run", "--out", str(out)])
    assert code == 0
    assert not out.exists()  # dry-run 不写结果


def test_llama_backend_resume_skips_done(monkeypatch, tmp_path):
    """llama 模式也要能断点续跑（全量 393 条属长跑）。"""
    data = _write_rows(tmp_path, _llama_rows(2))
    out = tmp_path / "eval.json"
    first = _FakeLlamaClient(replies=[json.dumps(_pred(), ensure_ascii=False)])
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(first))
    assert evaluate.main(["--data", str(data), "--backend", "llama",
                          "--out", str(out)]) == 0
    assert first.calls_made == 2

    second = _FakeLlamaClient(replies=[json.dumps(_pred(), ensure_ascii=False)])
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(second))
    assert evaluate.main(["--data", str(data), "--backend", "llama",
                          "--resume", "--out", str(out)]) == 0
    assert second.calls_made == 0  # 全部已评，不再发请求
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["n"] == 2 and payload["resumed"] == 2
    assert payload["G1"]["valid_rate"] == 1.0


def test_llama_backend_checkpoint_writes_partial(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(4))
    out = tmp_path / "eval.json"
    fake = _FakeLlamaClient(replies=[json.dumps(_pred(), ensure_ascii=False)])
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake))
    assert evaluate.main(["--data", str(data), "--backend", "llama",
                          "--checkpoint-every", "2", "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["n"] == 4 and payload["G1"]["valid_rate"] == 1.0


def test_is_retryable_error():
    assert evaluate.is_retryable_error({"fail": "client_error"}) is True
    assert evaluate.is_retryable_error({"fail": "", "error": "boom"}) is True
    assert evaluate.is_retryable_error({"fail": "not_json"}) is False
    assert evaluate.is_retryable_error({"fail": "truncated"}) is False
    assert evaluate.is_retryable_error({"fail": "", "parsed_ok": True}) is False


def test_llama_resume_retry_errors_reruns_only_failures(monkeypatch, tmp_path):
    data = _write_rows(tmp_path, _llama_rows(2))
    out = tmp_path / "eval.json"
    out.write_text(json.dumps({"items": [
        {"id": "L0", "parsed_ok": True, "fail": "", "field_match": {},
         "target": _target()},
        {"id": "L1", "parsed_ok": False, "fail": "client_error", "error": "boom",
         "field_match": {}, "target": _target()},
    ]}, ensure_ascii=False), encoding="utf-8")
    fake = _FakeLlamaClient(replies=[json.dumps(_pred(), ensure_ascii=False)])
    monkeypatch.setattr(evaluate, "_make_llama_client", _fake_factory(fake))
    code = evaluate.main(["--data", str(data), "--backend", "llama",
                          "--resume", "--retry-errors", "--out", str(out)])
    assert code == 0
    assert fake.calls_made == 1  # 只重跑失败的那 1 条
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["n"] == 2
    assert [i["id"] for i in payload["items"]] == ["L0", "L1"]
    assert payload["G1"]["valid_rate"] == 1.0
