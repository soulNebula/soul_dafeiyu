# -*- coding: utf-8 -*-
"""P0-10：修 run-on 粘连句（真机「…剪了剪你这两天…」缺标点）。

真机原样（已发出）：
> 我刚把窗台那盆绿萝的枯叶子剪了剪你这两天工作要是累，晚上吃点软和的。

两层意思粘连：`polish_outbound` 之前只补句末标点、只对 >60 字按已有标点切分，
模型输出「无内部标点 run-on」时会原样发出。

本文件锁住：
1. `find_runon` 定位规则（连续 ≥12 汉字 + 结尾紧接你/您/我 + 动词边界）；
2. 真机原句 → `…剪了剪。你这两天…`（确定性插句号，不丢内容）；
3. `polish_outbound` 幂等且不误伤短句/英文数字混排/已合规文本；
4. 处置链：检测到 → 重生成 1 次 → 仍 run-on → 确定性插入。
"""
from __future__ import annotations

import json
import time

from agi.selflearn import db, proactive, trust

T0 = time.mktime((2026, 10, 25, 10, 0, 0, 0, 0, -1))

RAW = "我刚把窗台那盆绿萝的枯叶子剪了剪你这两天工作要是累，晚上吃点软和的。"
FIXED = "我刚把窗台那盆绿萝的枯叶子剪了剪。你这两天工作要是累，晚上吃点软和的。"


class FakeClient:
    def __init__(self, queue: list[str] | None = None, text: str = ""):
        self.queue = list(queue or [])
        self.text = text
        self.prompts: list[str] = []
        self.calls = 0

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        self.calls += 1
        self.prompts.append(str(messages[-1].get("content") or ""))
        if self.queue:
            return {"ok": True, "text": self.queue.pop(0)}
        return {"ok": True, "text": self.text}


def _seed(tmp_path, *, name: str = "p", now: float = T0):
    path = tmp_path / f"{name}.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO messages(id,role,text,ts,proactive) VALUES(?,?,?,?,0)",
                ("M-1", "user", "周末去爬山了，累死", now - 6 * 3600))
    con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES('autonomy',?,?)",
                (json.dumps({"daily_budget": 0}, ensure_ascii=False), now))
    con.commit()
    con.close()
    trust.set_value(path, 0.6)
    return path


def _force_care(monkeypatch):
    monkeypatch.setattr(proactive, "build_candidates", lambda p, now=None: [{
        "kind": "care", "text": "兜底", "reason": "test", "base": 0.55,
        "score": 0.6, "anchor": "", "topic": "",
    }])


def _tick(path, *, client, now=T0):
    return proactive.tick(path, client=client, now=now, cooldown=0,
                          min_score=0, respect_state=False)


# --------------------------------------------------------------------------- #
# 1) find_runon 定位
# --------------------------------------------------------------------------- #
def test_find_runon_locates_split_on_real_message():
    pos = proactive.find_runon(RAW)
    assert pos is not None
    assert RAW[:pos].endswith("剪了剪")
    assert RAW[pos] == "你"
    # 切分点之后就是第二层意思
    assert RAW[pos:].startswith("你这两天工作要是累")
    # 修好之后不再被检测（幂等基础）
    assert proactive.find_runon(FIXED) is None


def test_find_runon_ignores_short_and_compliant_text():
    for text in ("今天有点累", "好的没问题", "今天有点累，先睡了。",
                 "我刚把窗台那盆绿萝的枯叶子剪了剪。你这两天还好吗？"):
        assert proactive.find_runon(text) is None, text


def test_find_runon_does_not_split_object_pronoun():
    # 「告诉了你」「跟你解释」里的代词是宾语，不是两层意思，不能切
    for text in ("我把整件事的来龙去脉都告诉了你",
                 "我今天把这件事从头到尾跟你解释了一遍"):
        assert proactive.find_runon(text) is None, text
        assert proactive.polish_outbound(text) == text + "。"


# --------------------------------------------------------------------------- #
# 2) polish_outbound：真机修复 / 幂等 / 不误伤
# --------------------------------------------------------------------------- #
def test_polish_fixes_real_runon_message_exactly():
    assert proactive.polish_outbound(RAW) == FIXED
    # 幂等：修好后再调用逐字节不变
    assert proactive.polish_outbound(FIXED) == FIXED


def test_polish_short_sentences_not_harmed():
    assert proactive.polish_outbound("今天有点累") == "今天有点累。"
    assert proactive.polish_outbound("好的") == "好的。"
    assert proactive.polish_outbound("还没睡吗") == "还没睡吗？"


def test_polish_english_and_numbers_do_not_crash():
    text = "我用iPhone 15 Pro拍了张照片你那边天气怎么样"
    out = proactive.polish_outbound(text)
    assert "iPhone 15 Pro" in out
    assert out.endswith("。")
    assert proactive.find_runon(out) is None


def test_polish_compliant_text_is_byte_identical():
    for text in ("今天有点累，先睡了。", "明天出去吃饭吗？", "好呀！",
                 "嗯，在的。", FIXED):
        assert proactive.polish_outbound(text) == text, text


def test_polish_fixes_multiple_runons_without_losing_content():
    # 两段无标点长句（各 ≥12 汉字 + 代词）；只加标点、不丢字
    text = ("我今天去超市买了很多东西你晚上想吃什么。"
            "我下午把家里书架上的旧书都整理了一遍你周末有空吗")
    out = proactive.polish_outbound(text)
    assert proactive.find_runon(out) is None
    # 去掉新增标点后内容完全一致（不丢字、不换词）
    def _strip(value):
        for punct in "。？!?…":
            value = value.replace(punct, "")
        return "".join(value.split())

    assert _strip(out) == _strip(text)
    assert out.count("。") >= 2


# --------------------------------------------------------------------------- #
# 3) tick 处置链：重生成 1 次 → 仍 run-on 确定性插入
# --------------------------------------------------------------------------- #
def test_tick_regenerates_runon_once_and_uses_second_result(tmp_path, monkeypatch):
    _force_care(monkeypatch)
    path = _seed(tmp_path)
    good = "刚剪完绿萝的枯叶。你这两天要是累，晚上吃点软和的。"
    client = FakeClient(queue=[RAW, good])
    row = _tick(path, client=client)
    assert row["available"] is True
    assert client.calls >= 2
    # 用了第二次（合规）结果，而不是第一次的 run-on
    assert "剪完绿萝" in row["messages"][0]
    assert "剪了剪" not in row["messages"][0]
    assert any("把两层意思用句号分开" in prompt for prompt in client.prompts)
    assert proactive.find_runon(proactive.polish_outbound(row["message"])) is None


def test_tick_inserts_period_deterministically_when_regen_still_runon(tmp_path, monkeypatch):
    _force_care(monkeypatch)
    path = _seed(tmp_path)
    client = FakeClient(text=RAW)   # 每次都是 run-on
    row = _tick(path, client=client)
    assert row["available"] is True
    assert client.calls >= 2        # 先尝试重生成 1 次
    # 重生成仍粘连 → 确定性插句号；发送前 polish 后必须等于真机修复结果
    assert proactive.polish_outbound(row["message"]) == FIXED


def test_kind_prompt_has_punctuation_constraints():
    for kind in proactive.KINDS:
        prompt = proactive._kind_prompt(
            kind, {"kind": kind, "anchor": "周末去爬山了", "topic": "喜欢喝手冲咖啡"},
            0.55, 0.45, "用户:在吗")
        assert proactive.RUNON_CONSTRAINTS in prompt, kind
        assert "只表达一件事" in prompt and "句与句之间必须有标点" in prompt, kind
    fix = proactive._kind_prompt("care", {"kind": "care"}, 0.55, 0.45,
                                 "", fix_runon=True)
    assert "把两层意思用句号分开" in fix


# --------------------------------------------------------------------------- #
# 4) P0-13：6 字档（真机「有点凉你」）+ 防误伤
# --------------------------------------------------------------------------- #
RAW13 = "刚把阳台的鞋刷了，手上还有点凉你今天心情还行吗？"
FIXED13 = "刚把阳台的鞋刷了，手上还有点凉。你今天心情还行吗？"

#: 这些代词是宾语/介词宾语/动宾短语，必须原样（只补句末标点）
NO_CUT_SAMPLES = (
    "我今天下午一直在跟你说话",
    "我今天下午一直在对你说",
    "这杯饮料是我给你买的",
    "我在楼下一直等你",
    "我上午一直在找你",
    "我晚上一直陪你吃饭",
    "我下午想问你个事",
    "我有个事要告诉你",
    "我一直挺喜欢你",
    "这些年一直谢谢你",
    "我刚才偷偷看了你一眼",
    "我在楼下等你一会儿",
    # 额外常见动宾/领属/量词结构
    "我今天问了你一个问题",
    "我昨天下午去了你推荐的店",
    "我昨天下午去了你说的那家店",
    "我今天买了你喜欢的花",
    # 心理/认知动词 + 你（动宾，不能切）
    "我觉得你挺好的",
    "我知道你很难受",
    "我想告诉你一件事",
    "我发现你今天不开心",
    "我还记得你说过的话",
    "别熬夜你早点睡",
    "忙完了你给我发个消息",
)


def test_p013_real_message_fixed():
    assert proactive.polish_outbound(RAW13) == FIXED13
    # 幂等：修好后再调用逐字节不变
    assert proactive.polish_outbound(FIXED13) == FIXED13


def test_p013_second_required_case_cut():
    assert proactive.polish_outbound("今天有点累你早点睡") == "今天有点累。你早点睡。"


def test_p013_object_phrases_never_cut():
    for text in NO_CUT_SAMPLES:
        assert proactive.find_runon(text) is None, text
        assert proactive.polish_outbound(text) == text + "。", text


def test_p013_threshold_boundary_5_vs_6():
    # 5 字且末字不是句末倾向 → 不切（只补句末）
    five = "我刚吃完饭你吃了吗"
    assert proactive.find_runon(five) is None
    assert proactive.polish_outbound(five) == "我刚吃完饭你吃了吗？"
    # 6 字 → 触发切分
    six = "我刚吃过晚饭你吃了吗"
    out = proactive.polish_outbound(six)
    assert out.startswith("我刚吃过晚饭。你吃了吗"), out
    assert proactive.find_runon(out) is None


def test_p013_english_numbers_not_affected():
    text = "我用iPhone 15 Pro拍了张照片你那边天气怎么样"
    out = proactive.polish_outbound(text)
    assert "iPhone 15 Pro" in out
    assert proactive.find_runon(out) is None


def test_p013_psychological_verb_pronoun_not_cut():
    """「知道/觉得/发现…+你」是动宾，不能切（lead 真机批量试出的误切）。"""
    text = "看了你一眼就知道你在想什么"
    assert proactive.find_runon(text) is None
    out = proactive.polish_outbound(text)
    assert out == "看了你一眼就知道你在想什么？"   # 只补句末问号，不插句中句号
    for text in ("我觉得你挺好的", "我知道你很难受", "我想告诉你一件事",
                 "我发现你今天不开心", "我还记得你说过的话"):
        assert proactive.find_runon(text) is None, text


def test_p013_lead_regression_cases():
    # 该切的切
    assert proactive.polish_outbound("今天有点冷你多穿点") == "今天有点冷。你多穿点。"
    # 5 字非句末倾向 → 只补句末问号
    assert proactive.polish_outbound("刚吃完饭你吃了吗") == "刚吃完饭你吃了吗？"
    # 偏保守的两例：不切（宁可漏标点也不切坏）
    for text in ("别熬夜你早点睡", "忙完了你给我发个消息"):
        assert proactive.find_runon(text) is None, text
