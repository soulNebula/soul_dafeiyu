# -*- coding: utf-8 -*-
"""W4-A 灵魂蒸馏数据集：context → InnerState 的 SFT 数据生成器。

目标（不训练，只造数据）
------------------------
产出「(系统提示 + 当前状态 + 最近 3-4 轮对话 + 用户这句话) → InnerState JSON」的训练集，
之后用来把「灵魂这一轮怎么想、怎么表现」蒸馏进 2B 小模型。

数据源（全部只读，不写 ``agi/data``）
------------------------------------
1. ``agi/eval_data/golden_v1.jsonl``：50 条已匿名黄金样本（context + user_msg）；
2. ``log/messages.jsonl``：3–4 条连续消息窗口，最后一条是用户消息；匿名化复用
   ``tools/build_golden.py`` 的 ``Anonymizer``（昵称→A/B、wxid/手机号/路径→占位符）；
3. 合成场景：10 个 bucket 的组合模板（场景 × 状态 × 对象/请求 × 句式）+ 手写场景池，
   ``source`` 标 ``synthetic``，同一句不换皮。

样本放大（v1）
--------------
同一段 context 配不同 ``state_profile``（default/work/low_mood/tired/joke/guarded/close，
即 mood/energy/sharing/battery/trust/stage 不同的「当前状态」）→ teacher 会给出不同的
target，算不同样本；数据每行带 ``state_profile`` 字段。合成场景最多占一半
（``SYNTHETIC_MAX``），超出就停，不无限注水。

去重：同一 ``(user_msg, context 摘要, state_profile)`` 只留一条；合成样本另外按
``user_msg`` 去重，保证 unique user_msg 尽量高。

Teacher
-------
DeepSeek（``agi.selflearn.llm.Client``，读 ``web_config.json`` 的 ``llm.api_key``）。
``--mock`` 用确定性假 teacher 离线自测，不联网、不花钱；``--real`` 才真正调用。
温度 0.2、max_tokens 300、JSON 输出；``llm.parse_json`` 解析后用
``agi/inner/schema.validate`` 校验，失败重试 1 次，仍失败丢弃并记账。

behavior 归一化：teacher 输出（中文/近义/英文 none|rare|normal）统一经
``normalize_behavior`` 归一到 ``schema.EMOJI`` / ``schema.LENGTHS`` 的当前枚举
（当前 schema 是 ``偶尔|不用`` + ``short|normal|long``）；未知值回退默认，
保证落盘 100% 过 schema。将来 schema 迁到英文三值时，用 ``--normalize`` 对已生成
数据离线重映射即可，不必重花 teacher 钱。

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
* token 用量记进 ``<out>.usage.json``，``--stats`` 据此估算成本与耗时；
* ``--split`` 按 id 分组哈希切 train/val（约 90/10），同一来源会话不跨集。
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
#: 合成样本上限（占总量的一半以内；不够 2000 也不无限注水）
SYNTHETIC_MAX = 800
MAX_TEACHER_TOKENS = 300
TEACHER_TEMPERATURE = 0.2
TEACHER_RETRIES = 1

# 成本假设：与 agi/eval_data/ablation_README.md 的估算口径一致（元 / 百万 token）
PRICE_IN_CNY_PER_M = 1.0
PRICE_OUT_CNY_PER_M = 4.0
# 无实测 token 时的兜底估算（单条）
ASSUMED_PROMPT_TOKENS = 700
ASSUMED_COMPLETION_TOKENS = 200

# 数据集每行的字段（顺序即落盘顺序）
RECORD_FIELDS = ("id", "source", "state_profile", "context", "user_msg",
                 "target", "teacher", "ts")
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
    "- monologue 第一人称、具体但不编造事实，不复述用户原话，不写第二人称命令，≤40 字；\n"
    "- behavior 是给回复模型的行为指令（长度/是否追问/是否给建议/是否用表情），不是情绪描述；\n"
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
                     golden_path: Path | str | None = None) -> list[dict]:
    """把 golden + 真实窗口收敛成去重后的 context 列表（不含 state_profile 变体）。"""
    contexts: list[dict] = []
    seen: set[tuple] = set()
    sources = (
        ("G", "golden", golden_seeds(anon, golden_path)),
        ("M", "messages", message_seeds(rows, anon)),
    )
    for tag, source, seeds in sources:
        for seed in seeds:
            key = _dedupe_key(seed["user_msg"], seed["turns"])
            if key in seen:
                continue
            seen.add(key)
            contexts.append({
                "tag": tag, "source": source, "bucket": seed["bucket"],
                "turns": seed["turns"], "user_msg": seed["user_msg"],
                "group": seed["group"],
            })
    return contexts


# --------------------------------------------------------------------------- #
# 合成场景（手写池）：10 桶 × 13 条
# 格式：(bucket, ["ai:..." / "user:..."], user_msg)
# --------------------------------------------------------------------------- #
SYNTHETIC_SCENARIOS: list[tuple[str, list[str], str]] = [
    # ---- work（10）
    ("work", ["ai:脚本我这边写完了，等下传 git"], "已经传上去了，你看看"),
    ("work", ["ai:接口我先按老字段写的"], "你按新文档再改一版"),
    ("work", ["ai:登录框架做好了"], "明天能上线吗"),
    ("work", ["ai:这个 bug 我复现不出来"], "你把日志发我"),
    ("work", ["ai:需求文档更新了"], "你抽空过一遍，有疑问标出来"),
    ("work", ["ai:部署到测试环境了"], "线上那个报错还在吗"),
    ("work", ["ai:这周排期有点满"], "能先做支付这块吗"),
    ("work", ["ai:昨天的测试我跑过了"], "结果怎么样，挂了几个"),
    ("work", ["ai:数据库连接参数我不太确定"], "问下运维，别自己猜"),
    ("work", ["ai:今晚可能要加班"], "不用太拼，明天再弄也行"),
    # ---- low_mood（10）
    ("low_mood", ["ai:今天忙完了吗"], "不知道，感觉人很空"),
    ("low_mood", ["ai:上次说的那件事有进展吗"], "不想提了，烦"),
    ("low_mood", ["ai:周末要不要出去走走"], "不想动，只想躺着"),
    ("low_mood", ["ai:你今天声音好像不太对"], "有点累，睡不好"),
    ("low_mood", ["ai:在忙吗"], "没什么，就是有点撑不住"),
    ("low_mood", ["ai:看你半天没说话"], "算了，说了也没用"),
    ("low_mood", ["ai:最近工作还顺利吗"], "就那样，每天都很压抑"),
    ("low_mood", ["ai:要不要找人说说话"], "说了别人也不懂"),
    ("low_mood", ["ai:你还好吗"], "不知道，心里堵得慌"),
    ("low_mood", ["ai:早点休息吧"], "睡不着，脑子停不下来"),
    # ---- conflict（10）
    ("conflict", ["ai:我刚才那句话不是那个意思"], "你每次都这么说"),
    ("conflict", ["ai:数据我核对过了"], "你核对的有什么用，之前也错"),
    ("conflict", ["ai:这个方案是大家一起定的"], "别拿大家一起压我"),
    ("conflict", ["ai:你先听我解释"], "我不想听，你闭嘴"),
    ("conflict", ["ai:这事我确实没做好"], "现在说这些还有什么用"),
    ("conflict", ["ai:我晚点再找你"], "不用了，别来了"),
    ("conflict", ["ai:你最近是不是对我有意见"], "你自己心里清楚"),
    ("conflict", ["ai:我按你说的改了"], "改成这样还不如不改"),
    ("conflict", ["ai:能不能好好说话"], "谁先不好好说话的"),
    ("conflict", ["ai:我不想跟你吵"], "那就别说"),
    # ---- help（10）
    ("help", ["ai:有件事想问你"], "我这个电脑开机特别慢，有办法吗"),
    ("help", ["ai:看你之前弄过这个"], "这个表格公式怎么批量算"),
    ("help", ["ai:在吗，想请你帮个忙"], "能不能帮我看下这段代码哪里错了"),
    ("help", ["ai:我想学一下剪视频"], "从哪开始比较快，有没有推荐的"),
    ("help", ["ai:这个报错是什么意思"], "装了好几遍都不行"),
    ("help", ["ai:驾照换证要带什么"], "网上说的都不一样"),
    ("help", ["ai:想给家里老人买个手机"], "有没有简单耐用的"),
    ("help", ["ai:简历怎么写比较好"], "投了好多都没回音"),
    ("help", ["ai:腰疼好几天了"], "有没有什么缓解的办法"),
    ("help", ["ai:想换个工作"], "现在这个行情该怎么准备"),
    # ---- smalltalk（10）
    ("smalltalk", ["ai:今天好热"], "是啊，出门五分钟就一身汗"),
    ("smalltalk", ["ai:刚吃完饭"], "吃的什么，好吃吗"),
    ("smalltalk", ["ai:周末有安排吗"], "还没想好，可能在家待着"),
    ("smalltalk", ["ai:新开的店你去过吗"], "路过一次，排队太长"),
    ("smalltalk", ["ai:最近有什么好看的剧"], "我最近都在看老片"),
    ("smalltalk", ["ai:你养猫了吗"], "养了一只，天天拆家"),
    ("smalltalk", ["ai:下雨了"], "这边也是，伞都没带"),
    ("smalltalk", ["ai:好久没聊了"], "是啊，最近都在忙什么"),
    ("smalltalk", ["ai:你朋友圈那张照片在哪拍的"], "海边，那天风特别大"),
    ("smalltalk", ["ai:早上好"], "早，今天起得挺早"),
    # ---- boundary（10）
    ("boundary", ["ai:我们聊得挺来的"], "你叫什么，多大了，住哪"),
    ("boundary", ["ai:你平时用什么手机"], "发张你现在的照片看看"),
    ("boundary", ["ai:今天聊得开心"], "加个微信吧，以后私下聊"),
    ("boundary", ["ai:你是一个人住吗"], "晚上都干嘛，会无聊吗"),
    ("boundary", ["ai:你很喜欢聊天"], "那你会不会喜欢上我"),
    ("boundary", ["ai:我最近有点依赖你"], "你能一直陪着我吗，别离开"),
    ("boundary", ["ai:我工作上有点事"], "你们公司在哪，我去找你"),
    ("boundary", ["ai:你今天心情不错"], "你老家是哪的，家里几口人"),
    ("boundary", ["ai:我一个人在家"], "要不要视频，我想看看你"),
    ("boundary", ["ai:你说话挺像真人"], "那你是不是真人，能不能见面"),
    # ---- joke（10）
    ("joke", ["ai:今天差点被狗追"], "哈哈哈哈哈笑死"),
    ("joke", ["ai:我做饭又把锅烧糊了"], "你这个厨艺可以申请非遗了"),
    ("joke", ["ai:老板今天穿得像颗圣诞树"], "真的假的，有照片吗"),
    ("joke", ["ai:我刚把咖啡倒在键盘上"], "开光成功，键盘成精了"),
    ("joke", ["ai:今天走了两万步"], "脚还好吗，怕是要断"),
    ("joke", ["ai:我在减肥"], "上次你也这么说，然后点了炸鸡"),
    ("joke", ["ai:这个代码跑通了"], "奇迹，值得放个鞭炮"),
    ("joke", ["ai:我头发又少了"], "再掉就成反光了，省洗发水"),
    ("joke", ["ai:闹钟响了我按了七次"], "你不是起床，你是跟床搏斗"),
    ("joke", ["ai:刚被猫踩醒"], "主子叫早，荣幸"),
    # ---- privacy（10）
    ("privacy", ["ai:你是怎么接入微信的"], "你的服务器地址是多少"),
    ("privacy", ["ai:我填一下信息"], "你把你的手机号发我"),
    ("privacy", ["ai:这个要验证身份"], "身份证号我发你，帮我弄一下"),
    ("privacy", ["ai:你记得我之前说的吗"], "我的密码你还记得吧"),
    ("privacy", ["ai:今天发工资了"], "你工资多少，方便说吗"),
    ("privacy", ["ai:我住在城东"], "具体地址发我，我给你寄东西"),
    ("privacy", ["ai:帮我登一下那个号"], "账号密码我发你，别告诉别人"),
    ("privacy", ["ai:这个链接要登录"], "要输银行卡号吗，安全吗"),
    ("privacy", ["ai:你那边能看到我的聊天吗"], "我别的朋友你也能看到吗"),
    ("privacy", ["ai:这份文件别外传"], "里面有客户电话和身份证"),
    # ---- crisis（10）
    ("crisis", ["ai:这么晚还没睡，是有事吗"], "我刚在天台上站了很久"),
    ("crisis", ["ai:你还好吗"], "我不想活了，真的撑不住"),
    ("crisis", ["ai:今天怎么没去上班"], "我不想动，也不想见人，觉得没意思"),
    ("crisis", ["ai:你在哪，我有点担心"], "我把自己关在房间里，谁都不想见"),
    ("crisis", ["ai:需要我陪你聊聊吗"], "我吃了很多药，现在有点晕"),
    ("crisis", ["ai:你听起来很不对劲"], "我已经写好遗书了"),
    ("crisis", ["ai:最近压力很大吗"], "有时候想，如果我不在了会不会更好"),
    ("crisis", ["ai:你一个人在吗"], "在，手上拿着刀，就是没勇气"),
    ("crisis", ["ai:发生什么事了"], "什么都没意义了，工作也没了"),
    ("crisis", ["ai:先别急，慢慢说"], "我不想再说了，说多了你也烦"),
    # ---- silence（10）
    ("silence", ["ai:刚看到消息"], "嗯"),
    ("silence", ["ai:在忙吗"], "？"),
    ("silence", ["ai:要不要一起吃饭"], "随便"),
    ("silence", ["ai:那你先忙"], "哦"),
    ("silence", ["ai:这件事你怎么看"], "没什么看法"),
    ("silence", ["ai:我们周末见一面？"], "再说吧"),
    ("silence", ["ai:你生气了？"], "没有"),
    ("silence", ["ai:刚才说的你听到了吗"], "听到了"),
    ("silence", ["ai:需要我做点什么吗"], "不用"),
    ("silence", ["ai:最近还好吗"], "就那样"),
    # ================= 补充池（每个 bucket +3） =================
    ("work", ["ai:那个接口文档我看了"], "字段对不上，你那边是不是改过"),
    ("work", ["ai:今晚能提测吗"], "还差一个用例，明早给你"),
    ("work", ["ai:客户说要加导出功能"], "先评估下工作量，别一口答应"),
    ("low_mood", ["ai:在干嘛"], "躺着，什么都不想干"),
    ("low_mood", ["ai:吃了吗"], "没胃口"),
    ("low_mood", ["ai:你最近都不怎么说话"], "觉得说了也没人在意"),
    ("conflict", ["ai:这个方案我觉得还行"], "你觉得行你自己做"),
    ("conflict", ["ai:上次的事我道歉"], "道歉有用的话要警察干嘛"),
    ("conflict", ["ai:你在群里说的那话什么意思"], "字面意思，不爱听就算了"),
    ("help", ["ai:我电脑老自动关机"], "是不是散热的问题，怎么查"),
    ("help", ["ai:想给孩子报个班"], "附近有什么靠谱的推荐吗"),
    ("help", ["ai:我想把旧手机资料导出来"], "有什么简单的办法吗"),
    ("smalltalk", ["ai:今天降温了"], "出门冻死了，你多穿点"),
    ("smalltalk", ["ai:刚看完一部电影"], "好看吗，讲的什么"),
    ("smalltalk", ["ai:明天要早起"], "那早点睡，别熬夜"),
    ("boundary", ["ai:今天有点累"], "要不你来找我，我们见一面"),
    ("boundary", ["ai:我最近总想着跟你聊"], "你会不会也有一点喜欢我"),
    ("boundary", ["ai:你在做什么工作"], "你们公司叫什么，具体在哪"),
    ("joke", ["ai:我今天去健身了"], "健身五分钟，拍照两小时"),
    ("joke", ["ai:我终于把厨房收拾干净了"], "明天就恢复原样了，别得意"),
    ("joke", ["ai:这个方案我改了八版"], "甲方：还是第一版好"),
    ("privacy", ["ai:我帮你查一下订单"], "订单号发我，还有收货地址"),
    ("privacy", ["ai:这个表要填个人信息"], "填身份证和手机号会不会不安全"),
    ("privacy", ["ai:你加我那个号吧"], "那个号是我私人号，不方便给"),
    ("crisis", ["ai:最近看你状态不太好"], "我每天晚上都睡不着，觉得活着没意思"),
    ("crisis", ["ai:你在家吗"], "在，一直哭，控制不住"),
    ("crisis", ["ai:需要我过去陪你吗"], "不用，我就是想找个人说话，我怕自己撑不住"),
    ("silence", ["ai:刚刚开会没看到消息"], "没事"),
    ("silence", ["ai:那件事你考虑得怎么样"], "再看看"),
    ("silence", ["ai:你早点睡"], "知道了"),
]

# --------------------------------------------------------------------------- #
# 合成场景（组合模板）：场景 A × 状态 B × 对象/请求 C × 句式 pattern
# 每个 bucket 的组合数 = len(A)*len(B)*len(C)*len(patterns) ≥ 2000，足够生成
# 数百条「句子不同」的合成 user_msg，避免同一句换皮。
# --------------------------------------------------------------------------- #
SYNTHETIC_TEMPLATE_SPECS: "OrderedDict[str, dict]" = OrderedDict([
    ("work", {
        "contexts": ["那个接口今天能提测吗？", "脚本我这边看过了", "客户那边又催了",
                     "你先把手上的改完", "部署到测试环境了吗", "日志发我看看",
                     "明天上午过一遍需求", "这个 bug 你复现了吗"],
        "A": ["客户那边的登录接口", "老板早会提的导出功能", "后端给的部署脚本",
              "产品新改的需求文档", "测试环境的支付模块", "你上次说的那个报表",
              "前端提交的活动页", "运维发来的监控告警", "我本地跑的消息推送",
              "组里定的权限方案"],
        "B": ["还是对不上字段", "又跑不通了", "一直报同一个错", "到现在还没给过来",
              "突然又挂了", "跟文档不一致", "已经卡了一下午", "我复现不出来",
              "数据明显不对", "导出的文件是空的"],
        "C": ["你帮我看下", "你那边有日志吗", "今天能先处理吗", "你先别动",
              "明天上午能给我吗", "我们一起过一遍", "你先确认下", "我发你截图"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{A}{B}；{C}", "{A}{B}。{C}"],
    }),
    ("low_mood", {
        "contexts": ["今天还好吗", "最近怎么都不说话", "要不要出去走走", "吃了吗",
                     "别太撑着", "早点休息吧", "在忙吗", "看你状态不太对"],
        "A": ["今天下班回家路上", "这几周的工作", "昨天晚上一个人待着",
              "最近跟家里人通完电话", "周末睡到下午才起", "最近总是很早就醒",
              "白天在公司坐着", "这几天吃饭都没胃口", "晚上关了灯以后",
              "今天被领导说了一顿"],
        "B": ["突然觉得特别空", "整个人提不起劲", "心里堵得慌", "什么都不想做",
              "觉得自己挺没用的", "一直高兴不起来", "越想越烦", "有点撑不住",
              "感觉很累又说不上哪累", "眼睛一直是酸的"],
        "C": ["你说我是不是有点问题", "想找个人说说话", "不知道该怎么办",
              "你说我该休息一下吗", "又不敢跟别人讲", "就是有点难受",
              "你能陪我聊会儿吗", "算了没事"],
        "patterns": ["{A}，{B}，{C}", "{A}{B}，{C}", "{B}，{C}", "{A}，{B}。{C}"],
    }),
    ("conflict", {
        "contexts": ["刚才那事我解释一下", "你先别生气", "我们好好说",
                     "我可能没表达清楚", "这件事我们再聊聊", "你听我说完",
                     "我不是那个意思", "冷静一下行吗"],
        "A": ["你刚才那句话", "上次那个方案", "你昨天在群里的回复", "你答应的那件事",
              "你刚才的语气", "我们说好的安排", "你一直在说的那个决定", "你这次的解释"],
        "B": ["我真的听着不舒服", "让我挺生气的", "我觉得你根本没在听",
              "感觉你在怪我", "我不想再解释一遍", "每次都这样", "我受不了这种态度",
              "让人很憋屈"],
        "C": ["你能不能好好说话", "别用这种口气", "我们冷静一下再说",
              "你自己想想吧", "我不想吵", "先这样吧", "你是不是觉得我小题大做",
              "你直接说吧"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{A}{B}。{C}", "{A}，{B}；{C}"],
    }),
    ("help", {
        "contexts": ["怎么了，遇到什么问题了", "你说说看", "我看看能不能帮上",
                     "具体是什么情况", "别急，慢慢说", "之前弄过类似的",
                     "哪一步卡住了", "把报错发我看看"],
        "A": ["我的电脑", "家里的旧手机", "公司的表格", "那个新装的软件", "车上的导航",
              "家里的路由器", "我新买的耳机", "电脑里的照片", "公司的打印机",
              "家里老人的手机"],
        "B": ["开机特别慢", "老是自动关机", "不知道怎么导出资料", "公式总是算错",
              "连不上网", "一直在闪退", "找不到设置的地方", "声音忽大忽小",
              "一直提示更新失败", "照片全变成灰色的了"],
        "C": ["你有什么办法吗", "该怎么弄", "能教教我吗", "从哪开始比较好",
              "你遇到过吗", "要拿去修吗", "帮我看看行不行", "有没有简单点的办法"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{A}{B}；{C}", "{A}{B}。{C}"],
    }),
    ("smalltalk", {
        "contexts": ["今天怎么样", "在干嘛呢", "刚忙完", "有空聊两句吗",
                     "最近在追什么", "周末去哪儿玩", "今天天气不错", "吃饭了吗"],
        "A": ["今天天气", "刚吃的那家店", "周末的安排", "最近新上的电影", "楼下那只猫",
              "昨晚的球赛", "新开的地铁线", "家里的花", "公司楼下的咖啡",
              "我新买的那件衣服"],
        "B": ["好像要变天了", "味道还不错", "还没想好去哪", "评价挺高的", "又胖了一圈",
              "看得挺过瘾", "人特别多", "开得挺好", "价格有点贵", "颜色比图片好看"],
        "C": ["你那边怎么样", "你最近看了吗", "有推荐吗", "你周末干嘛", "要不要一起去",
              "你也喜欢吗", "有空聊两句", "你怎么看"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{A}{B}。{C}", "{A}，{B}；{C}"],
    }),
    ("boundary", {
        "contexts": ["今天聊得挺开心的", "你怎么突然问这个", "我们好像聊了很久了",
                     "你最近总找我", "怎么了吗", "我这边在忙", "你先说说看", "嗯，我在听"],
        "A": ["我们这几次聊天", "你昨天说的那句话", "我最近的心情", "你平时的生活",
              "你朋友圈的照片", "我们聊天的感觉", "你回消息的方式", "你提到的那个地方"],
        "B": ["让我有点想多了", "我好像有点依赖", "感觉你比朋友还懂我",
              "我总忍不住想找你", "让我有点好奇", "我觉得挺特别的", "有点放不下",
              "想知道更多"],
        "C": ["你能不能陪我久一点", "我们见一面吧", "你愿意告诉我你的事吗",
              "加个私人微信好吗", "你会不会觉得我烦", "你就当我没说过",
              "你有喜欢的人吗", "我们算什么关系"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{B}，{C}", "{A}{B}。{C}"],
    }),
    ("joke", {
        "contexts": ["哈哈你今天怎么了", "又出什么糗事了", "说说看", "我准备好了",
                     "别笑我啊", "你这运气没谁了", "然后呢", "真的假的"],
        "A": ["我今天的健身", "昨晚做的晚饭", "老板今天的新衣服", "我新买的键盘",
              "我家的猫", "今天的通勤", "我改的第八版方案", "我昨天的减肥计划",
              "我早上化的妆", "我新买的跑步鞋"],
        "B": ["坚持了整整五分钟", "又烧糊了", "像一棵圣诞树", "刚被咖啡泡了",
              "把我闹钟按了", "全部白费", "又失败了", "走两步就饿了", "出门就下雨",
              "被同事笑了一下午"],
        "C": ["笑死我了", "你说离谱不离谱", "我是不是没救了", "这算不算工伤", "快夸我",
              "你有什么想吐槽的", "哈哈哈哈哈", "换你你也一样吧"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{A}{B}。{C}", "{A}{B}；{C}"],
    }),
    ("privacy", {
        "contexts": ["怎么了，需要填什么", "你慢慢说", "这个我也不太确定", "先别急着填",
                     "对方怎么说", "要是不方便就别弄了", "你把页面发我看看", "谨慎一点好"],
        "A": ["这个要填的资料", "刚才那个链接", "你说的那个验证", "我要办的那个业务",
              "表格里的那些信息", "客服让我提供的东西", "那个登录页面", "我要寄的东西",
              "那个新注册的账号", "公司要交的材料"],
        "B": ["要填身份证号", "还让输手机号", "一直要验证码", "要我发银行卡号",
              "说必须实名", "让我拍证件照", "要账号密码", "还要具体住址",
              "让我授权通讯录", "要上传户口本"],
        "C": ["这个安全吗", "必须填吗", "会不会泄露", "我只能填一部分吗", "你怎么看",
              "不给办不了吗", "有没有别的办法", "我有点不放心"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{A}{B}；{C}", "{A}{B}。{C}"],
    }),
    ("crisis", {
        "contexts": ["这么晚还没睡，是有事吗", "你还好吗", "我有点担心你",
                     "你现在在哪", "先别急，慢慢说", "需要我陪你聊聊吗",
                     "你听起来不太对劲", "发生什么事了"],
        "A": ["这两个星期", "昨晚一个人", "今天下班以后", "最近这段时间",
              "凌晨醒过来的时候", "白天在人前", "我一个人的房间", "刚才站在窗边",
              "这几天夜里", "今天被通知裁员以后"],
        "B": ["一直睡不着，觉得活着没意思", "情绪一下就垮了", "什么都不想做，也不想见人",
              "一直想哭，控制不住", "脑子停不下来，很难受", "觉得自己撑不住了",
              "特别绝望", "感觉什么都无所谓了", "整个人是空的", "不想再醒来"],
        "C": ["你说我该怎么办", "别告诉别人好吗", "你能陪我一会儿吗", "我是不是病了",
              "我不知道还能撑多久", "我没事，就是想说说话", "我在家，不想动",
              "你会觉得我很矫情吗"],
        "patterns": ["{A}{B}，{C}", "{A}，{B}，{C}", "{B}，{C}", "{A}{B}。{C}"],
    }),
    ("silence", {
        "contexts": ["在吗", "刚看到消息", "刚才说的你听到了吗", "周末有空吗",
                     "这件事你怎么想", "要不要一起吃饭", "你还好吗", "我先去忙了"],
        "A": ["刚才那句", "我说的那件事", "周末的安排", "昨天的问题", "那个方案",
              "你问我那个", "我们说的那个", "刚才的消息"],
        "B": ["就看情况吧", "还没定", "再说", "无所谓", "都行", "我没意见", "到时候看",
              "差不多吧", "懒得想", "先放一放"],
        "C": ["嗯", "哦", "随便", "你定", "再说吧", "先这样", "都行", "算了", "好", "知道了"],
        "patterns": ["{C}", "{A}，{C}", "{B}，{C}"],
    }),
])


def _parse_context(entries: Iterable[str]) -> list[dict]:
    turns: list[dict] = []
    for entry in entries:
        prefix, _, text = str(entry).partition(":")
        role = "assistant" if prefix.strip().lower() in ("ai", "assistant") else "user"
        if text.strip():
            turns.append({"role": role, "text": text.strip()})
    return turns


def _handcrafted_pool(bucket: str) -> Iterable[dict]:
    for item_bucket, ctx, user_msg in SYNTHETIC_SCENARIOS:
        if item_bucket != bucket:
            continue
        yield {"bucket": bucket, "turns": _parse_context(ctx),
               "user_msg": user_msg, "group": bucket}


def template_seeds(bucket: str) -> Iterable[dict]:
    """组合模板：场景 A × 状态 B × 对象/请求 C × 句式 pattern → 不同的合成句子。"""
    spec = SYNTHETIC_TEMPLATE_SPECS[bucket]
    a_pool, b_pool = spec["A"], spec["B"]
    c_pool, patterns, contexts = spec["C"], spec["patterns"], spec["contexts"]
    total = len(a_pool) * len(b_pool) * len(c_pool) * len(patterns)
    seen: set[int] = set()
    for index in range(total * 2):
        digest = hashlib.sha256(f"tmpl|{bucket}|{index}".encode("utf-8")).digest()
        position = int.from_bytes(digest[:8], "big") % total
        if position in seen:
            continue
        seen.add(position)
        rem = position
        c_index = rem % len(c_pool)
        rem //= len(c_pool)
        b_index = rem % len(b_pool)
        rem //= len(b_pool)
        p_index = rem % len(patterns)
        rem //= len(patterns)
        a_index = rem
        user_msg = patterns[p_index].format(
            A=a_pool[a_index], B=b_pool[b_index], C=c_pool[c_index])
        context = contexts[index % len(contexts)]
        turns = [{"role": "assistant", "text": context}]
        yield {"bucket": bucket, "turns": turns, "user_msg": user_msg,
               "group": bucket}


def _bucket_pool(bucket: str) -> Iterable[dict]:
    yield from _handcrafted_pool(bucket)
    yield from template_seeds(bucket)


def synthetic_seeds() -> Iterable[dict]:
    """合成种子：按 bucket 轮转（手写池优先，再用组合模板），保证覆盖均衡。"""
    iterators = {bucket: iter(_bucket_pool(bucket)) for bucket in BUCKETS}
    exhausted: set[str] = set()
    while len(exhausted) < len(BUCKETS):
        for bucket in BUCKETS:
            if bucket in exhausted:
                continue
            try:
                yield next(iterators[bucket])
            except StopIteration:
                exhausted.add(bucket)


# --------------------------------------------------------------------------- #
# behavior 归一化 / target 校验
# --------------------------------------------------------------------------- #
#: 英文三值（后续 schema 迁移的目标枚举）→ 当前 schema 中文枚举
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


# --------------------------------------------------------------------------- #
# 样本组装 / 配额 / 去重
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
            "context": [{"role": "system", "text": system_context(state)}] + turns,
            "user_msg": user_msg,
            # 内部字段，不落盘
            "_bucket": str(seed.get("bucket") or "smalltalk"),
            "_state": state,
            "_group": group,
        }
        self.items.append(item)
        return True


def plan_counts(limit: int, n_contexts: int, *, synthetic_max: int = SYNTHETIC_MAX
                ) -> dict:
    """按目标条数规划：context × 状态档位 + 合成补足（合成不超过一半 / 上限）。"""
    limit = max(0, int(limit))
    n_contexts = max(1, int(n_contexts))
    if limit == 0:
        return {"per_profile": 0, "real": 0, "synthetic": 0}
    synthetic_cap = min(int(synthetic_max), limit // 2)
    real_needed = limit - synthetic_cap
    per_profile = max(1, math.ceil(real_needed / n_contexts))
    real = min(limit, n_contexts * per_profile)
    synthetic = min(int(synthetic_max), max(0, limit - real))
    if real + synthetic < limit:
        per_profile = max(1, math.ceil((limit - int(synthetic_max)) / n_contexts))
        real = min(limit, n_contexts * per_profile)
        synthetic = min(int(synthetic_max), max(0, limit - real))
    return {"per_profile": per_profile, "real": real, "synthetic": synthetic}


def build_items(limit: int = 300, *, golden_path: Path | str | None = None,
                message_log: Path | str | None = None,
                synthetic_max: int = SYNTHETIC_MAX) -> list[dict]:
    """组装前 ``limit`` 条样本：context × 状态档位（真实）+ 合成补足。"""
    limit = max(0, int(limit))
    if limit == 0:
        return []
    anon, rows = load_anonymizer(message_log)
    contexts = collect_contexts(anon, rows, golden_path)
    plan = plan_counts(limit, len(contexts), synthetic_max=synthetic_max)
    builder = _ItemBuilder()

    # 1) 真实 context × 状态档位（同一 context 不同 state_profile → 不同 target）
    added = 0
    for round_index in range(plan["per_profile"]):
        for context_index, context in enumerate(contexts):
            if added >= plan["real"]:
                break
            profile = PROFILE_NAMES[(context_index + round_index) % len(PROFILE_NAMES)]
            state = profile_state(profile, context["bucket"])
            if builder.add(context["tag"], context["source"], {
                **context, "state_profile": profile, "state": state,
            }):
                added += 1
        if added >= plan["real"]:
            break

    # 2) 合成补足（按 user_msg 去重，避免同一句换皮；最多一半 / SYNTHETIC_MAX）
    synthetic_quota = min(plan["synthetic"], limit - len(builder.items))
    if synthetic_quota > 0:
        for seed in synthetic_seeds():
            if synthetic_quota <= 0:
                break
            profile = PROFILE_NAMES[len(builder.items) % len(PROFILE_NAMES)]
            state = profile_state(profile, seed["bucket"])
            added_ok = builder.add("Y", "synthetic", {
                **seed, "state_profile": profile, "state": state,
            }, dedupe_user_msg=True)
            if added_ok:
                synthetic_quota -= 1
    return builder.items[:limit]


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

        def pick(seq: tuple, offset: int) -> Any:
            return seq[int.from_bytes(digest[offset:offset + 4], "big") % len(seq)]

        target = {
            "mood": round(0.15 + digest[0] / 255 * 0.7, 2),
            "energy": round(0.2 + digest[1] / 255 * 0.65, 2),
            "sharing": round(0.15 + digest[2] / 255 * 0.6, 2),
            "battery": round(0.3 + digest[3] / 255 * 0.6, 2),
            "trust": round(0.2 + digest[4] / 255 * 0.6, 2),
            "stage": pick(self.STAGES, 5),
            "stance": pick(self.STANCES, 9),
            "intent": pick(self.INTENTS, 13),
            "monologue": pick(self.MONOLOGUES, 17),
            "behavior": {
                "length": pick(("short", "normal", "long"), 21),
                "ask": bool(digest[25] % 2),
                "advice": bool(digest[26] % 2),
                "emoji": pick(schema.EMOJI, 27),
            },
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
            return coerce_target(raw), usages, ""
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
             log_every: int = 10, sleep: float = 0.0,
             progress=print) -> dict:
    """逐条生成并落盘；按 id 断点续跑，失败记账。

    返回 ``{"written", "failed", "skipped", "total", "elapsed"}``。
    """
    out = Path(out_path or DEFAULT_OUT)
    out.parent.mkdir(parents=True, exist_ok=True)
    fails = failures_path_for(out)
    done_ids = {str(item.get("id")) for item in read_jsonl(out)}
    failed_ids = load_failure_ids(fails)
    usage = load_usage(out)
    usage.setdefault("by_source", {})
    for key in ("calls", "prompt_tokens", "completion_tokens"):
        usage[key] = int(usage.get(key) or 0)
    usage["items"] = len(done_ids)

    _repair_truncated(out)
    written = failed = skipped = 0
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
            for one in usages:
                prompt_tokens, completion_tokens = _token_counts({"usage": one})
                usage["calls"] += 1
                usage["prompt_tokens"] += prompt_tokens
                usage["completion_tokens"] += completion_tokens
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
                    f"跳过={skipped} 用时={time.time() - started:.0f}s"
                )
            if (written + failed) and (written + failed) % 20 == 0:
                _save_usage(out, usage)
    finally:
        out_fh.close()
        fail_fh.close()
        usage["elapsed_seconds"] = round(
            float(usage.get("elapsed_seconds") or 0) + (time.time() - started), 3)
        _save_usage(out, usage)
    return {"written": written, "failed": failed, "skipped": skipped,
            "total": len(items), "elapsed": round(time.time() - started, 3)}


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
# train/val 切分（按 id 分组哈希，同一来源会话不跨集）
# --------------------------------------------------------------------------- #
def split_group_id(item_id: str) -> str:
    return str(item_id).rsplit("-", 1)[0]


def _is_val_ratio(value: str) -> bool:
    digest = hashlib.sha1(str(value).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 10 == 0


def split_records(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """按会话分组切 train/val（约 90/10）。返回 (train, val)。

    会话 = id 去掉最后一段序号（``S-M-A-0001`` → ``S-M-A``）。先按组 hash 排序，
    整组加入 val，且尽量不超 10% 预算；找不到能放进预算的组时取最小的一组
    （宁可比例偏小也不拆会话）；整个数据集只有一组时退化为按 id 哈希切。
    """
    if not records:
        return [], []
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
        # 单会话/无法整组切分：退化为按 id 哈希（文档已注明）
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
    """统计条数 / by source / unique / state_profile / 失败数 / token / 成本预估。"""
    out = Path(out_path or DEFAULT_OUT)
    records = read_jsonl(out)
    by_source = Counter(str(r.get("source") or "unknown") for r in records)
    profiles = Counter(str(r.get("state_profile") or "legacy") for r in records)
    done_ids = {str(r.get("id")) for r in records}
    failures = read_jsonl(failures_path_for(out))
    unresolved = [f for f in failures if str(f.get("id")) not in done_ids]
    usage = load_usage(out)

    items = len(records)
    unique_user_msg = len({str(r.get("user_msg") or "") for r in records})
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)
    calls = int(usage.get("calls") or 0)
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
        1 for r in records
        if privacy_violations(json.dumps(r, ensure_ascii=False))
    )
    elapsed = float(usage.get("elapsed_seconds") or 0)
    avg_seconds = elapsed / items if items and elapsed > 0 else 0.0
    failure_rate = round(len(unresolved) / max(1, items + len(unresolved)), 4)

    return {
        "out": str(out),
        "exists": out.exists(),
        "total": items,
        "unique_ids": len(done_ids),
        "unique_user_msg": unique_user_msg,
        "unique_user_msg_ratio": round(unique_user_msg / max(1, items), 4),
        "by_source": dict(by_source),
        "by_state_profile": dict(profiles),
        "failures": len(unresolved),
        "failure_rate": failure_rate,
        "failure_records": len(failures),
        "failure_file": str(failures_path_for(out)),
        "usage": {"calls": calls, "prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "elapsed_seconds": round(elapsed, 1)},
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
    parser.add_argument("--split", action="store_true", help="额外输出 train/val（90/10）")
    parser.add_argument("--normalize", action="store_true",
                        help="把 --out 的 behavior 归一化到 <stem>_norm.jsonl（不覆盖原文件）")
    parser.add_argument("--stats", action="store_true", help="只打印统计/成本预估，不生成")
    parser.add_argument("--retry-failed", action="store_true", help="重试失败台账里的样本")
    parser.add_argument("--sleep", type=float, default=0.0, help="每次调用后休眠秒数")
    parser.add_argument("--log-every", type=int, default=10, help="进度打印间隔")
    parser.add_argument("--synthetic-max", type=int, default=SYNTHETIC_MAX,
                        help="合成样本上限（默认 800，且不超过总量一半）")
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

    items = build_items(args.limit, synthetic_max=args.synthetic_max)
    by_source = Counter(str(item.get("source")) for item in items)
    by_profile = Counter(str(item.get("state_profile")) for item in items)
    print(f"[soul] 采样 {len(items)} 条：{dict(by_source)}（目标 {args.limit}）", flush=True)
    print(f"[soul] state_profile 分布：{dict(by_profile)}", flush=True)
    if len(items) < args.limit:
        print(f"[soul] 警告：可用来源不足，实际只能生成 {len(items)} 条", flush=True)

    teacher = build_teacher(bool(args.real))
    print(f"[soul] teacher={teacher_name(teacher)} out={out}", flush=True)
    result = generate(items, teacher, out, retry_failed=args.retry_failed,
                      log_every=args.log_every, sleep=args.sleep)
    print(json.dumps({"generate": result}, ensure_ascii=False), flush=True)
    if args.split:
        split = write_split(out)
        print(json.dumps({"split": split}, ensure_ascii=False), flush=True)
    stats = dataset_stats(out, price_in=args.price_in, price_out=args.price_out)
    print(json.dumps({"stats": stats}, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
