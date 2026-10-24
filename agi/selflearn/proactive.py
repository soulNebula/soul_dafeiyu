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
_KIND_FALLBACK = {
    "care": "这两天忙不忙？不用急着回，我就是问一句。",
    "share": "刚想到你之前说的{topic}，突然想接着聊聊这个。",
    "followup": "上次你说「{anchor}」，后来怎么样了？",
    "light": "说个轻松的：最近有没有什么离谱事，让我也乐一乐？",
    "silence_ack": "好久没说话了，冒个泡。你最近还好吗？",
}


def kind_fallback(kind: str, *, anchor: str = "", topic: str = "") -> str:
    """某类意图的兜底文本（各类互不相同）。"""
    template = _KIND_FALLBACK.get(str(kind or ""), _KIND_FALLBACK["care"])
    return template.format(anchor=str(anchor or "").strip(),
                           topic=str(topic or "").strip())



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
            "kind": "care", "text": kind_fallback("care"),
            "reason": f"已 {idle_h:.1f} 小时没互动" if idle_h >= CARE_MIN_IDLE_H
                      else "上次情绪偏负",
            "base": 0.55,
        })
    # share：有记忆话题或关系够近，分享自己的想法
    if topic or trust_value >= 0.55:
        candidates.append({
            "kind": "share",
            "text": kind_fallback("share", topic=topic or "最近的事"),
            "reason": "记忆里有可接回的话题" if topic else "信任足够，适合分享自己的念头",
            "base": 0.50, "topic": topic,
        })
    # followup：接上次没聊完的具体内容（必须能引用到锚点）
    anchor = _followup_anchor(path, now)
    if anchor and idle_h >= FOLLOWUP_MIN_IDLE_H:
        candidates.append({
            "kind": "followup", "text": kind_fallback("followup", anchor=anchor),
            "reason": f"上次聊到「{anchor}」", "base": 0.52, "anchor": anchor,
        })
    # light：仅白天且关系 ≥ 熟悉
    if LIGHT_HOURS[0] <= hour < LIGHT_HOURS[1] and _STAGE_RANK.get(stage, 0) >= 2:
        candidates.append({
            "kind": "light", "text": kind_fallback("light"),
            "reason": "白天且关系够近，适合轻松接梗", "base": 0.45,
        })
    # silence_ack：久未联系 + 至少 7 天没发过
    last_ack = _last_kind_ts(path, "silence_ack")
    if idle_h >= SILENCE_MIN_IDLE_H and (not last_ack or now - last_ack >= SILENCE_ACK_GAP_S):
        candidates.append({
            "kind": "silence_ack", "text": kind_fallback("silence_ack"),
            "reason": f"已 {idle_h:.1f} 小时没联系", "base": 0.40,
        })
    if not candidates:
        # 关系还远/刚从沉默里出来：先轻轻问一句，别硬分享
        candidates.append({
            "kind": "care", "text": kind_fallback("care"),
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


def polish_outbound(text: Any) -> str:
    """出站文本质检（发送前）：去首尾空白、合并连续空白、补句末标点、超长按标点切分。

    规则：
    - 句尾缺标点：以「吗/呢/么/嘛」结尾补问号，其余陈述补句号；
    - 超过 ``LONG_TEXT_CHARS`` 且有多句时，按句末标点切分并用换行分隔（发送层逐段发），
      切分只加分隔符、不丢字；
    - 已合规文本（无多余空白、已有句末标点、未超长）**逐字节不变**。

    纯函数：不读文件、不依赖时间/随机。
    """
    value = _WS_RE.sub(" ", str(text or "")).strip()
    if not value:
        return ""
    if value[-1] not in END_PUNCT:
        value += "？" if value.endswith(_QUESTION_TAILS) else "。"
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
                 dialog: str) -> str:
    """按 kind 选不同的生成指令（同一 LLM 调用里只玩一种意图）。"""
    instruction = KIND_INSTRUCTION.get(kind) or KIND_INSTRUCTION["care"]
    prompt = (
        f"你现在心情 {mood:.2f}，分享欲 {share_drive:.2f}。用户安静了，你想主动说点什么。"
        + instruction
        + "只输出你要说的话，1-2 句，口语；不要客服腔、不报能力、不列清单。"
    )
    anchor = str(candidate.get("anchor") or "").strip()
    topic = str(candidate.get("topic") or "").strip()
    if kind == "followup" and anchor:
        prompt += f"必须提到这个具体内容：「{anchor}」，不要换成泛泛的近况。"
    elif kind == "share" and topic:
        prompt += f"可以从对方之前说过的「{topic}」自然引到你自己的想法。"
    prompt += "如果实在没话说，输出 SKIP。最近对话：" + dialog
    return prompt



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
    prompt = _kind_prompt(kind, best, mood, share_drive, dialog)
    text = ""
    llm_answered = False
    if client is not None and getattr(client, "available", lambda: False)():
        r = client.chat([{"role": "system", "content": "你说话像普通人，短句。"},
                         {"role": "user", "content": prompt}],
                        purpose="selflearn_proactive", max_tokens=150, temperature=0.8)
        if r.get("ok"):
            llm_answered = True
            text = str(r.get("text") or "").strip()
    if llm_answered and (not text or text.upper().startswith("SKIP")):
        # 明确让 LLM 自己决定"这次不说话"：它说 SKIP 就不发，不再用模板兜底
        return {"ok": True, "available": False, "reason": "llm_skip"}
    if not text:
        text = str(best.get("text") or kind_fallback(kind))
    # followup 硬约束：必须引用最近对话里的具体内容，跑题就回退到带锚点的兜底句
    if kind == "followup":
        anchor = str(best.get("anchor") or "").strip()
        if anchor and not _mentions_anchor(text, anchor):
            text = kind_fallback("followup", anchor=anchor)
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
