# -*- coding: utf-8 -*-
"""W4-G3 下游盲评：本地小模型产出的内状态块 vs DeepSeek teacher 内状态块。

这是 W4 的第三道门（也是唯一决定「能不能上线」的门）：

- G1 只看格式合法率；G2 只看与 teacher 逐字段一致（那是「像不像 teacher」，不是
  「回复有没有变好」）；
- G3 固定其余一切，只替换 prompt 里的内状态块，用 DeepSeek 生成两条回复，再让
  DeepSeek 裁判盲评，看**最终回复质量**有没有差别。

两臂
----
- ``local``   ：``agi/soul/llama_client.LlamaClient``（``local_soul=True``）请求
  llama-server；prompt 用 ``agi/soul/prompt.build_messages``（与训练/评测同源）。
- ``teacher`` ：DeepSeek（``agi/selflearn/llm.Client``）用打标签同源的
  ``soul.dataset.teacher_messages`` 生成 InnerState JSON（temperature=0.2）。
  注：DeepSeek ``json_object`` 要求 prompt 含 “json”，训练同源 prompt 不含该词会
  直接 400；两臂输入的信息相同、格式各按训练口径（见 README「两臂 prompt 为什么不对称」）。

两条输出都经 ``agi/inner/schema.validate`` + ``agi/inner/render.render_block``
变成 ``<inner_state>…</inner_state>`` 块，再分别注入
``chat.SelfLearnAgent._prompt(inner_ctx=...)``；除该块外 prompt 逐字节相同
（``test_downstream.py`` 锁住这条不变量——它是 G3 有效性的命门）。

回复生成统一用 DeepSeek（temperature=0.3，max_tokens=500）；盲评复用
``agi/eval_data/judge.py`` 的配对裁判协议（A/B 左右随机，另做双向位置偏差检查）。

输出
----
- ``--out``   JSON：``{"meta": {...}, "summary": {...}, "items": [...]}``，逐条落盘可续跑；
- ``--report`` Markdown：总体胜率 + Wilson 95% CI + 分 bucket + 位置偏差 + 限制。

用法（mock 自测不需要 key、不需要 llama-server）::

    python -m agi.eval_data.downstream --mock --limit 3 --out "$TEMP/g3_mock.json"
    python -m agi.eval_data.downstream --arms teacher --limit 50 \\
        --out E:\\agent\\logs\\g3_teacher.json --report E:\\agent\\logs\\g3_report.md
    python -m agi.eval_data.downstream --base-url http://127.0.0.1:8123 --limit 50 \\
        --out E:\\agent\\logs\\g3_downstream.json --report E:\\agent\\logs\\g3_report.md
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 允许 `python agi/eval_data/downstream.py` 直接运行
    sys.path.insert(0, str(ROOT))

from agi.eval_data import judge as judge_mod  # noqa: E402
from agi.inner import render as inner_render  # noqa: E402
from agi.inner import schema as inner_schema  # noqa: E402
from agi.inner.inject import sanitize_monologue  # noqa: E402
from agi.selflearn import chat as chat_mod  # noqa: E402
from agi.selflearn.llm import Client, parse_json  # noqa: E402
from agi.soul import prompt as soul_prompt  # noqa: E402

DEFAULT_DATA = ROOT / "agi" / "eval_data" / "golden_v1.jsonl"
DEFAULT_OUT = ROOT / "agi" / "eval_data" / "g3_downstream.json"
DEFAULT_REPORT = ROOT / "agi" / "eval_data" / "g3_report.md"
DEFAULT_BASE_URL = "http://127.0.0.1:8123"

ARMS = ("both", "local", "teacher")
INNER_TEMPERATURE = 0.2
REPLY_TEMPERATURE = 0.3
INNER_MAX_TOKENS = 300
REPLY_MAX_TOKENS = 500
MAX_TURNS = soul_prompt.MAX_TURNS  # 与训练数据 context 轮数一致（4）
RAW_OUTPUT_LIMIT = 1000

#: golden_v1 没有 state_profile，按 bucket 映射到最接近的训练档位（与 dataset 的
#: STATE_PROFILES 同名）；soul_sft 行自带 state_profile / system 状态行，优先用它们。
BUCKET_PROFILE = {
    "work": "work",
    "low_mood": "low_mood",
    "conflict": "guarded",
    "help": "default",
    "smalltalk": "default",
    "boundary": "guarded",
    "joke": "joke",
    "privacy": "guarded",
    "crisis": "default",   # profile_state(..., "crisis") 会强制 crisis=True
    "silence": "tired",
}

_STATE_RE = re.compile(
    r"mood=(?P<mood>[\d.]+)\s+energy=(?P<energy>[\d.]+)\s+"
    r"sharing=(?P<sharing>[\d.]+)\s+battery=(?P<battery>[\d.]+)\s+"
    r"trust=(?P<trust>[\d.]+)\s+stage=(?P<stage>\S+)\s+"
    r"crisis=(?P<crisis>true|false)"
)


# --------------------------------------------------------------------------- #
# mock client（确定性；不联网、不加载模型，供 CI 与 --mock）
# --------------------------------------------------------------------------- #
class MockSoulClient:
    """确定性 InnerState 生成器：两个臂返回不同但合法的 JSON，便于验证管线。

    不是真模型——mock 输出不能用来判断 G3 结论。
    """

    def __init__(self, tag: str, *, local: bool = False) -> None:
        self.tag = str(tag)
        self.local_soul = bool(local)
        self.model = f"mock-{self.tag}"
        self.calls = 0

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs) -> dict[str, Any]:
        self.calls += 1
        return {"ok": True, "text": json.dumps({
            "stance": f"{self.tag} 平和",
            "intent": f"{self.tag} 先接住对方",
            "monologue": f"{self.tag} 先听再说",
            "behavior": {"length": "short", "ask": self.tag == "local",
                         "advice": False, "emoji": "不用"},
        }, ensure_ascii=False)}


class MockReplyClient:
    """确定性回复生成器：回复里带臂标记，方便 mock 裁判与断言。"""

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs) -> dict[str, Any]:
        self.calls += 1
        text = _messages_text(messages)
        if "local 平和" in text:
            reply = "（local 臂）嗯，我听到了，你先歇口气。"
        elif "teacher 平和" in text:
            reply = "（teacher 臂）嗯，我听到了，你慢慢说。"
        else:
            reply = "（未知臂）嗯，我在听。"
        return {"ok": True, "text": reply}


class MockJudgeClient:
    """确定性裁判：内容里带 local 标记的回复获胜。

    mock 下 local 臂天然全胜，只证明盲评映射/统计正确，**不代表 G3 结论**。
    """

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs) -> dict[str, Any]:
        self.calls += 1
        prompt = _messages_text(messages)
        a = _section(prompt, "回复 A：", "回复 B：")
        b = _section(prompt, "回复 B：", "判断标准")
        if "local 臂" in a:
            winner = "A"
        elif "local 臂" in b:
            winner = "B"
        else:
            winner = "TIE"
        return {"ok": True, "text": json.dumps(
            {"winner": winner, "reason": "mock：local 标记优先"},
            ensure_ascii=False)}


def _messages_text(messages) -> str:
    return "\n".join(
        str(m.get("content") or "") if isinstance(m, dict) else str(m)
        for m in (messages or []))


def _section(text: str, start: str, end: str) -> str:
    i = str(text).find(start)
    if i < 0:
        return ""
    body = str(text)[i + len(start):]
    j = body.find(end)
    return body[:j] if j >= 0 else body


class _ForcedFlip:
    """把 judge.judge_one 的左右随机强制成指定方向（位置偏差检查用）。"""

    def __init__(self, flip: bool) -> None:
        self.flip = bool(flip)

    def random(self) -> float:
        return 0.1 if self.flip else 0.9


# --------------------------------------------------------------------------- #
# 数据 / 状态 / prompt 构造
# --------------------------------------------------------------------------- #
def load_items(path: Path | str, limit: int = 0) -> list[dict]:
    """读 JSONL（golden_v1 或 soul_sft_*），坏行跳过；limit<=0 = 全部。"""
    items: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("user_msg"):
                items.append(row)
            if limit and len(items) >= limit:
                break
    return items


def parse_state_line(text: str) -> dict | None:
    """从 soul_sft 的 system 状态行解析出 7 个状态字段；解析不到返回 None。"""
    match = _STATE_RE.search(str(text or ""))
    if not match:
        return None
    data = {k: float(match.group(k)) for k in
            ("mood", "energy", "sharing", "battery", "trust")}
    data["stage"] = match.group("stage")
    data["crisis"] = match.group("crisis") == "true"
    return data


def normalize_item(raw: dict) -> dict:
    """golden_v1 / soul_sft 行 → 统一结构（context 只留 user/assistant）。"""
    raw_context = [c for c in (raw.get("context") or []) if isinstance(c, dict)]
    system_text = next(
        (str(c.get("text") or "") for c in raw_context
         if c.get("role") == "system"), "")
    context = [
        {"role": str(c.get("role")), "text": str(c.get("text") or "")}
        for c in raw_context
        if c.get("role") in ("user", "assistant") and str(c.get("text") or "")
    ]
    bucket = str(raw.get("bucket") or raw.get("_bucket") or "unknown")
    profile = str(raw.get("state_profile") or BUCKET_PROFILE.get(bucket, "default"))
    return {
        "id": str(raw.get("id") or ""),
        "bucket": bucket,
        "context": context,
        "user_msg": str(raw.get("user_msg") or "").strip(),
        "ideal_points": [str(p) for p in (raw.get("ideal_points") or [])],
        "state_profile": profile,
        "state": parse_state_line(system_text),
        "source": str(raw.get("source") or ""),
    }


def state_for_item(item: dict) -> dict:
    """当前状态：soul_sft 行解析 system 的状态行；golden 行按 bucket 映射档位。"""
    parsed = item.get("state")
    if isinstance(parsed, dict) and parsed:
        return soul_prompt.state_dict(parsed)
    return soul_prompt.profile_state(item["state_profile"], item.get("bucket"))


def chat_state_from(state: dict) -> dict:
    """7 字段状态 → chat._prompt 需要的 ``state.load()`` 同构结构（两臂共用）。"""
    data = soul_prompt.state_dict(state)
    mood = float(data["mood"])
    crisis = bool(data.get("crisis"))
    if crisis:
        label, valence = "crisis", -1.0
    elif mood <= 0.4:
        label, valence = "negative", round(mood * 2 - 1, 3)
    elif mood >= 0.62:
        label, valence = "positive", round(mood * 2 - 1, 3)
    else:
        label, valence = "neutral", round(mood * 2 - 1, 3)
    return {
        "self": {"mood": mood, "energy": float(data["energy"]),
                 "sharing": float(data["sharing"]), "battery": float(data["battery"])},
        "emotion": {"label": label, "valence": valence, "arousal": 0.3},
        "relationship": {"stage": str(data["stage"]),
                         "closeness": round(float(data["trust"]), 3)},
        "boundary": {"level": 4 if crisis else 0,
                     "name": "危机" if crisis else "正常",
                     "crisis": crisis, "manipulation": False},
    }


def build_prompt_context(item: dict) -> dict:
    """一条 item → 所有 prompt 构造共用的只读上下文（不碰 DB、不联网）。"""
    state = state_for_item(item)
    return {
        "item": item,
        "state": state,
        "st": chat_state_from(state),
        "recent": list(item.get("context") or [])[-MAX_TURNS:],
    }


def build_soul_messages(ctx: dict) -> list[dict]:
    """两臂共用的 InnerState 生成 prompt（与训练/评测同源的 build_messages）。"""
    return soul_prompt.build_messages(ctx["state"], ctx["recent"],
                                      ctx["item"]["user_msg"])


#: DeepSeek 的 ``response_format=json_object`` 要求 prompt 里出现 “json” 一词
#: （否则 HTTP 400），而本地模型的训练 prompt 不含该词（格式由 llama-server 的
#: json_schema grammar 保证）。因此 teacher 臂复用**生成训练 target 时同一份**
#: 标注 prompt（``agi/soul/dataset.teacher_messages``：同一份 state/recent/user
#: 信息 + schema 约束），而不是强行让两个模型吃同一个字符串。见 README「两臂
#: prompt 为什么不对称」。


def build_teacher_messages(ctx: dict) -> list[dict]:
    """teacher 臂：复用生成 soul_sft 标签时的标注 prompt（与训练标注同源）。"""
    from agi.soul import dataset as soul_dataset

    item = {
        "context": [dict(t) for t in ctx["item"]["context"]],
        "user_msg": ctx["item"]["user_msg"],
        "_state": soul_prompt.state_dict(ctx["state"]),
        "_bucket": ctx["item"]["bucket"],
    }
    return soul_dataset.teacher_messages(item)


def build_inner_messages(client, ctx: dict) -> list[dict]:
    """按 client 类型构造 InnerState prompt。

    - local（``local_soul=True``）：``prompt.build_messages``，与训练/评测同源；
    - teacher（DeepSeek）：``dataset.teacher_messages``，与打标签时同一份 prompt。
    两者输入的信息完全相同（当前状态 + 最近对话 + 用户这句话），只是格式各按训练口径。
    """
    if getattr(client, "local_soul", False):
        return build_soul_messages(ctx)
    return build_teacher_messages(ctx)


def build_reply_prompt(ctx: dict, inner_block: str) -> str:
    """把内状态块注入 chat._prompt；除该块外所有参数都是常量/同一条 context。"""
    return chat_mod.SelfLearnAgent._prompt(
        ctx["item"]["user_msg"], [], "", {"variant_id": None, "hint": "自然回应"},
        ctx["recent"], st=ctx["st"], inner_ctx=inner_block)


def base_inner_state(ctx: dict) -> inner_schema.InnerState:
    """InnerState 的数值/关系以会话状态为准（模型只出行为与独白）。"""
    return inner_schema.validate({**soul_prompt.state_dict(ctx["state"]),
                                  "source": "model"})


def parse_inner_state(text: str, base: inner_schema.InnerState,
                      user_text: str) -> inner_schema.InnerState:
    """模型输出文本 → 合法 InnerState；与 inject._model_ctx 的后处理口径一致。"""
    obj = parse_json(str(text or ""))
    if not isinstance(obj, dict):
        raise ValueError("输出不是 JSON 对象")
    if not any(key in obj for key in ("stance", "intent", "monologue", "behavior")):
        raise ValueError("缺少 stance/intent/monologue/behavior")
    st = inner_schema.validate({**obj, "source": "model"})
    st.mood, st.energy, st.sharing, st.battery = (
        base.mood, base.energy, base.sharing, base.battery)
    st.trust, st.stage, st.crisis = base.trust, base.stage, base.crisis
    st.patience, st.openness = base.patience, base.openness
    st.monologue = sanitize_monologue(st.monologue, user_text)
    return st


def rule_block(ctx: dict) -> str:
    """模型输出不可用时的回退块（与 inject 的 model→rule 回退同口径）。"""
    st = inner_render.render_rule(base_inner_state(ctx))
    st.monologue = sanitize_monologue(st.monologue, ctx["item"]["user_msg"])
    return inner_render.render_block(st)


# --------------------------------------------------------------------------- #
# 两臂生成 / 回复生成
# --------------------------------------------------------------------------- #
def generate_arm_block(arm: str, client, ctx: dict) -> dict:
    """一臂：InnerState JSON → 内状态块；非法/失败回退 rule（记录来源）。"""
    started = time.perf_counter()
    out = {"arm": arm, "block": "", "source": "model", "error": None,
           "raw": "", "latency_ms": 0.0}
    base = base_inner_state(ctx)
    messages = build_inner_messages(client, ctx)
    try:
        result = client.chat(
            messages, purpose="g3_inner_state", max_tokens=INNER_MAX_TOKENS,
            temperature=INNER_TEMPERATURE, json_mode=True)
    except Exception as exc:  # noqa: BLE001 - 单臂失败不能让整条中断
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("error") if isinstance(result, dict) else "client 返回非 dict"
        out["error"] = str(detail or "LLM 调用失败")[:300]
    else:
        raw = str(result.get("text") or "")
        out["raw"] = raw[:RAW_OUTPUT_LIMIT]
        try:
            st = parse_inner_state(raw, base, ctx["item"]["user_msg"])
            out["block"] = inner_render.render_block(st)
        except Exception as exc:  # noqa: BLE001 - 非法 JSON/字段 → rule 回退
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    if not out["block"]:
        try:
            out["block"] = rule_block(ctx)
            out["source"] = "rule"
        except Exception as exc:  # noqa: BLE001
            out["source"] = "none"
            out["error"] = f"回退 rule 也失败：{type(exc).__name__}: {exc}"[:300]
    out["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out


def generate_reply(client, ctx: dict, inner_block: str) -> dict:
    """用固定 DeepSeek（mock 时为假 client）生成一条回复；temperature=0.3。"""
    started = time.perf_counter()
    prompt = build_reply_prompt(ctx, inner_block)
    out = {"reply": "", "error": None, "prompt_chars": len(prompt),
           "latency_ms": 0.0}
    try:
        result = client.chat(
            [{"role": "system", "content": chat_mod.SYSTEM},
             {"role": "user", "content": prompt}],
            purpose="g3_reply", max_tokens=REPLY_MAX_TOKENS,
            temperature=REPLY_TEMPERATURE)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("error") if isinstance(result, dict) else "client 返回非 dict"
        out["error"] = str(detail or "回复生成失败")[:300]
    else:
        out["reply"] = str(result.get("text") or "").strip()
        if not out["reply"]:
            out["error"] = "空回复"
    out["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return out


# --------------------------------------------------------------------------- #
# 盲评（复用 judge.py 协议）+ 位置偏差检查
# --------------------------------------------------------------------------- #
def golden_for_judge(ctx: dict) -> dict:
    item = ctx["item"]
    return {"user_msg": item["user_msg"], "context": item["context"],
            "ideal_points": item["ideal_points"], "bucket": item["bucket"]}


def judge_pair(judge_client, ctx: dict, reply_a: str, reply_b: str, *,
               seed: int = 42, rng=None) -> dict | None:
    """复用 judge.judge_one：左右随机、把 A/B 标签映射回 ``a``/``b``/``TIE``。"""
    if rng is None:
        rng = random.Random(f"{seed}:{ctx['item']['id']}")
    return judge_mod.judge_one(judge_client, golden_for_judge(ctx),
                               reply_a, reply_b, rng)


def judge_with_bias_check(judge_client, ctx: dict, reply_a: str, reply_b: str, *,
                          seed: int = 42) -> dict:
    """同一条配对用**相反左右顺序**再评一次，检查裁判是否受位置影响。"""
    main = judge_pair(judge_client, ctx, reply_a, reply_b, seed=seed)
    if not main:
        return {"main": None, "opposite": None, "consistent": None,
                "order_dependent": None}
    opposite = judge_pair(judge_client, ctx, reply_a, reply_b, seed=seed,
                          rng=_ForcedFlip(not main["flip"]))
    if not opposite:
        return {"main": main, "opposite": None, "consistent": None,
                "order_dependent": None}
    consistent = opposite.get("winner") == main.get("winner")
    return {"main": main, "opposite": opposite, "consistent": consistent,
            "order_dependent": not consistent}


# --------------------------------------------------------------------------- #
# 统计
# --------------------------------------------------------------------------- #
def _rate_block(wins_a: int, wins_b: int, ties: int) -> dict:
    decisive = wins_a + wins_b
    total = decisive + ties
    rate = wins_a / decisive if decisive else 0.0
    low, high = judge_mod.wilson(wins_a, decisive) if decisive else (0.0, 0.0)
    return {
        "n": total, "decisive": decisive, "a_wins": wins_a, "b_wins": wins_b,
        "ties": ties, "a_win_rate": round(rate, 4),
        "b_win_rate": round((wins_b / decisive) if decisive else 0.0, 4),
        "tie_rate": round((ties / total) if total else 0.0, 4),
        "wilson_95": {"low": round(low, 4), "high": round(high, 4)},
    }


def summarize(rows: list[dict]) -> dict:
    """总体 + 分 bucket 统计；只把有 winner 的行计入 judged。"""
    judged = [r for r in rows if r.get("winner") in ("a", "b", "TIE")]
    wins_a = sum(r["winner"] == "a" for r in judged)
    wins_b = sum(r["winner"] == "b" for r in judged)
    ties = sum(r["winner"] == "TIE" for r in judged)
    overall = _rate_block(wins_a, wins_b, ties)
    overall["rows"] = len(rows)
    buckets: dict[str, dict] = {}
    for bucket in sorted({str(r.get("bucket") or "") for r in judged}):
        rs = [r for r in judged if str(r.get("bucket") or "") == bucket]
        block = _rate_block(sum(r["winner"] == "a" for r in rs),
                            sum(r["winner"] == "b" for r in rs),
                            sum(r["winner"] == "TIE" for r in rs))
        buckets[bucket] = block
    # 块来源：local/teacher 各自有多少条走了 model / rule 回退 / 完全失败
    sources: dict[str, dict[str, int]] = {}
    for arm in ("a", "b"):
        counter = {"model": 0, "rule": 0, "none": 0, "missing": 0}
        for row in rows:
            src = row.get(f"arm_{arm}_source")
            counter[src if src in counter else "missing"] += 1
        sources[arm] = counter
    flips = [bool(r.get("judge_flip")) for r in judged if r.get("judge_flip") is not None]
    bias = [r["bias_check"] for r in rows if isinstance(r.get("bias_check"), dict)]
    retried_rows = [r for r in rows if int(r.get("retried") or 0) > 0]
    retries = {
        "rows": len(retried_rows),
        "attempts": sum(int(r.get("retried") or 0) for r in rows),
    }
    bias_stats = {
        "n": len(bias),
        "consistent": sum(1 for b in bias if b.get("consistent")),
        "order_dependent": sum(1 for b in bias if b.get("order_dependent")),
        "consistent_rate": round(
            sum(1 for b in bias if b.get("consistent")) / len(bias), 4
        ) if bias else None,
    }
    return {
        "overall": overall,
        "by_bucket": buckets,
        "block_sources": sources,
        "judge_flip": {
            "true": sum(flips), "false": len(flips) - sum(flips),
            "n": len(flips),
        },
        "retries": retries,
        "position_bias": bias_stats,
        "errors": {
            "local_block": sum(1 for r in rows if r.get("arm_a_error")),
            "teacher_block": sum(1 for r in rows if r.get("arm_b_error")),
            "reply": sum(1 for r in rows if r.get("reply_error")),
            "judge": sum(1 for r in rows if r.get("judge_error")),
        },
    }


# --------------------------------------------------------------------------- #
# 输出 / 报告
# --------------------------------------------------------------------------- #
def write_json(path: Path | str, payload: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _server_model_label(meta: dict) -> str:
    """报告里展示 llama-server /props 记录的实际 GGUF（mock/teacher 单臂为 -）。"""
    props = meta.get("local_server_props") or {}
    if not isinstance(props, dict) or not props:
        return "-"
    path = props.get("model_path") or props.get("model_alias") or props.get("model_name")
    extra = []
    if props.get("n_ctx"):
        extra.append(f"n_ctx={props['n_ctx']}")
    return f"{path or '-'}" + (f"（{', '.join(extra)}）" if extra else "")


def render_report(summary: dict, meta: dict) -> str:
    ov = summary["overall"]
    state_policy = meta.get("state_policy") or {}
    lines = [
        "# W4-G3 下游盲评报告（本地内状态块 vs teacher 内状态块）",
        "",
        "> 只看「最终回复质量」：两臂唯一差异是注入 chat._prompt 的内状态块，"
        "其余 prompt 逐字节相同。本报告由 `agi/eval_data/downstream.py` 自动生成，"
        "正式跑完直接可用。",
        "",
        "## 环境",
        "",
        f"- 数据：`{meta['data']}`（{meta['n_items']} 条；mode={meta['mode']}）",
        f"- arms={meta['arms']}；seed={meta['seed']}；bias-check={meta['bias_check']}",
        f"- 生成 InnerState：temperature={meta['inner_temperature']}，"
        f"max_tokens={meta['inner_max_tokens']}；"
        f"生成回复：temperature={meta['reply_temperature']}，"
        f"max_tokens={meta['reply_max_tokens']}",
        f"- teacher（InnerState/回复/裁判）：{meta.get('teacher_model') or '-'}",
        f"- local soul：{meta.get('local_model') or '-'}"
        f" @ {meta.get('base_url') or '-'}",
        f"- server 实际加载：{_server_model_label(meta)}",
        *([f"- 失败重试：{summary['retries']['rows']} 条曾 winner=null，"
           f"共重跑 {summary['retries']['attempts']} 次，最终缺 "
           f"{ov['rows'] - ov['decisive'] - ov['ties']} 条"
           f"（只重跑失败/空回复，不按结果筛选）"]
          if summary.get("retries", {}).get("rows") else []),
        f"- 耗时：{meta.get('elapsed_s')}s",
        "",
        "## 口径（两臂到底在比什么）",
        "",
        f"- 状态来源（两臂共用同一份）：golden → {state_policy.get('golden', '-')}；"
        f"soul_sft → {state_policy.get('soul_sft', '-')}",
        "- local 臂：InnerState prompt = `soul.prompt.build_messages`（训练/评测同源）；"
        "client = llama-server（GGUF 见「server 实际加载」）",
        "- teacher 臂：InnerState prompt = `soul.dataset.teacher_messages`（打标签同源）；"
        "client = DeepSeek（`json_object` 约束）",
        "- 两臂后处理完全相同：`parse_json` → `inner.schema.validate` → 数值字段以会话状态为准"
        " → `sanitize_monologue` → `inner.render.render_block`",
        "- 注入点：`chat.SelfLearnAgent._prompt(inner_ctx=块)`；两条最终 prompt 除该块外"
        "逐字节相同（单测锁住）",
        "- 回复生成：DeepSeek（两臂同一个 client，temperature/max_tokens 见「环境」）",
        "- 盲评：`judge.py` 的配对裁判（A/B 左右随机）；`--bias-check N` 用相反顺序再判一次",
        "",
        "## 总体",
        "",
        "| n | 可判定 | local(a) 胜 | teacher(b) 胜 | 平 | local 胜率(不含平) | Wilson 95% | tie 率 |",
        "|---|---|---|---|---|---|---|---|",
        f"| {ov['rows']} | {ov['decisive']} | {ov['a_wins']} | {ov['b_wins']} | "
        f"{ov['ties']} | {ov['a_win_rate']:.1%} | "
        f"{ov['wilson_95']['low']:.1%}–{ov['wilson_95']['high']:.1%} | "
        f"{ov['tie_rate']:.1%} |",
        "",
        "**预注册判读标准（看数据前就定好，避免事后找理由）：**",
        "",
        "- 主要指标：local 胜率（不含平）的 Wilson 95% CI；",
        "- CI 上界 < 50% ⇒ local 明显差于 teacher：G2 分数没有传导到回复质量，"
        "回头改训练数据/渲染方式；",
        "- CI 下界 > 50% ⇒ local 明显好于 teacher：本地小模型路线成立；",
        "- CI 跨 50% ⇒ **未测出显著差异**（不等于「一样好」）；n=50 时 CI 宽约 ±14pt，"
        "只能检出大差异，小而真实的差异需要更多样本；",
        "- 辅助：分 bucket 是否系统性掉分、`block_sources` 的 rule 回退条数、"
        "`position_bias.order_dependent`。",
        "",
        "## 分 bucket",
        "",
        "| bucket | n | local 胜 | teacher 胜 | 平 | local 胜率(不含平) | Wilson 95% |",
        "|---|---|---|---|---|---|---|",
    ]
    for bucket, block in sorted(summary["by_bucket"].items()):
        lines.append(
            f"| {bucket or '-'} | {block['n']} | {block['a_wins']} | "
            f"{block['b_wins']} | {block['ties']} | {block['a_win_rate']:.1%} | "
            f"{block['wilson_95']['low']:.1%}–{block['wilson_95']['high']:.1%} |")
    lines += ["", "## 内状态块来源", ""]
    for arm, label in (("a", "local"), ("b", "teacher")):
        c = summary["block_sources"][arm]
        lines.append(f"- {label}：model={c.get('model', 0)}，"
                     f"回退 rule={c.get('rule', 0)}，失败={c.get('none', 0)}")
    bias = summary["position_bias"]
    lines += ["", "## 裁判位置偏差检查", ""]
    if bias["n"]:
        lines.append(
            f"- 双向重评 {bias['n']} 条：顺序一致 {bias['consistent']} 条、"
            f"顺序相关（换边后胜者改变）{bias['order_dependent']} 条，"
            f"一致率 {bias['consistent_rate']:.1%}。")
        if bias["order_dependent"]:
            lines.append("- ⚠ 存在顺序相关：说明该裁判有一定位置偏差，"
                         "胜率结论需结合人工复核。")
    else:
        lines.append("- 未做双向重评（加 `--bias-check N` 随机抽 N 条检查）。")
    flip = summary["judge_flip"]
    lines += ["", f"- 左右随机分布：flip=True {flip['true']} 条 / flip=False "
                  f"{flip['false']} 条（共 {flip['n']}）。",
              "", "## 复现命令", "", "```bash",
              _reproduce_command(meta),
              "```", "",
              "（中断后重跑同一命令加 `--resume`，按 golden_id 跳过已完成条目，不重复调用。）",
              "", "## 已知限制", "",
              "1. 裁判与回复生成同为 DeepSeek（同族自评偏差），结果只作初筛，需人工复核。",
              "2. golden_v1 没有 state_profile：按 bucket 映射到最接近的训练档位（见 README 表）；"
              "对 soul_sft 行则解析其 system 状态行。",
              "3. 这里用 `chat._prompt` 的隔离 prompt（无记忆/画像/检索/教训），"
              "目的是只放大内状态块的影响；线上完整 prompt 的绝对效果需另测。",
              "4. mock 模式下 local 臂由假裁判固定获胜，只用于验证管线，不构成 G3 结论。",
              "5. 回复几乎完全相同（如「行，那就先这样」vs「行，那先这样」）时裁判会摆动"
              "（实测同一对重判出现 TIE/A/TIE）；平局/极短回复建议人工复核。"
              "本次双向重评的 `order_dependent` 条数见「裁判位置偏差检查」。",
              "6. 裁判 `max_tokens=120`：deepseek-flash 的 reasoning tokens 偶尔吃满预算 →"
              "返回空/非法 JSON；本次用 `--retry-failed` 只重跑失败项补齐，重跑次数已记录在"
              "「环境」段，不按胜负筛选。",
              ""]
    return "\n".join(lines)


def _reproduce_command(meta: dict) -> str:
    """从 meta 反推一条可复现本次运行的命令（写进报告，便于审计）。"""
    parts = ["python -m agi.eval_data.downstream",
             f'--data "{meta.get("data")}"',
             f'--arms {meta.get("arms", "both")}']
    limit = int(meta.get("limit") or 0)
    if limit:
        parts.append(f"--limit {limit}")
    parts += [f'--out "{meta.get("out")}"', f'--report "{meta.get("report")}"']
    if meta.get("seed") not in (None, 42):
        parts.append(f"--seed {meta['seed']}")
    if meta.get("bias_check"):
        parts.append(f"--bias-check {meta['bias_check']}")
    if meta.get("mode") == "mock":
        parts.append("--mock")
    elif meta.get("base_url"):
        parts.append(f'--base-url {meta["base_url"]}')
    return " ".join(parts)


# --------------------------------------------------------------------------- #
# 真实 client
# --------------------------------------------------------------------------- #
def build_deepseek_client() -> Client:
    """DeepSeek client（teacher / 回复 / 裁判共用）；缺 key 明确报错退出。"""
    from agi.selflearn.llm import load_config

    config = load_config()
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key is not None:  # 显式设置（含空串）优先于 web_config.json
        config["api_key"] = env_key.strip()
    client = Client(config)
    if not client.available():
        print(
            "错误：G3 真实跑需要 DeepSeek API key（teacher 内状态 + 回复 + 裁判）。\n"
            "  已检查 web_config.json 的 llm.api_key 与 DEEPSEEK_API_KEY，均未配置。\n"
            "  不会静默回退到 mock；请配置 key，或改用 --mock 离线自测/--arms local 只跑本地臂。",
            file=sys.stderr)
        raise SystemExit(2)
    return client


def build_local_client(base_url: str, timeout_s: float):
    """本地灵魂模型 client；llama-server 不可达时明确报错（不静默回退）。"""
    from agi.soul.llama_client import LlamaClient

    client = LlamaClient(base_url, timeout_s=timeout_s)
    if not client.available():
        print(
            f"错误：本地灵魂模型 llama-server 不可达（{base_url}）。\n"
            "  先按 tools/soul_serve/README.md 启动 server（--jinja），"
            "或改用 --arms teacher（只跑 teacher 臂）/ --mock。\n"
            "  不会静默回退成 teacher。",
            file=sys.stderr)
        raise SystemExit(2)
    return client


def summarize_props(payload: Any) -> dict:
    """从 llama-server ``/props`` 里提取「实际加载的是哪个模型」，用于留档/防跑错底座。"""
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("model_path", "model_alias", "model_name"):
        value = payload.get(key)
        if value:
            out[key] = str(value)
    settings = payload.get("default_generation_settings")
    n_ctx = (settings or {}).get("n_ctx") if isinstance(settings, dict) else None
    if n_ctx is None:
        n_ctx = payload.get("n_ctx")
    if n_ctx is not None:
        try:
            out["n_ctx"] = int(n_ctx)
        except (TypeError, ValueError):
            pass
    for key in ("total_slots", "n_ctx_train"):
        if payload.get(key) is not None:
            try:
                out[key] = int(payload[key])
            except (TypeError, ValueError):
                pass
    return out


def probe_local_server(base_url: str, timeout_s: float = 3.0) -> dict:
    """GET llama-server ``/props``（只读、失败静默返回 {}），用于记录实际 GGUF。"""
    try:
        with urllib.request.urlopen(
                str(base_url).rstrip("/") + "/props",
                timeout=min(3.0, float(timeout_s))) as resp:
            return summarize_props(json.loads(resp.read().decode("utf-8", "replace")))
    except Exception:  # noqa: BLE001 - 老版本 server 可能没有 /props
        return {}


def _warn_about_local_model(props: dict) -> None:
    """底座防呆：提示确认不是 v0/旧 adapter，并把 /props 打印出来留档。"""
    if not props:
        print("[g3] 警告：读不到 llama-server /props（老版本或无该端点）。"
              "请人工确认 server 加载的是**最佳底座**（不要用 v0 的 GGUF）。",
              file=sys.stderr)
        return
    print(f"[g3] llama-server props: {json.dumps(props, ensure_ascii=False)}", flush=True)
    model_path = str(props.get("model_path") or props.get("model_alias") or "")
    if "v0" in model_path.lower():
        print(f"[g3] ⚠ 警告：server 加载的模型疑似 v0（{model_path}），"
              "正式跑请确认已换成最佳底座！", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _load_resume_rows(out_path: Path, resume: bool) -> list[dict]:
    if not resume or not out_path.exists():
        return []
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        print(f"[g3] 警告：{out_path} 不可解析，忽略续跑数据", file=sys.stderr)
        return []
    rows = payload.get("items") if isinstance(payload, dict) else None
    return [r for r in rows if isinstance(r, dict) and r.get("golden_id")] \
        if isinstance(rows, list) else []


def _load_previous_elapsed(out_path: Path, resume: bool) -> float:
    """续跑/重试时把上一轮累计耗时带上（报告里的「耗时」是累计墙钟）。"""
    if not resume or not out_path.exists():
        return 0.0
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        return float((payload.get("meta") or {}).get("elapsed_s") or 0.0)
    except (ValueError, OSError, TypeError):
        return 0.0


def _apply_cumulative_elapsed(meta: dict, previous_elapsed: float) -> None:
    """把「上一轮累计 + 本轮」写回 meta；同时保留本轮/上轮拆分便于审计。"""
    this_run = float(meta.get("elapsed_s") or 0.0)
    meta["elapsed_s_this_run"] = round(this_run, 2)
    meta["elapsed_s_previous"] = round(previous_elapsed, 2)
    meta["elapsed_s"] = round(previous_elapsed + this_run, 2)


def _item_row(ctx: dict) -> dict:
    item = ctx["item"]
    return {
        "golden_id": item["id"],
        "bucket": item["bucket"],
        "state_profile": item["state_profile"],
        "state": {k: ctx["state"][k] for k in
                  ("mood", "energy", "sharing", "battery", "trust", "stage", "crisis")},
        "arm_a_block": None, "arm_a_source": None, "arm_a_error": None,
        "arm_a_latency_ms": None, "arm_a_raw": None,
        "arm_b_block": None, "arm_b_source": None, "arm_b_error": None,
        "arm_b_latency_ms": None, "arm_b_raw": None,
        "reply_a": None, "reply_b": None, "reply_error": None,
        "winner": None, "judge_reason": None, "judge_flip": None,
        "judge_error": None, "bias_check": None, "ts": time.time(),
    }


def run_downstream(*, data: Path | str = DEFAULT_DATA,
                   out: Path | str = DEFAULT_OUT,
                   report: Path | str = DEFAULT_REPORT,
                   arms: str = "both", limit: int = 0, seed: int = 42,
                   base_url: str = DEFAULT_BASE_URL, timeout_s: float = 30.0,
                   mock: bool = False, resume: bool = False, bias_check: int = 0,
                   retry_failed: bool = False,
                   teacher_client=None, local_client=None, reply_client=None,
                   judge_client=None) -> dict:
    """跑 G3；返回 ``{"meta", "summary", "items"}`` 并写 --out/--report。

    ``retry_failed=True``（隐含 resume）：只重跑上轮 ``winner=null`` 的条目——
    为空的那一臂重新生成回复、再重判；不按结果筛选，只修瞬态错误。
    """
    if arms not in ARMS:
        raise SystemExit(f"未知 arms：{arms!r}，可选 {', '.join(ARMS)}")
    items = [normalize_item(raw) for raw in load_items(data, limit)]
    out_path, report_path = Path(out), Path(report)

    local_props: dict = {}
    if mock:
        teacher_client = teacher_client or MockSoulClient("teacher")
        local_client = local_client or MockSoulClient("local", local=True)
        reply_client = reply_client or MockReplyClient()
        judge_client = judge_client or MockJudgeClient()
        mode = "mock"
    else:
        teacher_client = teacher_client or build_deepseek_client()
        if arms in ("both", "local"):
            if local_client is None:
                local_client = build_local_client(base_url, timeout_s)
            local_props = probe_local_server(base_url, timeout_s)
            _warn_about_local_model(local_props)
        reply_client = reply_client or teacher_client
        judge_client = judge_client or teacher_client
        mode = "real"

    previous = _load_resume_rows(out_path, resume)
    previous_elapsed = _load_previous_elapsed(out_path, resume)
    rows = list(previous)
    row_by_id = {str(r["golden_id"]): r for r in previous}
    done_ids = set(row_by_id)
    skipped = 0
    retried = 0
    retry_fixed = 0
    retried_ids: list[str] = []
    retry_info = {"requested": bool(retry_failed), "rows": 0, "fixed": 0, "ids": []}
    started = time.time()
    print(f"[g3] mode={mode} arms={arms} items={len(items)} 已续跑={len(done_ids)} "
          f"bias_check={bias_check} retry_failed={retry_failed}", flush=True)
    if mode == "real":
        # both：teacher InnerState n + 回复 2n + 裁判 n；teacher 单臂：InnerState n + 回复 n；
        # local 单臂：只有回复 n 次（本地 InnerState 不花 API 钱）
        if arms == "both":
            n_calls = len(items) * 4 + (min(bias_check, len(items)) if bias_check else 0)
        elif arms == "teacher":
            n_calls = len(items) * 2
        else:
            n_calls = len(items)
        print(f"[g3] 预计 DeepSeek 调用≈{n_calls} 次（arms={arms}"
              f"{' + 双向复核 ' + str(min(bias_check, len(items))) if bias_check and arms == 'both' else ''}）",
              flush=True)
    for index, item in enumerate(items, 1):
        existing = row_by_id.get(item["id"])
        retry_this = bool(existing is not None and retry_failed
                          and existing.get("winner") is None)
        if existing is not None and not retry_this:
            skipped += 1
            continue
        ctx = build_prompt_context(item)
        row = existing if retry_this else _item_row(ctx)
        total_started = time.perf_counter()
        if arms in ("both", "local") and not row.get("arm_a_block"):
            arm_a = generate_arm_block("local", local_client, ctx)
            row.update({"arm_a_block": arm_a["block"],
                        "arm_a_source": arm_a["source"],
                        "arm_a_error": arm_a["error"],
                        "arm_a_latency_ms": arm_a["latency_ms"],
                        "arm_a_raw": arm_a["raw"]})
        if arms in ("both", "teacher") and not row.get("arm_b_block"):
            arm_b = generate_arm_block("teacher", teacher_client, ctx)
            row.update({"arm_b_block": arm_b["block"],
                        "arm_b_source": arm_b["source"],
                        "arm_b_error": arm_b["error"],
                        "arm_b_latency_ms": arm_b["latency_ms"],
                        "arm_b_raw": arm_b["raw"]})
        # 生成回复：两条都生成（单臂模式只生成选中的那条；重试时只补空的那条）
        if arms in ("both", "local") and not row.get("reply_a"):
            rep_a = generate_reply(reply_client, ctx, row["arm_a_block"] or "")
            row["reply_a"] = rep_a["reply"]
            if rep_a["error"] and not row["reply_error"]:
                row["reply_error"] = f"a: {rep_a['error']}"
            row["arm_a_latency_ms"] = round(
                float(row["arm_a_latency_ms"] or 0) + rep_a["latency_ms"], 1)
        if arms in ("both", "teacher") and not row.get("reply_b"):
            rep_b = generate_reply(reply_client, ctx, row["arm_b_block"] or "")
            row["reply_b"] = rep_b["reply"]
            if rep_b["error"] and not row["reply_error"]:
                row["reply_error"] = f"b: {rep_b['error']}"
            row["arm_b_latency_ms"] = round(
                float(row["arm_b_latency_ms"] or 0) + rep_b["latency_ms"], 1)
        # 重试时把已补齐的回复错误清掉
        if row.get("reply_a") and row.get("reply_b"):
            row["reply_error"] = None
        # 盲评（任何裁判异常都只记该条错误，不中断整批）
        if arms == "both":
            try:
                if not row["reply_a"] or not row["reply_b"]:
                    row["judge_error"] = "有一臂回复为空，跳过盲评"
                elif bias_check and index <= bias_check:
                    checked = judge_with_bias_check(
                        judge_client, ctx, row["reply_a"], row["reply_b"], seed=seed)
                    main = checked["main"]
                    if main:
                        row.update({"winner": main["winner"],
                                    "judge_reason": main["reason"],
                                    "judge_flip": main["flip"],
                                    "judge_error": None})
                        row["bias_check"] = {
                            "opposite_winner": checked["opposite"].get("winner")
                            if checked["opposite"] else None,
                            "opposite_flip": checked["opposite"].get("flip")
                            if checked["opposite"] else None,
                            "consistent": checked["consistent"],
                            "order_dependent": checked["order_dependent"],
                        }
                    else:
                        row["judge_error"] = "裁判输出不可解析"
                else:
                    res = judge_pair(judge_client, ctx, row["reply_a"],
                                     row["reply_b"], seed=seed)
                    if res:
                        row.update({"winner": res["winner"],
                                    "judge_reason": res["reason"],
                                    "judge_flip": res["flip"],
                                    "judge_error": None})
                    else:
                        row["judge_error"] = "裁判输出不可解析"
            except Exception as exc:  # noqa: BLE001 - 裁判异常只影响该条
                row["judge_error"] = f"{type(exc).__name__}: {exc}"[:300]
        elapsed_item = (time.perf_counter() - total_started) * 1000
        if retry_this:
            retried += 1
            row["retried"] = int(row.get("retried") or 0) + 1
            row["latency_ms"] = round(float(row.get("latency_ms") or 0) + elapsed_item, 1)
            if row.get("winner") is not None:
                retry_fixed += 1
            retried_ids.append(item["id"])
            retry_info.update({"rows": retried, "fixed": retry_fixed,
                               "ids": list(retried_ids)})
        else:
            row["latency_ms"] = round(elapsed_item, 1)
            rows.append(row)
            done_ids.add(item["id"])
            row_by_id[item["id"]] = row
        checkpoint_meta = _meta(data, out_path, report_path, items, mode, arms,
                                seed, base_url, bias_check, limit,
                                teacher_client, local_client, previous, started,
                                local_props=local_props)
        checkpoint_meta["retry"] = dict(retry_info)
        _apply_cumulative_elapsed(checkpoint_meta, previous_elapsed)
        payload = {"meta": checkpoint_meta,
                   "summary": summarize(rows), "items": rows}
        write_json(out_path, payload)  # 每条约 1KB，逐条落盘便于续跑
        print(f"[{index}/{len(items)}] {item['id']} "
              f"winner={row['winner']} flip={row['judge_flip']} "
              f"local={row['arm_a_source']} teacher={row['arm_b_source']} "
              f"{row['latency_ms']}ms", flush=True)

    meta = _meta(data, out_path, report_path, items, mode, arms, seed, base_url,
                 bias_check, limit, teacher_client, local_client, previous, started,
                 local_props=local_props)
    meta["retry"] = dict(retry_info)
    _apply_cumulative_elapsed(meta, previous_elapsed)
    summary = summarize(rows)
    write_json(out_path, {"meta": meta, "summary": summary, "items": rows})
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(render_report(summary, meta), encoding="utf-8")
    ov = summary["overall"]
    print(f"[g3] 完成：rows={ov['rows']} judged={ov['decisive'] + ov['ties']} "
          f"local 胜 {ov['a_wins']} / teacher 胜 {ov['b_wins']} / 平 {ov['ties']}；"
          f"local 胜率(不含平)={ov['a_win_rate']:.1%} "
          f"Wilson 95%=[{ov['wilson_95']['low']:.1%}, {ov['wilson_95']['high']:.1%}] "
          f"跳过={skipped}；-> {out_path}", flush=True)
    print(f"[g3] 报告 -> {report_path}", flush=True)
    if retry_info["rows"]:
        print(f"[g3] 失败重试：{retry_info['rows']} 条 winner=null → 补齐 "
              f"{retry_info['fixed']} 条（仍缺 {retry_info['rows'] - retry_info['fixed']} 条）",
              flush=True)
    return {"meta": meta, "summary": summary, "items": rows, "skipped": skipped}


def _meta(data, out_path, report_path, items, mode, arms, seed, base_url,
          bias_check, limit, teacher_client, local_client, previous,
          started, local_props: dict | None = None) -> dict:
    return {
        "tool": "W4-G3 downstream blind eval",
        "data": str(data),
        "out": str(out_path),
        "report": str(report_path),
        "mode": mode,
        "arms": arms,
        "n_items": len(items),
        "n_resumed": len(previous),
        "limit": limit,
        "seed": seed,
        "bias_check": bias_check,
        "base_url": base_url if (mode == "real" and arms in ("both", "local")) else None,
        "teacher_model": getattr(teacher_client, "model", None),
        "local_model": getattr(local_client, "model", None),
        "local_server_props": local_props or None,
        "inner_temperature": INNER_TEMPERATURE,
        "reply_temperature": REPLY_TEMPERATURE,
        "inner_max_tokens": INNER_MAX_TOKENS,
        "reply_max_tokens": REPLY_MAX_TOKENS,
        "max_turns": MAX_TURNS,
        "local_prompt": "soul.prompt.build_messages（训练/评测同源）",
        "teacher_prompt": "soul.dataset.teacher_messages（打标签同源 + JSON schema 约束）",
        "state_policy": {
            "golden": "bucket→state_profile 映射后 profile_state()",
            "soul_sft": "解析 context[0].text（system）里的状态行",
            "bucket_profile": BUCKET_PROFILE,
        },
        "started_at": started,
        "finished_at": time.time(),
        "elapsed_s": round(time.time() - started, 2),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m agi.eval_data.downstream",
        description="W4-G3 下游盲评：本地内状态块 vs teacher 内状态块",
    )
    ap.add_argument("--data", default=str(DEFAULT_DATA),
                    help=f"输入 JSONL（golden_v1 或 soul_sft_*，默认 {DEFAULT_DATA}）")
    ap.add_argument("--out", default=str(DEFAULT_OUT),
                    help=f"逐条结果 JSON（默认 {DEFAULT_OUT}）")
    ap.add_argument("--report", default=str(DEFAULT_REPORT),
                    help=f"Markdown 报告（默认 {DEFAULT_REPORT}）")
    ap.add_argument("--arms", choices=ARMS, default="both",
                    help="both=两臂对比；local/teacher=只跑单臂（单臂不盲评，用于打通链路）")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0=全部）")
    ap.add_argument("--seed", type=int, default=42, help="左右随机种子（可复现）")
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL,
                    help=f"llama-server 地址（默认 {DEFAULT_BASE_URL}）")
    ap.add_argument("--timeout-s", type=float, default=30.0,
                    help="llama-server 单次请求超时秒数")
    ap.add_argument("--bias-check", type=int, default=0,
                    help="对前 N 条做双向重评，检查裁判位置偏差（0=不检查）")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑：读 --out 里已有 items，跳过同 golden_id")
    ap.add_argument("--retry-failed", action="store_true",
                    help="配合 --resume（隐含 resume）：只重跑上轮 winner=null 的条目"
                         "（为空的那一臂重生成回复、再重判），用于修复瞬态 API 错误")
    ap.add_argument("--mock", action="store_true",
                    help="用确定性假 client（不联网、不需要 key/llama-server）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.limit < 0:
        raise SystemExit("--limit 不能为负")
    if args.bias_check < 0:
        raise SystemExit("--bias-check 不能为负")
    try:
        run_downstream(
            data=args.data, out=args.out, report=args.report, arms=args.arms,
            limit=args.limit, seed=args.seed, base_url=args.base_url,
            timeout_s=args.timeout_s, mock=args.mock,
            resume=args.resume or args.retry_failed,
            bias_check=args.bias_check, retry_failed=args.retry_failed)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
