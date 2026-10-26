# -*- coding: utf-8 -*-
"""P12-B：兴趣画像（三来源加权 + 自我提出 + 搜索关键词）。

存储：persona 库 interests / interest_events。
兴趣只存 topic，不存原句；证据只存引用/来源摘要。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from agi.llm import chat as llm_chat
from agi.selflearn import db
from agi.selflearn.config import DB_PATH

SOURCE_WEIGHTS = {"chat": 0.5, "video": 0.3, "self": 0.2, "recommend": 0.15}
DELTA_CAP = 0.15
MAX_NEW_PER_OBSERVE = 5
HALF_LIFE_DAYS = 7.0
FLOOR = 0.1
CACHE_KEY = "interests_search_keywords"
STOPWORDS = frozenset(
    "的 了 是 在 和 也 就 都 而 及 与 或者 一个 一些 这个 那个 什么 怎么 为什么 "
    "可以 可能 觉得 感觉 最近 现在 今天 明天 昨天 一直 有点 比较 还是 但是 因为 "
    "所以 如果 然后 其实 自己 我们 你们 他们 我 你 他 她 它 这 那 吧 呢 啊 吗 哦 嗯".split())
GENERIC_TOPICS = frozenset({
    "网络连接", "用户需求", "代码分析", "记忆库", "问号", "系统", "功能",
    "问题", "内容", "信息", "消息", "数据", "记忆", "需求", "连接",
    "你的", "些什么", "些什", "你的记忆",
})
SYNONYMS = {
    "人工智能": "ai", "artificialintelligence": "ai", "artificial intelligence": "ai",
    "ai": "ai", "大模型": "大模型", "llm": "大模型", "语言模型": "大模型",
    "机器学习": "机器学习", "machinelearning": "机器学习",
    "编程": "编程", "写代码": "编程", "coding": "编程", "代码": "编程",
    "b站": "b站", "哔哩哔哩": "b站", "bilibili": "b站",
    "手冲": "咖啡", "手冲咖啡": "咖啡", "coffee": "咖啡",
    "游戏": "游戏", "game": "游戏", "游戏开发": "游戏开发",
    "python": "python", "java": "java", "rust": "rust",
}
_ASCII_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{1,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")


def _pytest_mode() -> bool:
    return "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ


def _use_llm_default() -> bool:
    env = os.environ.get("INTERESTS_USE_LLM")
    if env is not None:
        return str(env).strip().lower() in ("1", "true", "yes", "on")
    return not _pytest_mode()


def _clean(text: Any) -> str:
    value = str(text or "").strip().lower()
    value = re.sub(r"[\s，,。.！!？?；;、…「」“”\"'（）()【】\[\]]+", "", value)
    value = value.strip("#*-_")
    return value


def normalize(topic: Any) -> str:
    """大小写/标点/同义词归一；不合法返回空串。"""
    value = _clean(topic)
    if not value:
        return ""
    if value in SYNONYMS:
        return SYNONYMS[value]
    # 去掉常见后缀，保留核心词
    for suffix in ("相关", "内容", "话题", "视频", "知识"):
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            value = value[:-len(suffix)]
    value = SYNONYMS.get(value, value)
    if len(value) < 2 or len(value) > 30:
        return ""
    if value in STOPWORDS or value in GENERIC_TOPICS:
        return ""
    return value


def _rule_extract(texts: list[str]) -> list[str]:
    text = " ".join(str(item or "") for item in texts)
    counts: dict[str, int] = {}
    for token in _ASCII_RE.findall(text):
        key = normalize(token)
        if key:
            counts[key] = counts.get(key, 0) + 1
    for run in _CJK_RE.findall(text):
        for size in (2, 3, 4):
            for index in range(0, max(0, len(run) - size + 1)):
                token = normalize(run[index:index + size])
                if token and token not in STOPWORDS:
                    counts[token] = counts.get(token, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [token for token, _count in ordered[:10]]


def extract_topics(texts: list[str], *, use_llm: bool | None = None) -> list[str]:
    """优先本地 keyword_extract；失败/坏 JSON 退回规则。"""
    if use_llm is None:
        use_llm = _use_llm_default()
    if use_llm:
        try:
            payload = "\n".join(str(item or "") for item in texts)[:4000]
            result = llm_chat("keyword_extract", [
                {"role": "system", "content": "只输出 JSON：{\"topics\":[\"...\"]}。"},
                {"role": "user", "content": "提取 3-8 个兴趣话题关键词：\n" + payload},
            ], max_tokens=200)
            if result.get("ok"):
                raw = str(result.get("text") or "")
                start, end = raw.find("{"), raw.rfind("}")
                data = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
                topics = [normalize(item) for item in data.get("topics") or []]
                topics = [item for item in topics if item]
                if topics:
                    return topics[:10]
        except Exception:  # noqa: BLE001
            pass
    return _rule_extract(texts)


def _event_id(topic: str, at: float, delta: float) -> str:
    return "IE-" + hashlib.sha1(f"{topic}|{at}|{delta}".encode()).hexdigest()[:16]


def _interest_id(topic: str) -> str:
    return "IN-" + hashlib.sha1(topic.encode("utf-8", "replace")).hexdigest()[:16]


def observe(texts: list[str] | str, *, source: str = "chat", ref: Any = None,
            path: Path | str | None = None, now: float | None = None,
            use_llm: bool | None = None, _topics: list[str] | None = None) -> dict:
    """从文本抽 topic 并累加权重；同 topic 单次 delta≤0.15，最多新增 5 个。"""
    if isinstance(texts, str):
        texts = [texts]
    source = str(source or "chat")
    if source not in SOURCE_WEIGHTS:
        source = "chat"
    weight = SOURCE_WEIGHTS[source]
    delta = min(DELTA_CAP, weight * 0.3)
    now = time.time() if now is None else float(now)
    topics = (_topics if _topics is not None
              else extract_topics(list(texts or []), use_llm=use_llm))
    seen: set[str] = set()
    added = updated = 0
    applied: list[dict] = []
    con = db.connect(path or DB_PATH)
    try:
        for raw in topics:
            topic = normalize(raw)
            if not topic or topic in seen:
                continue
            seen.add(topic)
            row = con.execute("SELECT * FROM interests WHERE topic=?", (topic,)).fetchone()
            ref_json = json.dumps(ref if ref is not None else {}, ensure_ascii=False)[:2000]
            if row is not None:
                new_weight = min(1.0, float(row["weight"] or 0) + delta)
                evidence = []
                try:
                    evidence = json.loads(row["evidence_json"] or "[]")
                except (TypeError, ValueError):
                    evidence = []
                if ref is not None:
                    evidence = (evidence + [ref])[-20:]
                con.execute(
                    "UPDATE interests SET weight=?,source=?,evidence_json=?,hits=hits+1,"
                    "last_seen_at=?,updated_at=? WHERE topic=?",
                    (new_weight, source, json.dumps(evidence, ensure_ascii=False),
                     now, now, topic))
                con.execute(
                    "INSERT INTO interest_events(id,topic,delta,reason,source,ref_json,at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (_event_id(topic, now, delta), topic, delta, "observe_update",
                     source, ref_json, now))
                updated += 1
                applied.append({"topic": topic, "delta": delta, "new": False})
            elif added < MAX_NEW_PER_OBSERVE:
                # 新 topic：初始权重取本次 delta（来源越强，起点越高）
                con.execute(
                    "INSERT INTO interests(id,topic,weight,source,evidence_json,hits,"
                    "last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
                    (_interest_id(topic), topic, delta, source,
                     json.dumps([ref] if ref is not None else [], ensure_ascii=False)[:2000],
                     now, now, now))
                con.execute(
                    "INSERT INTO interest_events(id,topic,delta,reason,source,ref_json,at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (_event_id(topic, now, delta), topic, delta, "observe_new",
                     source, ref_json, now))
                added += 1
                applied.append({"topic": topic, "delta": delta, "new": True})
        con.commit()
    finally:
        con.close()
    return {"ok": True, "source": source, "delta": delta,
            "topics": [item["topic"] for item in applied],
            "new": added, "updated": updated}


def decay(path: Path | str | None = None, now: float | None = None) -> dict:
    """半衰期 7 天，向 0.1 回落；小步/大步数学等价；hits 不清零。"""
    now = time.time() if now is None else float(now)
    con = db.connect(path or DB_PATH)
    try:
        rows = con.execute("SELECT topic,weight,updated_at FROM interests").fetchall()
        changed = 0
        for row in rows:
            age_days = max(0.0, (now - float(row["updated_at"] or now)) / 86400.0)
            factor = 0.5 ** (age_days / HALF_LIFE_DAYS)
            old = float(row["weight"] or FLOOR)
            new = FLOOR + max(0.0, old - FLOOR) * factor
            if abs(new - old) > 1e-9:
                con.execute("UPDATE interests SET weight=?,updated_at=? WHERE topic=?",
                            (new, now, row["topic"]))
                con.execute(
                    "INSERT INTO interest_events(id,topic,delta,reason,source,ref_json,at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (_event_id(row["topic"], now, new - old), row["topic"], new - old,
                     "decay", "system", "{}", now))
                changed += 1
        con.commit()
        return {"ok": True, "decayed": changed, "now": now}
    finally:
        con.close()


def top_topics(limit: int = 10, *, path: Path | str | None = None) -> list[dict]:
    con = db.connect(path or DB_PATH)
    try:
        rows = con.execute(
            "SELECT topic,weight,source,hits,last_seen_at,created_at,updated_at "
            "FROM interests ORDER BY weight DESC,last_seen_at DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def search_keywords(limit: int = 3, *, path: Path | str | None = None,
                    now: float | None = None) -> list[str]:
    """权重高 + 近期 + 未探索（hits 低）的 topic。"""
    now = time.time() if now is None else float(now)
    con = db.connect(path or DB_PATH)
    try:
        rows = con.execute(
            "SELECT topic,weight,hits,last_seen_at FROM interests").fetchall()
    finally:
        con.close()
    scored = []
    for row in rows:
        topic = str(row["topic"] or "")
        if not topic:
            continue
        age_days = max(0.0, (now - float(row["last_seen_at"] or now)) / 86400.0)
        recency = 0.5 ** (age_days / HALF_LIFE_DAYS)
        unexplored = 1.0 / (1.0 + max(0, int(row["hits"] or 0)) * 0.05)
        score = float(row["weight"] or 0) * recency * unexplored
        scored.append((score, topic))
    scored.sort(key=lambda item: (-item[0], item[1]))
    out: list[str] = []
    for _score, topic in scored:
        if topic not in out:
            out.append(topic)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _chat_topics(purpose: str, prompt: str) -> list[str]:
    try:
        result = llm_chat(purpose, [
            {"role": "system", "content": "只输出 JSON，不要解释。"},
            {"role": "user", "content": prompt},
        ], max_tokens=250)
        if not result.get("ok"):
            return []
        raw = str(result.get("text") or "")
        start, end = raw.find("{"), raw.rfind("}")
        data = json.loads(raw[start:end + 1]) if start >= 0 and end > start else {}
        values = data.get("topics") or data.get("directions") or []
        return [normalize(item) for item in values if normalize(item)]
    except Exception:  # noqa: BLE001
        return []


def propose_new(limit: int = 2, *, path: Path | str | None = None,
                now: float | None = None) -> list[str]:
    """用 persona_reflect(API) 基于现有画像提出 1–2 个新方向。"""
    existing = [item["topic"] for item in top_topics(8, path=path)]
    prompt = ("我现在喜欢/关注：" + ("、".join(existing) or "还没有明确兴趣") +
              "。基于这些，提出 1-2 个我可能会好奇、但还没探索的新方向关键词；"
              "每个不超过 8 个字。")
    topics = _chat_topics("persona_reflect", prompt)
    topics = [item for item in topics if item not in existing][:max(1, int(limit))]
    if topics:
        observe([], source="self", ref={"mode": "propose"}, path=path, now=now,
                use_llm=False, _topics=topics)
    return topics


def rebuild_from_history(days: int = 30, *, path: Path | str | None = None,
                         now: float | None = None) -> dict:
    """从 bili_impressions.topics + 近 N 天记忆/聊天重建（幂等：先清后建）。"""
    now = time.time() if now is None else float(now)
    since = now - max(1, int(days)) * 86400
    con = db.connect(path or DB_PATH)
    try:
        topics: dict[str, dict] = {}
        try:
            rows = con.execute(
                "SELECT bvid,topics_json FROM bili_impressions WHERE created_at>=?",
                (since,)).fetchall()
            for row in rows:
                try:
                    values = json.loads(row["topics_json"] or "[]")
                except (TypeError, ValueError):
                    values = []
                for value in values:
                    topic = normalize(value)
                    if topic:
                        entry = topics.setdefault(topic, {"sources": set(), "refs": []})
                        entry["sources"].add("video")
                        entry["refs"].append({"bvid": row["bvid"]})
        except Exception:  # noqa: BLE001
            pass
        chat_texts: list[str] = []
        try:
            rows = con.execute(
                "SELECT content FROM user_memories WHERE status='active' AND created_at>=?",
                (since,)).fetchall()
            chat_texts.extend(str(row[0] or "") for row in rows)
        except Exception:  # noqa: BLE001
            pass
        try:
            rows = con.execute(
                "SELECT text FROM messages WHERE role='user' AND ts>=?",
                (since,)).fetchall()
            chat_texts.extend(str(row[0] or "") for row in rows)
        except Exception:  # noqa: BLE001
            pass
        if chat_texts:
            # 生产用本地 keyword_extract 抽词；pytest 下自动退规则，保证离线可测。
            for value in extract_topics(chat_texts):
                topic = normalize(value)
                if topic:
                    entry = topics.setdefault(topic, {"sources": set(), "refs": []})
                    entry["sources"].add("chat")
                    entry["refs"].append({"from": "history"})
        con.execute("DELETE FROM interest_events")
        con.execute("DELETE FROM interests")
        inserted = 0
        for topic, entry in sorted(topics.items(),
                                   key=lambda item: (-len(item[1]["refs"]), item[0])):
            sources = entry["sources"] or {"chat"}
            source = "video" if "video" in sources else "chat"
            weight = SOURCE_WEIGHTS.get(source, 0.3) * min(2.0, 1 + 0.15 * len(entry["refs"]))
            weight = max(FLOOR, min(1.0, weight))
            con.execute(
                "INSERT INTO interests(id,topic,weight,source,evidence_json,hits,"
                "last_seen_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (_interest_id(topic), topic, weight, source,
                 json.dumps(entry["refs"][:20], ensure_ascii=False), len(entry["refs"]),
                 now, now, now))
            con.execute(
                "INSERT INTO interest_events(id,topic,delta,reason,source,ref_json,at) "
                "VALUES(?,?,?,?,?,?,?)",
                (_event_id(topic, now, weight), topic, weight, "rebuild",
                 source, "{}", now))
            inserted += 1
        con.commit()
        return {"ok": True, "topics": inserted, "days": int(days)}
    finally:
        con.close()


def next_search_keywords(limit: int = 3, *, path: Path | str | None = None,
                         now: float | None = None) -> list[str]:
    """search_keywords + propose_new，24h 缓存（state 表）。"""
    now = time.time() if now is None else float(now)
    con = db.connect(path or DB_PATH)
    try:
        try:
            row = con.execute("SELECT value FROM state WHERE key=?", (CACHE_KEY,)).fetchone()
            if row:
                cached = json.loads(str(row[0] or "{}"))
                if now - float(cached.get("at") or 0) < 86400:
                    return list(cached.get("keywords") or [])[:limit]
        except Exception:  # noqa: BLE001
            pass
        keywords = search_keywords(limit, path=path, now=now)
        proposed = propose_new(2, path=path, now=now)
        for item in proposed:
            if item not in keywords:
                keywords.append(item)
        keywords = keywords[:max(1, int(limit))]
        try:
            con.execute("INSERT OR REPLACE INTO state(key,value,updated_at) VALUES(?,?,?)",
                        (CACHE_KEY, json.dumps({"at": now, "keywords": keywords},
                                               ensure_ascii=False), now))
            con.commit()
        except Exception:  # noqa: BLE001
            pass
        return keywords
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.selflearn.interests")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--propose", action="store_true")
    parser.add_argument("--decay", action="store_true")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--path", default="")
    args = parser.parse_args(argv)
    path = args.path or None
    if args.show:
        print(json.dumps({"topics": top_topics(args.limit, path=path),
                          "keywords": search_keywords(3, path=path)},
                         ensure_ascii=False, indent=2))
        return 0
    if args.rebuild:
        print(json.dumps(rebuild_from_history(args.days, path=path),
                         ensure_ascii=False, indent=2))
        return 0
    if args.propose:
        print(json.dumps({"topics": propose_new(2, path=path)}, ensure_ascii=False))
        return 0
    if args.decay:
        print(json.dumps(decay(path=path), ensure_ascii=False))
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
