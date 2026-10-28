# -*- coding: utf-8 -*-
"""语料清洗后端：上传 / 文本解析（三种格式）/ 图片 OCR（mock）/ 保存数据集。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app import corpusstudio as cs


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_datasets_dir", lambda: tmp_path / "datasets")
    return tmp_path / "corpus"


def _job(root):
    return cs.new_job(root=root)["id"]


def _upload(jid, root, text, name="chat.txt"):
    out = cs.save_upload(jid, name, text.encode("utf-8"), root=root)
    assert out["ok"], out
    return out


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #
def test_upload_whitelist_and_limits(root):
    jid = _job(root)
    assert cs.save_upload(jid, "a.txt", b"hi", root=root)["ok"]
    assert cs.save_upload(jid, "a.png", b"\x89PNG", root=root)["ok"]
    assert cs.save_upload(jid, "a.exe", b"x", root=root)["error"] == "bad_ext"
    assert cs.save_upload(jid, "big.txt", b"x" * (20 * 1024 * 1024 + 1),
                          root=root)["error"] == "too_large"
    assert cs.save_upload("nope", "a.txt", b"hi", root=root)["error"] == "job_not_found"


def test_duplicate_names_do_not_overwrite(root):
    jid = _job(root)
    _upload(jid, root, "a")
    out = cs.save_upload(jid, "chat.txt", b"b", root=root)
    assert out["ok"] and out["name"] != "chat.txt"


# --------------------------------------------------------------------------- #
# 文本解析
# --------------------------------------------------------------------------- #
def test_parse_prefix_style(root):
    jid = _job(root)
    _upload(jid, root, "我：今天好累\n你：先歇会儿\nAI: 那就瘫着\n")
    parsed = cs.parse_job(jid, root=root)
    assert parsed["ok"] and parsed["stats"]["total"] == 3
    roles = [i["role"] for i in parsed["items"]]
    assert roles == ["me", "you", "me"]


def test_parse_wechat_style(root):
    jid = _job(root)
    text = ("白云 2026-10-27 12:31\n今天好累啊\n"
            "白云 2026-10-27 12:32\n你先歇会儿\n"
            "小鱼 2026-10-27 12:33\n那就瘫着呗\n")
    _upload(jid, root, text)
    parsed = cs.parse_job(jid, me_names="小鱼", root=root)
    assert parsed["ok"]
    assert [i["role"] for i in parsed["items"]] == ["you", "you", "me"]


def test_parse_alternating_and_cleanup(root):
    jid = _job(root)
    text = "今天好累啊\n\n先歇会儿\nhttps://example.com/x\n那就瘫着呗\n对方撤回了一条消息\n"
    _upload(jid, root, text)
    parsed = cs.parse_job(jid, root=root)
    assert parsed["ok"]
    texts = [i["text"] for i in parsed["items"]]
    assert "https://example.com/x" not in texts
    assert all("撤回" not in t for t in texts)
    assert [i["role"] for i in parsed["items"][:2]] == ["you", "me"]


def test_parse_jsonl_messages(root):
    jid = _job(root)
    rows = [
        {"messages": [{"role": "user", "content": "在吗"},
                      {"role": "assistant", "content": "在的"}]},
    ]
    _upload(jid, root, "\n".join(json.dumps(r, ensure_ascii=False) for r in rows),
            name="chat.jsonl")
    parsed = cs.parse_job(jid, root=root)
    assert parsed["ok"]
    assert [(i["role"], i["text"]) for i in parsed["items"]] == [
        ("you", "在吗"), ("me", "在的")]


# --------------------------------------------------------------------------- #
# 图片 OCR（mock）
# --------------------------------------------------------------------------- #
def test_parse_image_uses_ocr_result(root, monkeypatch):
    jid = _job(root)
    _upload(jid, root, "fake-image", name="shot.png")
    monkeypatch.setattr(cs, "_parse_image_file", lambda path, **kw: [
        {"role": "you", "text": "今天好累"},
        {"role": "me", "text": "先瘫着"},
    ])
    parsed = cs.parse_job(jid, root=root)
    assert parsed["ok"] and parsed["stats"]["total"] == 2


# --------------------------------------------------------------------------- #
# 保存数据集
# --------------------------------------------------------------------------- #
def _prefix_job(root):
    jid = _job(root)
    _upload(jid, root, "你：今天好累\n我：先瘫着呗\n你：晚上吃啥\n我：随便点吧\n")
    parsed = cs.parse_job(jid, root=root)
    return jid, [i["id"] for i in parsed["items"]]


def test_save_messages_dataset(root):
    jid, ids = _prefix_job(root)
    out = cs.save_dataset(jid, ids, format="messages", window=6, split=0.0,
                          out_name="probe", system="你是小鱼", root=root)
    assert out["ok"] and out["rows"] == 2 and out["format"] == "messages"
    rows = [json.loads(line) for line in Path(out["out"]).read_text(
        encoding="utf-8").splitlines() if line]
    first = rows[0]["messages"]
    assert first[0] == {"role": "system", "content": "你是小鱼"}
    assert first[-1]["role"] == "assistant"


def test_save_instruction_dataset(root):
    jid, ids = _prefix_job(root)
    out = cs.save_dataset(jid, ids, format="instruction", split=0.0, root=root)
    assert out["ok"] and out["rows"] == 2
    row = json.loads(Path(out["out"]).read_text(encoding="utf-8").splitlines()[0])
    assert set(row) == {"instruction", "output"}


def test_save_requires_selection_and_valid_name(root):
    jid, ids = _prefix_job(root)
    assert cs.save_dataset(jid, [], root=root)["error"] == "empty_selection"
    assert cs.save_dataset(jid, ids, format="bad", root=root)["error"] == "invalid_format"
    out = cs.save_dataset(jid, ids, out_name="../../evil", root=root)
    assert out["ok"] and "evil" in Path(out["out"]).name and ".." not in out["out"]


def test_delete_job(root):
    jid = _job(root)
    assert cs.delete_job(jid, root=root)["ok"]
    assert cs.get_job(jid, root=root)["error"] == "job_not_found"
