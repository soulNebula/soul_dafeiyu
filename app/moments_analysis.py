# -*- coding: utf-8 -*-
"""Moments-B：朋友圈 LLM 分析（统一 LLM 路由 + 严格 JSON + 缓存/历史）。

依赖同事的 ``app/momentsstudio.py``（只 import ``list_moments/detail``；未就绪时返回
明确错误，不编造）。本模块不改 main.py/web，路由由 Lead 接线：

- ``POST /api/wb/moments/analyze {limit?,tids?,question?}`` → :func:`analyze_and_cache`
- ``GET  /api/wb/moments/analyses?limit=`` → :func:`list_analyses`
- ``GET  /api/wb/moments/analysis/{name}`` → :func:`get_analysis`

LLM 调用走 SoulAgent 的统一路由 ``agi.llm.router.Router().chat("chat", messages,
temperature=0.3, max_tokens=900)``；路由自带 api/local/rule fallback，失败时本模块
显式返回 error（不编造分析）。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from . import audit, paths

#: prompt 总字符上限（system + question + 朋友圈正文）
PROMPT_LIMIT = 6000
#: 分析结果缓存目录（工作台自身数据目录，gitignore）
ANALYSIS_DIR = paths.DATA_DIR / "moments"
#: 默认取多少条朋友圈
DEFAULT_LIMIT = 30
#: LLM 调用参数
TEMPERATURE = 0.3
MAX_TOKENS = 900
#: 严格 JSON 的字段（顺序固定）
ANALYSIS_FIELDS = ("summary", "topics", "mood", "social_signals",
                   "notable", "suggestions")
_LIST_FIELDS = ("topics", "social_signals", "notable", "suggestions")

SYSTEM_PROMPT = (
    "你是朋友圈内容分析助手。只根据用户给出的朋友圈条目做归纳，不要编造条目里没有的"
    "事实、人物或数字。只输出一个 JSON 对象，不要 markdown、不要解释。字段："
    '{"summary": "≤120字总体概况", '
    '"topics": ["3-6个主题词"], '
    '"mood": "整体情绪，≤30字", '
    '"social_signals": ["≤5条社交信号，如谁常和谁互动/生活方式/关注点"], '
    '"notable": ["≤5条值得注意的具体内容"], '
    '"suggestions": ["≤5条可执行的互动建议"]}'
)


# ------------------------------------------------------------------ 读朋友圈
def _moments_module():
    """惰性 import 同事的 momentsstudio；未就绪返回 (None, error)。"""
    try:
        from . import momentsstudio  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return None, f"moments 后端未就绪：{type(exc).__name__}: {exc}"
    if not hasattr(momentsstudio, "list_moments"):
        return None, "moments 后端未就绪：momentsstudio 缺少 list_moments"
    return momentsstudio, ""


def _extract_items(result: Any) -> list[dict]:
    """兼容 momentsstudio 返回 list / {items} / {moments} / {ok,data:{items}}。"""
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if isinstance(result, dict):
        for key in ("items", "moments", "list", "data", "item"):
            value = result.get(key)
            if isinstance(value, dict):
                if value.get("content") or value.get("text") or value.get("tid"):
                    return [value]
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = _extract_items(value)
                if nested:
                    return nested
    return []


def load_moments(limit: int = DEFAULT_LIMIT, tids: Iterable[str] | None = None) -> dict:
    """从 momentsstudio 取朋友圈；返回 ``{ok,moments,error}``。"""
    module, error = _moments_module()
    if module is None:
        return {"ok": False, "moments": [], "error": error}
    try:
        tid_list = [str(tid) for tid in (tids or []) if str(tid)]
        if tid_list:
            detail = getattr(module, "detail", None)
            items: list[dict] = []
            if callable(detail):
                for tid in tid_list:
                    raw = detail(tid)
                    found = _extract_items(raw)
                    if not found and isinstance(raw, dict):
                        found = [raw] if (raw.get("content") or raw.get("text")) else []
                    items.extend(found)
            if not items and hasattr(module, "list_moments"):
                raw = module.list_moments(limit=max(1, int(limit)))
                wanted = set(tid_list)
                items = [item for item in _extract_items(raw)
                         if str(item.get("tid") or item.get("id") or "") in wanted]
            return {"ok": True, "moments": items[:max(1, int(limit))], "error": ""}
        raw = module.list_moments(limit=max(1, int(limit)))
        return {"ok": True, "moments": _extract_items(raw), "error": ""}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "moments": [],
                "error": f"moments 读取失败：{type(exc).__name__}: {exc}"}


# ------------------------------------------------------------------ prompt
def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _first(moment: dict, keys: Iterable[str], default: Any = "") -> Any:
    for key in keys:
        if key in moment and moment.get(key) not in (None, ""):
            return moment.get(key)
    return default


def _fmt_time(value: Any) -> str:
    try:
        number = float(value)
        if number > 10_000_000_000:       # 毫秒
            number /= 1000.0
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(number))
    except (TypeError, ValueError, OSError):
        return _text(value)[:20]


def _format_moment(moment: dict) -> str:
    author = _text(_first(moment, ("nickname", "author", "name", "user", "contact")))
    content = _text(_first(moment, ("content", "text", "desc", "description")))
    if not content:
        content = "（仅图片/无文字）"
    created = _first(moment, ("create_time", "timestamp", "time", "date", "created_at"))
    likes = _first(moment, ("likes", "like_count", "likeCount"), 0)
    comments = _first(moment, ("comments", "comment_count", "commentCount"), 0)
    if isinstance(likes, list):
        likes = len(likes)
    if isinstance(comments, list):
        comments = len(comments)
    images = _first(moment, ("images", "media", "pics", "image_count"), None)
    has_image = _first(moment, ("has_image", "hasImage"), None)
    if has_image is None:
        has_image = bool(images) if not isinstance(images, (int, float)) else images > 0
    if images is None:
        images = 1 if has_image else 0
    if isinstance(images, list):
        images = len(images)
    if len(content) > 400:
        content = content[:399] + "…"
    line = (f"- [{_fmt_time(created)}] {author or '某人'}：{content}"
            f"（赞 {likes}，评论 {comments}，图 {images}）")
    return line


def build_messages(moments: list[dict] | None,
                   question: str = "") -> dict:
    """把朋友圈条目压成 messages；system+question+正文总字符 ≤ PROMPT_LIMIT。

    超长单条会截断正文（保留时间/昵称/互动数），超出总量的后续条目不加入。
    """
    question = _text(question)[:500]
    header = "朋友圈条目（从新到旧）："
    entries = [_format_moment(moment) for moment in (moments or [])
               if isinstance(moment, dict)]
    budget = PROMPT_LIMIT - len(SYSTEM_PROMPT) - len(question) - len(header) - 32
    body = ""
    for line in entries:
        candidate = body + (chr(10) if body else "") + line
        if len(candidate) <= budget:
            body = candidate
            continue
        remaining = budget - len(body) - (1 if body else 0)
        if remaining > 40:
            body = body + (chr(10) if body else "") + line[:remaining - 1] + "…"
        break
    user = header + chr(10) + (body or "（暂无朋友圈）")
    if question:
        user += chr(10) + chr(10) + "分析重点：" + question
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user}]
    total = sum(len(str(item.get("content") or "")) for item in messages)
    if total > PROMPT_LIMIT:  # 兜底硬截断，保证契约
        overflow = total - PROMPT_LIMIT
        user = user[:max(0, len(user) - overflow)]
        messages[1]["content"] = user
    return {"messages": messages, "count": len(moments or []),
            "chars": sum(len(str(item.get("content") or "")) for item in messages)}


# ------------------------------------------------------------------ LLM
def _router_cls():
    sys.path.insert(0, str(paths.soulagent_home()))
    from agi.llm import router as llm_router  # type: ignore

    cls = getattr(llm_router, "LLMRouter", None) or getattr(llm_router, "Router")
    return cls


def _chat(messages: list[dict]) -> dict:
    """可 mock 的统一路由调用；purpose 优先 "chat"，路由没配时回退 "chat_reply"。"""
    try:
        sys.path.insert(0, str(paths.soulagent_home()))
        from agi.llm import router as llm_router  # type: ignore
        purpose = "chat"
        try:
            if "chat" not in (llm_router.purposes() or {}):
                purpose = "chat_reply"
        except Exception:  # noqa: BLE001
            purpose = "chat"
        router = _router_cls()()
        result = router.chat(purpose, messages, temperature=TEMPERATURE,
                             max_tokens=MAX_TOKENS)
        if (not isinstance(result, dict) or not result.get("ok")) and purpose == "chat":
            # 运行时 unknown_purpose:chat（旧配置）→ 回退 chat_reply
            error = str((result or {}).get("error") or "")
            if "unknown_purpose" in error:
                result = router.chat("chat_reply", messages,
                                     temperature=TEMPERATURE,
                                     max_tokens=MAX_TOKENS)
        return result
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "text": "",
                "error": f"llm_unavailable: {type(exc).__name__}: {exc}"}


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_analysis(text: str) -> dict:
    """严格 JSON 解析：容忍 ```json 围栏/前后废话，其余非法返回 parse_error。"""
    raw = str(text or "").strip()
    cleaned = _FENCE_RE.sub("", raw).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return {"ok": False, "parse_error": "no_json_object"}
    try:
        payload = json.loads(cleaned[start:end + 1])
    except (TypeError, ValueError) as exc:
        return {"ok": False, "parse_error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(payload, dict):
        return {"ok": False, "parse_error": "json_not_object"}
    analysis: dict[str, Any] = {}
    analysis["summary"] = _text(payload.get("summary"))[:500]
    analysis["mood"] = _text(payload.get("mood"))[:120]
    for field in _LIST_FIELDS:
        value = payload.get(field)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            value = []
        analysis[field] = [_text(item)[:300] for item in value if _text(item)][:10]
    missing = [field for field in ANALYSIS_FIELDS if field not in payload]
    if missing:
        return {"ok": False, "parse_error": "missing_fields:" + ",".join(missing)}
    return {"ok": True, "analysis": analysis}


def analyze(moments: list[dict] | None = None, question: str = "",
            limit: int = DEFAULT_LIMIT, tids: Iterable[str] | None = None) -> dict:
    """分析朋友圈，返回严格结构化结果；空数据/LLM 失败/解析失败都明确报错。"""
    if moments is None:
        loaded = load_moments(limit=limit, tids=tids)
        if not loaded.get("ok"):
            return {"ok": False, "error": loaded.get("error") or "moments_unavailable",
                    "analysis": None, "latency_ms": 0, "model": ""}
        moments = loaded.get("moments") or []
    moments = [item for item in (moments or []) if isinstance(item, dict)]
    if not moments:
        return {"ok": False, "error": "no_moments", "analysis": None,
                "latency_ms": 0, "model": ""}
    built = build_messages(moments, question=question)
    started = time.perf_counter()
    result = _chat(built["messages"])
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "error": "llm_failed: " + str(
                    (result or {}).get("error") or "unknown"), "analysis": None,
                "latency_ms": latency_ms,
                "model": str((result or {}).get("backend") or "")}
    text = str(result.get("text") or "")
    parsed = _parse_analysis(text)
    model = str(result.get("backend") or result.get("model") or "")
    if not parsed.get("ok"):
        return {"ok": False, "error": "parse_error",
                "parse_error": parsed.get("parse_error") or "invalid_json",
                "raw": text, "analysis": None, "latency_ms": latency_ms,
                "model": model, "count": len(moments),
                "prompt_chars": built["chars"]}
    return {"ok": True, "analysis": parsed["analysis"],
            "raw": text, "latency_ms": latency_ms, "model": model,
            "count": len(moments), "prompt_chars": built["chars"],
            "usage": result.get("usage") or {}, "error": ""}


# ------------------------------------------------------------------ 缓存/历史
def _analysis_dir() -> Path:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    return ANALYSIS_DIR


def _timestamp_name(now: float | None = None) -> str:
    moment = time.localtime(time.time() if now is None else float(now))
    return time.strftime("%Y%m%d-%H%M%S", moment)


def analyze_and_cache(moments: list[dict] | None = None, question: str = "",
                      limit: int = DEFAULT_LIMIT,
                      tids: Iterable[str] | None = None) -> dict:
    """analyze + 写 ``.data/moments/analysis_<YYYYMMDD-HHMMSS>.json`` + 审计。"""
    result = analyze(moments=moments, question=question, limit=limit, tids=tids)
    if not result.get("ok"):
        return result
    stem = "analysis_" + _timestamp_name()
    name = stem + ".json"
    target = _analysis_dir() / name
    suffix = 1
    while target.exists():
        name = f"{stem}-{suffix}.json"
        target = _analysis_dir() / name
        suffix += 1
    payload = {
        "name": name,
        "created_at": time.time(),
        "question": _text(question),
        "count": result.get("count") or 0,
        "model": result.get("model") or "",
        "latency_ms": result.get("latency_ms"),
        "usage": result.get("usage") or {},
        "analysis": result.get("analysis"),
    }
    try:
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        cached_path = str(target)
    except OSError as exc:
        return {**result, "ok": True, "cached_path": "",
                "cache_error": f"{type(exc).__name__}: {exc}"}
    try:
        audit.record("moments.analyze", name, actor="workbench",
                     after=payload.get("analysis"),
                     meta={"count": payload["count"], "model": payload["model"],
                           "question": payload["question"]})
    except Exception:  # noqa: BLE001
        pass
    return {**result, "cached_path": cached_path, "name": name}


def list_analyses(limit: int = 20) -> dict:
    """最近的分析列表（文件名倒序；读 summary/model/created_at）。"""
    directory = _analysis_dir()
    try:
        files = sorted(directory.glob("analysis_*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    items = []
    for path in files[:max(1, int(limit))]:
        item = {"name": path.name, "path": str(path),
                "mtime": round(path.stat().st_mtime, 3)}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            item.update({
                "created_at": payload.get("created_at"),
                "summary": _text((payload.get("analysis") or {}).get("summary"))[:200],
                "mood": _text((payload.get("analysis") or {}).get("mood"))[:80],
                "count": payload.get("count"),
                "model": payload.get("model") or "",
                "question": payload.get("question") or "",
            })
        except Exception as exc:  # noqa: BLE001 - 坏文件也列出，标错误
            item["error"] = f"{type(exc).__name__}: {exc}"
        items.append(item)
    return {"ok": True, "items": items, "count": len(items)}


def get_analysis(name: str) -> dict:
    """按文件名读一次分析（只允许缓存目录内 basename）。"""
    safe = Path(str(name or "")).name
    if not safe or not safe.endswith(".json") or safe.startswith("."):
        return {"ok": False, "error": "invalid_name"}
    target = _analysis_dir() / safe
    if not target.is_file():
        return {"ok": False, "error": "not_found", "name": safe}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "name": safe}
    return {"ok": True, "item": payload, "name": safe, "path": str(target)}


#: 给 Lead 接 main.py 的路由契约（前端/文档用）
ROUTES = (
    ("POST", "/api/wb/moments/analyze", "analyze_and_cache"),
    ("GET", "/api/wb/moments/analyses", "list_analyses"),
    ("GET", "/api/wb/moments/analysis/{name}", "get_analysis"),
)
