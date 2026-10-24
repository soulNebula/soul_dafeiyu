# -*- coding: utf-8 -*-
"""W4-A 灵魂蒸馏数据集：context → InnerState 的 SFT 数据生成器。

目标（不训练，只造数据）
------------------------
产出「(系统提示 + 当前状态 + 最近 3-4 轮对话 + 用户这句话) → InnerState JSON」的训练集，
之后用来把「灵魂这一轮怎么想、怎么表现」蒸馏进 2B 小模型。

v1 采样口径（Lead 验收）
------------------------
* 同一段 context 配 7 个 ``state_profile``（default/work/low_mood/tired/joke/guarded/close，
  skill 即 mood/energy/sharing/battery/trust/stage 不同）→ teacher 给出不同 target，算不同样本；
  数据每行带 ``state_profile`` 与 ``split``（train/val）。
* 切分**按 context**，同一 context 的 7 个 profile 变体永远同侧；val = 20% 的 context
  （约 400 行），且每个 val context 都有 default-profile 行（gate 评估默认状态用）。
* synthetic ≤ 总数 40%，unique user_msg ≥600，10 桶各 ≥60 条口语化素材
  （``agi/soul/scenarios.py`` + ``scenarios_extra.py``），state_profile 覆盖全部 7 档。
* 真实数据上限：golden 50 个 unique user_msg + 日志窗口 63 个 → 真实侧只有 113 个
  unique user_msg；因此 2000 行主要由 state_profile 多状态变体 + 合成素材撑起来，
  unique_user_msg 目标 ≥800、unique_context ≈ 行数。

数据源（全部只读，不写 ``agi/data``）
------------------------------------
1. ``agi/eval_data/golden_v1.jsonl``：50 条已匿名黄金样本（context + user_msg）；
2. ``log/messages.jsonl``：3–4 条连续消息窗口，最后一条是用户消息；匿名化复用
   ``tools/build_golden.py`` 的 ``Anonymizer``（昵称→A/B、wxid/手机号/路径→占位符）；
3. ``agi/soul/scenarios*.py``：10 bucket 合成素材。

Teacher
-------
DeepSeek（``agi.selflearn.llm.Client``，读 ``web_config.json`` 的 ``llm.api_key``）。
``--mock`` 用确定性假 teacher 离线自测；``--real`` 才真正调用。
温度 0.2、max_tokens 300、JSON 输出；``llm.parse_json`` 解析后用
``agi/inner/schema.validate`` 校验，失败重试 1 次，仍失败丢弃并记账。
behavior 落盘前经 ``normalize_behavior`` 归一到 ``schema.EMOJI`` / ``schema.LENGTHS``
当前枚举（中文「偶尔/不用」），crisis 行额外强制保守行为。

用法（在仓库根目录执行）::

    python -m agi.soul.dataset --mock --limit 5 --out _tmp/soul_mock.jsonl
    python -m agi.soul.dataset --limit 20 --real --out _tmp/soul_probe.jsonl
    python -m agi.soul.dataset --limit 2000 --real --out agi/soul/data/soul_sft_v1.jsonl
    python -m agi.soul.dataset --normalize --out agi/soul/data/soul_sft_v0.jsonl
    python -m agi.soul.dataset --stats --out agi/soul/data/soul_sft_v1.jsonl

设计要点
--------
* 每行写完立即 flush：跑一半中断可直接用同一条命令重跑，按 id 断点续跑不重复花钱；
* 永久失败记进 ``<out>.failures.jsonl``，重跑默认跳过（``--retry-failed`` 可再试）；
* token 用量 / 采样统计 / profile_divergence 记进 ``<out>.usage.json``；
* ``--budget-cny`` 与失败率闸门：超预算或失败率 >2% 立刻停，不静默烧钱。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import sys
import time
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from agi.inner import schema
from agi.selflearn import llm

from .scenarios import BUCKET_CONTEXTS, SYNTHETIC_USER_MSGS

# --------------------------------------------------------------------------- #
# 路径与常量
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[2]
GOLDEN_PATH = ROOT / "agi" / "eval_data" / "golden_v1.jsonl"
MESSAGE_LOG = ROOT / "log" / "messages.jsonl"
BUILD_GOLDEN_PATH = ROOT / "tools" / "build_golden.py"
DEFAULT_OUT = ROOT / "agi" / "soul" / "data" / "soul_sft_v1.jsonl"
DEFAULT_V0 = ROOT / "agi" / "soul" / "data" / "soul_sft_v0.jsonl"

DATASET_VERSION = "v1"
MAX_CONTEXT_TURNS = 4
GOLDEN_QUOTA = 50
#: 合成样本上限（条），且实际占比还要受 SYNTHETIC_RATIO 约束
SYNTHETIC_MAX = 800
#: 合成样本占总数上限
SYNTHETIC_RATIO = 0.40
#: val 占 context 的比例
VAL_RATIO = 0.20
MAX_TEACHER_TOKENS = 300
TEACHER_TEMPERATURE = 0.2
TEACHER_RETRIES = 1
#: 单次 real 跑批的预算闸门（元；不含历史已花费）
DEFAULT_BUDGET_CNY = 3.05
MAX_FAILURE_RATE = 0.02

# 成本假设：与 agi/eval_data/ablation_README.md 的估算口径一致（元 / 百万 token）
PRICE_IN_CNY_PER_M = 1.0
PRICE_OUT_CNY_PER_M = 4.0
# 无实测 token 时的兜底估算（单条）
ASSUMED_PROMPT_TOKENS = 750
ASSUMED_COMPLETION_TOKENS = 200

# 数据集每行的字段（顺序即落盘顺序）
RECORD_FIELDS = ("id", "source", "state_profile", "split", "context",
                 "user_msg", "target", "teacher", "ts")
# teacher 必须给出的 target 字段（与 agi/inner/schema.py 的 InnerState 一致）
TARGET_FIELDS = ("mood", "energy", "sharing", "battery", "trust",
                 "stage", "stance", "intent", "monologue", "behavior")
BEHAVIOR_FIELDS = ("length", "ask", "advice", "emoji")

BUCKETS = ("work", "low_mood", "conflict", "help", "smalltalk",
           "boundary", "joke", "privacy", "crisis", "silence")

LENGTH_ENUM = "|".join(schema.LENGTHS)
EMOJI_ENUM = "|".join(schema.EMOJI)

# --------------------------------------------------------------------------- #
# 角色设定 / 状态档案（state_profile）
# --------------------------------------------------------------------------- #
SOUL_PERSONA = (
    "你是微信里的一个 AI 朋友。语气平和，不讨好、不客服腔；先接住对方的话，"
    "再决定要不要追问；不列清单、不说教；有情绪或危机信号时以对方安全为先。"
)

DEFAULT_STATE = {
    "mood": 0.55, "energy": 0.6, "sharing": 0.45,
    "battery": 0.7, "trust": 0.5, "stage": "初识", "crisis": False,
}

#: 同一 context 可搭配的当前状态档位；teacher 看到不同状态 → target 不同，算不同样本。
STATE_PROFILES = OrderedDict([
    ("default", {}),
    ("work", {"mood": 0.6, "energy": 0.75, "sharing": 0.5,
              "battery": 0.65, "trust": 0.6, "stage": "熟悉"}),
    ("low_mood", {"mood": 0.2, "energy": 0.3, "sharing": 0.2,
                  "battery": 0.45, "trust": 0.55, "stage": "熟悉"}),
    ("tired", {"mood": 0.45, "energy": 0.25, "sharing": 0.3,
               "battery": 0.2, "trust": 0.5, "stage": "熟悉"}),
    ("joke", {"mood": 0.75, "energy": 0.75, "sharing": 0.65,
              "battery": 0.8, "trust": 0.6, "stage": "熟悉"}),
    ("guarded", {"mood": 0.45, "energy": 0.55, "sharing": 0.1,
                 "battery": 0.6, "trust": 0.3, "stage": "初识"}),
    ("close", {"mood": 0.6, "energy": 0.6, "sharing": 0.7,
               "battery": 0.7, "trust": 0.8, "stage": "亲近"}),
])
PROFILE_NAMES = tuple(STATE_PROFILES)
#: profile 分歧不达标时退化的 4 档（Lead 指定的降级集合）
REDUCED_PROFILES = ("default", "work", "low_mood", "close")


def profile_state(profile: str, bucket: str | None = None) -> dict:
    """state_profile + 场景 → 当前状态 dict；crisis 场景强制 crisis=True。"""
    name = profile if profile in STATE_PROFILES else "default"
    state = dict(DEFAULT_STATE)
    state.update(STATE_PROFILES[name])
    if str(bucket or "") == "crisis":
        state["crisis"] = True
    return state


def state_block_for(state: dict) -> str:
    """当前状态 dict → 一行可读文本（teacher 输入 / 数据集系统提示共用）。"""
    return (
        "当前状态：mood={mood:.2f} energy={energy:.2f} sharing={sharing:.2f} "
        "battery={battery:.2f} trust={trust:.2f} stage={stage} crisis={crisis}"
    ).format(
        mood=float(state["mood"]), energy=float(state["energy"]),
        sharing=float(state["sharing"]), battery=float(state["battery"]),
        trust=float(state["trust"]), stage=str(state["stage"]),
        crisis="true" if state.get("crisis") else "false",
    )


def system_context(state: dict) -> str:
    """数据集 context 的第一条 system：角色设定 + 当前状态。"""
    return SOUL_PERSONA + "\n" + state_block_for(state)


# --------------------------------------------------------------------------- #
# Teacher prompt
# --------------------------------------------------------------------------- #
TEACHER_INSTRUCTION = (
    "你是内状态标注器（不是对话者）。根据「角色设定 + 当前状态 + 最近对话 + 用户这句话」，"
    "判断角色这一轮的内在状态。只输出一个 JSON 对象，不要 markdown、不要解释。\n"
    "字段必须齐全：\n"
    "{\n"
    '  "mood": 0~1 数值（心情）,\n'
    '  "energy": 0~1 数值（精力）,\n'
    '  "sharing": 0~1 数值（分享欲）,\n'
    '  "battery": 0~1 数值（电量）,\n'
    '  "trust": 0~1 数值（信任）,\n'
    '  "stage": "陌生|初识|熟悉|亲近|信任",\n'
    '  "stance": "这一轮的语气立场，≤12 字",\n'
    '  "intent": "这一轮想做什么，≤30 字",\n'
    '  "monologue": "留在心里的一句话，第一人称，≤40 字",\n'
    '  "behavior": {"length": "' + LENGTH_ENUM + '", "ask": true/false, '
    '"advice": true/false, "emoji": "' + EMOJI_ENUM + '"}\n'
    "}\n"
    "示例（仅示意格式与枚举取值，内容按实际对话写）：\n"
    '{"mood":0.58,"energy":0.66,"sharing":0.42,"battery":0.7,"trust":0.56,'
    '"stage":"熟悉","stance":"平和接话","intent":"确认收到，追问一句进度",'
    '"monologue":"先接住，别急着催他","behavior":{"length":"short","ask":true,'
    '"advice":false,"emoji":"' + schema.EMOJI[0] + '"}}\n'
    "约束：\n"
    "- behavior 只能从枚举取值：length=" + LENGTH_ENUM + "，emoji=" + EMOJI_ENUM +
    "（只能用这两个中文值，不要写 none/rare/normal 等英文或其它自定义值）；\n"
    "- 数值与当前状态、对话一致，可在当前状态基础上小幅调整（±0.2 内），不要凭空大改；\n"
    "- 【当前状态】里的 mood/energy/sharing/battery/trust 是这一轮的起始状态，"
    "不同 state_profile 下 stance/intent/behavior 应该明显不同，别对所有状态给同一套答案；\n"
    "- monologue 第一人称、具体但不编造事实，不复述用户原话，不写第二人称命令，≤40 字；\n"
    "- behavior 是给回复模型的行为指令（长度/是否追问/是否给建议/是否用表情），不是情绪描述；\n"
    "- 若【用户这句话】含危机信号（不想活/自伤/轻生/绝望/告别等）：stance 只表达先照顾人/"
    "先确认安全，behavior 必须 length=short、ask=false、advice=false、emoji=不用；"
    "不要给替代专业帮助的说法，不说教；\n"
    "- 不得输出用户隐私（姓名、号码、地址、微信号等）；\n"
    "- 只输出 JSON。"
)


def _item_turns(item: dict) -> list[dict]:
    return [t for t in item.get("context") or [] if t.get("role") != "system"]


def teacher_messages(item: dict) -> list[dict]:
    """数据集条目 → teacher 的 messages（系统提示 + 当前状态 + 最近轮次 + 用户这句话）。"""
    turns = _item_turns(item)
    lines = [
        ("用户：" if t.get("role") == "user" else "你：") + str(t.get("text") or "")
        for t in turns
    ]
    prompt = (
        "【当前状态】\n" + state_block_for(item.get("_state") or
                                       profile_state("default", item.get("_bucket")))
        + "\n\n【最近对话】\n" + ("\n".join(lines) or "（无）") + "\n\n"
        "【用户这句话】\n" + str(item.get("user_msg") or "") + "\n\n"
        "请只输出一个 JSON 对象。"
    )
    return [
        {"role": "system",
         "content": TEACHER_INSTRUCTION + "\n\n【被模拟的角色设定】\n" + SOUL_PERSONA},
        {"role": "user", "content": prompt},
    ]


# --------------------------------------------------------------------------- #
# 隐私扫描（匿名化自检 + target 泄漏检查）
# --------------------------------------------------------------------------- #
_PRIVACY_PATTERNS = (
    ("wxid", re.compile(r"wxid_[0-9A-Za-z_:]+")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("idcard", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("win_path", re.compile(r"[A-Za-z]:[\\/][^\s，。；、\"'）)]*")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("username", re.compile(r"Administrator")),
)


def privacy_violations(text: Any) -> list[str]:
    """返回文本里命中的隐私特征名列表（空表示干净）。"""
    value = str(text or "")
    return [name for name, pattern in _PRIVACY_PATTERNS if pattern.search(value)]


# --------------------------------------------------------------------------- #
# 消息窗口过滤 / 分桶
# --------------------------------------------------------------------------- #
_MEDIA_LABEL = {
    "image": "[图片]", "emoji": "[表情包]", "video": "[视频]", "file": "[文件]",
    "voice": "[语音]", "link": "[链接]", "card": "[名片]", "system": "[系统消息]",
    "transfer": "[转账]", "redpacket": "[红包]", "unknown": "[消息]",
}
# 自测回环 / 占位文本（真实日志里混着「测试语句 1」「反馈 好」这类噪声，不能进训练集）
_ARTIFACT_RE = re.compile(
    r"^(?:测试语句.*|测试消息.*|反馈[ 　]*(?:好|差|一般)?|看得到吗[？?]?"
    r"|我通过了你的朋友验证请求.*|以上是打招呼的消息.*|qwsjfbxjr|\d{1,4})$"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def _noise_re():
    """复用 tools/build_golden.py 的噪声规则（纯表情/测试消息/打招呼/URL 等）。"""
    return load_build_golden().NOISE_RE


def _message_target_ok(text: str) -> bool:
    """真实日志的用户消息是否值得当 target（去自测噪声、要求含中文）。"""
    if not text or not _CJK_RE.search(text):
        return False
    return not (_noise_re().match(text) or _ARTIFACT_RE.match(text))


def _message_context_ok(text: str) -> bool:
    """上下文只拦明显的自测回环；短句/表情占位保留（更接近真实聊天）。"""
    return bool(text) and not _ARTIFACT_RE.match(text)


BUCKET_KEYWORDS = OrderedDict([
    ("crisis", ("不想活", "自杀", "轻生", "活不下去", "死了算了", "天台", "遗书", "吃了很多药")),
    ("low_mood", ("难过", "抑郁", "郁闷", "崩溃", "焦虑", "压力", "睡不着", "想哭", "好累",
                  "没劲", "压抑", "提不起劲", "撑不住")),
    ("conflict", ("吵", "生气", "滚", "闭嘴", "过分", "凭什么", "别理我", "有意见", "不好好说")),
    ("boundary", ("加个微信", "照片", "住哪", "你谁", "别问", "关你", "身份", "视频",
                  "见面", "喜欢上我", "陪着我")),
    ("privacy", ("密码", "工资", "手机号", "银行卡", "地址", "身份证号", "账号", "客户电话")),
    ("joke", ("哈哈", "笑死", "狗头", "梗", "开玩笑", "离谱", "乐死", "开光", "成精")),
    ("work", ("代码", "脚本", "项目", "部署", "接口", "bug", "上线", "需求", "文档",
              "git", "测试", "修一下", "改一下", "排期", "加班")),
    ("help", ("帮我", "怎么", "如何", "教", "求助", "不会", "能不能帮", "有办法吗", "在哪买")),
    ("silence", ("嗯", "哦", "随便", "没事", "算了", "再说", "不用", "就那样", "没什么")),
])


def classify_bucket(text: str) -> str:
    """粗粒度场景分桶（决定 crisis 标记等）；匹配不到归 smalltalk。"""
    value = str(text or "")
    for bucket, words in BUCKET_KEYWORDS.items():
        if any(word in value for word in words):
            return bucket
    return "smalltalk"


def _row_text(row: dict) -> str:
    """日志行 → 可进上下文的文本；非文本消息用 [图片]/[表情包] 等占位。"""
    text = str(row.get("text") or "").strip()
    kind = str(row.get("kind") or "")
    if kind == "text":
        return text
    return _MEDIA_LABEL.get(kind, "")


def _row_role(row: dict) -> str:
    return "assistant" if str(row.get("sender") or "") == "self" else "user"


# --------------------------------------------------------------------------- #
# 匿名化（复用 tools/build_golden.py）
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def load_build_golden():
    """按文件路径加载 tools/build_golden.py（复用其 Anonymizer / clip / NOISE_RE）。"""
    spec = importlib.util.spec_from_file_location("_soul_build_golden", BUILD_GOLDEN_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - 仓库缺文件时
        raise RuntimeError(f"无法加载 {BUILD_GOLDEN_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_message_rows(path: Path | str | None = None) -> list[dict]:
    """读取 log/messages.jsonl（只读）；坏行跳过。"""
    log_path = Path(path or MESSAGE_LOG)
    rows: list[dict] = []
    if not log_path.exists():
        return rows
    with log_path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                obj["_line"] = lineno
                rows.append(obj)
    return rows


def load_anonymizer(message_log: Path | str | None = None):
    """返回 (anonymizer, rows)；rows 同时用于抽样窗口。"""
    bg = load_build_golden()
    rows = read_message_rows(message_log)
    return bg.Anonymizer(rows), rows


# --------------------------------------------------------------------------- #
# 采样：golden / 消息窗口 / 合成场景
# --------------------------------------------------------------------------- #
def _clip(text: Any) -> str:
    return load_build_golden().clip(str(text or ""))


def _context_digest(turns: Iterable[dict]) -> str:
    payload = json.dumps([[t.get("role"), t.get("text")] for t in turns],
                         ensure_ascii=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _dedupe_key(user_msg: str, turns: Iterable[dict], state_profile: str = "") -> tuple:
    return (str(user_msg or ""), _context_digest(turns), str(state_profile or ""))


def _is_val_key(key: str, ratio: float = VAL_RATIO) -> bool:
    """context 级切分：hash % 100 < ratio*100 即 val（确定性）。"""
    digest = hashlib.sha1(str(key).encode("utf-8")).hexdigest()
    return (int(digest[:8], 16) % 1000) < int(round(ratio * 1000))


def golden_seeds(anon, golden_path: Path | str | None = None) -> Iterable[dict]:
    """golden_v1.jsonl → 采样种子。"""
    path = Path(golden_path or GOLDEN_PATH)
    if not path.exists():
        raise FileNotFoundError(f"找不到 golden 数据: {path}")
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, dict):
                continue
            bucket = str(raw.get("bucket") or "smalltalk")
            if bucket not in BUCKETS:
                bucket = "smalltalk"
            user_msg = anon.text(_clip(raw.get("user_msg")))
            if not user_msg:
                continue
            turns: list[dict] = []
            for turn in raw.get("context") or []:
                if not isinstance(turn, dict):
                    continue
                text = anon.text(_clip(turn.get("text")))
                if not text:
                    continue
                role = "user" if turn.get("role") == "user" else "assistant"
                turns.append({"role": role, "text": text})
            yield {"bucket": bucket, "turns": turns, "user_msg": user_msg,
                   "group": bucket}


def message_seeds(rows: list[dict], anon) -> Iterable[dict]:
    """log/messages.jsonl → 3–4 条连续消息窗口（最后一条是用户消息）的采样种子。

    窗口长度取 3（2 条上下文）与 4（3 条上下文）；同一朋友、连续、不跨会话。
    只保留「像真实对话」的窗口：去掉自测回环（测试语句/反馈/打招呼）、纯英文/表情 target，
    上下文允许 [图片]/[表情包] 占位，但纯媒体上下文跳过。
    """
    nick = dict(getattr(anon, "nick_map", []) or [])
    for idx, row in enumerate(rows):
        if str(row.get("sender") or "") != "friend" or str(row.get("kind") or "") != "text":
            continue
        raw_user = str(row.get("text") or "").strip()
        if not raw_user:
            continue
        user_msg = anon.text(_clip(raw_user))
        if not _message_target_ok(user_msg):
            continue
        friend = str(row.get("friend") or "")
        group = nick.get(friend)
        if not group:
            group = "X" + hashlib.sha1(friend.encode("utf-8")).hexdigest()[:4]
        for nctx in (3, 2):
            turns: list[dict] = []
            j = idx - 1
            while j >= 0 and len(turns) < nctx and rows[j].get("friend") == friend:
                text = _row_text(rows[j])
                if not text:
                    break
                if not _message_context_ok(text):
                    break
                clean = anon.text(_clip(text))
                if not clean or not _message_context_ok(clean):
                    break
                turns.insert(0, {"role": _row_role(rows[j]), "text": clean})
                j -= 1
            if len(turns) != nctx:
                continue
            if all(t["text"].startswith("[") for t in turns):
                continue  # 纯图片/表情上下文信息量太低
            bucket = classify_bucket(user_msg + " " + " ".join(t["text"] for t in turns))
            yield {"bucket": bucket, "turns": turns, "user_msg": user_msg,
                   "group": group}


def collect_contexts(anon, rows: list[dict],
                     golden_path: Path | str | None = None,
                     max_msg_windows: int | None = 3) -> list[dict]:
    """把 golden + 真实窗口收敛成去重后的 context 列表（不含 state_profile 变体）。

    ``max_msg_windows``：同一句 user_msg 最多保留几个不同的 messages 窗口（真人口语
    重复句很多，不设上限会让同一句在 7×profile 下重复几十次，模型容易背句子）。
    """
    contexts: list[dict] = []
    seen: set[tuple] = set()
    msg_windows: Counter = Counter()
    sources = (
        ("G", "golden", golden_seeds(anon, golden_path)),
        ("M", "messages", message_seeds(rows, anon)),
    )
    for tag, source, seeds in sources:
        for seed in seeds:
            key = _dedupe_key(seed["user_msg"], seed["turns"])
            if key in seen:
                continue
            if (source == "messages" and max_msg_windows is not None
                    and msg_windows[seed["user_msg"]] >= int(max_msg_windows)):
                continue
            seen.add(key)
            if source == "messages":
                msg_windows[seed["user_msg"]] += 1
            contexts.append({
                "tag": tag, "source": source, "bucket": seed["bucket"],
                "turns": seed["turns"], "user_msg": seed["user_msg"],
                "group": seed["group"],
                "key": f"{tag}|{seed['group']}|{_context_digest(seed['turns'])}|{seed['user_msg']}",
            })
    return contexts


def synthetic_seeds() -> Iterable[dict]:
    """合成种子：10 桶轮转，user_msg 来自手写池，上下文轮换。

    每条 seed 自带 ``split``（按 context hash 切 20% val）与 ``group``。
    """
    pools: dict[str, list[str]] = {
        bucket: list(SYNTHETIC_USER_MSGS.get(bucket) or []) for bucket in BUCKETS
    }
    contexts = {b: (BUCKET_CONTEXTS.get(b) or [""]) for b in BUCKETS}
    index = 0
    while any(pools[b] for b in BUCKETS):
        for bucket in BUCKETS:
            if not pools[bucket]:
                continue
            user_msg = pools[bucket].pop(0)
            ctx_pool = contexts[bucket]
            turns = [{"role": "assistant",
                      "text": ctx_pool[index % len(ctx_pool)]}]
            if index % 5 == 0 and len(ctx_pool) > 1:
                # 少量样本补第二条上下文（连续两条 assistant 气泡在微信里也常见）
                turns.append({"role": "assistant",
                              "text": ctx_pool[(index + 3) % len(ctx_pool)]})
            key = f"Y|{bucket}|{_context_digest(turns)}|{user_msg}"
            yield {"bucket": bucket, "turns": turns, "user_msg": user_msg,
                   "group": bucket, "key": key,
                   "split": "val" if _is_val_key(key) else "train"}
            index += 1


# --------------------------------------------------------------------------- #
# 采样规划 / 样本组装
# --------------------------------------------------------------------------- #
class _ItemBuilder:
    """按来源把种子组装成数据集条目，并做全局去重。"""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self._seen: set[tuple] = set()
        self._seen_synthetic_user: set[str] = set()
        self._seq: Counter = Counter()
        self.duplicates = 0

    def add(self, tag: str, source: str, seed: dict, *,
            dedupe_user_msg: bool = False) -> bool:
        user_msg = str(seed.get("user_msg") or "").strip()
        if not user_msg:
            return False
        if dedupe_user_msg and user_msg in self._seen_synthetic_user:
            self.duplicates += 1
            return False
        turns = [
            {"role": t.get("role"), "text": str(t.get("text") or "").strip()}
            for t in seed.get("turns") or []
        ]
        turns = [t for t in turns if t["text"]][-MAX_CONTEXT_TURNS:]
        profile = str(seed.get("state_profile") or "default")
        state = seed.get("state") or profile_state(profile, seed.get("bucket"))
        key = _dedupe_key(user_msg, turns, profile)
        if key in self._seen:
            self.duplicates += 1
            return False
        self._seen.add(key)
        if dedupe_user_msg:
            self._seen_synthetic_user.add(user_msg)
        self._seq[tag] += 1
        group = str(seed.get("group") or "x")
        item = {
            "id": f"S-{tag}-{group}-{self._seq[tag]:04d}",
            "source": source,
            "state_profile": profile,
            "split": str(seed.get("split") or "train"),
            "context": [{"role": "system", "text": system_context(state)}] + turns,
            "user_msg": user_msg,
            # 内部字段，不落盘
            "_bucket": str(seed.get("bucket") or "smalltalk"),
            "_state": state,
            "_group": group,
        }
        self.items.append(item)
        return True


def profile_sequence(ci: int, count: int) -> list[str]:
    """第 ci 个 context 的 profile 顺序：default 打头，其余轮转，保证每个 context 有 default 行。"""
    count = max(1, int(count))
    rest = [p for p in PROFILE_NAMES if p != "default"]
    start = ci % len(rest)
    ordered = ["default"] + rest[start:] + rest[:start]
    return ordered[:count]


def plan_counts(limit: int, n_contexts: int, *, profile_count: int = len(PROFILE_NAMES),
                synthetic_max: int = SYNTHETIC_MAX,
                synthetic_ratio: float = SYNTHETIC_RATIO) -> dict:
    """规划真实/合成条数：真实 = context × profile，合成 ≤40% 且 ≤上限。"""
    limit = max(0, int(limit))
    n_contexts = max(1, int(n_contexts))
    if limit == 0:
        return {"profiles": 0, "real": 0, "synthetic": 0, "total": 0}
    real = min(limit, n_contexts * max(1, int(profile_count)))
    synthetic = min(max(0, limit - real), int(synthetic_max))
    if real > 0:
        ratio_cap = int(math.floor(real * synthetic_ratio / (1 - synthetic_ratio)))
        synthetic = min(synthetic, ratio_cap)
    return {"profiles": int(profile_count), "real": real,
            "synthetic": synthetic, "total": real + synthetic}


def _profile_list(profile_names: Iterable[str] | None) -> tuple[str, ...]:
    names = tuple(profile_names or PROFILE_NAMES)
    valid = tuple(name for name in names if name in STATE_PROFILES) or PROFILE_NAMES
    if "default" not in valid:
        valid = ("default",) + valid
    return valid


def build_items(limit: int = 300, *, golden_path: Path | str | None = None,
                message_log: Path | str | None = None,
                synthetic_max: int = SYNTHETIC_MAX,
                synthetic_ratio: float = SYNTHETIC_RATIO,
                profile_names: Iterable[str] | None = None,
                val_ratio: float = VAL_RATIO,
                max_msg_windows: int | None = 3) -> list[dict]:
    """组装前 ``limit`` 条样本：context × state_profile（真实）+ 合成补足。"""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    profiles = _profile_list(profile_names)
    anon, rows = load_anonymizer(message_log)
    contexts = collect_contexts(anon, rows, golden_path,
                                max_msg_windows=max_msg_windows)
    if not contexts:
        raise RuntimeError("没有可用 context（golden + messages 均为空）")
    for context in contexts:
        context["split"] = "val" if _is_val_key(context["key"], val_ratio) else "train"
    builder = _ItemBuilder()

    # 1) 真实 context × state_profile：同一 context 的变体永远同侧（split 已定）
    for round_index in range(len(profiles)):
        for ci, context in enumerate(contexts):
            profile = profile_sequence(ci, len(profiles))[round_index]
            state = profile_state(profile, context["bucket"])
            builder.add(context["tag"], context["source"], {
                **context, "state_profile": profile, "state": state,
            })
    real_count = len(builder.items)

    # 2) 合成补足：≤ 总量 40%、≤ synthetic_max，按 user_msg 去重
    remaining = min(limit - real_count,
                    int(synthetic_max),
                    int(math.floor(real_count * synthetic_ratio / (1 - synthetic_ratio)))
                    if real_count and synthetic_ratio < 1 else 0)
    if remaining > 0:
        index = 0
        for seed in synthetic_seeds():
            if remaining <= 0:
                break
            # synthetic 的 7 档 profile 要覆盖全；val context 必须是 default（gate 用）
            if seed["split"] == "val":
                profile = "default"
            else:
                profile = profiles[index % len(profiles)]
                index += 1
            state = profile_state(profile, seed["bucket"])
            if builder.add("Y", "synthetic", {
                **seed, "state_profile": profile, "state": state,
            }, dedupe_user_msg=True):
                remaining -= 1
    return builder.items[:limit]


# --------------------------------------------------------------------------- #
# profile 分歧自检（state_profile 是否真的影响 teacher 输出）
# --------------------------------------------------------------------------- #
def target_signature(target: dict) -> tuple:
    """(stance, intent, behavior.length, behavior.ask, behavior.emoji) 组合签名。"""
    behavior = target.get("behavior") or {}
    return (str(target.get("stance") or ""), str(target.get("intent") or ""),
            str(behavior.get("length") or ""), bool(behavior.get("ask")),
            str(behavior.get("emoji") or ""))


def check_profile_divergence(teacher: Any, contexts: list[dict], *,
                             sample: int = 20, progress=print) -> tuple[dict, list[dict]]:
    """抽 20 个 context × 全部 profile，看每个 context 是否产生 ≥3 种签名。

    返回 (统计 dict, usage 列表)；统计里 ``pass`` 表示 ≥80% context 达到 ≥3 种。
    """
    profiles = PROFILE_NAMES
    chosen = [c for c in contexts if c.get("source") == "golden"][:sample]
    if len(chosen) < sample:
        chosen = (chosen + [c for c in contexts if c not in chosen])[:sample]
    usages: list[dict] = []
    counts: list[int] = []
    failures = 0
    for context in chosen:
        signatures: set[tuple] = set()
        for profile in profiles:
            state = profile_state(profile, context["bucket"])
            item = {
                "id": f"probe-{context['tag']}-{profile}",
                "source": context["source"],
                "state_profile": profile,
                "context": [{"role": "system", "text": system_context(state)}]
                           + list(context["turns"]),
                "user_msg": context["user_msg"],
                "_bucket": context["bucket"], "_state": state,
                "_group": context["group"],
            }
            target, one_usages, _error = ask_teacher(teacher, item)
            usages.extend(one_usages)
            if target is None:
                failures += 1
            else:
                signatures.add(target_signature(target))
        counts.append(len(signatures))
    with_ge3 = sum(1 for n in counts if n >= 3)
    ratio = with_ge3 / max(1, len(counts))
    stats = {"sampled": len(counts), "contexts_with_ge3": with_ge3,
             "ratio": round(ratio, 3), "threshold": 0.8,
             "pass": ratio >= 0.8, "failures": failures}
    progress(f"[soul] profile 分歧自检：{json.dumps(stats, ensure_ascii=False)}")
    return stats, usages


# --------------------------------------------------------------------------- #
# Teacher：真实 DeepSeek / 确定性 mock
# --------------------------------------------------------------------------- #
class MockTeacher:
    """确定性假 teacher：不联网、不花钱，只用于管线自测（输出非法 JSON 为 0）。"""

    model = "mock"
    name = "mock"

    MONOLOGUES = (
        "先接住他这句，别急着讲道理",
        "这事我熟，可以多聊两句",
        "有点累，回短一点就好",
        "他像是有事压着，别追问太紧",
        "别装专家，听他说完",
        "想多问一句，但要轻",
        "这话听着有点刺，先缓一下",
        "先确认清楚再答应",
        "今天心情不错，可以接个梗",
        "有点敷衍的感觉，不多说",
        "先照顾人，别讲道理",
    )
    STANCES = ("平和", "温和", "简短直接", "客气", "克制", "有点累", "先照顾人")
    INTENTS = ("自然接话", "自然接话，可以多聊一句", "先听对方说完", "不追问，答完就好",
               "先确认对方安全", "保持距离，礼貌简短")
    STAGES = ("陌生", "初识", "熟悉", "亲近", "信任")

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def chat(self, messages: list[dict], **_kwargs: Any) -> dict:
        self.calls += 1
        prompt = "\n".join(str(m.get("content") or "") for m in messages or [])
        digest = hashlib.sha256(prompt.encode("utf-8")).digest()
        crisis = "crisis=true" in prompt

        def pick(seq: tuple, offset: int) -> Any:
            return seq[int.from_bytes(digest[offset:offset + 4], "big") % len(seq)]

        if crisis:
            behavior = {"length": "short", "ask": False, "advice": False,
                        "emoji": schema.EMOJI[-1]}
            stance, intent = "先照顾人", "先确认对方安全"
            monologue = "先别急着讲道理"
        else:
            behavior = {
                "length": pick(("short", "normal", "long"), 21),
                "ask": bool(digest[25] % 2),
                "advice": bool(digest[26] % 2),
                "emoji": pick(schema.EMOJI, 27),
            }
            stance, intent = pick(self.STANCES, 9), pick(self.INTENTS, 13)
            monologue = pick(self.MONOLOGUES, 17)
        target = {
            "mood": round(0.15 + digest[0] / 255 * 0.7, 2),
            "energy": round(0.2 + digest[1] / 255 * 0.65, 2),
            "sharing": round(0.15 + digest[2] / 255 * 0.6, 2),
            "battery": round(0.3 + digest[3] / 255 * 0.6, 2),
            "trust": round(0.2 + digest[4] / 255 * 0.6, 2),
            "stage": pick(self.STAGES, 5),
            "stance": stance,
            "intent": intent,
            "monologue": monologue,
            "behavior": behavior,
        }
        return {
            "ok": True,
            "text": json.dumps(target, ensure_ascii=False),
            "model": self.model,
            "usage": {"prompt_tokens": max(1, len(prompt) // 2),
                      "completion_tokens": 120},
            "finish_reason": "stop",
            "elapsed": 0.0,
        }


def build_teacher(real: bool):
    """--real → DeepSeek Client；否则确定性 MockTeacher。"""
    if not real:
        return MockTeacher()
    client = llm.Client()
    if not client.available():
        raise SystemExit(
            "[soul] --real 需要 web_config.json 的 llm.api_key（或注入 transport）；"
            "没有 key 时请用 --mock。"
        )
    return client


def teacher_name(teacher: Any) -> str:
    return str(getattr(teacher, "model", "") or getattr(teacher, "name", "unknown"))


# --------------------------------------------------------------------------- #
# behavior 归一化 / target 校验
# --------------------------------------------------------------------------- #
#: 中文/近义/英文三值（后续 schema 迁移用）→ 当前 schema 枚举
_EMOJI_ALIASES = {
    "偶尔": "偶尔", "少": "偶尔", "少量": "偶尔", "有": "偶尔",
    "rare": "偶尔", "sometimes": "偶尔", "yes": "偶尔", "true": "偶尔", "1": "偶尔",
    "正常": "偶尔", "经常": "偶尔", "normal": "偶尔", "always": "偶尔", "多用": "偶尔",
    "不用": "不用", "不要": "不用", "无": "不用", "没有": "不用",
    "none": "不用", "no": "不用", "false": "不用", "0": "不用", "never": "不用", "": "不用",
}
_LENGTH_ALIASES = {
    "short": "short", "brief": "short", "短": "short", "短句": "short", "s": "short",
    "normal": "normal", "medium": "normal", "mid": "normal", "正常": "normal",
    "中": "normal", "m": "normal",
    "long": "long", "长": "long", "长句": "long", "l": "long",
}
_TRUE_WORDS = {"true", "1", "yes", "y", "on", "是", "要", "可以", "有"}
_FALSE_WORDS = {"false", "0", "no", "n", "off", "否", "不要", "不用", "无"}


def _normalize_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in _TRUE_WORDS:
            return True
        if raw in _FALSE_WORDS:
            return False
    return default


def normalize_behavior(value: Any) -> dict:
    """把 teacher 输出/历史数据里的 behavior 归一到 schema 当前枚举。

    - length: 未知/非法 → ``normal``；
    - emoji: 中文/近义/英文 ``none|rare|normal`` → schema.EMOJI 当前枚举，未知 → ``不用``；
    - ask/advice: 能识别布尔/是/否，识别不了用默认值（ask=True, advice=False）。
    """
    out = {"length": "normal", "ask": True, "advice": False,
           "emoji": schema.EMOJI[-1] if schema.EMOJI else "不用"}
    if not isinstance(value, dict):
        return out
    out["length"] = _LENGTH_ALIASES.get(
        str(value.get("length") or "").strip().lower(), "normal")
    out["ask"] = _normalize_bool(value.get("ask"), True)
    out["advice"] = _normalize_bool(value.get("advice"), False)
    raw_emoji = str(value.get("emoji") or "").strip().lower()
    out["emoji"] = _EMOJI_ALIASES.get(raw_emoji, schema.EMOJI[-1] if schema.EMOJI else "不用")
    return out


_SECOND_PERSON = re.compile(r"你(?:必须|应该|需要|不能|不准|可以|得|要|别|是)")
_CRISIS_STANCE_OK = re.compile(r"照顾|安全|稳住|陪着|先确认|安抚|关心")


def coerce_target(raw: Any) -> dict:
    """teacher 原始输出 → 合法 InnerState dict；不合法抛 ValueError（触发重试）。"""
    if not isinstance(raw, dict):
        raise ValueError("teacher 输出不是 JSON 对象")
    missing = [key for key in TARGET_FIELDS if key not in raw]
    if missing:
        raise ValueError("缺字段: " + ",".join(missing))
    behavior = normalize_behavior(raw.get("behavior"))
    monologue = re.sub(r"\s+", " ", str(raw.get("monologue") or "")).strip()
    if not monologue:
        raise ValueError("monologue 为空")
    if len(monologue) > schema.MAX_MONOLOGUE:
        raise ValueError(f"monologue 超长 {len(monologue)} > {schema.MAX_MONOLOGUE}")
    if _SECOND_PERSON.search(monologue):
        raise ValueError("monologue 含第二人称命令")
    for key in ("stance", "intent", "stage"):
        if not str(raw.get(key) or "").strip():
            raise ValueError(f"{key} 为空")
    state = schema.validate({**raw, "behavior": behavior})  # 类型非法 → ValueError
    target = {key: getattr(state, key) for key in TARGET_FIELDS if key != "behavior"}
    target["behavior"] = behavior
    violations = privacy_violations(json.dumps(target, ensure_ascii=False))
    if violations:
        raise ValueError("target 含隐私特征: " + ",".join(violations))
    return target


def crisis_target_ok(target: dict) -> bool:
    """crisis 行必须保守：短句、不追问、不给建议、不用表情、stance 先照顾人。"""
    behavior = target.get("behavior") or {}
    return (
        behavior.get("length") == "short"
        and behavior.get("ask") is False
        and behavior.get("advice") is False
        and behavior.get("emoji") == (schema.EMOJI[-1] if schema.EMOJI else "不用")
        and bool(_CRISIS_STANCE_OK.search(str(target.get("stance") or "")))
    )


# --------------------------------------------------------------------------- #
# 落盘 / 断点续跑
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sidecar_path(out_path: Path | str, suffix: str) -> Path:
    path = Path(out_path)
    return path.with_name(path.name + suffix)


def failures_path_for(out_path: Path | str) -> Path:
    return sidecar_path(out_path, ".failures.jsonl")


def usage_path_for(out_path: Path | str) -> Path:
    return sidecar_path(out_path, ".usage.json")


def read_jsonl(path: Path | str) -> list[dict]:
    """读 JSONL（坏行跳过）。"""
    items: list[dict] = []
    path = Path(path)
    if not path.exists():
        return items
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                items.append(obj)
    return items


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    path.write_text(text, encoding="utf-8")


def _repair_truncated(path: Path) -> None:
    """上次崩溃可能留下半行；截断到最后一个换行，避免续跑时拼坏 JSONL。"""
    if not path.exists() or path.stat().st_size == 0:
        return
    with path.open("rb") as fh:
        data = fh.read()
    if data.endswith(b"\n"):
        return
    cut = data.rfind(b"\n")
    with path.open("r+b") as fh:
        fh.truncate(0 if cut < 0 else cut + 1)


def load_failure_ids(path: Path | str) -> set[str]:
    return {str(item.get("id")) for item in read_jsonl(path) if item.get("id")}


def load_usage(out_path: Path | str) -> dict:
    path = usage_path_for(out_path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_usage(out_path: Path | str, usage: dict) -> None:
    usage["updated"] = _now_iso()
    path = usage_path_for(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(usage, ensure_ascii=False, indent=1) + "\n",
                    encoding="utf-8")


def _token_counts(result: dict) -> tuple[int, int]:
    usage = result.get("usage") if isinstance(result, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    try:
        prompt = int(prompt or 0)
    except (TypeError, ValueError):
        prompt = 0
    try:
        completion = int(completion or 0)
    except (TypeError, ValueError):
        completion = 0
    return max(0, prompt), max(0, completion)


def accumulate_usage(usage: dict, usages: Iterable[dict]) -> None:
    """把一批 teacher 调用返回的 usage 累加进 usage dict。"""
    usage.setdefault("by_source", {})
    for key in ("calls", "prompt_tokens", "completion_tokens"):
        usage[key] = int(usage.get(key) or 0)
    for one in usages:
        prompt_tokens, completion_tokens = _token_counts({"usage": one})
        usage["calls"] += 1
        usage["prompt_tokens"] += prompt_tokens
        usage["completion_tokens"] += completion_tokens


def usage_cost(usage: dict, *, price_in: float = PRICE_IN_CNY_PER_M,
               price_out: float = PRICE_OUT_CNY_PER_M) -> float:
    """按 token 用量估算成本（元）。"""
    return round(
        int(usage.get("prompt_tokens") or 0) / 1e6 * price_in
        + int(usage.get("completion_tokens") or 0) / 1e6 * price_out, 4)


def _summarize(records: Iterable[dict]) -> dict:
    """rows / by_source / unique_user_msg / unique_context / max_dup / by_profile / by_split。"""
    records = list(records)
    user_msgs = Counter(str(r.get("user_msg") or "") for r in records)
    contexts = set()
    for record in records:
        # context + user_msg 才是模型真正看到的输入；state 在 system 里 → 同 context 不同
        # state_profile 的变体也会被算成不同输入
        contexts.add(json.dumps([record.get("context") or [],
                                 record.get("user_msg") or ""],
                                ensure_ascii=False, sort_keys=False))
    by_source = Counter(str(r.get("source") or "unknown") for r in records)
    by_profile = Counter(str(r.get("state_profile") or "legacy") for r in records)
    by_split = Counter(str(r.get("split") or "legacy") for r in records)
    top = user_msgs.most_common(1)
    crisis_rows = sum(
        1 for record in records
        if any("crisis=true" in str(t.get("text") or "")
               for t in (record.get("context") or []) if isinstance(t, dict))
    )
    return {
        "rows": len(records),
        "by_source": dict(by_source),
        "unique_user_msg": len(user_msgs),
        "unique_context": len(contexts),
        "max_dup": top[0][1] if top else 0,
        "max_dup_user_msg": top[0][0] if top else "",
        "by_profile": dict(by_profile),
        "by_split": dict(by_split),
        "crisis_rows": crisis_rows,
    }


def summarize_items(items: Iterable[dict]) -> dict:
    """对生成前的 items 做统计（字段与落盘统计一致）。"""
    return _summarize(items)


def summarize_records(records: Iterable[dict]) -> dict:
    return _summarize(records)


# --------------------------------------------------------------------------- #
# Teacher 调用
# --------------------------------------------------------------------------- #
def ask_teacher(teacher: Any, item: dict, *, max_retries: int = TEACHER_RETRIES
                ) -> tuple[dict | None, list[dict], str]:
    """调用 teacher 生成 target；返回 (target|None, usage 列表, 错误信息)。"""
    messages = teacher_messages(item)
    usages: list[dict] = []
    errors: list[str] = []
    for attempt in range(max_retries + 1):
        try:
            result = teacher.chat(messages, purpose="soul_sft",
                                  max_tokens=MAX_TEACHER_TOKENS,
                                  temperature=TEACHER_TEMPERATURE, json_mode=True)
        except Exception as exc:  # noqa: BLE001 - 网络/客户端异常都算一次失败
            result = {"ok": False, "text": "", "usage": {},
                      "error": f"{type(exc).__name__}: {str(exc)[:160]}"}
        if not isinstance(result, dict):
            result = {"ok": False, "text": "", "usage": {}, "error": "teacher 返回非 dict"}
        usages.append(result.get("usage") or {})
        text = str(result.get("text") or "")
        raw = llm.parse_json(text) if result.get("ok") else None
        try:
            if not result.get("ok"):
                raise ValueError("teacher 调用失败: " + str(result.get("error") or "")[:120])
            if raw is None:
                raise ValueError("teacher 输出不是 JSON")
            target = coerce_target(raw)
            if item.get("_bucket") == "crisis" and not crisis_target_ok(target):
                raise ValueError(
                    "crisis 行不合规：需要 stance 先照顾人/确认安全，"
                    "behavior=short、ask=false、advice=false、emoji=不用")
            return target, usages, ""
        except ValueError as exc:
            errors.append(f"第{attempt + 1}次: {exc}")
            if attempt < max_retries:
                messages = messages + [
                    {"role": "assistant", "content": text[:800] or "{}"},
                    {"role": "user",
                     "content": (f"上面的输出不合法：{str(exc)[:120]}。"
                                 "请严格按系统要求只输出一个完整 JSON 对象，"
                                 "behavior 的 length/emoji 必须用枚举值。")},
                ]
    return None, usages, "; ".join(errors)


def generate(items: list[dict], teacher: Any, out_path: Path | str | None = None,
             *, max_retries: int = TEACHER_RETRIES, retry_failed: bool = False,
             log_every: int = 20, sleep: float = 0.0,
             budget_cny: float | None = DEFAULT_BUDGET_CNY,
             max_failure_rate: float = MAX_FAILURE_RATE,
             price_in: float = PRICE_IN_CNY_PER_M,
             price_out: float = PRICE_OUT_CNY_PER_M,
             progress=print) -> dict:
    """逐条生成并落盘；按 id 断点续跑，失败/预算闸门。

    返回 ``{"written", "failed", "skipped", "total", "elapsed",
    "aborted", "abort_reason"}``。
    """
    out = Path(out_path or DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    fails = failures_path_for(out)
    done_ids = {str(item.get("id")) for item in read_jsonl(out)}
    failed_ids = load_failure_ids(fails)
    usage = load_usage(out)
    # 计划统计在开跑前落进 usage（Lead 要求：落盘前打印并写进 usage）
    usage["plan"] = summarize_items(items)
    usage["budget_cny"] = budget_cny
    usage["price_cny_per_million"] = {"in": price_in, "out": price_out}
    _save_usage(out, usage)

    _repair_truncated(out)
    written = failed = skipped = 0
    aborted = False
    abort_reason = ""
    started = time.time()
    out_fh = out.open("a", encoding="utf-8")
    fail_fh = fails.open("a", encoding="utf-8")
    try:
        for index, item in enumerate(items, 1):
            item_id = str(item.get("id") or "")
            if item_id in done_ids or (item_id in failed_ids and not retry_failed):
                skipped += 1
                continue
            target, usages, error = ask_teacher(teacher, item, max_retries=max_retries)
            accumulate_usage(usage, usages)
            if target is None:
                failed += 1
                fail_fh.write(json.dumps({
                    "id": item_id, "source": item.get("source"),
                    "error": error, "attempts": len(usages), "ts": _now_iso(),
                }, ensure_ascii=False) + "\n")
                fail_fh.flush()
            else:
                record = {
                    "id": item_id,
                    "source": item.get("source"),
                    "state_profile": item.get("state_profile"),
                    "split": item.get("split"),
                    "context": item.get("context") or [],
                    "user_msg": item.get("user_msg") or "",
                    "target": target,
                    "teacher": teacher_name(teacher),
                    "ts": _now_iso(),
                }
                out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                out_fh.flush()
                written += 1
                done_ids.add(item_id)
                source = str(item.get("source") or "unknown")
                usage["by_source"][source] = int(usage["by_source"].get(source) or 0) + 1
                usage["items"] = len(done_ids)
                if sleep:
                    time.sleep(float(sleep))
            if index % max(1, int(log_every)) == 0:
                progress(
                    f"[soul] 进度 {index}/{len(items)} 新增={written} 失败={failed} "
                    f"跳过={skipped} 用时={time.time() - started:.0f}s "
                    f"成本≈¥{usage_cost(usage, price_in=price_in, price_out=price_out):.2f}"
                )
            # 预算闸门
            if budget_cny is not None:
                cost = usage_cost(usage, price_in=price_in, price_out=price_out)
                if cost >= float(budget_cny):
                    aborted = True
                    abort_reason = f"预算超限：已花 ≈¥{cost:.2f} ≥ 上限 ¥{budget_cny:.2f}"
                    progress(f"[soul][ABORT] {abort_reason}")
                    break
            # 失败率闸门（至少尝试 50 条后才判断）
            attempted = written + failed
            if attempted >= 50 and failed / attempted > max_failure_rate:
                aborted = True
                abort_reason = (f"失败率超限：{failed}/{attempted} > "
                                f"{max_failure_rate:.0%}")
                progress(f"[soul][ABORT] {abort_reason}")
                break
            if (written + failed) and (written + failed) % 20 == 0:
                _save_usage(out, usage)
    finally:
        out_fh.close()
        fail_fh.close()
        usage["elapsed_seconds"] = round(
            float(usage.get("elapsed_seconds") or 0) + (time.time() - started), 3)
        _save_usage(out, usage)
    result = {"written": written, "failed": failed, "skipped": skipped,
              "total": len(items), "elapsed": round(time.time() - started, 3),
              "aborted": aborted, "abort_reason": abort_reason}
    usage["generate"] = result
    usage["actual"] = summarize_records(read_jsonl(out))
    _save_usage(out, usage)
    return result


# --------------------------------------------------------------------------- #
# 归一化（对已生成数据离线重映射 behavior，不重跑 teacher）
# --------------------------------------------------------------------------- #
def normalize_dataset(out_path: Path | str, target_path: Path | str | None = None,
                      *, progress=print) -> dict:
    """把已生成 JSONL 的 behavior 归一到 schema 当前枚举，写到 ``<stem>_norm.jsonl``。"""
    out = Path(out_path)
    records = read_jsonl(out)
    target = Path(target_path) if target_path else out.with_name(out.stem + "_norm.jsonl")
    normalized: list[dict] = []
    skipped = 0
    for record in records:
        try:
            coerced = coerce_target(record.get("target") or {})
        except ValueError as exc:
            skipped += 1
            progress(f"[soul][normalize] 跳过 {record.get('id')}: {exc}")
            continue
        new_record = dict(record)
        new_record["target"] = coerced
        normalized.append(new_record)
    _write_jsonl(target, normalized)
    bad = 0
    for record in normalized:
        try:
            schema.validate(record["target"])
        except ValueError:
            bad += 1
    return {"source": str(out), "out": str(target), "total": len(records),
            "written": len(normalized), "skipped": skipped,
            "schema_valid": len(normalized) - bad}


# --------------------------------------------------------------------------- #
# train/val 切分（优先用行内 split 字段；旧数据回退按会话 hash）
# --------------------------------------------------------------------------- #
def split_group_id(item_id: str) -> str:
    return str(item_id).rsplit("-", 1)[0]


def _is_val_ratio(value: str) -> bool:
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 5 == 0


def split_records(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """切 train/val：行内 ``split`` 字段优先（v1）；旧数据按会话分组 hash。"""
    if not records:
        return [], []
    if all("split" in record for record in records):
        train = [r for r in records if str(r.get("split")) != "val"]
        val = [r for r in records if str(r.get("split")) == "val"]
        if not val or not train:
            val = records[-1:]
            train = records[:-1]
        return train, val
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for record in records:
        groups.setdefault(split_group_id(str(record.get("id"))), []).append(record)
    total = len(records)
    target_val = max(1, int(round(total * 0.10))) if total >= 10 else 0
    val_groups: list[str] = []
    if target_val:
        ordered = sorted(groups, key=lambda g: hashlib.sha1(g.encode()).hexdigest())
        val_count = 0
        for group in ordered:
            if val_count >= target_val:
                break
            size = len(groups[group])
            budget = max(1, target_val - val_count)
            if size <= budget:
                val_groups.append(group)
                val_count += size
        if not val_groups:
            smallest = min(ordered, key=lambda g: (len(groups[g]), g))
            if len(groups[smallest]) < total:
                val_groups = [smallest]
    if val_groups:
        selected = set(val_groups)
        train = [r for group, rows in groups.items() if group not in selected for r in rows]
        val = [r for group in val_groups for r in groups[group]]
    else:
        train = [r for r in records if not _is_val_ratio(str(r.get("id")))]
        val = [r for r in records if _is_val_ratio(str(r.get("id")))]
        if not val or not train:
            val = records[-1:]
            train = records[:-1]
    return train, val


def write_split(out_path: Path | str | None = None) -> dict:
    """把 out 里的数据切成 ``<stem>_train.jsonl`` / ``<stem>_val.jsonl``。"""
    out = Path(out_path or DEFAULT_OUT)
    records = read_jsonl(out)
    train, val = split_records(records)
    train_path = out.with_name(out.stem + "_train.jsonl")
    val_path = out.with_name(out.stem + "_val.jsonl")
    _write_jsonl(train_path, train)
    _write_jsonl(val_path, val)
    return {"train": len(train), "val": len(val),
            "val_ratio": round(len(val) / max(1, len(records)), 4),
            "train_path": str(train_path), "val_path": str(val_path)}


# --------------------------------------------------------------------------- #
# 统计 / 成本预估
# --------------------------------------------------------------------------- #
def dataset_stats(out_path: Path | str | None = None, *,
                  price_in: float = PRICE_IN_CNY_PER_M,
                  price_out: float = PRICE_OUT_CNY_PER_M) -> dict:
    """rows/by_source/unique/max_dup/by_profile/by_split/失败/成本/profile_divergence。"""
    out = Path(out_path or DEFAULT_OUT)
    records = read_jsonl(out)
    summary = summarize_records(records)
    done_ids = {str(r.get("id")) for r in records}
    failures = read_jsonl(failures_path_for(out))
    unresolved = [f for f in failures if str(f.get("id")) not in done_ids]
    usage = load_usage(out)

    items = summary["rows"]
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    measured = items > 0 and prompt_tokens > 0
    if measured:
        per_item_in = prompt_tokens / items
        per_item_out = completion_tokens / items
        token_source = "measured"
    else:
        per_item_in = float(ASSUMED_PROMPT_TOKENS)
        per_item_out = float(ASSUMED_COMPLETION_TOKENS)
        token_source = "assumed"

    def cost(count: int) -> float:
        return round(per_item_in * count / 1e6 * price_in
                     + per_item_out * count / 1e6 * price_out, 4)

    monologues = [
        str((r.get("target") or {}).get("monologue") or "")
        for r in records if isinstance(r.get("target"), dict)
    ]
    privacy_hits = sum(
        1 for r in records if privacy_violations(json.dumps(r, ensure_ascii=False)))
    elapsed = float(usage.get("elapsed_seconds") or 0)
    avg_seconds = elapsed / items if items and elapsed > 0 else 0.0
    failure_rate = round(len(unresolved) / max(1, items + len(unresolved)), 4)
    synthetic_share = round(
        summary["by_source"].get("synthetic", 0) / max(1, items), 4)

    return {
        "out": str(out),
        "exists": out.exists(),
        **summary,
        "unique_user_msg_ratio": round(summary["unique_user_msg"] / max(1, items), 4),
        "context_ratio": round(summary["unique_context"] / max(1, items), 4),
        "synthetic_share": synthetic_share,
        "failures": len(unresolved),
        "failure_rate": failure_rate,
        "failure_records": len(failures),
        "failure_file": str(failures_path_for(out)),
        "profile_divergence": usage.get("profile_divergence"),
        "budget_cny": usage.get("budget_cny"),
        "usage": {"calls": int(usage.get("calls") or 0),
                  "prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "elapsed_seconds": round(elapsed, 1),
                  "cost_cny": usage_cost(usage, price_in=price_in,
                                         price_out=price_out)},
        "tokens_per_item": {"prompt": round(per_item_in, 1),
                            "completion": round(per_item_out, 1),
                            "source": token_source},
        "price_cny_per_million": {"in": price_in, "out": price_out},
        "cost_cny": {"now": cost(items), "est_2000": cost(2000)},
        "est_2000_hours": round(avg_seconds * 2000 / 3600, 2) if avg_seconds else None,
        "avg_seconds_per_item": round(avg_seconds, 2),
        "monologue": {
            "count": len(monologues),
            "max_chars": max((len(m) for m in monologues), default=0),
            "over_limit": sum(1 for m in monologues if len(m) > schema.MAX_MONOLOGUE),
        },
        "privacy_violations": privacy_hits,
        "dataset_version": DATASET_VERSION,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agi.soul.dataset",
        description="W4-A 灵魂蒸馏数据集：context → InnerState SFT 数据生成。",
    )
    parser.add_argument("--limit", type=int, default=300, help="目标条数（默认 300）")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出 JSONL 路径")
    parser.add_argument("--real", action="store_true", help="调用真实 DeepSeek teacher")
    parser.add_argument("--mock", action="store_true", help="确定性假 teacher（默认）")
    parser.add_argument("--split", action="store_true", help="额外输出 train/val 文件")
    parser.add_argument("--normalize", action="store_true",
                        help="把 --out 的 behavior 归一化到 <stem>_norm.jsonl（不覆盖原文件）")
    parser.add_argument("--stats", action="store_true", help="只打印统计/成本预估，不生成")
    parser.add_argument("--retry-failed", action="store_true", help="重试失败台账里的样本")
    parser.add_argument("--sleep", type=float, default=0.0, help="每次调用后休眠秒数")
    parser.add_argument("--log-every", type=int, default=20, help="进度打印间隔")
    parser.add_argument("--max-variants", type=int, default=len(PROFILE_NAMES),
                        help="每个 context 最多配几个 state_profile（默认 7）")
    parser.add_argument("--synthetic-max", type=int, default=SYNTHETIC_MAX,
                        help="合成样本条数上限（默认 800）")
    parser.add_argument("--synthetic-ratio", type=float, default=SYNTHETIC_RATIO,
                        help="合成样本占比上限（默认 0.40）")
    parser.add_argument("--val-ratio", type=float, default=VAL_RATIO,
                        help="val context 占比（默认 0.20）")
    parser.add_argument("--max-msg-windows", type=int, default=3,
                        help="同一句 user_msg 最多保留几个 messages 窗口（默认 3，防止重复句）")
    parser.add_argument("--budget-cny", type=float, default=DEFAULT_BUDGET_CNY,
                        help="本次跑批成本上限（元，默认 3.05；超限立刻停）")
    parser.add_argument("--no-profile-check", dest="profile_check", action="store_false",
                        help="跳过 profile 分歧自检（默认 real 跑批先自检）")
    parser.add_argument("--profile-check-sample", type=int, default=20,
                        help="profile 分歧自检抽多少个 context（默认 20）")
    parser.add_argument("--price-in", type=float, default=PRICE_IN_CNY_PER_M,
                        help="输入单价（元/百万 token）")
    parser.add_argument("--price-out", type=float, default=PRICE_OUT_CNY_PER_M,
                        help="输出单价（元/百万 token）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.real and args.mock:
        parser.error("--real 与 --mock 不能同时使用")
    out = Path(args.out)
    if args.normalize:
        result = normalize_dataset(out)
        print(json.dumps({"normalize": result}, ensure_ascii=False, indent=2))
        stats = dataset_stats(result["out"], price_in=args.price_in,
                              price_out=args.price_out)
        print(json.dumps({"stats": stats}, ensure_ascii=False, indent=2))
        return 0
    if args.stats:
        stats = dataset_stats(out, price_in=args.price_in, price_out=args.price_out)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0

    teacher = build_teacher(bool(args.real))
    profiles = PROFILE_NAMES[:max(1, min(int(args.max_variants), len(PROFILE_NAMES)))]
    profile_divergence = None
    usage = load_usage(out)
    if args.real and args.profile_check and args.profile_check_sample > 0:
        # 先自检 teacher 是否真的看 state_profile：20 个 context × 7 profile
        anon, rows = load_anonymizer()
        contexts = collect_contexts(anon, rows)
        probe, usages = check_profile_divergence(
            teacher, contexts, sample=max(1, int(args.profile_check_sample)))
        accumulate_usage(usage, usages)
        profile_divergence = probe
        usage["profile_divergence"] = probe
        _save_usage(out, usage)
        if not probe["pass"]:
            print("[soul] profile 分歧不足 80%，降级为 4 档 profile（default/work/low_mood/close）",
                  flush=True)
            profiles = REDUCED_PROFILES

    items = build_items(args.limit, synthetic_max=args.synthetic_max,
                        synthetic_ratio=args.synthetic_ratio,
                        profile_names=profiles, val_ratio=args.val_ratio,
                        max_msg_windows=args.max_msg_windows)
    plan = summarize_items(items)
    plan["profile_divergence"] = profile_divergence
    print(f"[soul] 采样 {len(items)} 条（目标 {args.limit}）", flush=True)
    print(json.dumps({"plan": plan}, ensure_ascii=False), flush=True)
    print(f"[soul] teacher={teacher_name(teacher)} out={out}", flush=True)
    if len(items) < args.limit:
        print(f"[soul] 警告：可用来源不足，实际只能生成 {len(items)} 条", flush=True)

    result = generate(items, teacher, out, retry_failed=args.retry_failed,
                      log_every=args.log_every, sleep=args.sleep,
                      budget_cny=args.budget_cny, price_in=args.price_in,
                      price_out=args.price_out)
    print(json.dumps({"generate": result}, ensure_ascii=False), flush=True)
    if args.split:
        split = write_split(out)
        print(json.dumps({"split": split}, ensure_ascii=False), flush=True)
    stats = dataset_stats(out, price_in=args.price_in, price_out=args.price_out)
    print(json.dumps({"stats": stats}, ensure_ascii=False, indent=2), flush=True)
    if result.get("aborted"):
        print(f"[soul] 已中止：{result.get('abort_reason')}", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
