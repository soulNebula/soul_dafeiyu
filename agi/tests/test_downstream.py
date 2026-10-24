# -*- coding: utf-8 -*-
"""W4-G3 下游盲评（agi/eval_data/downstream.py）的离线单测。

不联网、不加载模型、不需要 llama-server：
- mock 两臂 + 假裁判跑通完整管线；
- 锁住「两臂 prompt 只差内状态块」这条命门；
- 统计函数 / 位置偏差检查 / 断点续跑。
"""
from __future__ import annotations

import json

import pytest

from agi.eval_data import downstream as g3

GOLDEN_PATH = g3.ROOT / "agi" / "eval_data" / "golden_v1.jsonl"
SOUL_VAL_PATH = g3.ROOT / "agi" / "soul" / "data" / "soul_sft_v1_val.jsonl"


def _golden_rows(limit: int = 0):
    rows = g3.load_items(GOLDEN_PATH, limit)
    assert rows, "golden_v1.jsonl 为空"
    return rows


def _golden_ctx(index: int = 0):
    return g3.build_prompt_context(g3.normalize_item(_golden_rows()[index]))


class _RaisingJudge:
    def available(self):
        return True

    def chat(self, messages, **kwargs):
        raise RuntimeError("judge boom")


class AlwaysAJudge:
    """永远说「左边(A)更好」的裁判：用来验证翻转映射与位置偏差。"""

    def __init__(self):
        self.calls = 0

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
        return {"ok": True, "text": '{"winner":"A","reason":"左边更好"}'}


# --------------------------------------------------------------------------- #
# 1) G3 命门：两臂 prompt 只差内状态块
# --------------------------------------------------------------------------- #
def test_two_arms_prompt_differs_only_in_inner_block():
    ctx = _golden_ctx(0)
    block_a = "<inner_state>语气：平和。这轮最多一句，短句。不要主动追问。</inner_state>"
    block_b = "<inner_state>语气：温和。这轮可以多说两句。可以追问一句。</inner_state>"
    prompt_a = g3.build_reply_prompt(ctx, block_a)
    prompt_b = g3.build_reply_prompt(ctx, block_b)

    assert prompt_a != prompt_b
    # 把各自的块换成占位符后，其余部分必须逐字节相同
    assert prompt_a.replace(block_a, "@BLOCK@") == prompt_b.replace(block_b, "@BLOCK@")
    header = "你的内在状态（只影响语气与选择，不是事实，不要说出口）："
    for prompt in (prompt_a, prompt_b):
        assert prompt.count(header) == 1
        assert "上下文过长" not in prompt  # 不能被 context.fit 截断
    # 同一块重复构造必须逐字节一致（可复现）
    assert g3.build_reply_prompt(ctx, block_a) == prompt_a
    # 两臂都基于同一条用户消息与同一份状态
    assert ctx["item"]["user_msg"] in prompt_a


def test_teacher_prompt_uses_annotation_prompt():
    """local 臂用训练同源 build_messages；teacher 臂用打标签同源 prompt（含 JSON 约束）。"""
    ctx = _golden_ctx(0)
    local_messages = g3.build_inner_messages(g3.MockSoulClient("local", local=True), ctx)
    assert local_messages == g3.build_soul_messages(ctx)

    teacher_messages = g3.build_inner_messages(g3.MockSoulClient("teacher"), ctx)
    joined = "\n".join(str(m.get("content") or "") for m in teacher_messages)
    assert "只输出一个 JSON 对象" in joined      # DeepSeek json_object 的硬要求
    assert "当前状态：mood=" in joined           # 与 local 臂同一份状态信息
    assert ctx["item"]["user_msg"] in joined     # 同一句用户消息
    assert teacher_messages != local_messages    # 格式各按训练口径，内容信息相同


def test_summarize_server_props():
    """llama-server /props → 记录实际加载的 GGUF（防止正式跑误用 v0 底座）。"""
    payload = {
        "model_path": r"E:\agent\output\gguf\soul_qwen25_05b_v1-q4_k_m.gguf",
        "model_alias": "local-soul",
        "default_generation_settings": {"n_ctx": 2048},
        "total_slots": 1,
        "n_ctx_train": 32768,
    }
    props = g3.summarize_props(payload)
    assert props["model_path"].endswith("soul_qwen25_05b_v1-q4_k_m.gguf")
    assert props["model_alias"] == "local-soul"
    assert props["n_ctx"] == 2048 and props["total_slots"] == 1
    assert props["n_ctx_train"] == 32768
    assert g3.summarize_props(None) == {}
    assert g3.summarize_props({"unexpected": 1}) == {}
    assert g3._server_model_label({"local_server_props": props}).startswith(
        r"E:\agent\output\gguf\soul_qwen25_05b_v1")
    assert g3._server_model_label({}) == "-"


def test_soul_item_messages_rebuild_same_system():
    if not SOUL_VAL_PATH.exists():
        pytest.skip("soul_sft_v1_val.jsonl 不存在")
    raw = g3.load_items(SOUL_VAL_PATH, 1)[0]
    item = g3.normalize_item(raw)
    ctx = g3.build_prompt_context(item)
    messages = g3.build_soul_messages(ctx)
    original_system = next(c["text"] for c in raw["context"] if c["role"] == "system")
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == original_system  # 与训练/评测 system 逐字节一致
    body = messages[1:]
    assert body[-1] == {"role": "user", "content": raw["user_msg"]}
    assert all(m["role"] in ("user", "assistant") for m in body)


# --------------------------------------------------------------------------- #
# 2) 统计：全胜 / 全平 / 空
# --------------------------------------------------------------------------- #
def _fake_rows(winner: str, n: int = 10, bucket: str = "work"):
    return [{"winner": winner, "bucket": bucket, "arm_a_source": "model",
             "arm_b_source": "model"} for _ in range(n)]


def test_summarize_all_local_wins_and_all_tie():
    summary = g3.summarize(_fake_rows("a", 10))
    ov = summary["overall"]
    assert ov["a_wins"] == 10 and ov["b_wins"] == 0 and ov["ties"] == 0
    assert ov["a_win_rate"] == 1.0
    assert ov["wilson_95"]["high"] == 1.0
    assert ov["tie_rate"] == 0.0
    assert summary["by_bucket"]["work"]["a_win_rate"] == 1.0

    tie = g3.summarize(_fake_rows("TIE", 5))
    assert tie["overall"]["tie_rate"] == 1.0
    assert tie["overall"]["decisive"] == 0
    assert tie["overall"]["a_win_rate"] == 0.0

    empty = g3.summarize([])
    assert empty["overall"]["rows"] == 0
    assert empty["overall"]["decisive"] == 0
    assert empty["by_bucket"] == {}


# --------------------------------------------------------------------------- #
# 3) 盲评：左右随机映射 + 位置偏差检查
# --------------------------------------------------------------------------- #
def test_judge_flip_mapping():
    ctx = _golden_ctx(0)
    judge = AlwaysAJudge()
    # 裁判只看左右，不看臂；judge_pair 必须把 A/B 标签映射回 a/b
    flipped = g3.judge_pair(judge, ctx, "回复一", "回复二", rng=g3._ForcedFlip(True))
    assert flipped["flip"] is True and flipped["winner"] == "b"
    normal = g3.judge_pair(judge, ctx, "回复一", "回复二", rng=g3._ForcedFlip(False))
    assert normal["flip"] is False and normal["winner"] == "a"


def test_position_bias_check_detects_order_dependence():
    ctx = _golden_ctx(0)
    # 永远判左边赢的裁判：换边后胜者必变 → 顺序相关
    checked = g3.judge_with_bias_check(AlwaysAJudge(), ctx, "回复一", "回复二", seed=42)
    assert checked["main"] is not None
    assert checked["consistent"] is False
    assert checked["order_dependent"] is True
    # 内容型假裁判（mock）：换边后仍选 local 臂 → 顺序一致
    mock_checked = g3.judge_with_bias_check(
        g3.MockJudgeClient(), ctx, "（local 臂）好", "（teacher 臂）一般", seed=42)
    assert mock_checked["consistent"] is True
    assert mock_checked["main"]["winner"] == "a"


# --------------------------------------------------------------------------- #
# 4) mock 全链路：结构、盲评、断点续跑
# --------------------------------------------------------------------------- #
def _run_mock(tmp_path, **kwargs):
    out = tmp_path / "g3.json"
    report = tmp_path / "g3.md"
    result = g3.run_downstream(
        data=GOLDEN_PATH, out=out, report=report, mock=True,
        limit=kwargs.pop("limit", 3), bias_check=kwargs.pop("bias_check", 2),
        **kwargs)
    return out, report, result


def test_mock_run_structure_and_stats(tmp_path):
    out, report, result = _run_mock(tmp_path)
    items = result["items"]
    assert len(items) == 3
    assert result["skipped"] == 0
    assert json.loads(out.read_text(encoding="utf-8"))["items"] == items
    for row in items:
        for field in ("arm_a_block", "arm_b_block", "reply_a", "reply_b", "winner"):
            assert field in row, row
        assert row["arm_a_block"].startswith("<inner_state>")
        assert row["arm_b_block"].startswith("<inner_state>")
        assert row["reply_a"] and row["reply_b"]
        assert row["winner"] in ("a", "b", "TIE")
        assert isinstance(row["judge_flip"], bool)
        assert row["arm_a_source"] == "model"
        assert row["arm_b_source"] == "model"
        assert row["arm_a_latency_ms"] >= 0 and row["arm_b_latency_ms"] >= 0
        # mock 裁判固定偏好 local，两条臂的盲评映射必须还原成 a
        assert row["winner"] == "a"
    summary = result["summary"]
    assert summary["overall"]["a_wins"] == 3
    assert summary["overall"]["a_win_rate"] == 1.0
    assert summary["overall"]["wilson_95"]["high"] == 1.0
    assert summary["block_sources"]["a"]["model"] == 3
    # 前 2 条做了双向重评，mock 裁判顺序一致
    assert summary["position_bias"]["n"] == 2
    assert summary["position_bias"]["order_dependent"] == 0
    assert [bool(r.get("bias_check")) for r in items] == [True, True, False]
    report_text = report.read_text(encoding="utf-8")
    # 正式跑后报告直接可用：环境（server 实际加载）+ 口径 + 预注册判读 + 复现命令
    assert "## 总体" in report_text
    assert "server 实际加载：-" in report_text
    assert "## 口径（两臂到底在比什么）" in report_text
    assert "soul.prompt.build_messages" in report_text
    assert "soul.dataset.teacher_messages" in report_text
    assert "预注册判读标准" in report_text
    assert "## 复现命令" in report_text


def test_reproduce_command_from_meta():
    cmd = g3._reproduce_command({
        "data": "agi/eval_data/golden_v1.jsonl", "arms": "both", "limit": 5,
        "out": "o.json", "report": "r.md", "mode": "mock", "seed": 42,
        "bias_check": 3,
    })
    assert "--mock" in cmd and "--limit 5" in cmd and "--bias-check 3" in cmd
    real = g3._reproduce_command({
        "data": "x.jsonl", "arms": "both", "limit": 0, "out": "o.json",
        "report": "r.md", "mode": "real", "seed": 7, "bias_check": 0,
        "base_url": "http://127.0.0.1:8123",
    })
    assert "--mock" not in real and "--seed 7" in real
    assert "--bias-check" not in real and "--base-url http://127.0.0.1:8123" in real


def test_mock_run_resume_does_not_duplicate(tmp_path):
    out, report, first = _run_mock(tmp_path)
    assert len(first["items"]) == 3

    # 断点续跑：全部已存在 → 全部跳过，文件行数不变
    second = g3.run_downstream(
        data=GOLDEN_PATH, out=out, report=report, mock=True, limit=3, resume=True)
    assert second["skipped"] == 3
    assert len(second["items"]) == 3
    assert [r["golden_id"] for r in second["items"]] == [
        r["golden_id"] for r in first["items"]]

    # --resume 只补缺：把最后一条删掉再跑，只补 1 条
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["items"] = payload["items"][:-1]
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    third = g3.run_downstream(
        data=GOLDEN_PATH, out=out, report=report, mock=True, limit=3, resume=True)
    assert third["skipped"] == 2
    assert len(third["items"]) == 3
    ids = [r["golden_id"] for r in third["items"]]
    assert len(ids) == len(set(ids))


def test_judge_exception_is_contained(tmp_path):
    """裁判抛异常只让该条 winner=null，不中断整批。"""
    result = g3.run_downstream(
        data=GOLDEN_PATH, out=tmp_path / "g3.json", report=tmp_path / "g3.md",
        mock=True, limit=1, judge_client=_RaisingJudge())
    row = result["items"][0]
    assert row["winner"] is None
    assert "judge boom" in (row["judge_error"] or "")
    assert result["summary"]["overall"]["decisive"] == 0
    assert result["summary"]["errors"]["judge"] == 1


def test_single_arm_teacher_no_judge(tmp_path):
    result = g3.run_downstream(
        data=GOLDEN_PATH, out=tmp_path / "g3.json", report=tmp_path / "g3.md",
        mock=True, arms="teacher", limit=2)
    items = result["items"]
    assert len(items) == 2
    for row in items:
        assert row["arm_b_block"].startswith("<inner_state>")
        assert row["reply_b"]
        assert row["arm_a_block"] is None and row["reply_a"] is None
        assert row["winner"] is None
    assert result["summary"]["overall"]["decisive"] == 0
    assert result["summary"]["overall"]["rows"] == 2


# --------------------------------------------------------------------------- #
# 5) 真实模式缺 key：明确报错，不静默回退
# --------------------------------------------------------------------------- #
def test_real_without_key_fails_loudly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")  # 显式空值覆盖 web_config.json
    out = tmp_path / "g3.json"
    with pytest.raises(SystemExit) as excinfo:
        g3.main(["--limit", "1", "--arms", "teacher", "--out", str(out),
                 "--report", str(tmp_path / "g3.md")])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert "API key" in err and "静默" in err
    assert not out.exists()
