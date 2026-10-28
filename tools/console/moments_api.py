# -*- coding: utf-8 -*-
"""802 控制台：朋友圈抓取/缓存/LLM 分析（只读 + 本地缓存）。"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("CONSOLE_MOMENTS_DIR") or ROOT / "agi" / "data" / "moments")
FETCH_SCRIPT = Path(os.environ.get("CONSOLE_MOMENTS_FETCH_SCRIPT")
                    or ROOT / "tools" / "moments_fetch.py")
FETCH_PYTHON = Path(os.environ.get("CONSOLE_MOMENTS_PYTHON")
                    or r"E:/agent/venv_wx312/Scripts/python.exe")
FETCH_TIMEOUT = 180
CACHE_LIMIT = 500
_FIELDS = ("summary", "topics", "mood", "social_signals", "notable", "suggestions")


def _now() -> float:
    return time.time()


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _date_of(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(ts or 0)))
    except (ValueError, OSError):
        return time.strftime("%Y-%m-%d")


def _id(row: dict) -> str:
    """唯一键：优先用动态 id；``tid`` 在朋友圈数据里通常等于「账号 wxid」，
    多条动态会共享同一个 tid，用它去重会把整条时间线并成 1-2 条（2026-10-29 用户发现）。"""
    return str(row.get("id") or row.get("tid") or "").strip()


def _norm(row: dict) -> dict:
    images = []
    for image in row.get("images") or []:
        if isinstance(image, dict):
            images.append({key: image.get(key) for key in
                           ("md5", "url", "size", "width", "height", "local_path")})
    return {
        "id": str(row.get("id") or ""),
        "tid": str(row.get("tid") or row.get("id") or ""),
        "username": str(row.get("username") or ""),
        "nickname": str(row.get("nickname") or row.get("username") or ""),
        "text": str(row.get("text") or ""),
        "create_time": _safe_int(row.get("create_time"), 0),
        "images": images,
        "likes": list(row.get("likes") or []),
        "comments": list(row.get("comments") or []),
        "location": row.get("location") if isinstance(row.get("location"), dict) else {},
    }


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and _id(item):
                    rows.append(_norm(item))
    except OSError:
        pass
    return rows


def load_cache(limit: int = 50, keyword: str = "") -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    items: list[dict] = []
    try:
        files = sorted(DATA_DIR.glob("*.jsonl"), reverse=True)
    except OSError:
        files = []
    needle = str(keyword or "").strip().lower()
    for path in files:
        for item in _read_jsonl(path):
            key = _id(item)
            if not key or key in seen:
                continue
            if needle and needle not in (item.get("text") or "").lower() \
                    and needle not in (item.get("nickname") or "").lower():
                continue
            seen.add(key)
            items.append(item)
    items.sort(key=lambda row: row.get("create_time") or 0, reverse=True)
    total = len(items)
    return {"ok": True, "items": items[:max(1, min(500, limit))], "total": total,
            "cached": total > 0, "count": len(items[:max(1, min(500, limit))])}


def _merge_cache(rows: list[dict]) -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, dict[str, dict]] = {}
    for item in rows:
        day = _date_of(item.get("create_time") or 0)
        by_day.setdefault(day, {})[_id(item)] = _norm(item)
    added = 0
    for day, fresh in by_day.items():
        path = DATA_DIR / f"{day}.jsonl"
        existing = {_id(row): row for row in _read_jsonl(path)}
        for key, item in fresh.items():
            if key not in existing:
                existing[key] = item
                added += 1
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            for item in sorted(existing.values(),
                               key=lambda row: row.get("create_time") or 0):
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        tmp.replace(path)
    return added


def fetch_moments(limit: int = 50, keyword: str = "", timeout: int = FETCH_TIMEOUT) -> dict:
    if not FETCH_SCRIPT.is_file():
        return {"ok": False, "error": "fetch_script_missing", "hint": str(FETCH_SCRIPT)}
    if not FETCH_PYTHON.is_file():
        return {"ok": False, "error": "fetch_python_missing", "hint": str(FETCH_PYTHON)}
    command = [str(FETCH_PYTHON), str(FETCH_SCRIPT), "--limit", str(max(1, limit))]
    if keyword:
        command.extend(["--keyword", str(keyword)])
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=max(1, timeout))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "fetch_timeout", "hint": f">{timeout}s"}
    except OSError as exc:
        return {"ok": False, "error": f"fetch_failed:{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"ok": False, "error": "fetch_failed",
                "hint": (result.stderr or "")[-500:], "code": result.returncode}
    try:
        rows = json.loads(result.stdout)
    except ValueError:
        return {"ok": False, "error": "fetch_bad_json", "hint": result.stdout[:300]}
    if not isinstance(rows, list):
        return {"ok": False, "error": "fetch_bad_payload"}
    normalized = [_norm(row) for row in rows if isinstance(row, dict)
                  and (_id(row) or row.get("text"))]
    added = _merge_cache(normalized)
    cache = load_cache(limit=max(limit, CACHE_LIMIT), keyword=keyword)
    return {"ok": True, "fetched": len(normalized), "added": added,
            "items": cache["items"][:max(1, min(500, limit))],
            "count": len(cache["items"][:max(1, min(500, limit))])}


def get_moment(tid: str) -> dict:
    target = str(tid or "").strip()
    if not target:
        return {"ok": False, "error": "id_required"}
    cache = load_cache(limit=CACHE_LIMIT)
    for item in cache["items"]:
        if target in (item.get("tid"), item.get("id")):
            return {"ok": True, "item": item}
    return {"ok": False, "error": "not_found", "id": target}


def find_media(md5: str) -> dict:
    target = str(md5 or "").strip()
    if not target:
        return {"ok": False, "error": "md5_required"}
    for item in load_cache(limit=CACHE_LIMIT)["items"]:
        for image in item.get("images") or []:
            if str(image.get("md5") or "") == target:
                local = str(image.get("local_path") or "")
                if local and Path(local).is_file():
                    return {"ok": True, "path": local, "item": item}
                return {"ok": False, "error": "media_not_local", "md5": target}
    return {"ok": False, "error": "not_found", "md5": target}


def _compact_item(item: dict) -> dict:
    return {
        "tid": _id(item),
        "nickname": item.get("nickname"),
        "time": _date_of(item.get("create_time") or 0),
        "text": str(item.get("text") or "")[:300],
        "images": len(item.get("images") or []),
        "likes": [str(like.get("nickname") or like.get("name") or "")
                  for like in (item.get("likes") or []) if isinstance(like, dict)][:10],
        "comments": [str(comment.get("content") or comment.get("text") or "")[:120]
                     for comment in (item.get("comments") or [])
                     if isinstance(comment, dict)][:10],
    }


def _parse_json(text: str) -> dict | None:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def analyze(limit: int = 30, question: str = "", *, client: str = "") -> dict:
    cache = load_cache(limit=max(1, min(50, limit)))
    items = cache["items"][:max(1, min(50, limit))]
    if not items:
        return {"ok": False, "error": "no_moments", "hint": "先刷新朋友圈再分析"}
    payload = [_compact_item(item) for item in items]
    prompt = (
        "你是朋友圈内容分析器。根据下面的朋友圈数据，输出严格 JSON（不要 markdown）：\n"
        '{"summary": "一句话总结", "topics": ["主题"], "mood": "整体情绪",'
        ' "social_signals": {"谁给谁点赞/评论等互动信号": "说明"},'
        ' "notable": ["值得注意的条目"], "suggestions": ["可执行建议"]}\n'
        f"用户问题：{str(question or '').strip() or '（无，做通用总结）'}\n"
        "朋友圈数据：\n" + json.dumps(payload, ensure_ascii=False)
    )
    from agi.llm.router import Router
    try:
        router = Router()
        result = router.chat("chat_reply", [
            {"role": "system", "content": "只输出严格 JSON，字段固定。"},
            {"role": "user", "content": prompt},
        ], max_tokens=900, temperature=0.2, json_mode=True)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"llm_failed:{type(exc).__name__}: {exc}"}
    if not result.get("ok"):
        return {"ok": False, "error": "llm_failed",
                "hint": result.get("error") or result.get("text") or ""}
    parsed = _parse_json(result.get("text") or "")
    if parsed is None:
        return {"ok": False, "error": "analysis_bad_json",
                "hint": str(result.get("text") or "")[:500]}
    analysis = {field: parsed.get(field, [] if field != "summary" and field != "mood" else "")
                for field in _FIELDS}
    analysis.update({"count": len(items), "question": str(question or "").strip(),
                     "created_at": _now(), "backend": result.get("backend"),
                     "model": result.get("model"),
                     "usage": result.get("usage"), "items": [_id(item) for item in items]})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    name = f"analysis_{stamp}.json"
    path = DATA_DIR / name
    with path.open("w", encoding="utf-8") as handle:
        json.dump(analysis, handle, ensure_ascii=False, indent=2)
    return {"ok": True, "analysis": analysis, "name": name, "file": str(path)}


def list_analyses(limit: int = 10) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []
    try:
        files = sorted(DATA_DIR.glob("analysis_*.json"), reverse=True)
    except OSError:
        files = []
    for path in files[:max(1, min(100, limit))]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            items.append({"name": path.name, "file": str(path),
                          "created_at": data.get("created_at") or 0,
                          "summary": data.get("summary") or "",
                          "count": data.get("count") or 0, "analysis": data})
    return {"ok": True, "items": items, "count": len(items)}


def get_analysis(name: str) -> dict:
    target = Path(str(name or "")).name
    if not re.fullmatch(r"analysis_[0-9_]+\.json", target):
        return {"ok": False, "error": "bad_name"}
    path = DATA_DIR / target
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"ok": False, "error": "not_found", "name": target}
    return {"ok": True, "name": target, "analysis": data}
