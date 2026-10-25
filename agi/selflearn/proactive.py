# -*- coding: utf-8 -*-
"""11/12 分享欲与主动表达候选：只生成候选，不自动发送。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from . import db, initiative, memory, share_store, sharing, state, style_profile, trust
from .config import DB_PATH

COOLDOWN = 2 * 3600
MIN_SCORE = 0.45
DAILY_BUDGET = 6

# --------------------------------------------------------------------------- #
# P0-3：主动消息意图（4 类 + 可选低频 silence_ack），防止「查岗」模板塌缩
# --------------------------------------------------------------------------- #
KINDS = ("care", "share", "followup", "light", "silence_ack")
#: 轮换窗口：看最近 N 条主动消息的 kind 分布（proactive 表）
KIND_WINDOW = 20
#: 单一 kind 在窗口内占比上限（超过即降权/禁用，硬上限保证 ≤50%）
KIND_MAX_RATIO = 0.5
#: light 只在这些小时可用：8:00 ≤ hour < 22:00
LIGHT_HOURS = (8, 22)
#: silence_ack 至少间隔 7 天，且要求久未联系
SILENCE_ACK_GAP_S = 7 * 86400
SILENCE_MIN_IDLE_H = 24.0
#: care 只在低活跃时（用户 ≥1h 没说话）或上次情绪偏负
CARE_MIN_IDLE_H = 1.0
#: followup 要接「上次没聊完」：用户安静 ≥30 分钟，且上次对话在 7 天内
FOLLOWUP_MIN_IDLE_H = 0.5
FOLLOWUP_MAX_AGE_H = 7 * 24.0
#: 出站文本超过该字符数时按句末标点切分，避免长句粘连
LONG_TEXT_CHARS = 60
#: 中文/英文句末标点（已合规不再追加）
END_PUNCT = "。！？!?…～~"
#: 句尾是这些语气词 → 补问号，否则补句号
_QUESTION_TAILS = ("吗", "呢", "么", "嘛")
#: 通用短句/招呼，不当 followup 的「具体内容」
_STOP_CLAUSES = {
    "在吗", "在么", "在不在", "你好", "你好呀", "嗨", "hi", "hello",
    "嗯", "嗯嗯", "哦", "好", "好的", "行", "没事", "算了", "就那样",
    "没什么", "还行", "随便", "再说", "不用",
}
_STAGE_RANK = {"陌生": 0, "疏远": 0, "初识": 1, "熟悉": 2, "亲近": 3, "信任": 4}
_WS_RE = re.compile(r"\s+")
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?])")
_CLAUSE_SPLIT_RE = re.compile(r"[，。！？!?；;,.、\s]+")
# 主题相似度用：去掉空白/标点，只留中英文数字（避免标点把 2-gram 稀释）
_SIM_CLEAN_RE = re.compile(r"[^\w\u4e00-\u9fff]+")

# --------------------------------------------------------------------------- #
# P0-8：反「安静」内容塌缩（禁元话题 / 主题去重 / 问句配额 / 后处理兜底）
# --------------------------------------------------------------------------- #
#: 正文禁词（真机日志里高频的「元话题」；命中即重写，保证线上绝不出现）
BANNED_TOPIC_WORDS = (
    "安静", "没动静", "没消息", "好久没聊", "忙吗", "在忙吗", "在干嘛",
    "冒个泡", "打扰了", "没人理我",
    # 同义改写 / 真机样例变体：一起拦，避免换个说法又冒出来
    "好久没", "没声音", "声音都没有", "一点声音", "冷清", "没人回",
)
#: 主题去重：最近 N 条已发主动消息、字符 2-gram Jaccard 上限
TOPIC_WINDOW = 10
TOPIC_SIMILARITY_MAX = 0.35
#: 问句配额：最近 N 条里 ≥M 条以问号结尾 → 本次强制陈述/分享句
QUESTION_WINDOW = 3
QUESTION_FORCE_AT = 2

# --------------------------------------------------------------------------- #
# P0-9：反编造共同经历（真机首条消息「便当盒」杜撰）
# --------------------------------------------------------------------------- #
#: 「共同经历断言」句式：正文命中后必须有真实锚点支撑（≥2 字重叠），否则视为编造。
#: 真实锚点 = 该次 tick 提供给模型的最近用户消息 / memory 话题 / followup 锚点。
SHARED_CLAIM_PATTERNS = (
    "你上回", "你上次", "你之前", "你以前", "你昨天", "你前天", "你那次",
    "你说过", "你提过", "你放在我这", "你放在我这儿", "你留在我这",
    "上次你", "还记得你", "我们上次", "我们之前", "我们以前",
    "你送我的", "你给我的",
)
#: 全局不编造约束（拼进每个 kind 的 prompt；followup 有真实锚点时例外）
GROUNDING_CONSTRAINTS = (
    "不编造约束（比具体更重要）：\n"
    "- 只能引用「记忆/最近聊天里真实出现过」的内容；没有依据时只讲你自己的事，"
    "不得声称对方做过什么；\n"
    "- 禁止编造和对方共同的经历，禁止这些句式：你上回…、上次你…、还记得你…、"
    "你说过要…、你放在我这的…；\n"
    "- 禁止杜撰具体的私人物品、共同住所、共同行程。"
)

# --------------------------------------------------------------------------- #
# P0-10：修 run-on 粘连句（真机「…剪了剪你这两天…」缺标点）
# --------------------------------------------------------------------------- #
#: run-on 判定：汉字连续 ≥N 字且无标点、结尾紧接「你/您/我」→ 在动词边界后切句
RUNON_MIN_CJK = 12
RUNON_PRONOUNS = "你您我"
RUNON_BOUNDARIES = ("剪了剪", "了", "着", "过", "完", "好")
#: 全局标点/分句约束（拼进每个 kind 的 prompt）
RUNON_CONSTRAINTS = (
    "标点/分句约束：\n"
    "- 一条消息只表达一件事，最多两句；句与句之间必须有标点（句号/问号）；\n"
    "- 不要一条消息连着说两件事，不要把两层意思粘在一起。"
)
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
#: 「跟/对/给…你」里的代词是宾语，不是两层意思，不能在这里切句
_RUNON_OBJECT_PREPOSITIONS = "跟对给和让把被替帮同与朝向"
#: 全局负向约束（拼进每个 kind 的 prompt；光靠轮换挡不住「安静」主题）
TOPIC_CONSTRAINTS = (
    "硬约束（违反会被重写，必须遵守）：\n"
    "- 正文禁止出现这些词及其同义改写：安静、没动静、没消息、好久没聊、"
    "忙吗/在忙吗、在干嘛、冒个泡、打扰了、没人理我；\n"
    "- 不要评论对方不回、不说话、冷清，不要把「沉默/没动静」当话题，沉默是正常的；\n"
    "- 不要写「窗外/声音/犯困/发呆/冷清」这类和对方沉默、无聊相关的环境感受；\n"
    "- 必须说具体内容：一件刚发生的小事 / 一个具体问题 / 一段记忆里的具体细节；"
    "禁止「最近怎么样」式泛问；\n"
    "- 不复述、不概括最近已发过的内容，换一个具体话题；\n"
    "- 最多一个问句，以陈述为主，像微信里随手发的一句话。"
)

#: 每类意图的生成约束（同一 LLM 调用里按 kind 选指令，禁止多类共用）
KIND_INSTRUCTION = {
    "care": "意图=关心近况：问一句具体的近况（工作/身体/心情里挑一个），"
            "不要泛泛说「最近怎么样」，不要查岗式连问。",
    "share": "意图=分享自己：说一件你自己的想法/感受，可以自然带出你记得的对方话题，"
             "不要客服腔、不要只提问。",
    "followup": "意图=接上次没聊完的事：必须提到最近对话里的具体内容（会给锚点），"
                "围绕它问一句或接一句，不要换成泛泛的近况。",
    "light": "意图=轻松一下：接个梗/调侃一句/讲件小趣事，短句，不查岗、不讲道理。",
    "silence_ack": "意图=久未联系后的轻打招呼：只说一句轻的，不追问、不解释、不查岗。",
}

#: 每类意图的兜底句（LLM 不可用/跳过/跑题时使用；彼此不同，不允许多类共用）
#: 已审查：不含禁词、不评论沉默、主题各异（followup 的锚点由调用方注入）
_KIND_FALLBACK = {
    "care": "这两天降温，出门记得加件外套。",
    "share": "刚想到你之前说的{topic}，我今天也碰上一件类似的小事。",
    "followup": "上次你说「{anchor}」，我这两天还一直想着这事。",
    "light": "刚看到楼下猫在追自己的尾巴，笑死。",
    "silence_ack": "今天路过一家老店，闻到刚出炉的面包味，买了两个。",
}

#: 后处理兜底用的**只讲自己的事/中性事实**安全句（P0-9：绝不编造共同经历；
#: 不含禁词、彼此不同）
_SAFE_FALLBACK = {
    "care": "这两天降温，我出门差点被风吹回去。",
    "share": "刚泡了杯手冲，酸得我皱了下眉，下次想换个豆子。",
    "followup": "刚把书架上那本旧书翻出来，纸页有点发黄了。",
    "light": "刚看到楼下猫在追自己的尾巴，笑死。",
    "silence_ack": "今天路过一家老店，闻到刚出炉的面包味，买了两个。",
}

#: followup 的主题去重变体：都保留锚点，避免「换话题」把具体内容换没了
_ANCHOR_VARIANTS = (
    "上次你说「{anchor}」，我这两天还一直想着这事。",
    "你之前提过「{anchor}」，我今天突然又想起这茬。",
    "关于「{anchor}」，我后来还琢磨了一下。",
)


def kind_fallback(kind: str, *, anchor: str = "", topic: str = "") -> str:
    """某类意图的兜底文本（各类互不相同）。"""
    template = _KIND_FALLBACK.get(str(kind or ""), _KIND_FALLBACK["care"])
    return template.format(anchor=str(anchor or "").strip(),
                           topic=str(topic or "").strip())


def _safe_fallback(kind: str, *, anchor: str = "", topic: str = "") -> str:
    """带锚点/话题的兜底句再过一遍禁词；锚点/话题本身脏就退到无锚点安全句。"""
    name = str(kind or "")
    text = kind_fallback(name, anchor=anchor, topic=topic)
    # 空锚点/空话题会拼出「上次你说「」」这种破句，直接退到静态安全句
    if (name == "followup" and not str(anchor or "").strip()) or \
            (name == "share" and not str(topic or "").strip()):
        return _SAFE_FALLBACK.get(name, _SAFE_FALLBACK["care"])
    if _has_banned_topic(text):
        return _SAFE_FALLBACK.get(name, _SAFE_FALLBACK["care"])
    return text


def _has_banned_topic(text: Any) -> bool:
    """正文是否命中「安静/没动静/忙吗…」禁词。"""
    value = str(text or "")
    return any(word in value for word in BANNED_TOPIC_WORDS)


def _ends_question(text: Any) -> bool:
    return str(text or "").strip().endswith(("？", "?"))


def _bigrams(text: Any) -> set[str]:
    value = _SIM_CLEAN_RE.sub("", str(text or "")).replace("_", "")
    if len(value) < 2:
        return set()
    return {value[index:index + 2] for index in range(len(value) - 1)}


def topic_overlap(first: Any, second: Any) -> float:
    """两段文本的字符 2-gram Jaccard（0~1）。"""
    left, right = _bigrams(first), _bigrams(second)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def max_topic_overlap(text: Any, history: list[str]) -> float:
    return max((topic_overlap(text, old) for old in list(history or [])), default=0.0)


def _has_shared_experience_claim(text: Any) -> bool:
    """正文是否出现「你上回/上次你/还记得你…」这类共同经历断言。"""
    value = str(text or "")
    return any(pattern in value for pattern in SHARED_CLAIM_PATTERNS)


def grounding_ok(text: Any, anchors) -> bool:
    """反编造校验：命中共同经历断言的句子，必须与某个真实锚点有 ≥2 字重叠。

    - 没有共同经历断言 → 直接通过（只讲自己的事/中性事实）；
    - 有断言但 ``anchors`` 里找不到对应实词（字符 2-gram 交集为空）→ 视为编造；
    - ``anchors`` 为空时，任何共同经历断言都视为编造。
    """
    value = str(text or "").strip()
    if not value:
        return True
    if not _has_shared_experience_claim(value):
        return True
    anchor_list = [str(item).strip() for item in list(anchors or [])
                   if str(item or "").strip()]
    if not anchor_list:
        return False
    anchor_grams = [_bigrams(item) for item in anchor_list]
    for sentence in re.split(r"[。！？!?；;\n]+", value):
        sentence = sentence.strip()
        if not sentence or not _has_shared_experience_claim(sentence):
            continue
        grams = _bigrams(sentence)
        if not any(grams & other for other in anchor_grams if other):
            return False
    return True


def _grounding_anchors(path: Path | str, candidate: dict | None = None) -> list[str]:
    """该次 tick 提供给模型的真实上下文：最近用户消息 + memory 话题/证据 + followup 锚点。"""
    anchors: list[Any] = []
    if isinstance(candidate, dict):
        anchors.append(candidate.get("anchor"))
        anchors.append(candidate.get("topic"))
    try:
        memories = memory.list_user_memories(path, 20)
    except Exception:  # noqa: BLE001
        memories = []
    for item in memories:
        anchors.append(item.get("content"))
        anchors.append(item.get("evidence"))
    try:
        recent = memory.recent_messages(path, limit=40)
    except Exception:  # noqa: BLE001
        recent = []
    for msg in recent:
        if msg.get("role") != "user":
            continue
        anchors.append(msg.get("text"))
    cleaned: list[str] = []
    for item in anchors:
        text = str(item or "").strip()
        if not text or text.startswith("["):  # 媒体占位不算真实内容
            continue
        cleaned.append(text)
    return cleaned


def _recent_proactive_texts(path: Path | str, limit: int = TOPIC_WINDOW) -> list[str]:
    """最近 N 条**已发**主动消息正文（新→旧）。

    优先读 worker 写的 ``proactive_events``（status='sent'，真正出站过）；
    表还不存在/没记录时退回 ``proactive`` 表（tick 自己生成的历史），
    这样纯 tick 场景（单测、网页预览）也有去重依据。
    """
    con = db.connect(path)
    try:
        try:
            rows = con.execute(
                "SELECT text FROM proactive_events WHERE status='sent' "
                "ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        except Exception:  # noqa: BLE001 - 旧库没有 proactive_events 表
            rows = []
        if not rows:
            rows = con.execute(
                "SELECT text FROM proactive WHERE status!='rejected' "
                "ORDER BY created_at DESC LIMIT ?", (int(limit),)).fetchall()
    finally:
        con.close()
    return [str(row["text"] or "") for row in rows if str(row["text"] or "").strip()]


def sanitize_topic(text: Any, kind: str = "care") -> str:
    """后处理兜底：命中禁词就换成该 kind 的安全兜底句（发送前最后一道闸）。

    保证线上绝不出现「安静/没动静/忙吗/冒个泡…」这类元话题；空文本同样兜底。
    """
    value = str(text or "").strip()
    if value and not _has_banned_topic(value):
        return value
    name = str(kind or "").strip()
    return _SAFE_FALLBACK.get(name) or _SAFE_FALLBACK["care"]


def _low_overlap_fallback(kind: str, candidate: dict,
                          recent: list[str]) -> str:
    """主题仍超标时：从兜底句里挑与历史重叠最低的（followup 保留锚点）。"""
    anchor = str(candidate.get("anchor") or "").strip()
    topic = str(candidate.get("topic") or "").strip()
    if kind == "followup" and anchor:
        pool = [variant.format(anchor=anchor) for variant in _ANCHOR_VARIANTS]
    else:
        pool = [_safe_fallback(other, anchor=anchor, topic=topic) for other in KINDS]
        pool.append(_safe_fallback(kind, anchor=anchor, topic=topic))
    unique = list(dict.fromkeys(pool))
    safe = [text for text in unique if not _has_banned_topic(text)] or unique
    return min(safe, key=lambda text: max_topic_overlap(text, recent))



def _daily_budget(path: Path | str) -> int:
    """日预算：优先读 autonomy 配置；<=0 表示不限。"""
    try:
        from . import autonomy
        value = autonomy._config(path).get("daily_budget", DAILY_BUDGET)
        return int(value)
    except Exception:  # noqa: BLE001
        return DAILY_BUDGET


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def _last_interaction(path: Path | str) -> float:
    con = db.connect(path)
    try:
        # 只看"人"最后说话的时间：自己刚发过的主动消息不算"刚聊过"，
        # 这样 LLM 想连发时不会被 8 秒静默挡住。
        row = con.execute("SELECT max(ts) FROM messages WHERE role='user'").fetchone()
        return float(row[0] or 0) if row else 0.0
    finally:
        con.close()


def _cooldown_ok(path: Path | str, now: float) -> bool:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        return not row or not row[0] or now - float(row[0]) >= COOLDOWN
    finally:
        con.close()


def _idle_hours(path: Path | str, now: float) -> float:
    last = _last_interaction(path)
    return (now - last) / 3600 if last else 99.0


def _followup_anchor(path: Path | str, now: float, limit: int = 40) -> str:
    """从最近对话里取一段「具体内容」当 followup 的引用锚点（不要空泛招呼）。

    limit 放大到 40：主动消息自己也会不断写进 messages 表，窗口太小会把
    「上次用户说的话」挤出视野，followup 就退化没了。
    """
    try:
        recent = memory.recent_messages(path, limit=limit)
    except Exception:  # noqa: BLE001
        return ""
    for msg in reversed(recent):
        if msg.get("role") != "user":
            continue
        ts = float(msg.get("ts") or 0)
        if ts and (now - ts) > FOLLOWUP_MAX_AGE_H * 3600:
            continue
        text = str(msg.get("text") or "").strip()
        if not text or text.startswith("["):  # [图片]/[表情包] 不算具体内容
            continue
        for clause in _CLAUSE_SPLIT_RE.split(text):
            clause = clause.strip()
            if len(clause) >= 2 and clause not in _STOP_CLAUSES and not clause.isdigit():
                if _has_banned_topic(clause):  # 锚点本身不能带「安静」类禁词
                    continue
                return clause[:20]
    return ""


def _kind_counts(path: Path | str, limit: int = KIND_WINDOW) -> dict[str, int]:
    """最近 N 条主动消息的 kind 分布（proactive 表）。"""
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT kind FROM proactive ORDER BY created_at DESC LIMIT ?",
            (int(limit),)).fetchall()
    finally:
        con.close()
    counts: dict[str, int] = {}
    for row in rows:
        kind = str(row["kind"] or "").strip()
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


def _last_proactive_kind(path: Path | str) -> str:
    con = db.connect(path)
    try:
        row = con.execute(
            "SELECT kind FROM proactive ORDER BY created_at DESC LIMIT 1").fetchone()
    finally:
        con.close()
    return str(row["kind"] or "") if row else ""


def _last_kind_ts(path: Path | str, kind: str) -> float:
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive WHERE kind=?",
                          (str(kind),)).fetchone()
    finally:
        con.close()
    return float(row[0] or 0) if row and row[0] else 0.0


def build_candidates(path: Path | str = DB_PATH, now: float | None = None) -> list[dict]:
    """按 4+1 类意图生成候选；每类有自己的触发条件与兜底句。"""
    now = time.time() if now is None else float(now)
    st = state.load(path)
    own = st["self"]
    mood = float(own.get("mood", 0.55))
    share_drive = float(own.get("sharing", 0.45))
    emotion = str((st.get("emotion") or {}).get("label") or "")
    trust_value = trust.get(path)
    stage = trust.stage(trust_value)
    idle_h = _idle_hours(path, now)
    hour = time.localtime(now).tm_hour
    try:
        mems = memory.list_user_memories(path, 10)
    except Exception:  # noqa: BLE001
        mems = []
    topic = str(mems[0].get("content") or "").strip() if mems else ""
    candidates: list[dict] = []

    # care：低活跃 / 上次情绪偏负
    if idle_h >= CARE_MIN_IDLE_H or emotion == "negative":
        candidates.append({
            "kind": "care", "text": _safe_fallback("care"),
            "reason": f"已 {idle_h:.1f} 小时没互动" if idle_h >= CARE_MIN_IDLE_H
                      else "上次情绪偏负",
            "base": 0.55,
        })
    # share：有记忆话题或关系够近，分享自己的想法
    if topic or trust_value >= 0.55:
        candidates.append({
            "kind": "share",
            "text": _safe_fallback("share", topic=topic or "最近的事"),
            "reason": "记忆里有可接回的话题" if topic else "信任足够，适合分享自己的念头",
            "base": 0.50, "topic": topic,
        })
    # followup：接上次没聊完的具体内容（必须能引用到锚点）
    anchor = _followup_anchor(path, now)
    if anchor and idle_h >= FOLLOWUP_MIN_IDLE_H:
        candidates.append({
            "kind": "followup", "text": _safe_fallback("followup", anchor=anchor),
            "reason": f"上次聊到「{anchor}」", "base": 0.52, "anchor": anchor,
        })
    # light：仅白天且关系 ≥ 熟悉
    if LIGHT_HOURS[0] <= hour < LIGHT_HOURS[1] and _STAGE_RANK.get(stage, 0) >= 2:
        candidates.append({
            "kind": "light", "text": _safe_fallback("light"),
            "reason": "白天且关系够近，适合轻松接梗", "base": 0.45,
        })
    # silence_ack：久未联系 + 至少 7 天没发过
    last_ack = _last_kind_ts(path, "silence_ack")
    if idle_h >= SILENCE_MIN_IDLE_H and (not last_ack or now - last_ack >= SILENCE_ACK_GAP_S):
        candidates.append({
            "kind": "silence_ack", "text": _safe_fallback("silence_ack"),
            "reason": f"已 {idle_h:.1f} 小时没联系", "base": 0.40,
        })
    if not candidates:
        # 关系还远/刚从沉默里出来：先轻轻问一句，别硬分享
        candidates.append({
            "kind": "care", "text": _safe_fallback("care"),
            "reason": "没有特别理由，只是想轻轻问一句", "base": 0.35,
        })
    for candidate in candidates:
        candidate["score"] = round(min(0.95, candidate["base"] + 0.1 * trust_value), 3)
        candidate.setdefault("anchor", "")
        candidate.setdefault("topic", topic)
    return sorted(candidates, key=lambda item: (-item["score"], item["kind"]))


def choose_candidate(candidates: list[dict], path: Path | str = DB_PATH,
                     now: float | None = None) -> dict | None:
    """加权反重复选 kind：窗口内计数少的优先、同计数比分数，超 50% 硬排除。

    - 反重复：最近 ``KIND_WINDOW`` 条里该 kind 出现次数最少的优先（并列才比分数），
      某 kind 占比 >50% 时直接排除（除非没有别的可选）——强制降权落到「选不上」；
    - 同类不连发：上一轮刚发过的 kind 在并列时排到最后（silence_ack 因此不会连续出现）；
    - 选择是确定性的（同状态同历史落在同一 kind），避免测试随机抖动；文本仍由 LLM 生成。
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    now = time.time() if now is None else float(now)
    counts = _kind_counts(path)
    total = sum(counts.values())
    last_kind = _last_proactive_kind(path)
    # 硬上限：若选它会让窗口内占比超过 KIND_MAX_RATIO，先排除
    eligible: list[dict] = []
    for candidate in candidates:
        kind = str(candidate.get("kind") or "")
        count = int(counts.get(kind, 0))
        if total and (count + 1) / (total + 1) > KIND_MAX_RATIO + 1e-9:
            continue
        eligible.append(candidate)
    if not eligible:
        eligible = list(candidates)
    # 反重复：计数最少的 kind 优先；并列按分数，再按轮转起点避免永远同一顺序
    least = min(int(counts.get(str(c.get("kind") or ""), 0)) for c in eligible)
    pool = [c for c in eligible
            if int(counts.get(str(c.get("kind") or ""), 0)) == least]
    offset = (total + int(now)) % len(pool)
    ordered = list(pool[offset:]) + list(pool[:offset])
    # 上一轮同 kind 在并列时排后面（不连发）
    ordered.sort(key=lambda c: 0 if str(c.get("kind") or "") != last_kind else 1)
    return max(ordered, key=lambda c: float(c.get("score") or 0.0))


def find_runon(text: Any) -> int | None:
    """定位 run-on 粘连句的切分点（在代词前插入句号的位置）。

    规则（P0-10，真机实证「…剪了剪你这两天…」）：
    - 找一段**连续 ≥RUNON_MIN_CJK 个汉字且中间无标点**、且紧跟着
      「你/您/我」的片段；
    - 切分点 = 该片段内最后一个动词边界（了/着/过/完/好，或「剪了剪」这类
      动词重叠）之后；若边界不在片段末尾（后面还有补语/量词），切在片段末尾、
      代词之前，避免把「整理了一遍」切成「整理了。一遍」；
    - 没有动词边界时兜底切在片段末尾（代词之前）。

    返回插入「。」的绝对位置（int），没检测到返回 None。纯函数。
    """
    value = str(text or "")
    if not value:
        return None
    for index, char in enumerate(value):
        if char not in RUNON_PRONOUNS:
            continue
        count = 0
        cursor = index - 1
        while cursor >= 0 and _CJK_CHAR_RE.match(value[cursor]):
            count += 1
            cursor -= 1
        if count < RUNON_MIN_CJK:
            continue
        run_start = index - count
        run = value[run_start:index]
        # 防误伤：代词是介词/动词宾语（…跟你、…对你）→ 这是同一句话，不是两层意思
        if run and run[-1] in _RUNON_OBJECT_PREPOSITIONS:
            continue
        # 防误伤：代词后面必须还有一小段像样的分句（≥3 个汉字），
        # 否则「…都告诉了你」这种单句会被错误切开。
        tail = 0
        for char in value[index + 1:]:
            if not _CJK_CHAR_RE.match(char):
                break
            tail += 1
        if tail < 3:
            continue
        boundary_end = None
        for token in RUNON_BOUNDARIES:
            pos = run.rfind(token)
            if pos < 0:
                continue
            token_end = pos + len(token)
            if boundary_end is None or token_end > boundary_end:
                boundary_end = token_end
        # 边界必须在片段末尾（后面没有补语/量词）才在边界后切；否则切在代词前
        if boundary_end is not None and boundary_end == len(run):
            return run_start + boundary_end
        return run_start + count
    return None


def _insert_runon_periods(text: Any) -> str:
    """确定性修复 run-on：在 find_runon 定位处插入「。」，只加标点、不丢内容。"""
    value = str(text or "")
    offset = 0
    guard = 0
    while guard < 8:  # 防御：超长/异常文本也不会死循环
        pos = find_runon(value[offset:])
        if pos is None:
            break
        split_at = offset + pos
        if split_at <= offset:  # pragma: no cover - 防御
            break
        value = value[:split_at] + "。" + value[split_at:]
        offset = split_at + 1
        guard += 1
    return value


def polish_outbound(text: Any) -> str:
    """出站文本质检（发送前）：去首尾空白、合并连续空白、补句末标点、修 run-on、超长按标点切分。

    规则：
    - 句尾缺标点：以「吗/呢/么/嘛」结尾补问号，其余陈述补句号；
    - run-on 粘连（连续 ≥12 汉字无标点、后接你/您/我）→ 在动词边界后确定性插入「。」，
      只加标点、不丢内容；
    - 超过 ``LONG_TEXT_CHARS`` 且有多句时，按句末标点切分并用换行分隔（发送层逐段发），
      切分只加分隔符、不丢字；
    - 已合规文本（无多余空白、已有句末标点、无 run-on、未超长）**逐字节不变**；
      修复幂等（对已修好的文本再调用不变）。

    纯函数：不读文件、不依赖时间/随机。
    """
    value = _WS_RE.sub(" ", str(text or "")).strip()
    if not value:
        return ""
    if value[-1] not in END_PUNCT:
        value += "？" if value.endswith(_QUESTION_TAILS) else "。"
    value = _insert_runon_periods(value)
    if value[-1] not in END_PUNCT:  # 插入点只在句中最前，兜底防御
        value += "。"
    if len(value) > LONG_TEXT_CHARS:
        parts = [part.strip() for part in _SENT_SPLIT_RE.split(value) if part.strip()]
        if len(parts) > 1:
            value = "\n".join(parts)
    return value


def _mentions_anchor(text: str, anchor: str) -> bool:
    """LLM 文本有没有真的引用到锚点（整句或至少一个 2 字片段）。"""
    value = str(text or "")
    target = str(anchor or "").strip()
    if not target:
        return True
    if target in value:
        return True
    compact = _WS_RE.sub("", target)
    return any(compact[i:i + 2] in value for i in range(len(compact) - 1))


def _kind_prompt(kind: str, candidate: dict, mood: float, share_drive: float,
                 dialog: str, *, force_statement: bool = False,
                 regenerate: bool = False, own_only: bool = False,
                 fix_runon: bool = False) -> str:
    """按 kind 选不同的生成指令 + 全局约束（禁元话题/反编造/标点分句/问句配额）。"""
    instruction = KIND_INSTRUCTION.get(kind) or KIND_INSTRUCTION["care"]
    prompt = (
        f"你现在心情 {mood:.2f}，分享欲 {share_drive:.2f}。你想主动给对方发条微信。"
        + instruction
        + TOPIC_CONSTRAINTS
        + GROUNDING_CONSTRAINTS
        + RUNON_CONSTRAINTS
        + "只输出你要说的话，1-2 句，口语；不要客服腔、不报能力、不列清单。"
    )
    anchor = str(candidate.get("anchor") or "").strip()
    topic = str(candidate.get("topic") or "").strip()
    if kind == "followup" and anchor:
        prompt += f"必须提到这个具体内容：「{anchor}」，不要换成泛泛的近况。"
        prompt += f"（例外：可以引用这条真实锚点「{anchor}」，其他共同经历一律不许编造。）"
    elif kind == "share" and topic:
        prompt += f"可以从对方之前说过的「{topic}」自然引到你自己的想法。"
    if force_statement:
        prompt += "\n这次只写陈述句或分享句，禁止提问、禁止出现问号。"
    if regenerate:
        prompt += ("\n【重生成】上一次生成的内容和最近发过的消息主题太像了，"
                   "换一个完全不同的具体话题重说一句。")
    if own_only:
        prompt += ("\n【重生成】不要提对方做过/说过的事，也不要提你们共同的经历；"
                   "只说你自己的事（你的经历/想法/刚观察到的小事）。")
    if fix_runon:
        prompt += ("\n【重生成】上一句把两层意思粘在一起了（中间没有标点）："
                   "把两层意思用句号分开，重写这一条，不要丢内容。")
    prompt += "如果实在没话说，输出 SKIP。最近对话：" + dialog
    return prompt


def _ask_llm(client, prompt: str) -> tuple[str, bool]:
    """调一次主动生成；返回 (text, 是否拿到可用回复)。异常/不可用都安全降级。"""
    if client is None or not getattr(client, "available", lambda: False)():
        return "", False
    try:
        result = client.chat(
            [{"role": "system", "content": "你说话像普通人，短句。"},
             {"role": "user", "content": prompt}],
            purpose="selflearn_proactive", max_tokens=150, temperature=0.8)
    except Exception:  # noqa: BLE001 - 生成失败走模板兜底，不影响主链
        return "", False
    if not isinstance(result, dict) or not result.get("ok"):
        return "", False
    return str(result.get("text") or "").strip(), True


def _normalize_content(text: str, kind: str, candidate: dict, *,
                       force_statement: bool = False) -> str:
    """生成文本的硬约束后处理：空/问句配额/followup 锚点，全部走安全兜底。"""
    anchor = str(candidate.get("anchor") or "").strip()
    topic = str(candidate.get("topic") or "").strip()
    value = str(text or "").strip()
    if not value:
        return _safe_fallback(kind, anchor=anchor, topic=topic)
    if force_statement and _ends_question(value):
        return _safe_fallback(kind, anchor=anchor, topic=topic)
    if kind == "followup" and anchor and not _mentions_anchor(value, anchor):
        return _safe_fallback("followup", anchor=anchor)
    return value



def record_proactive_turn(path: Path | str, assistant_message_id: str, reply: str,
                          now: float | None = None) -> str | None:
    """给主动消息补一条 turn（user_message_id 挂最近一条用户消息）。

    这样"反馈 好/差"才能落到具体轮次上（否则主动消息永远收不到反馈）。
    没有用户消息时返回 None（首次对话前不主动）。
    """
    now = time.time() if now is None else float(now)
    try:
        con = db.connect(path)
    except Exception:  # noqa: BLE001
        return None
    try:
        row = con.execute(
            "SELECT id FROM messages WHERE role='user' ORDER BY ts DESC LIMIT 1").fetchone()
        if not row or not row["id"]:
            return None
        turn_id = _id("T-PRO-", row["id"], now)
        exists = con.execute("SELECT 1 FROM turns WHERE id=?", (turn_id,)).fetchone()
        if exists:
            return turn_id
        con.execute(
            "INSERT INTO turns(id,user_message_id,assistant_message_id,reply,skill_id,"
            "variant_id,feedback,score,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (turn_id, row["id"], str(assistant_message_id or ""), str(reply or ""),
             None, None, None, None, now))
        con.commit()
        return turn_id
    except Exception:  # noqa: BLE001
        return None
    finally:
        con.close()


def tick(path: Path | str = DB_PATH, client=None, now: float | None = None,
         idle_seconds: float = 30, cooldown: float = 900,
         min_score: float | None = None, respect_state: bool = True) -> dict[str, Any]:
    """网页轮询：由它自己的心情/分享欲决定说不说，可一次说 1-2 条。"""
    now = time.time() if now is None else float(now)
    own = state.load(path)["self"]
    mood = float(own.get("mood", 0.55))
    share_drive = float(own.get("sharing", 0.45))
    if respect_state and (mood < 0.32 or share_drive < 0.30):
        return {"ok": True, "available": False, "reason": "现在不太想说话"}
    last = _last_interaction(path)
    if not last:
        return {"ok": True, "available": False, "reason": "还没有对话"}
    if now - last < 8:
        return {"ok": True, "available": False, "reason": "刚聊过"}
    if respect_state and idle_seconds > 0 and mood < 0.55 and share_drive < 0.65 and now - last < idle_seconds:
        return {"ok": True, "available": False, "reason": "还在聊"}
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        last_p = float(row[0] or 0) if row else 0.0
    finally:
        con.close()
    if cooldown > 0 and last_p and now - last_p < cooldown:
        return {"ok": True, "available": False, "reason": "主动表达冷却中"}
    con = db.connect(path)
    try:
        daily = int(con.execute(
            "SELECT count(*) FROM proactive WHERE created_at>=? AND status IN ('spoken','proposed')",
            (now - 86400,)).fetchone()[0])
    finally:
        con.close()
    budget = _daily_budget(path)
    if budget > 0 and daily >= budget:
        return {"ok": True, "available": False, "reason": "今日主动预算已用完"}
    candidates = build_candidates(path, now)
    init_info = {}
    try:
        init_info = initiative.decide(path, now)
    except Exception:  # noqa: BLE001
        init_info = {}
    share_info = {}
    try:
        share_info = sharing.choose_level(path)
    except Exception:  # noqa: BLE001
        share_info = {}
    best = choose_candidate(candidates, path, now)
    quadrant = str(init_info.get("quadrant") or "")
    if quadrant == "skip" or (quadrant == "hold" and int(init_info.get("confidence_n") or 0) >= 3):
        return {"ok": True, "available": False,
                "reason": f"决策象限 {quadrant}", "initiative": init_info,
                "sharing_level": share_info}
    threshold = MIN_SCORE if min_score is None else float(min_score)
    if best["score"] < threshold:
        return {"ok": True, "available": False, "reason": "分享欲不足"}
    kind = str(best.get("kind") or "care")
    recent = memory.recent_messages(path, limit=8)
    dialog = " | ".join(f"{m['role']}:{m['text']}" for m in recent[-6:])
    anchors = _grounding_anchors(path, best)
    # P0-8：问句配额（最近 3 条里 ≥2 条问号结尾 → 本次强制陈述句）
    recent_proactive = _recent_proactive_texts(path, TOPIC_WINDOW)
    recent_questions = sum(1 for old in recent_proactive[:QUESTION_WINDOW]
                           if _ends_question(old))
    force_statement = recent_questions >= QUESTION_FORCE_AT
    prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                          force_statement=force_statement)
    text, llm_answered = _ask_llm(client, prompt)
    if llm_answered and (not text or text.upper().startswith("SKIP")):
        # 明确让 LLM 自己决定"这次不说话"：它说 SKIP 就不发，不再用模板兜底
        return {"ok": True, "available": False, "reason": "llm_skip"}
    text = _normalize_content(text, kind, best, force_statement=force_statement)
    # P0-9 反编造：命中共同经历断言但 anchors 里没有依据 → 重生成 1 次（只讲自己），
    # 仍不过 → 该 kind 的安全兜底句（只讲自己的事）。
    if not grounding_ok(text, anchors):
        own_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                  force_statement=force_statement, own_only=True)
        own_text, own_answered = _ask_llm(client, own_prompt)
        if own_answered and own_text and not own_text.upper().startswith("SKIP"):
            text = _normalize_content(own_text, kind, best,
                                      force_statement=force_statement)
        if not grounding_ok(text, anchors):
            text = _SAFE_FALLBACK.get(kind) or _SAFE_FALLBACK["care"]
    # P0-10 run-on：无标点粘连句（连续 ≥12 汉字后接你/您/我）→ 重生成 1 次
    # （prompt：两层意思用句号分开）→ 仍粘连 → 确定性插入「。」（不丢内容、不换兜底句）。
    if find_runon(text) is not None:
        split_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                    force_statement=force_statement, fix_runon=True)
        split_text, split_answered = _ask_llm(client, split_prompt)
        if split_answered and split_text and not split_text.upper().startswith("SKIP"):
            split_candidate = _normalize_content(split_text, kind, best,
                                                 force_statement=force_statement)
            if grounding_ok(split_candidate, anchors):
                text = split_candidate
        text = _insert_runon_periods(text)
    # P0-8 主题去重：与最近 10 条已发主动消息 2-gram Jaccard ≥0.35 →
    # 同 kind 重生成 1 次（prompt 要求换话题）；仍超标 → 换低重叠兜底句。
    if recent_proactive and max_topic_overlap(text, recent_proactive) >= TOPIC_SIMILARITY_MAX:
        retry_prompt = _kind_prompt(kind, best, mood, share_drive, dialog,
                                    force_statement=force_statement, regenerate=True)
        retry_text, retry_answered = _ask_llm(client, retry_prompt)
        if retry_answered and retry_text and not retry_text.upper().startswith("SKIP"):
            text = _normalize_content(retry_text, kind, best,
                                      force_statement=force_statement)
        if max_topic_overlap(text, recent_proactive) >= TOPIC_SIMILARITY_MAX:
            text = _low_overlap_fallback(kind, best, recent_proactive)
    # P0-10 最终兜底：主题去重重生成/兜底后仍可能粘连 → 确定性插句号（幂等）
    text = _insert_runon_periods(text)
    # P0-9 最后一道：主题兜底句也不能编造共同经历
    if not grounding_ok(text, anchors):
        text = _SAFE_FALLBACK.get(kind) or _SAFE_FALLBACK["care"]
    text = style_profile.tune_reply("", text)[:200]
    max_bubbles = 3 if share_drive >= 0.7 else (2 if share_drive >= 0.55 or mood >= 0.7 else 1)
    if int(init_info.get("max_bubbles") or 0) > 0:
        max_bubbles = min(max_bubbles, int(init_info["max_bubbles"]))
    if quadrant == "explore":
        max_bubbles = min(max_bubbles, 1)
    bubbles = style_profile.split_bubbles(text, max_bubbles)
    bubbles = [style_profile.strip_periods(b) for b in bubbles if style_profile.strip_periods(b)]
    if not bubbles:
        return {"ok": True, "available": False, "reason": "没组织好语言"}
    ids = [memory.store_message(path, "assistant", b, now=now, proactive=True) for b in bubbles]
    joined = " ".join(bubbles)
    turn_id = record_proactive_turn(path, ids[0] if ids else "", joined, now)
    try:
        cand = share_store.record_candidate(
            path, level=str(share_info.get("level") or ""),
            kind=best["kind"], text=joined, reason=best["reason"],
            source="proactive", score=best["score"], status="spoken")
        if cand.get("id"):
            share_store.record_event(path, cand["id"], "spoken",
                                     {"quadrant": quadrant})
    except Exception:  # noqa: BLE001
        pass
    con = db.connect(path)
    try:
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (_id("PRO-", best["kind"], joined, now), best["kind"], joined,
                     best["reason"], best["score"], "spoken", now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "available": True, "message": bubbles[0], "messages": bubbles,
            "message_id": ids[0], "turn_id": turn_id,
            "kind": best["kind"], "score": best["score"],
            "reason": best["reason"], "mood": mood, "sharing": share_drive,
            "initiative": {k: init_info.get(k) for k in
                            ("available", "score", "reason", "max_bubbles", "bubble_count",
                             "confidence_lcb", "quadrant")},
            "sharing_level": share_info}


def propose(path: Path | str = DB_PATH, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else float(now)
    if not _cooldown_ok(path, now):
        return {"ok": True, "available": False, "reason": "主动表达冷却中"}
    candidates = build_candidates(path, now)
    init_info = {}
    try:
        init_info = initiative.decide(path, now)
    except Exception:  # noqa: BLE001
        init_info = {}
    share_info = {}
    try:
        share_info = sharing.choose_level(path)
    except Exception:  # noqa: BLE001
        share_info = {}
    best = choose_candidate(candidates, path, now)
    quadrant = str(init_info.get("quadrant") or "")
    if quadrant == "skip" or (quadrant == "hold" and int(init_info.get("confidence_n") or 0) >= 3):
        return {"ok": True, "available": False,
                "reason": f"决策象限 {quadrant}", "initiative": init_info,
                "sharing_level": share_info}
    if best["score"] < MIN_SCORE:
        return {"ok": True, "available": False, "reason": "分享欲分数不足",
                "candidates": candidates}
    pid = _id("PRO-", best["kind"], best["text"], now)
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (pid, best["kind"], best["text"], best["reason"], best["score"], "proposed", now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "available": True, "id": pid, **best,
            "candidates": candidates,
            "initiative": {k: init_info.get(k) for k in
                           ("available", "score", "confidence_lcb", "quadrant")},
            "sharing_level": share_info,
            "note": "这是候选，不会自动发送"}


def decide(path: Path | str, proposal_id: str, action: str) -> dict[str, Any]:
    action = str(action).lower()
    if action not in ("accepted", "rejected"):
        return {"ok": False, "error": "action 只允许 accepted/rejected"}
    con = db.connect(path)
    try:
        cur = con.execute("UPDATE proactive SET status=? WHERE id=? AND status='proposed'",
                          (action, proposal_id))
        con.commit()
        row = con.execute("SELECT * FROM proactive WHERE id=?", (proposal_id,)).fetchone()
    finally:
        con.close()
    if cur.rowcount == 0:
        return {"ok": False, "error": "候选不存在或已处理"}
    return {"ok": True, "action": action, "proposal": dict(row)}


def list_proactive(path: Path | str = DB_PATH, limit: int = 20) -> list[dict]:
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM proactive ORDER BY created_at DESC LIMIT ?", (int(limit),))]
    finally:
        con.close()
