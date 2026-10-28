# -*- coding: utf-8 -*-
"""sft_train.py 测试：三种格式解析 / auto 探测 / dry-run 统计 / 参数校验。

不加载真实模型（torch/transformers 都不 import），tokenizer 用 Fake。
"""
from __future__ import annotations

import json

import pytest

from tools.train import sft_train


class FakeTokenizer:
    eos_token_id = 2
    pad_token = None
    pad_token_id = 0

    def apply_chat_template(self, messages, tokenize=False,
                            add_generation_prompt=True, **kwargs):
        parts = [f"{m['role']}:{m['content']}" for m in messages]
        text = "\n".join(parts) + "\nassistant:"
        return text

    def __call__(self, text, add_special_tokens=False):
        return {"input_ids": [1] * max(1, len(str(text)))}


def _write(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _messages_row(assistant="好的，我在。", user="在吗", system="你是小鱼"):
    return {"messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ], "source": "test"}


def _instruction_row(output="答案", instruction="问题", input_text=""):
    return {"instruction": instruction, "input": input_text, "output": output}


def _soul_row():
    return {
        "id": "S-1",
        "context": [{"role": "system", "text": "你是小鱼"},
                    {"role": "user", "text": "今天好累"}],
        "user_msg": "你在吗",
        "target": {"mood": 0.5, "energy": 0.6},
    }


# --------------------------------------------------------------------------- #
# 1. 格式探测
# --------------------------------------------------------------------------- #
def test_detect_item_format():
    assert sft_train.detect_item_format(_messages_row()) == "messages"
    assert sft_train.detect_item_format(_instruction_row()) == "instruction"
    assert sft_train.detect_item_format(_soul_row()) == "soul"
    assert sft_train.detect_item_format({"foo": 1}) == ""
    assert sft_train.detect_item_format(None) == ""


def test_build_samples_messages_and_text_alias():
    row = {"messages": [
        {"role": "user", "content": "在吗"},
        {"role": "assistant", "text": "在的"},
    ]}
    built = sft_train.build_samples([row], "messages")
    assert len(built["samples"]) == 1
    sample = built["samples"][0]
    assert sample["format"] == "messages"
    assert sample["answer"] == "在的"
    assert sample["messages"] == [{"role": "user", "content": "在吗"}]
    assert built["format"] == "messages"


def test_build_samples_instruction_with_input():
    built = sft_train.build_samples(
        [_instruction_row(instruction="翻译", input_text="hello", output="你好")],
        "instruction")
    sample = built["samples"][0]
    assert sample["messages"][0]["role"] == "system"
    assert "翻译\nhello" in sample["messages"][-1]["content"]
    assert sample["answer"] == "你好"


def test_build_samples_soul_uses_smoke_logic():
    built = sft_train.build_samples([_soul_row()], "soul")
    assert len(built["samples"]) == 1
    sample = built["samples"][0]
    assert sample["format"] == "soul"
    assert sample["messages"][0]["role"] == "system"
    assert "mood" in sample["answer"]         # target JSON


def test_build_samples_skips_bad_rows():
    rows = [
        _messages_row(),
        {"messages": [{"role": "user", "content": "没有 assistant"}]},
        {"messages": "bad"},
        {"instruction": "", "output": "x"},
        {"foo": 1},
    ]
    built = sft_train.build_samples(rows, "auto")
    assert len(built["samples"]) == 1
    assert len(built["skipped"]) == 4
    assert built["format"] == "messages"


def test_build_samples_auto_mixed():
    built = sft_train.build_samples([_messages_row(), _instruction_row()], "auto")
    assert len(built["samples"]) == 2
    assert built["format"] == "mixed"
    assert built["formats"] == {"messages": 1, "instruction": 1}


def test_build_samples_unknown_format_raises():
    with pytest.raises(ValueError):
        sft_train.build_samples([], "nope")


# --------------------------------------------------------------------------- #
# 2. 分词/统计
# --------------------------------------------------------------------------- #
def test_encode_and_stats_with_fake_tokenizer(tmp_path):
    path = _write(tmp_path / "data.jsonl", [
        _messages_row(), _instruction_row(), {"foo": 1},
    ])
    items, bad = sft_train.load_items(path)
    assert len(items) == 3 and bad == 0
    stats = sft_train.data_stats(items, "auto", tokenizer=FakeTokenizer(),
                                 max_len=512)
    assert stats["ok"] is True
    assert stats["rows"] == 2 and stats["skipped"] == 1
    assert stats["format"] == "mixed"
    assert stats["len_min"] > 0 and stats["len_mean"] >= stats["len_min"]
    assert stats["len_max"] >= stats["len_mean"]


def test_stats_all_invalid_returns_error():
    stats = sft_train.data_stats([{"foo": 1}], "auto",
                                 tokenizer=FakeTokenizer(), max_len=512)
    assert stats["ok"] is False
    assert stats["error"] == "no_valid_samples"


def test_dry_run_cli_prints_data(tmp_path, capsys, monkeypatch):
    path = _write(tmp_path / "data.jsonl", [
        _messages_row(), _instruction_row(), {"bad": 1},
    ])
    monkeypatch.setattr(sft_train, "_load_tokenizer",
                        lambda model_path: FakeTokenizer())
    args = sft_train.build_parser().parse_args(
        ["--dry-run", "--data", str(path), "--format", "auto"])
    code = sft_train.main(["--dry-run", "--data", str(path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "[sft] DATA " in out
    payload = json.loads(out.split("[sft] DATA ", 1)[1].splitlines()[0])
    assert payload["rows"] == 2 and payload["skipped"] == 1
    assert payload["max_seq_len"] == 512


def test_dry_run_missing_data(tmp_path, capsys):
    code = sft_train.main(["--dry-run", "--data", str(tmp_path / "nope.jsonl")])
    assert code == 2
    assert "找不到数据集" in capsys.readouterr().err


def test_bad_json_lines_counted(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"messages": []}\nnot json\n[1,2]\n', encoding="utf-8")
    items, bad = sft_train.load_items(path)
    assert items == [{"messages": []}] and bad == 2


# --------------------------------------------------------------------------- #
# 3. 参数校验
# --------------------------------------------------------------------------- #
def _args(argv):
    return sft_train.build_parser().parse_args(argv)


def test_validate_full_ft_conflicts_and_defaults():
    args = _args(["--full-ft", "--load-4bit"])
    assert "冲突" in sft_train.validate_args(args)

    args = _args(["--full-ft"])
    assert sft_train.validate_args(args) == ""
    assert args.grad_accum == 8
    assert args.out.endswith("sft_full")
    assert args.dtype == "bf16"


def test_validate_lora_defaults_and_bad_numbers():
    args = _args([])
    assert sft_train.validate_args(args) == ""
    assert args.grad_accum == 1
    assert args.save_adapter.endswith("sft_lora")
    args = _args(["--max-seq-len", "0"])
    assert "max_seq_len" in sft_train.validate_args(args)
    args = _args(["--full-ft", "--dtype", "fp16"])
    assert sft_train.validate_args(args) == ""
    assert args.dtype == "bf16"          # FFT 强制 bf16
    args = _args(["--full-ft", "--out", ""])
    sft_train.validate_args(args)
    assert args.out.endswith("sft_full")


def test_validate_bad_format():
    args = _args(["--format", "auto"])
    args.format = "nope"
    assert "format" in sft_train.validate_args(args)
