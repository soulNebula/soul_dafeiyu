# -*- coding: utf-8 -*-
"""B站关键词解析（V1 PR9）：用户填的优先；留空让 LLM 按 ``extract`` 路由自选。

- 配置了关键词 → 直接返回（清洗空白/去重/限长）；
- 没配置 → 拿推荐流标题让 LLM 提取（`agi.llm.chat("keyword_extract")`，
  bot 生效时按 bot 的 extract 通道走）；
- LLM 不可用 → 退化为标题词频（规则降级，不编造关键词）。
"""
from __future__ import annotations

import json
import re
from collections import Counter

MAX_KEYWORDS = 5
MAX_KEYWORD_CHARS = 16
_STOPWORDS = frozenset({
    "视频", "一个", "这个", "那个", "什么", "怎么", "为什么", "我们", "你们",
    "他们", "自己", "真的", "可以", "没有", "就是", "还是", "但是", "然后",
    "bilibili", "B站", "哔哩哔哩", "教程", "合集", "最新", "全集", "高清",
})
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,6}")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{2,}")


def _clean(values) -> list[str]:
    result: list[str] = []
    for value in values or []:
        text = re.sub(r"\s+", " ", str(value or "")).strip().strip("#，,、")
        if not text or len(text) > MAX_KEYWORD_CHARS or text in _STOPWORDS:
            continue
        if text not in result:
            result.append(text)
        if len(result) >= MAX_KEYWORDS:
            break
    return result


def _rule_keywords(titles: list[str], limit: int = 3) -> list[str]:
    """标题词频：中文取 2–4 字 n-gram、英文取整词；短词优先，避免整句被当关键词。"""
    counter: Counter[str] = Counter()
    first_seen: dict[str, int] = {}
    order = 0
    for title in titles:
        text = str(title or "")
        words: list[str] = []
        for run in _CJK_RE.findall(text):
            for size in (2, 3, 4):
                words.extend(run[i:i + size] for i in range(len(run) - size + 1))
        words.extend(_WORD_RE.findall(text))
        for word in words:
            if word in _STOPWORDS:
                continue
            counter[word] += 1
            if word not in first_seen:
                first_seen[word] = order
                order += 1
    ranked = sorted(counter, key=lambda w: (-counter[w], len(w), first_seen[w]))
    return ranked[:max(1, int(limit))]


def _parse_llm_keywords(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except ValueError:
        # 兼容「关键词：A、B、C」这类非严格 JSON 输出
        payload = re.split(r"[,，、;；\n]+", re.sub(r"^[^:：]*[:：]", "", raw))
    if isinstance(payload, dict):
        payload = payload.get("keywords") or payload.get("items") or []
    if isinstance(payload, str):
        payload = re.split(r"[,，、;；\n]+", payload)
    if not isinstance(payload, list):
        return []
    return _clean(payload)


def resolve_keywords(configured=None, titles=None, *, limit: int = 3,
                     llm_chat=None) -> dict:
    """返回 ``{keywords, source}``；source ∈ config/llm/rule/empty。"""
    cleaned = _clean(configured or [])
    if cleaned:
        return {"keywords": cleaned, "source": "config"}
    rows = [str(title or "") for title in (titles or []) if str(title or "").strip()]
    if not rows:
        return {"keywords": [], "source": "empty"}
    chat = llm_chat
    if chat is None:
        try:
            from agi.llm import chat as chat
        except Exception:  # noqa: BLE001 - 路由层不可用就规则降级
            chat = None
    if chat is not None:
        try:
            prompt = ("下面是一批 B站视频标题。请提取 1–3 个**共同兴趣关键词**"
                      "（名词短语，不要泛词如「视频」「合集」）。"
                      '只输出 JSON：{"keywords": ["..."]}。\n\n标题：\n'
                      + "\n".join(f"- {title}" for title in rows[:30]))
            result = chat("keyword_extract",
                          [{"role": "user", "content": prompt}],
                          max_tokens=120, temperature=0.2, json_mode=True)
            if isinstance(result, dict) and result.get("ok"):
                parsed = _parse_llm_keywords(result.get("text") or "")
                if parsed:
                    return {"keywords": parsed, "source": "llm"}
        except Exception:  # noqa: BLE001 - LLM 失败不阻塞刷B站
            pass
    return {"keywords": _rule_keywords(rows, limit=limit), "source": "rule"}