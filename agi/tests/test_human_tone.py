# -*- coding: utf-8 -*-
"""P2-a 真人语气语料单测：corpus 字段/隐私、profile 数字可复算、fewshot、OCR 清洗函数。

不重新 OCR、不读联系人库：只校验已落盘的 corpus/profile/fewshot + 纯函数；
这样 CI/其他机器上也能跑（截图与 E:\\agent 的 OCR 缓存不随仓库走）。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from tools import build_human_tone as ht

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "agi" / "eval_data" / "human_tone"
CORPUS = OUT_DIR / "corpus.jsonl"
PROFILE = OUT_DIR / "profile.md"
FEWSHOT = OUT_DIR / "fewshot.json"
PROFILE_JSON_RE = re.compile(r"## 8\. 机器可读统计.*?```json\s*(\{.*?\})\s*```", re.S)


def read_corpus() -> list[dict]:
    lines = [line for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [json.loads(line) for line in lines]


def read_profile() -> dict:
    text = PROFILE.read_text(encoding="utf-8")
    match = PROFILE_JSON_RE.search(text)
    assert match, "profile.md 里找不到机器可读 JSON（## 8 段）"
    return json.loads(match.group(1))


def read_fewshot() -> dict:
    return json.loads(FEWSHOT.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- corpus


def test_corpus_fields_and_values():
    rows = read_corpus()
    assert len(rows) >= 50, f"语料太少: {len(rows)}"
    ids = [row["id"] for row in rows]
    assert len(ids) == len(set(ids)), "id 有重复"
    for row in rows:
        assert set(row) == {"id", "source", "speaker", "text", "ts_hint", "lang", "count"}
        assert row["speaker"] in ("self", "other")
        assert isinstance(row["text"], str) and row["text"].strip()
        assert row["lang"] == "zh"
        assert isinstance(row["count"], int) and row["count"] >= 1
        assert row["id"].startswith("ht-")
    self_rows = [row for row in rows if row["speaker"] == "self"]
    other_rows = [row for row in rows if row["speaker"] == "other"]
    assert len(self_rows) >= 40
    assert len(other_rows) >= 10


def test_all_outputs_have_no_privacy_leak():
    """三个产出（corpus/profile/fewshot）都不能出现真实称呼/OCR 乱码/号码。

    注意：profile 里的截图文件名是 16+ 位 hash，可能偶然包含 11 位数字（假阳性），
    扫描手机号前先把 hash 文件名抹掉，只扫真正对外内容。
    """
    import re as _re
    hash_name = _re.compile(r"[0-9a-f]{8,}\.(?:jpg|jpeg|png)")
    for path in (CORPUS, PROFILE, FEWSHOT):
        text = path.read_text(encoding="utf-8")
        if path == PROFILE:
            text = hash_name.sub("<hash>.jpg", text)
        for name in ht.REAL_NAME_BLOCKLIST:
            assert name not in text, f"{path.name} 真实称呼泄漏: {name}"
        for token in ht.OCR_GARBAGE_TOKENS:
            assert token not in text, f"{path.name} OCR 乱码残留: {token}"
        assert "wxid_" not in text
        assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", text)
        assert not re.search(r"[A-Za-z]:[\/]", text)


def test_corpus_has_no_privacy_leak():
    rows = read_corpus()
    full_text = CORPUS.read_text(encoding="utf-8")
    for name in ht.REAL_NAME_BLOCKLIST:
        assert name not in full_text, f"真实称呼泄漏: {name}"
    for token in ht.OCR_GARBAGE_TOKENS:
        assert token not in full_text, f"OCR 水印乱码残留: {token}"
    for row in rows:
        assert ht.privacy_violations(row["text"]) == [], (row["id"], row["text"])
    assert not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", full_text)
    assert "wxid_" not in full_text
    assert not re.search(r"[A-Za-z]:[\\/]", full_text)


def test_corpus_dedupe_count():
    rows = read_corpus()
    keys = [(row["speaker"], row["text"]) for row in rows]
    assert len(keys) == len(set(keys)), "corpus 内仍有 (speaker,text) 重复行"
    counts = [row["count"] for row in rows]
    assert max(counts) >= 2, "至少应有一条重复消息被 count 合并（如 ok/你好）"


# ---------------------------------------------------------------- profile 可复算


def test_profile_numbers_match_corpus():
    rows = read_corpus()
    profile = read_profile()
    self_texts = [row["text"] for row in rows if row["speaker"] == "self"]
    stats = ht.basic_text_stats(self_texts)
    assert profile["self_rows"] == len(self_texts)
    assert profile["other_rows"] == sum(1 for row in rows if row["speaker"] == "other")
    assert profile["self_total_occurrences"] == sum(
        row["count"] for row in rows if row["speaker"] == "self")
    assert profile["ending"] == stats["ending_dist"]
    assert profile["length"]["median"] == stats["length_median"]
    assert profile["length"]["dist"] == stats["length_dist"]
    assert profile["emoji_rate"] == stats["emoji_rate"]
    # 口语词 top 也可复算
    lexicon_counts: Counter = Counter()
    for text in self_texts:
        for word in ht.LEXICON:
            hits = text.count(word)
            if hits:
                lexicon_counts[word] += hits
    assert [list(item) for item in profile["lexicon_top"]] == [
        list(item) for item in lexicon_counts.most_common(20)]
    # by_source 可复算
    by_source = Counter(row["source"].split(":")[0] for row in rows
                        if row["speaker"] == "self")
    assert profile["by_source"] == dict(by_source)


def test_profile_punctuation_is_documented():
    text = PROFILE.read_text(encoding="utf-8")
    profile = read_profile()
    assert "句尾标点分布" in text
    # 关键数字以百分比形式出现在正文里
    assert f"{profile['ending']['无标点'] * 100:.1f}%" in text
    assert "## 6. 与当前 AI 输出的差距" in text
    assert profile["ai"]["count"] > 0
    assert "proactive" in profile["ai"] and "reply" in profile["ai"]


# ---------------------------------------------------------------- fewshot


def test_fewshot_examples():
    payload = read_fewshot()
    rows = {row["id"]: row for row in read_corpus()}
    examples = payload["examples"]
    assert payload["count"] == len(examples) == 30
    for item in examples:
        assert set(item) == {"id", "text", "source", "length", "ending"}
        assert item["id"] in rows
        assert rows[item["id"]]["speaker"] == "self", "fewshot 必须来自 self 语料"
        assert rows[item["id"]]["text"] == item["text"]
        assert item["length"] == len(item["text"]) <= 14
        assert item["ending"] == ht.ending_category(item["text"])
        assert ht.privacy_violations(item["text"]) == []
    endings = {item["ending"] for item in examples}
    assert {"无标点", "问号"} <= endings, f"fewshot 没覆盖主要句尾习惯: {endings}"


# ---------------------------------------------------------------- OCR 清洗纯函数


def test_normalize_ocr_text():
    assert ht.normalize_ocr_text("那 还 是 去 体 育 中 心 吧") == "那还是去体育中心吧"
    assert ht.normalize_ocr_text("O那明天再去吧") == "那明天再去吧"
    assert ht.normalize_ocr_text("下，02咋了呀") == "咋了呀"
    assert ht.normalize_ocr_text("有星星看吗7：铲") == "有星星看吗？"
    assert ht.normalize_ocr_text("真的挺好看的，嗯：铲") == "真的挺好看的，嗯"
    assert ht.normalize_ocr_text("一0 ' 0诶") == "诶"
    assert ht.normalize_ocr_text("我爸叫我一天放一串") == "我爸叫我一天放一串"


def test_side_and_noise_classifiers():
    assert ht.is_self_green((62, 181, 117)) is True
    assert ht.is_self_green((44, 44, 44)) is False
    assert ht.classify_side((62, 181, 117)) == "self"
    assert ht.classify_side((44, 44, 44)) == "other"
    assert ht.is_header_color((17, 17, 17)) is True
    assert ht.is_timestamp_text("昨天下午4:48") is True
    assert ht.is_timestamp_text("3:30~4:30") is False, "纯时间范围可能是聊天内容"
    assert ht.is_timestamp_text("8点半？") is False
    assert ht.looks_like_garbage("我張八又") is True
    assert ht.looks_like_garbage("小忄目节，他就在那里") is True
    assert ht.looks_like_garbage("正常说的一句话") is False
    assert ht.is_watermark_geometry(703, 500, 69, 16, 800) is True
    assert ht.is_avatar_geometry(24, 422, 80, 92, 800, 1600) is True


def test_anonymize_and_db_clean():
    raw = "小杰手机号13812345678 微信 wxid_abc123 文件在 C:\\Users\\Administrator\\a.txt 邮箱 a@b.com"
    clean = ht.anonymize(raw)
    for expected in ("朋友A", "[号码]", "[微信号]", "[本地路径]", "[邮箱]"):
        assert expected in clean, clean
    assert ht.privacy_violations(clean) == []
    assert ht.clean_db_text("测试语句 1") == ""
    assert ht.clean_db_text("[呲牙][呲牙]") == ""
    assert ht.clean_db_text("在吗？") == "在吗？"


def test_ending_and_runs():
    assert ht.ending_category("你好") == "无标点"
    assert ht.ending_category("在吗？") == "问号"
    assert ht.ending_category("好的！") == "感叹号"
    assert ht.ending_category("好的。") == "句号"
    assert ht.ending_category("好的…") == "省略号"
    assert ht.ending_category("好的...") == "省略号"
    sequence = [{"speaker": "self"}, {"speaker": "self"}, {"speaker": "other"},
                {"speaker": "self"}, {"speaker": "other"}]
    runs = ht.run_lengths([sequence])
    assert runs == Counter({1: 1, 2: 1})


def test_select_fewshot_filters_garbage():
    corpus = []
    for index in range(30):
        corpus.append({"id": f"ht-{index:04d}", "source": "ocr:test", "speaker": "self",
                       "text": f"第{index}句正常话", "ts_hint": "", "lang": "zh", "count": 1})
    corpus.append({"id": "ht-9990", "source": "ocr:test", "speaker": "self",
                   "text": "至", "ts_hint": "", "lang": "zh", "count": 1})
    corpus.append({"id": "ht-9991", "source": "contacts_db", "speaker": "self",
                   "text": "在吗？", "ts_hint": "", "lang": "zh", "count": 1})
    examples = ht.select_fewshot(corpus, 20)
    assert len(examples) == 20
    assert all(item["id"] != "ht-9990" for item in examples), "单字乱码不应进 fewshot"
    assert all(
        next(row for row in corpus if row["id"] == item["id"])["speaker"] == "self"
        for item in examples)


def test_profile_render_is_deterministic():
    rows = read_corpus()
    info = {"raw_sequences": [[{"speaker": row["speaker"]} for row in rows]],
            "assistant_items": [], "ocr_files": [], "ocr_self_by_image": {}}
    profile1 = ht.compute_profile(rows, info)
    profile2 = ht.compute_profile(rows, info)
    assert profile1 == profile2
    assert ht.render_profile_md(profile1) == ht.render_profile_md(profile2)
