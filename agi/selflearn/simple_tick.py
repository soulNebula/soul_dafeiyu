# -*- coding: utf-8 -*-
"""P6-e：主动消息 simple 链路（B 方案）。

- ``proactive_style`` 默认 legacy；运行期 ``agi/data/wechat/config.json``
  或环境变量 ``PROACTIVE_STYLE=simple`` 切到本链路。
- simple：prompt 只留人格边界 + 不编造 + 像微信 + 禁元话题；生成一次 →
  解析 media → 硬检查；违规重生成 1 次；仍违规丢弃本条。
- **不调用** legacy 的开头池/转折词/口语/截断/连续性/兜底/style_profile 后处理。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
WECHAT_CONFIG = ROOT / "agi" / "data" / "wechat" / "config.json"
DEFAULT_WECHAT_CONFIG = WECHAT_CONFIG
MAX_LEN = 80
META_RE = re.compile(r"表情包|素材|清单|表情库|记忆|系统|提示词|prompt|数据库|指令|\[\[media")
BRACKET_RE = re.compile(
    r"^[（(【\[].{1,20}[）)】\]]"
    r"|[（(][^）)]{2,30}[）)]"
    r"|【.{1,20}】"
    r"|\*.{1,20}\*"
    r"|（?(?:他|她|它)?(?:放下|拿起|抬头|低头|笑|叹|顿|沉默|想了想)")
SILENCE_RE = re.compile(
    r"你(?:刚才|刚才怎么|怎么)?没(?:接话|回|理我)"
    r"|是不是(?:不想|懒得)理|你(?:都)?不理|怎么不回|忙吗|在忙吗|冒个泡|"
    r"好久没(?:动静|消息)")
FRAGMENT_TRANSITIONS = ("换个话题", "聊点别的", "说个事")


def proactive_style(path: Path | str | None = None) -> str:
    """读取 simple/legacy；默认 legacy，运行期以 wechat config 为准。"""
    env = os.environ.get("PROACTIVE_STYLE")
    if env in ("legacy", "simple"):
        return env
    # pytest 隔离：生产 config 的 simple 不应改变既有 legacy 基线；测试显式设 env 或
    # monkeypatch WECHAT_CONFIG 到临时文件时才读文件。
    if (("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules)
            and Path(WECHAT_CONFIG) == DEFAULT_WECHAT_CONFIG):
        return "legacy"
    try:
        raw = json.loads(WECHAT_CONFIG.read_text(encoding="utf-8"))
        value = str(raw.get("proactive_style") or "").strip().lower()
        if value in ("legacy", "simple"):
            return value
    except Exception:  # noqa: BLE001
        pass
    return "legacy"


def _normalized(text: str) -> str:
    return re.sub(r"[\s，,。.！!？?；;、…]+", "", str(text or ""))


def _recent_texts(path, limit: int = 5) -> list[str]:
    from . import proactive
    try:
        return proactive._recent_proactive_texts(path, limit=limit)
    except Exception:  # noqa: BLE001
        return []


def _violations(text: str, media: dict | None, anchors: list[str],
                recent: list[str], *, kind: str = "", anchor: str = "") -> list[str]:
    """simple 硬检查；只返回原因，不做任何改写。"""
    from . import media_choice, proactive
    value = str(text or "").strip()
    reasons: list[str] = []
    if not value or len(value) <= 2:
        reasons.append("empty_or_too_short")
    if len(value) > MAX_LEN:
        reasons.append("too_long")
    if META_RE.search(value):
        reasons.append("meta_topic")
    if proactive.find_physical_detail(value) is not None:
        reasons.append("physical_detail")
    if proactive._has_shared_experience_claim(value) or not proactive.grounding_ok(value, anchors):
        reasons.append("fabrication")
    if media_choice.INNER_MONOLOGUE_RE.search(value):
        reasons.append("inner_monologue")
    if media_choice.polarity_conflict(value):
        reasons.append("polarity_conflict")
    norm = _normalized(value)
    if any(norm and norm == _normalized(item) for item in recent[:5]):
        reasons.append("duplicate_recent")
    if media_choice.STICKER_REFERENCE_RE.search(value) and not media_choice._has_sticker(media):
        reasons.append("sticker_ref_without_media")
    if BRACKET_RE.search(value):
        reasons.append("bracket_action")
    if SILENCE_RE.search(value):
        reasons.append("silence_comment")
    if kind == "progress_check" and anchor and anchor not in value:
        reasons.append("progress_anchor_missing")
    if kind == "progress_check" and not re.search(
            r"[?？]|怎么样|如何|顺利|进展|结果|定下来|弄完|完成|成功", value):
        reasons.append("progress_question_missing")
    return reasons


def _simple_prompt(kind: str, dialog: str, media_ctx: str,
                   fix_reason: str = "", topic_ctx: str = "",
                   anchor: str = "") -> str:
    from . import proactive
    prompt = (proactive.PERSONA_CONSTRAINTS + "\n" + proactive.GROUNDING_CONSTRAINTS +
              "\n像真人微信聊天：短句、口语、不加句号；只说一句，不写作文/独白。"
              "\n一条消息只说一件事，尽量 30 字以内；不要括号动作/旁白；"
              "不要问对方为什么不回。"
              "\n正文禁止出现：表情包/素材/清单/表情库/系统/指令/记忆库/记忆/[[media："
              "想发贴纸就单独输出 [[media:...]] 指令行，不要用文字描述。")
    if topic_ctx:
        prompt += "\n" + topic_ctx
    if media_ctx:
        prompt += "\n" + media_ctx
    if fix_reason:
        prompt += ("\n【重生成】上次的问题：" + fix_reason +
                   "。只输出一句自然微信，不要解释，不要写元话题/内心独白。")
    prompt += "\n最近对话：" + dialog
    return prompt


def _ask_simple(client, prompt: str, temperature: float = 0.8) -> tuple[str, bool]:
    """simple 独立生成：给足 tokens，避免 legacy 150 token 把自然句截断。"""
    if client is None or not getattr(client, "available", lambda: False)():
        return "", False
    try:
        result = client.chat(
            [{"role": "system", "content": "你说话像普通人，短句。"},
             {"role": "user", "content": prompt}],
            purpose="selflearn_proactive_simple", max_tokens=300,
            temperature=temperature)
    except Exception:  # noqa: BLE001
        return "", False
    if not isinstance(result, dict) or not result.get("ok"):
        return "", False
    return str(result.get("text") or "").strip(), True


def _light_polish(text: str) -> str:
    """轻量标点安全层：句中句号→逗号、去尾部句号、连续标点归一；幂等。"""
    value = str(text or "").strip()
    if not value:
        return value
    value = re.sub(r"[，,]{2,}", "，", value)
    value = re.sub(r"，。", "。", value)
    value = re.sub(r"。+", "。", value)
    value = re.sub(r"。+(?=.)", "，", value)
    value = re.sub(r"[。.]+\s*$", "", value)
    value = re.sub(r"[，,]{2,}", "，", value)
    return value.strip("，, ").strip()


def _parse_generated(raw: str):
    from . import media_choice, proactive
    text, directives = media_choice.parse_media_directive(raw)
    _choice, body = proactive.parse_voice_choice(text)
    if body:
        text = body
    text = re.sub(r"(?i)\b(?:action|choice)\s*[:：]\s*[a-z_]*\s*", "", text).strip()
    return text.strip(), directives


def _record(path, kind: str, text: str, reason: str, score: float,
            now: float, media: dict | None) -> dict:
    from . import memory, proactive, db
    message_id = memory.store_message(path, "assistant", text, now=now, proactive=True)
    try:
        turn_id = proactive.record_proactive_turn(path, message_id, text, now)
    except Exception:  # noqa: BLE001
        turn_id = None
    con = db.connect(path)
    try:
        con.execute("INSERT INTO proactive(id,kind,text,reason,score,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (proactive._id("PRO-", kind, text, now), kind, text, reason,
                     float(score or 0), "spoken", now))
        con.commit()
    finally:
        con.close()
    return {"message": text, "messages": [text], "message_id": message_id,
            "turn_id": turn_id, "media": media}


def tick_simple(path: Path | str, client=None, *, now: float | None = None,
                idle_seconds: float = 30, cooldown: float = 900,
                min_score: float | None = None, respect_state: bool = True,
                inner_client=None) -> dict:
    from . import db, initiative, memory, proactive, sharing, state

    now = time.time() if now is None else float(now)
    own = state.load(path)["self"]
    mood = float(own.get("mood", 0.55))
    share_drive = float(own.get("sharing", 0.45))
    energy = float(own.get("energy", 0.6))
    if respect_state and (mood < 0.32 or share_drive < 0.30):
        return {"ok": True, "available": False, "reason": "现在不太想说话",
                "style": "simple"}
    last = proactive._last_interaction(path)
    if not last:
        return {"ok": True, "available": False, "reason": "还没有对话", "style": "simple"}
    if now - last < 8:
        return {"ok": True, "available": False, "reason": "刚聊过", "style": "simple"}
    if respect_state and idle_seconds > 0 and mood < 0.55 and share_drive < 0.65 \
            and now - last < idle_seconds:
        return {"ok": True, "available": False, "reason": "还在聊", "style": "simple"}
    con = db.connect(path)
    try:
        row = con.execute("SELECT max(created_at) FROM proactive").fetchone()
        last_p = float(row[0] or 0) if row else 0.0
    finally:
        con.close()
    if cooldown > 0 and last_p and now - last_p < cooldown:
        return {"ok": True, "available": False, "reason": "主动表达冷却中",
                "style": "simple"}
    con = db.connect(path)
    try:
        daily = int(con.execute(
            "SELECT count(*) FROM proactive WHERE created_at>=? AND status IN ('spoken','proposed')",
            (now - 86400,)).fetchone()[0])
    finally:
        con.close()
    if proactive._daily_budget(path) > 0 and daily >= proactive._daily_budget(path):
        return {"ok": True, "available": False, "reason": "今日主动预算已用完",
                "style": "simple"}
    candidates = proactive.build_candidates(path, now)
    try:
        init_info = initiative.decide(path, now)
    except Exception:  # noqa: BLE001
        init_info = {}
    try:
        share_info = sharing.choose_level(path)
    except Exception:  # noqa: BLE001
        share_info = {}
    best = proactive.choose_candidate(candidates, path, now)
    quadrant = str(init_info.get("quadrant") or "")
    if quadrant == "skip" or (quadrant == "hold"
                              and int(init_info.get("confidence_n") or 0) >= 3):
        return {"ok": True, "available": False, "reason": f"决策象限 {quadrant}",
                "style": "simple", "initiative": init_info, "sharing_level": share_info}
    threshold = proactive.MIN_SCORE if min_score is None else float(min_score)
    if best["score"] < threshold:
        return {"ok": True, "available": False, "reason": "分享欲不足", "style": "simple"}
    kind = str(best.get("kind") or "care")
    recent = memory.recent_messages(path, limit=8)
    dialog = " | ".join(f"{m['role']}:{m['text']}" for m in recent[-6:])
    anchors = proactive._grounding_anchors(path, best)
    recent_texts = _recent_texts(path, 5)
    media_ctx = ""
    try:
        from . import media_choice
        media_ctx = media_choice.media_prompt_block(path)
    except Exception:  # noqa: BLE001
        media_ctx = ""
    from . import topic_seed as _topic_seed
    topic = {"picked": "", "candidates": [], "reason": "disabled"}
    topic_ctx = ""
    if _topic_seed.enabled():
        try:
            topic = _topic_seed.build(path, now)
            topic_ctx = _topic_seed.prompt_block(topic)
        except Exception:  # noqa: BLE001
            topic = {"picked": "", "candidates": [], "reason": "error"}
            topic_ctx = ""
    progress_anchor = str(best.get("anchor") or "") if kind == "progress_check" else ""
    prompt = _simple_prompt(kind, dialog, media_ctx,
                            topic_ctx="" if kind == "progress_check" else topic_ctx,
                            anchor=progress_anchor)
    raw, answered = _ask_simple(client, prompt,
                                 temperature=0.35 if kind == "progress_check" else 0.8)
    if not answered or not raw or raw.upper().startswith("SKIP"):
        return {"ok": True, "available": False, "reason": "llm_skip",
                "style": "simple", "topic_seed": topic}
    text, directives = _parse_generated(raw)
    text = _light_polish(text)
    media_choice = None
    media = None
    try:
        from . import media_choice as _media
        media_choice = _media
        media = _media.build_plan(path, directives, text,
                                  state={"mood": mood, "crisis": bool(own.get("crisis"))},
                                  recent_media=_media.recent_media(path),
                                  allow_sticker=True, now=now)
    except Exception:  # noqa: BLE001
        media = None
    reasons = _violations(text, media, anchors, recent_texts,
                          kind=kind, anchor=str(best.get("anchor") or ""))
    regen_reason = ",".join(reasons)
    if reasons:
        fix_prompt = _simple_prompt(kind, dialog, media_ctx,
                                    fix_reason=regen_reason,
                                    topic_ctx="" if kind == "progress_check" else topic_ctx,
                                    anchor=str(best.get("anchor") or ""))
        fix_raw, fix_answered = _ask_simple(
            client, fix_prompt, temperature=0.35 if kind == "progress_check" else 0.8)
        if fix_answered and fix_raw and not fix_raw.upper().startswith("SKIP"):
            fix_text, fix_directives = _parse_generated(fix_raw)
            fix_text = _light_polish(fix_text)
            try:
                fix_media = media_choice.build_plan(
                    path, fix_directives, fix_text,
                    state={"mood": mood, "crisis": bool(own.get("crisis"))},
                    recent_media=media_choice.recent_media(path),
                    allow_sticker=True, now=now) if media_choice else None
            except Exception:  # noqa: BLE001
                fix_media = None
            fix_reasons = _violations(fix_text, fix_media, anchors, recent_texts,
                                      kind=kind,
                                      anchor=str(best.get("anchor") or ""))
            if not fix_reasons:
                text, media = fix_text, fix_media
                reasons = []
    # P0-15：进度追问是确定性任务；模型两次都没写出问句时，用真实 anchor 的
    # 安全问句兜底（不编造、无 anchor 不乱问），而不是把这条好待办丢掉。
    if reasons and kind == "progress_check" and progress_anchor:
        fallback_text = f"想起你说「{progress_anchor}」，现在进展怎么样了？"
        fallback_reasons = _violations(fallback_text, None, anchors, recent_texts,
                                       kind=kind, anchor=progress_anchor)
        if not fallback_reasons:
            text = fallback_text
            reasons = []
            media = None
    if reasons:
        return {"ok": True, "available": False, "reason": "discarded",
                "discard_reason": ",".join(reasons), "regen_reason": regen_reason,
                "style": "simple", "media": None, "topic_seed": topic}
    if media_choice is not None:
        try:
            media_choice.record_media(path, bool(media and media.get("bubbles")))
        except Exception:  # noqa: BLE001
            pass
    recorded = _record(path, kind, text, str(best.get("reason") or "simple"),
                       best.get("score"), now, media)
    return {"ok": True, "available": True, "style": "simple",
            "regen_reason": regen_reason, "discard_reason": "",
            "topic_seed": topic,
            "progress_check": ({"anchor": best.get("anchor"),
                                "msg_id": str((best.get("todo") or {}).get("msg_id") or "")}
                               if kind == "progress_check" else None),
            "kind": kind, "action": "simple", "score": best.get("score"),
            "reason": best.get("reason"), **recorded}
