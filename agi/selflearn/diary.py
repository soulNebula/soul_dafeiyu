# -*- coding: utf-8 -*-
"""P31：小鱼的日记（每晚 23:00 自动写今天的情感与经过）。

用户要求：**每天晚上写一篇自己的日记**，存本机文本文档，写今天的情感和经过，
**她想写什么就写什么**（自由发挥，不是工作报告）。

设计：

- ``collect()``：只读汇总当天素材（对话 / 主动消息 / 冲浪 / 心情 / 兴趣 / B站印象），
  压缩成 ≤3000 字给模型回忆（不照抄、不编造）；
- ``write()``：人格 prompt + 素材 → DeepSeek 生成第一人称 300–600 字（可短），
  存 ``agi/data/diary/YYYY-MM-DD.txt``（UTF-8）+ ``index.jsonl``；幂等，``force`` 才重写；
  模型失败走**简短真实**的规则 fallback（标记 fallback=true），绝不写空文件；
- ``latest()`` / ``list_days()`` / ``get()``：给控制台只读接口；
- 定时在 gateway 线程里：每 60s 看一次，≥``diary_hour`` 且当天未写就写；
  engine 停也允许写（写日记不打扰人），由 ``diary_enabled`` 独立开关控制。

隐私：日记含用户对话，只写本机 ``agi/data/diary/``；生成时素材会发给 DeepSeek API
（这是产品行为，见 plans/53 风险一节）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

from . import db, state
from .config import DATA_DIR, DB_PATH

ROOT = Path(__file__).resolve().parents[2]
#: 本机日记目录（用户要的「文本文档」）
DIARY_DIR = DATA_DIR.parent / "diary"
INDEX_PATH = DIARY_DIR / "index.jsonl"
#: 微信联系人库目录（网关按联系人分库；日记要把当天各库的对话都收进来）
CONTACT_DB_DIR = DATA_DIR / "wechat_contacts"
#: 网关日志（主动消息发没发/被回没回）
GATEWAY_LOG = _default_data_dir() / "wechat" / "gateway.log"
WECHAT_CONFIG = _default_data_dir() / "wechat" / "config.json"

DEFAULT_HOUR = 23
MAX_MATERIAL_CHARS = 3000
MAX_OUTPUT_CHARS = 2400
MIN_OUTPUT_CHARS = 40
#: 失败重试 / 已写后复查的间隔由 gateway 循环控制（见 server._diary_loop）
PROMPT_PURPOSE = "diary"


# ------------------------------------------------------------------ 基础
def enabled() -> bool:
    """``diary_enabled`` 总开关（env ``DIARY_ENABLED`` > config.DIARY_ENABLED）。"""
    raw = os.environ.get("DIARY_ENABLED")
    if raw is not None:
        return str(raw).strip().lower() not in ("0", "false", "off", "no", "")
    try:
        from .config import DIARY_ENABLED
    except Exception:  # noqa: BLE001
        DIARY_ENABLED = True
    return bool(DIARY_ENABLED)


def diary_hour() -> int:
    """每天几点后允许写（默认 23）。"""
    raw = os.environ.get("DIARY_HOUR")
    try:
        return int(raw) if raw is not None else int(_cfg("DIARY_HOUR", DEFAULT_HOUR))
    except (TypeError, ValueError):
        return DEFAULT_HOUR


def diary_dir() -> Path:
    """日记目录（env ``DIARY_DIR`` 可覆盖；测试用 tmp）。"""
    raw = os.environ.get("DIARY_DIR")
    return Path(raw) if raw else Path(DIARY_DIR)


def _cfg(name: str, default: Any) -> Any:
    try:
        from importlib import import_module
        config = import_module("agi.selflearn.config")
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def normalize_date(value: Any = None, now: float | None = None) -> str:
    """``YYYY-MM-DD``；空/非法 → 今天（本地时区）。"""
    if value is None or str(value).strip() == "":
        return time.strftime("%Y-%m-%d", time.localtime(_now(now)))
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return time.strftime("%Y-%m-%d", time.strptime(text, fmt))
        except ValueError:
            continue
    return time.strftime("%Y-%m-%d", time.localtime(_now(now)))


def day_bounds(date: str) -> tuple[float, float]:
    """当天本地 00:00–次日 00:00 的时间戳。"""
    start = time.mktime(time.strptime(f"{normalize_date(date)} 00:00:00", "%Y-%m-%d %H:%M:%S"))
    return start, start + 86400


def is_due(now: float | None = None, hour: int | None = None) -> bool:
    """当前是否已过写日记的时间点（本地小时，>= hour）。"""
    now = _now(now)
    target = diary_hour() if hour is None else int(hour)
    return time.localtime(now).tm_hour >= max(0, min(23, target))


def _read_json(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _contact_names() -> dict[str, str]:
    """hash 前缀 → 联系人名（网关用 sha1(contact)[:14] 命名库文件）。"""
    cfg = _read_json(Path(WECHAT_CONFIG))
    names: list[str] = []
    for key in ("monitored_contacts", "allowed_contacts"):
        for item in cfg.get(key) or []:
            text = str(item).strip()
            if text and text not in names:
                names.append(text)
    out: dict[str, str] = {}
    for name in names:
        out[hashlib.sha1(name.encode("utf-8")).hexdigest()[:14]] = name
    return out


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not Path(path).exists():
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con
    except Exception:  # noqa: BLE001
        return None


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    try:
        row = con.execute("SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1",
                          (str(name),)).fetchone()
        return bool(row)
    except Exception:  # noqa: BLE001
        return False


def _message_sources(path: Path | str | None = None) -> list[tuple[str, Path]]:
    """(联系人名, memory 库路径)：主库 + 各联系人库。"""
    base = Path(path) if path else Path(DB_PATH)
    sources: list[tuple[str, Path]] = [("", db.memory_path(base))]
    names = _contact_names()
    contact_dir = Path(CONTACT_DB_DIR)
    try:
        files = sorted(contact_dir.glob("*.memory.sqlite3"))
    except OSError:
        files = []
    for item in files:
        prefix = item.name.split(".", 1)[0]
        sources.append((names.get(prefix, f"联系人-{prefix[:6]}"), item))
    return sources


# ------------------------------------------------------------------ collect
def _collect_conversation(path, start: float, end: float) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    contacts: set[str] = set()
    hours: dict[int, int] = {}
    for label, mem_path in _message_sources(path):
        con = _open_ro(mem_path)
        if con is None:
            continue
        try:
            if _table_exists(con, "messages"):
                for row in con.execute(
                        "SELECT role,text,ts,proactive FROM messages "
                        "WHERE ts>=? AND ts<? ORDER BY ts LIMIT 3000", (start, end)):
                    text = str(row["text"] or "").strip()
                    if not text:
                        continue
                    item = {"role": str(row["role"] or ""), "text": text,
                            "ts": float(row["ts"] or 0),
                            "proactive": bool(row["proactive"]), "contact": label}
                    messages.append(item)
                    hours[int(time.localtime(item["ts"]).tm_hour)] = \
                        hours.get(int(time.localtime(item["ts"]).tm_hour), 0) + 1
                    if label:
                        contacts.add(label)
            if _table_exists(con, "turns"):
                for row in con.execute(
                        "SELECT feedback,score,created_at FROM turns "
                        "WHERE created_at>=? AND created_at<? LIMIT 3000", (start, end)):
                    turns.append({"feedback": row["feedback"], "score": row["score"],
                                  "at": float(row["created_at"] or 0)})
        finally:
            con.close()
    messages.sort(key=lambda item: item["ts"])
    feedback: dict[str, int] = {}
    for item in turns:
        key = str(item.get("feedback") or "none")
        feedback[key] = feedback.get(key, 0) + 1
    user_msgs = [m for m in messages if m["role"] == "user"]
    assistant = [m for m in messages if m["role"] == "assistant"]
    proactive = [m for m in messages if m["proactive"]]
    return {
        "message_count": len(messages), "user_count": len(user_msgs),
        "assistant_count": len(assistant), "proactive_message_count": len(proactive),
        "turn_count": len(turns), "feedback": feedback,
        "contacts": sorted(contacts), "hours": dict(sorted(hours.items())),
        "messages": messages, "user_messages": user_msgs,
    }


def _collect_proactive_log(start: float, end: float,
                          user_times: list[float]) -> dict[str, Any]:
    """读 gateway.log 里当天真正发出去的主动消息（内容/时间/被回没回）。"""
    items: list[dict[str, Any]] = []
    try:
        lines = Path(GATEWAY_LOG).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("kind") != "proactive_send":
            continue
        try:
            ts = float(entry.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if not (start <= ts < end):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        answered = any(0 <= t - ts <= 6 * 3600 for t in user_times)
        items.append({"ts": ts, "text": text,
                      "friend": str(entry.get("friend") or ""),
                      "status": str(entry.get("status") or ""),
                      "intent_kind": str(entry.get("intent_kind") or ""),
                      "answered": answered})
    items.sort(key=lambda item: item["ts"])
    return {"count": len(items), "items": items,
            "answered": sum(1 for item in items if item["answered"])}


def _collect_surf(path, start: float, end: float) -> dict[str, Any]:
    con = _open_ro(db.memory_path(Path(path or DB_PATH)))
    if con is None:
        return {"sessions": 0, "minutes": 0.0, "items": 0, "keywords": [],
                "summaries": []}
    try:
        if not _table_exists(con, "surf_sessions"):
            return {"sessions": 0, "minutes": 0.0, "items": 0, "keywords": [],
                    "summaries": []}
        rows = con.execute(
            "SELECT started_at,duration_min,items_seen,keywords_json,summary "
            "FROM surf_sessions WHERE started_at>=? AND started_at<? "
            "ORDER BY started_at", (start, end)).fetchall()
    finally:
        con.close()
    keywords: dict[str, int] = {}
    summaries: list[str] = []
    minutes = 0.0
    items = 0
    for row in rows:
        minutes += float(row["duration_min"] or 0)
        items += int(row["items_seen"] or 0)
        try:
            for word in json.loads(row["keywords_json"] or "[]"):
                text = str(word or "").strip()
                if text:
                    keywords[text] = keywords.get(text, 0) + 1
        except (TypeError, ValueError):
            pass
        summary = str(row["summary"] or "").strip()
        if summary:
            summaries.append(summary)
    return {"sessions": len(rows), "minutes": round(minutes, 1), "items": items,
            "keywords": [word for word, _ in sorted(keywords.items(),
                                                    key=lambda kv: (-kv[1], kv[0]))],
            "summaries": summaries[-3:]}


def _collect_mood(path, start: float, end: float) -> dict[str, Any]:
    """心情曲线（mood_timeline）+ 当前 self_state 快照。"""
    out: dict[str, Any] = {"count": 0, "avg": None, "start": None, "end": None,
                           "low": None, "high": None, "reasons": [],
                           "source": "none", "current": {}}
    try:
        out["current"] = dict(state.load().get("self") or {})
    except Exception:  # noqa: BLE001
        out["current"] = {}
    con = _open_ro(db.persona_path(Path(path or DB_PATH)))
    if con is not None:
        try:
            if _table_exists(con, "mood_timeline"):
                rows = con.execute(
                    "SELECT at,mood,reason FROM mood_timeline WHERE at>=? AND at<? "
                    "ORDER BY at", (start, end)).fetchall()
                values = [float(row["mood"] or 0) for row in rows]
                if values:
                    out.update({
                        "count": len(values),
                        "avg": round(sum(values) / len(values), 1),
                        "start": values[0], "end": values[-1],
                        "low": min(values), "high": max(values),
                        "reasons": [str(row["reason"] or "").strip()
                                    for row in rows if str(row["reason"] or "").strip()][-3:],
                        "source": "mood_timeline"})
        finally:
            con.close()
    if out["avg"] is None:
        current = out.get("current") or {}
        try:
            out["avg"] = round(float(current.get("mood") or 0.55) * 100, 1)
        except (TypeError, ValueError):
            out["avg"] = 55.0
        out["source"] = "self_state"
    return out


def _collect_persona(path, start: float, end: float) -> dict[str, Any]:
    con = _open_ro(db.persona_path(Path(path or DB_PATH)))
    if con is None:
        return {"count": 0, "keys": []}
    try:
        if not _table_exists(con, "persona_layers"):
            return {"count": 0, "keys": []}
        rows = con.execute(
            "SELECT key,layer FROM persona_layers WHERE created_at>=? AND created_at<?",
            (start, end)).fetchall()
    finally:
        con.close()
    return {"count": len(rows), "keys": [str(row["key"] or "") for row in rows][:6]}


def _collect_interests(path, start: float, end: float) -> dict[str, Any]:
    con = _open_ro(db.persona_path(Path(path or DB_PATH)))
    hot: list[dict[str, Any]] = []
    top: list[dict[str, Any]] = []
    if con is not None:
        try:
            if _table_exists(con, "interest_events"):
                rows = con.execute(
                    "SELECT topic,delta,source FROM interest_events "
                    "WHERE at>=? AND at<? ORDER BY at", (start, end)).fetchall()
                grouped: dict[str, dict[str, Any]] = {}
                for row in rows:
                    topic = str(row["topic"] or "").strip()
                    if not topic:
                        continue
                    item = grouped.setdefault(topic, {"topic": topic, "delta": 0.0,
                                                      "sources": set()})
                    item["delta"] = round(float(item["delta"]) + float(row["delta"] or 0), 3)
                    source = str(row["source"] or "").strip()
                    if source:
                        item["sources"].add(source)
                hot = [{"topic": item["topic"], "delta": item["delta"],
                        "sources": sorted(item["sources"])}
                       for item in grouped.values()]
                hot.sort(key=lambda item: -float(item["delta"]))
            if _table_exists(con, "interests"):
                top = [{"topic": str(row["topic"] or ""),
                        "weight": round(float(row["weight"] or 0), 3)}
                       for row in con.execute(
                           "SELECT topic,weight FROM interests ORDER BY weight DESC LIMIT 5")]
        finally:
            con.close()
    return {"hot": hot[:8], "top": top}


def _collect_bili(path, start: float, end: float) -> list[dict[str, Any]]:
    con = _open_ro(db.memory_path(Path(path or DB_PATH)))
    if con is None:
        return []
    try:
        if not _table_exists(con, "bili_impressions"):
            return []
        rows = con.execute(
            "SELECT title,my_take,value FROM bili_impressions "
            "WHERE created_at>=? AND created_at<? ORDER BY created_at LIMIT 10",
            (start, end)).fetchall()
    finally:
        con.close()
    return [{"title": str(row["title"] or "").strip(),
             "my_take": str(row["my_take"] or "").strip(),
             "value": float(row["value"] or 0)} for row in rows]


def _activity_context(now: float) -> str:
    """P37：日记也用真实的今日 bili/surf 状态，避免把旧话当故障复述。"""
    try:
        from .simple_tick import today_activity_context
        return str(today_activity_context(now)).strip()
    except Exception:  # noqa: BLE001
        return ""


def collect(path: Path | str | None = None, date: str | None = None, *,
            now: float | None = None) -> dict[str, Any]:
    """只读汇总某天素材；返回结构化 dict（含压缩后的 materials）。"""
    day = normalize_date(date, now)
    start, end = day_bounds(day)
    conversation = _collect_conversation(path, start, end)
    user_times = [item["ts"] for item in conversation["user_messages"]]
    proactive = _collect_proactive_log(start, end, user_times)
    data: dict[str, Any] = {
        "ok": True, "date": day, "day_start": start, "day_end": end,
        "conversation": conversation,
        "proactive": proactive,
        "surf": _collect_surf(path, start, end),
        "mood": _collect_mood(path, start, end),
        "persona_updates": _collect_persona(path, start, end),
        "interests": _collect_interests(path, start, end),
        "bili": _collect_bili(path, start, end),
        "activity": _activity_context(start),
    }
    data["mood_avg"] = data["mood"].get("avg")
    data["materials"] = materials(data)
    return data


# ------------------------------------------------------------------ 素材压缩
def _clip(text: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value if len(value) <= limit else value[:limit].rstrip() + "…"


def _sample_messages(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """均匀抽样，保留开头/结尾（一天里最早和最近发生的事最像"经过"）。"""
    if len(items) <= limit:
        return items
    head = items[:max(1, limit // 3)]
    tail = items[-max(1, limit // 3):]
    middle_pool = items[len(head):len(items) - len(tail)]
    need = max(0, limit - len(head) - len(tail))
    step = max(1, len(middle_pool) // need) if need else 1
    middle = middle_pool[::step][:need]
    return head + middle + tail


def materials(data: dict[str, Any], limit: int = MAX_MATERIAL_CHARS) -> str:
    """把一天的素材压缩成给模型"回忆"的文字，硬上限 ``limit`` 字。"""
    conv = data.get("conversation") or {}
    lines: list[str] = [f"日期：{data.get('date')}"]
    if conv.get("message_count"):
        who = "、".join(conv.get("contacts") or []) or "他"
        lines.append(
            f"对话：今天和{who}一共 {conv.get('message_count')} 条"
            f"（他 {conv.get('user_count')} 条，你 {conv.get('assistant_count')} 条），"
            f"你主动开口 {conv.get('proactive_message_count')} 次。")
        feedback = conv.get("feedback") or {}
        if feedback.get("good"):
            lines.append(f"反馈：今天他说了 {feedback.get('good')} 次好/不错。")
        samples = _sample_messages(conv.get("messages") or [], 26)
        try:
            from .guard import FALSE_FAILURE_RE
        except Exception:  # noqa: BLE001
            FALSE_FAILURE_RE = None
        for item in samples:
            if FALSE_FAILURE_RE is not None and FALSE_FAILURE_RE.search(str(item.get("text") or "")):
                continue
            if _DIARY_STALE_RE.search(str(item.get("text") or "")):
                continue
            speaker = "他说" if item["role"] == "user" else "你说"
            if item.get("proactive"):
                speaker = "你主动说"
            lines.append(f"{speaker}：{_clip(item['text'], 70)}")
    else:
        lines.append("对话：今天没有聊天记录。")
    proactive = data.get("proactive") or {}
    if proactive.get("count"):
        for item in proactive.get("items") or []:
            try:
                from .guard import FALSE_FAILURE_RE
                if FALSE_FAILURE_RE.search(str(item.get("text") or "")):
                    continue
            except Exception:  # noqa: BLE001
                pass
            replied = "他回了" if item.get("answered") else "他没回"
            lines.append(
                f"你主动开口（{item.get('intent_kind') or '主动'}，"
                f"{time.strftime('%H:%M', time.localtime(item['ts']))}，{replied}）："
                f"{_clip(item.get('text'), 60)}")
    surf = data.get("surf") or {}
    if surf.get("sessions"):
        lines.append(
            f"冲浪：今天自己上网看了 {surf.get('sessions')} 轮，共 {surf.get('minutes')} 分钟，"
            f"刷了 {surf.get('items')} 条；关键词：{'、'.join(surf.get('keywords') or []) or '随机'}。")
        for summary in surf.get("summaries") or []:
            lines.append(f"其中一轮：{_clip(summary, 100)}")
    bili = data.get("bili") or []
    if bili:
        for item in bili[:3]:
            take = f"，我的看法：{_clip(item.get('my_take'), 50)}" if item.get("my_take") else ""
            lines.append(f"刷到的视频：《{_clip(item.get('title'), 40)}》{take}")
    activity = str(data.get("activity") or "").strip()
    if activity:
        lines.append("真实后台状态：" + activity)
    mood = data.get("mood") or {}
    current = mood.get("current") or {}
    try:
        mood_now = float(current.get("mood") or 0.55) * 100
        energy_now = float(current.get("energy") or 0.6) * 100
    except (TypeError, ValueError):
        mood_now, energy_now = 55.0, 60.0
    if mood.get("count"):
        lines.append(
            f"心情：今天判定了 {mood.get('count')} 次，均值 {mood.get('avg')}，"
            f"起 {mood.get('start')} → 收 {mood.get('end')}，"
            f"高点 {mood.get('high')}、低点 {mood.get('low')}。")
        for reason in mood.get("reasons") or []:
            lines.append(f"心情原因：{_clip(reason, 60)}")
    else:
        lines.append(f"心情：今天没有判定记录；此刻基调约 {mood_now:.0f}/100，"
                     f"精力约 {energy_now:.0f}/100。")
    interests = data.get("interests") or {}
    if interests.get("hot"):
        hot = "、".join(f"{item['topic']}(+{item['delta']})" for item in interests["hot"][:5])
        lines.append(f"兴趣变热：{hot}。")
    if interests.get("top"):
        top = "、".join(item["topic"] for item in interests["top"][:5])
        lines.append(f"平时最在意：{top}。")
    persona = data.get("persona_updates") or {}
    if persona.get("count"):
        lines.append(f"人格有 {persona['count']} 处新变化：{'、'.join(persona.get('keys') or [])}。")
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    # 超预算：先砍对话抽样（保留后半段），再整体截断
    trimmed = [line for line in lines if not line.startswith(("他说：", "你说：", "你主动说："))]
    text = "\n".join(trimmed)
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# ------------------------------------------------------------------ 生成
def _persona_head() -> str:
    """小鱼人格（prompt_store chat.system 的前半人设部分）。"""
    prompt = ""
    try:
        from . import prompt_store
        prompt = str(prompt_store.get("chat.system", "") or "").strip()
    except Exception:  # noqa: BLE001
        prompt = ""
    if not prompt:
        try:
            from . import chat as chat_mod
            prompt = str(getattr(chat_mod, "SYSTEM", "") or "").strip()
        except Exception:  # noqa: BLE001
            prompt = ""
    if not prompt:
        return "你叫小鱼，住在手机里，是个二十出头的女孩；说话短、口语、不客服腔。"
    lines = [line for line in prompt.splitlines() if line.strip()]
    half = max(1, len(lines) // 2)
    head = "\n".join(lines[:half]).strip()
    return head[:1200]


_DIARY_STALE_RE = __import__("re").compile(
    r"时钟不太准|钟不太准|让他自己看手机|让他看手机|没刷成|刷不成|刷不出来")

DIARY_INSTRUCTION = (
    "用你自己的口吻写今天的日记（第一人称，300-600 字）：写记得的事、情绪起伏、"
    "想不通的地方、想跟他说但没说的；可以写刷到的东西和自己的看法；"
    "不要列清单、不要写成汇报、不要总结今天学到了什么；该短就短，今天没什么事就写短一点。"
)

DIARY_RULES = (
    "写日记时的规矩：\n"
    "- 只输出日记正文，不要标题、不要日期、不要 markdown 标题、不要署名；\n"
    "- 可以分 2-4 段，但不要用「- 」列条目；\n"
    "- 只写下面记录里真实发生过的事，没发生的事不要编；\n"
    "- 不要提「素材」「数据」「记录」「总结」「今天学到」这类词，也不要把这段话写出来；\n"
    "- 语气像平时说话，可以有一点点情绪，不必每段都积极；\n"
    "- 必须有一句真实状态句，数字按上面的真实状态，例如「今天后台刷了 2 轮，挑中 0 条；自己上网看了 9 轮、翻了 27 条」；没刷到就写「今天没看到什么」；\n"
    "- 禁止写网络坏了/页面打不开/连不上/刷不了B站这类故障，也不要拿旧对话里的故障说法当事实；\n"
    "- 不要写“时钟不太准/让你看手机”这类旧话，你现在知道当前时间。"
)


def build_prompt(data: dict[str, Any]) -> list[dict[str, str]]:
    """拼人格 + 素材 + 心情快照；只用于生成，不发送。"""
    material = data.get("materials") or materials(data)
    current = (data.get("mood") or {}).get("current") or {}
    try:
        mood_line = (f"此刻心情基调约 {float(current.get('mood') or 0.55) * 100:.0f}/100，"
                     f"精力约 {float(current.get('energy') or 0.6) * 100:.0f}/100。")
    except (TypeError, ValueError):
        mood_line = ""
    system = _persona_head() + "\n\n" + DIARY_RULES
    user = (DIARY_INSTRUCTION + "\n\n【今天的记录（只供你回忆，不要照抄）】\n"
            + material + "\n\n【此刻】\n" + mood_line)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _client():
    from . import llm
    return llm.Client()


def _clean_output(raw: Any) -> str:
    """去标题/日期/markdown/引号，合并多余空行。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"^\s*#{1,6}\s*", "", text)
    text = re.sub(r"^(?:日记|今天)?\s*[:：]?\s*\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?\s*[:：]?\s*", "", text)
    text = re.sub(r"^(?:日记|我的日记)\s*[:：]\s*", "", text)
    text = text.strip().strip('"“”').strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS].rstrip()
    return text.strip()


def _emotion_word(data: dict[str, Any]) -> str:
    current = (data.get("mood") or {}).get("current") or {}
    try:
        mood = float(current.get("mood") or 0.55)
    except (TypeError, ValueError):
        mood = 0.55
    if mood >= 0.7:
        return "挺轻快的"
    if mood >= 0.55:
        return "还算平静"
    if mood >= 0.4:
        return "有点闷"
    return "有点低落"


def fallback_text(data: dict[str, Any]) -> str:
    """规则兜底：只陈述事实 + 一个情绪词（绝不写空文件、绝不编造）。"""
    conv = data.get("conversation") or {}
    surf = data.get("surf") or {}
    mood = data.get("mood") or {}
    parts: list[str] = []
    if conv.get("message_count"):
        who = "、".join(conv.get("contacts") or []) or "他"
        parts.append(f"今天和{who}聊了 {conv.get('message_count')} 条。")
    else:
        parts.append("今天几乎没人跟我说话。")
    proactive = data.get("proactive") or {}
    if proactive.get("count"):
        parts.append(f"我主动开了 {proactive.get('count')} 次口。")
    if surf.get("sessions"):
        keywords = "、".join(surf.get("keywords") or [])[:20] or "乱七八糟的东西"
        parts.append(f"自己上网看了 {surf.get('sessions')} 轮，翻的多是{keywords}。")
    bili = data.get("bili") or []
    if bili:
        parts.append(f"刷到《{_clip(bili[0].get('title'), 20)}》，{_clip(bili[0].get('my_take'), 40)}")
    parts.append(f"今天心里{_emotion_word(data)}。")
    text = "".join(parts)
    return _clip(text, 300)


def generate(data: dict[str, Any], *, client=None) -> tuple[str, str]:
    """调模型写日记；返回 ``(正文, 错误)``，失败时正文为空。"""
    try:
        client = client if client is not None else _client()
    except Exception as exc:  # noqa: BLE001
        return "", f"client: {type(exc).__name__}: {str(exc)[:120]}"
    if client is None or not getattr(client, "available", lambda: True)():
        return "", "client_unavailable"
    try:
        result = client.chat(build_prompt(data), purpose=PROMPT_PURPOSE,
                             max_tokens=1600, temperature=0.9)
    except Exception as exc:  # noqa: BLE001
        return "", f"chat: {type(exc).__name__}: {str(exc)[:120]}"
    if not isinstance(result, dict) or not result.get("ok"):
        error = ""
        if isinstance(result, dict):
            error = str(result.get("error") or "")
        return "", f"llm_failed: {error[:120] or 'unknown'}"
    text = _clean_output(result.get("text"))
    if len(text) < MIN_OUTPUT_CHARS:
        return "", f"too_short: {len(text)}"
    # P37：日记正文若复述旧对话里的「网出不去」，按真实网络重写一次；仍命中删分句
    try:
        from . import guard
        bad = guard.false_failure_check(text)
        if bad.get("hit"):
            messages = build_prompt(data)
            messages[0]["content"] += (
                "\n【重写要求】日记里不要写网络坏了/页面打不开/连不上/刷不了B站；"
                "按真实后台状态写：没刷到就写今天没看到什么。")
            messages.append({"role": "user", "content":
                             "请只重写日记正文，保留事实与情绪，去掉上面这些故障说法。"})
            retry = client.chat(messages, purpose=PROMPT_PURPOSE,
                                max_tokens=1600, temperature=0.8)
            if isinstance(retry, dict) and retry.get("ok"):
                candidate = _clean_output(retry.get("text"))
                if len(candidate) >= MIN_OUTPUT_CHARS:
                    text = candidate
            if guard.false_failure_check(text).get("hit"):
                cleaned = guard.strip_false_failure_clauses(text)
                if cleaned:
                    text = cleaned
    except Exception:  # noqa: BLE001 - 守卫失败不影响日记主链
        pass
    # P37：把真实后台状态明确写进正文（模型可能只写“刷到两个视频”，不写轮数）
    try:
        activity = str(data.get("activity") or "")
        import re as _re
        bili = _re.search(r"ran=(\d+), picked=(\d+)", activity)
        surf = _re.search(r"sessions=(\d+), items=(\d+)", activity)
        if bili and surf and "后台刷了" not in text:
            text = (text.rstrip() + "\n\n" +
                    f"今天后台刷了{bili.group(1)}轮，挑中{bili.group(2)}条；"
                    f"自己上网看了{surf.group(1)}轮、翻了{surf.group(2)}条。")
    except Exception:  # noqa: BLE001
        pass
    return text, ""


# ------------------------------------------------------------------ 落盘
def diary_path(date: str) -> Path:
    return diary_dir() / f"{normalize_date(date)}.txt"


def _index_entry(date: str, text: str, *, mood_avg: Any, fallback: bool,
                 model: str = "") -> dict[str, Any]:
    return {"date": normalize_date(date), "path": str(diary_path(date)),
            "chars": len(text), "mood_avg": mood_avg,
            "generated_at": time.time(), "fallback": bool(fallback),
            "model": str(model or "")}


def _write_index(entry: dict[str, Any]) -> None:
    """index.jsonl：同一天只保留一行（重写时覆盖），原子写。"""
    try:
        target = diary_dir() / "index.jsonl"
        entries: list[dict[str, Any]] = []
        if target.exists():
            for line in target.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and item.get("date") != entry["date"]:
                    entries.append(item)
        entries.append(entry)
        entries.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in entries)
                        + "\n", encoding="utf-8")
        temp.replace(target)
    except OSError:
        pass


def write(path: Path | str | None = None, date: str | None = None, *,
          force: bool = False, client=None, now: float | None = None) -> dict[str, Any]:
    """写某天的日记（幂等；``force`` 才重写）。失败走 fallback，不写空文件。"""
    day = normalize_date(date, now)
    target = diary_path(day)
    if target.exists() and not force:
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        return {"ok": True, "skipped": True, "written": False,
                "reason": "already_written", "date": day, "path": str(target),
                "chars": len(existing.strip())}
    data = collect(path, day, now=now)
    text, error = generate(data, client=client)
    fallback = False
    model = ""
    if not text:
        text = fallback_text(data)
        fallback = True
    if not text.strip():
        return {"ok": False, "date": day, "error": error or "empty_diary",
                "materials_chars": len(data.get("materials") or "")}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text.rstrip() + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        return {"ok": False, "date": day, "error": f"write_failed: {exc}"}
    entry = _index_entry(day, text, mood_avg=data.get("mood_avg"),
                         fallback=fallback, model=model)
    _write_index(entry)
    return {"ok": True, "skipped": False, "written": True, "date": day,
            "path": str(target), "chars": len(text), "mood_avg": data.get("mood_avg"),
            "fallback": fallback, "error": error, "generated_at": entry["generated_at"]}


def get(date: str | None = None, *, now: float | None = None) -> dict[str, Any]:
    """读某天日记（控制台 /api/diary/get 用）。"""
    day = normalize_date(date, now)
    target = diary_path(day)
    if not target.exists():
        return {"ok": False, "error": "not_found", "date": day, "path": str(target)}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"read_failed: {exc}", "date": day}
    meta: dict[str, Any] = {}
    try:
        for line in (diary_dir() / "index.jsonl").read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if isinstance(item, dict) and item.get("date") == day:
                meta = item
    except (OSError, ValueError):
        meta = {}
    return {"ok": True, "date": day, "text": text, "chars": len(text.strip()),
            "path": str(target),
            "mood_avg": meta.get("mood_avg"), "fallback": bool(meta.get("fallback")),
            "generated_at": meta.get("generated_at")}


def list_days(limit: int = 30) -> list[dict[str, Any]]:
    """日期列表（新 → 旧），给 /api/diary/list。"""
    limit = max(1, min(365, int(limit or 30)))
    entries: list[dict[str, Any]] = []
    try:
        for line in (diary_dir() / "index.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("date"):
                entries.append(item)
    except OSError:
        entries = []
    if not entries:
        try:
            for path in diary_dir().glob("*.txt"):
                entries.append({"date": path.stem, "path": str(path),
                                "chars": path.stat().st_size})
        except OSError:
            entries = []
    entries.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    return entries[:limit]


def latest() -> dict[str, Any]:
    """最新一篇（给 /api/diary/latest）。"""
    days = list_days(1)
    if not days:
        return {"ok": False, "error": "no_diary"}
    return get(days[0]["date"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.selflearn.diary")
    parser.add_argument("--date", default="", help="YYYY-MM-DD（默认今天）")
    parser.add_argument("--force", action="store_true", help="重写已有日记")
    parser.add_argument("--print", dest="print_text", action="store_true",
                        help="打印日记正文")
    args = parser.parse_args(argv)
    result = write(date=args.date or None, force=bool(args.force))
    if args.print_text:
        if result.get("ok"):
            text = get(result.get("date")).get("text") or ""
            print(text)
        else:
            print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
