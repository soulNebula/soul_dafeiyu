# -*- coding: utf-8 -*-
"""P2-c QQ 语气语料单测：解析/清洗/隐私/speaker 判定/统计复算。

全部用合成 fixture（不依赖真实 QQ 导出/密钥/本地库），CI 可跑。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools import build_qq_tone as bq

CORPUS_HEADER = "{"


def make_export(tmp_path: Path, rows: list[dict], name: str = "peer1") -> Path:
    export_dir = tmp_path / "export"
    export_dir.mkdir(parents=True, exist_ok=True)
    payload = []
    for index, row in enumerate(rows):
        payload.append({
            "chat_name": row.get("chat_name", "王小明"),
            "chat_type": "c2c",
            "peer": row.get("peer", "10001"),
            "msg_id": index + 1,
            "msg_type": row.get("msg_type", 2),
            "msg_type_name": "text",
            "is_self": row.get("is_self", False),
            "sender_uin": row.get("sender_uin", 10001),
            "timestamp": row.get("timestamp", 1700000000 + index),
            "time": row.get("time", f"2024-01-01 12:00:0{index % 10}"),
            "text": row["text"],
        })
    (export_dir / f"{name}_full.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return export_dir


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---------------------------------------------------------------- classify_text


def test_classify_text_kinds():
    assert bq.classify_text("今晚打球去不去")[1] == "text"
    assert bq.classify_text("[图片] xxx")[1] == "image"
    assert bq.classify_text("[表情] xxx")[1] == "emoji"
    assert bq.classify_text("[文件] a.pdf")[1] == "file"
    assert bq.classify_text("[card] {\"app\":\"x\"}")[1] == "card"
    assert bq.classify_text("撤回了一条消息")[1] == "system"
    assert bq.classify_text("")[1] == "empty"
    long_text = "这是一条很长很长的转发消息" * 30
    assert bq.classify_text(long_text)[1] == "too_long"
    assert bq.classify_text("🙂🙂")[1] == "non_cjk"


def test_anonymize_qq_privacy():
    text = ("王小明 手机号13812345678 看这个 https://example.com/x "
            "路径 C:\\Users\\Administrator\\a.txt 邮箱 a@b.com QQ 123456789")
    clean = bq.anonymize(text, label="朋友Q1", chat_name="王小明")
    assert "王小明" not in clean
    assert "朋友Q1" in clean
    assert "13812345678" not in clean
    assert "C:\\Users" not in clean
    assert "a@b.com" not in clean
    assert "123456789" not in clean
    # 复用微信版 privacy 扫描，保证两套语料同一标准
    assert bq.ht.privacy_violations(clean) == []


# ---------------------------------------------------------------- build_corpus


def test_build_corpus_filters_and_dedupes(tmp_path: Path):
    rows = [
        {"text": "在吗？", "is_self": True},
        {"text": "在的", "is_self": False},
        {"text": "在吗？", "is_self": True},          # 重复 → count=2
        {"text": "[图片] 截图.png", "is_self": False},  # 图片丢弃
        {"text": "[表情] 哈哈", "is_self": False},      # 表情丢弃
        {"text": "撤回了一条消息", "is_self": False},   # 系统丢弃
        {"text": "今晚打球去不去", "is_self": True},
        {"text": "我请客", "is_self": True},
        {"text": "好", "is_self": False},
    ]
    export_dir = make_export(tmp_path, rows)
    messages = bq.load_export(sorted(export_dir.glob("*_full.json")))
    corpus, info = bq.build_corpus(messages)
    assert info["sessions"] == 1
    assert info["raw_messages"] == len(rows)
    assert info["dropped"].get("image") == 1
    assert info["dropped"].get("emoji") == 1
    assert info["dropped"].get("system") == 1
    texts = {row["text"]: row for row in corpus}
    assert texts["在吗？"]["count"] == 2
    assert texts["在吗？"]["speaker"] == "self"
    assert texts["在的"]["speaker"] == "other"
    # 去重后 self unique = 在吗？/今晚打球去不去/我请客 = 3
    assert info["self_rows"] == 3
    assert len(texts) == 5
    assert all(row["source"] == "qq" and row["lang"] == "zh" for row in corpus)
    assert all(row["ts_hint"] for row in corpus)
    assert [row["id"] for row in corpus] == [f"qq-{i:04d}" for i in range(1, 6)]


def test_build_corpus_label_and_privacy(tmp_path: Path):
    rows = [
        {"text": "王小明你好啊", "is_self": True, "peer": "20002"},
        {"text": "我的电话 13900001111", "is_self": False, "peer": "20002"},
    ]
    export_dir = make_export(tmp_path, rows, name="peer2")
    corpus, _ = bq.build_corpus(bq.load_export(sorted(export_dir.glob("*_full.json"))))
    joined = "\n".join(row["text"] for row in corpus)
    assert "王小明" not in joined
    assert "朋友Q1" in joined
    assert "13900001111" not in joined


# ---------------------------------------------------------------- 端到端 + 画像


def test_build_end_to_end_and_profile_recompute(tmp_path: Path):
    rows = [
        {"text": "周末去爬山吗", "is_self": False},
        {"text": "去啊", "is_self": True},
        {"text": "几点", "is_self": False},
        {"text": "八点吧", "is_self": True},
        {"text": "行", "is_self": False},
        {"text": "那我在门口等你", "is_self": True},
        {"text": "好！", "is_self": False},
        {"text": "[图片] a.jpg", "is_self": True},
    ]
    export_dir = make_export(tmp_path, rows)
    out_dir = tmp_path / "out"
    result = bq.build(export_dir, out_dir, dry_run=False)
    corpus_path = out_dir / bq.CORPUS_NAME
    profile_path = out_dir / bq.PROFILE_NAME
    assert corpus_path.exists() and profile_path.exists()

    corpus = read_jsonl(corpus_path)
    self_texts = [row["text"] for row in corpus if row["speaker"] == "self"]
    stats = bq.ht.basic_text_stats(self_texts)
    text = profile_path.read_text(encoding="utf-8")
    match = re.search(r"## 7\. 机器可读统计.*?```json\s*(\{.*?\})\s*```", text, re.S)
    assert match, "qq_profile.md 里找不到机器可读 JSON"
    profile = json.loads(match.group(1))
    assert profile["self_rows"] == len(self_texts)
    assert profile["ending"] == stats["ending_dist"]
    assert profile["length"]["median"] == stats["length_median"]
    assert profile["corpus_rows"] == len(corpus)
    assert profile["merge_recommendation"]["decision"] in ("merge", "separate_or_weight", "hold")
    assert profile["enough_for_formal_merge"] is False   # 样本远小于 200

    # 隐私扫描：三个产出都不能出现长数字/真实路径/昵称
    all_text = "\n".join(path.read_text(encoding="utf-8") for path in
                         (corpus_path, profile_path))
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", all_text)
    assert not re.search(r"[A-Za-z]:[\\/]", all_text)
    assert "王小明" not in all_text


def test_dry_run_does_not_write(tmp_path: Path):
    export_dir = make_export(tmp_path, [{"text": "你好", "is_self": True}])
    out_dir = tmp_path / "out"
    bq.build(export_dir, out_dir, dry_run=True)
    assert not (out_dir / bq.CORPUS_NAME).exists()
    assert not (out_dir / bq.PROFILE_NAME).exists()


def test_idempotent(tmp_path: Path):
    export_dir = make_export(tmp_path, [{"text": "好的", "is_self": True},
                                        {"text": "嗯", "is_self": False}])
    out_dir = tmp_path / "out"
    bq.build(export_dir, out_dir, dry_run=False)
    first = {name: (out_dir / name).read_bytes()
             for name in (bq.CORPUS_NAME, bq.PROFILE_NAME)}
    bq.build(export_dir, out_dir, dry_run=False)
    second = {name: (out_dir / name).read_bytes()
              for name in (bq.CORPUS_NAME, bq.PROFILE_NAME)}
    assert first == second


# ---------------------------------------------------------------- 合并建议


def test_merge_recommendation():
    wechat = {"self_rows": 59, "length_median": 7, "no_punct_rate": 0.86,
              "question_rate": 0.13, "period_rate": 0.0, "emoji_rate": 0.05}
    close = {"count": 60, "length_median": 8, "no_punct_rate": 0.80}
    far = {"count": 60, "length_median": 18, "no_punct_rate": 0.45}
    tiny = {"count": 5, "length_median": 8, "no_punct_rate": 0.80}
    assert bq.recommend_merge(close, wechat)["decision"] == "merge"
    assert bq.recommend_merge(far, wechat)["decision"] == "separate_or_weight"
    assert bq.recommend_merge(tiny, wechat)["decision"] == "hold"
    assert bq.recommend_merge({"count": 60, "length_median": 8, "no_punct_rate": 0.8}, {})["decision"] == "hold"
