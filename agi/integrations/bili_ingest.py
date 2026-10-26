# -*- coding: utf-8 -*-
"""P12-A：B站素材摄取 + 印象库。

数据流：bilibili_api 抓元信息/字幕/热评/弹幕 → 本地 topics/summary →
API video_understanding（真理解）→ API share_copy → memory 库 bili_impressions。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from agi.llm import chat as llm_chat
from agi.selflearn import db

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CREDENTIAL = Path.home() / ".dsh" / "bili-duty" / "bili_credential.json"
BILLI = "bilibili"
MAX_SUBTITLE_CHARS = 8000
MAX_MATERIAL_CHARS = 12000
MAX_COMMENTS = 20
MAX_DANMAKU = 100
MAX_TAKE = 60
MAX_SHARE = 40
SHARE_BANNED = ("视频", "刷到", "B站", "bilibili", "字幕", "链接", "播放量",
                "UP主", "封面", "评论区")
_SHARE_BANNED_RE = re.compile("|".join(re.escape(word) for word in SHARE_BANNED),
                              re.I)

try:  # 运行时依赖；测试通过 monkeypatch 替身
    from bilibili_api import Credential
    from bilibili_api import comment as bili_comment
    from bilibili_api import search as bili_search
    from bilibili_api import video as bili_video
except Exception:  # pragma: no cover - 未安装时 fetch 返回明确错误
    Credential = None  # type: ignore[assignment]
    bili_comment = None  # type: ignore[assignment]
    bili_search = None  # type: ignore[assignment]
    bili_video = None  # type: ignore[assignment]


class BiliIngestError(RuntimeError):
    pass


class BiliNotLoggedIn(BiliIngestError):
    pass


def credential_path() -> Path:
    raw = os.environ.get("BILI_CREDENTIAL_PATH")
    return Path(raw).expanduser() if raw else DEFAULT_CREDENTIAL


def load_credential(path: Path | str | None = None):
    """读四字段凭证；缺失/损坏 → BiliNotLoggedIn。"""
    target = Path(path) if path else credential_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise BiliNotLoggedIn(f"bili_not_logged_in: {exc}") from exc
    fields = {key: str(raw.get(key) or "").strip()
              for key in ("sessdata", "bili_jct", "buvid3", "dedeuserid")}
    if not all(fields.values()):
        raise BiliNotLoggedIn("bili_not_logged_in: credential fields incomplete")
    if Credential is None:
        raise BiliNotLoggedIn("bili_not_logged_in: bilibili_api unavailable")
    return Credential(fields["sessdata"], fields["bili_jct"],
                      fields["buvid3"], dedeuserid=fields["dedeuserid"])


def _video_factory(bvid: str, credential):
    return bili_video.Video(bvid=bvid, credential=credential)


async def _download_subtitle(url: str) -> str:
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            data = await response.json(content_type=None)
    body = data.get("body") if isinstance(data, dict) else []
    return "\n".join(str(item.get("content") or "") for item in body or [])


async def _fetch_video_async(bvid: str, credential_path_value: str | None = None) -> dict:
    credential = load_credential(credential_path_value)
    video = _video_factory(bvid, credential)
    info = await video.get_info()
    cid = info.get("cid")
    pages = info.get("pages") or []
    if not cid and pages:
        cid = pages[0].get("cid")
    subtitle = ""
    subtitle_reason = ""
    try:
        raw = await video.get_subtitle(cid)
        subtitles = (raw or {}).get("subtitles") if isinstance(raw, dict) else None
        if not subtitles:
            subtitle_reason = "no_subtitle"
        else:
            chosen = next((item for item in subtitles
                           if "zh" in str(item.get("lan") or "").lower()), subtitles[0])
            url = str(chosen.get("subtitle_url") or "")
            if not url:
                subtitle_reason = "subtitle_url_missing"
            else:
                if url.startswith("//"):
                    url = "https:" + url
                subtitle = await _download_subtitle(url)
    except Exception as exc:  # noqa: BLE001
        subtitle_reason = f"subtitle_error:{type(exc).__name__}"
    subtitle = str(subtitle or "")[:MAX_SUBTITLE_CHARS]
    comments: list[dict] = []
    try:
        if bili_comment is not None and info.get("aid"):
            from bilibili_api.comment import CommentResourceType, OrderType
            payload = await bili_comment.get_comments(
                int(info["aid"]), CommentResourceType.VIDEO, page_index=1,
                order=OrderType.LIKE, credential=credential)
            for item in (payload or {}).get("replies") or []:
                content = (item.get("content") or {}).get("message") or ""
                member = item.get("member") or {}
                comments.append({
                    "comment": str(content)[:500],
                    "like": int(item.get("like") or 0),
                    "author": str(member.get("uname") or ""),
                })
    except Exception:  # noqa: BLE001
        comments = []
    comments.sort(key=lambda item: item["like"], reverse=True)
    comments = comments[:MAX_COMMENTS]
    danmaku: list[str] = []
    try:
        raw_danmaku = await video.get_danmakus() or []
        for item in raw_danmaku:
            text = str(getattr(item, "text", "") or "")
            if text and text not in danmaku:
                danmaku.append(text[:120])
            if len(danmaku) >= MAX_DANMAKU:
                break
    except Exception:  # noqa: BLE001
        danmaku = []
    tags: list[str] = []
    try:
        raw_tags = await video.get_tags() or []
        for item in raw_tags:
            name = item.get("tag_name") if isinstance(item, dict) else getattr(item, "tag_name", "")
            if name:
                tags.append(str(name))
    except Exception:  # noqa: BLE001
        tags = []
    stat = info.get("stat") or {}
    owner = info.get("owner") or {}
    return {
        "ok": True, "bvid": bvid,
        "title": str(info.get("title") or ""),
        "owner": str(owner.get("name") or ""),
        "owner_mid": str(owner.get("mid") or ""),
        "duration_s": float(info.get("duration") or 0),
        "cover_url": str(info.get("pic") or ""),
        "description": str(info.get("desc") or ""),
        "tags": tags or ([str(info.get("tname") or "")] if info.get("tname") else []),
        "zone": str(info.get("tname") or ""),
        "subtitle": subtitle,
        "subtitle_reason": subtitle_reason,
        "subtitle_missing": not bool(subtitle.strip()),
        "comments": comments,
        "danmaku": danmaku,
        "view": int(stat.get("view") or 0),
        "like": int(stat.get("like") or 0),
        "coin": int(stat.get("coin") or 0),
        "favorite": int(stat.get("favorite") or 0),
        "url": f"https://www.bilibili.com/video/{bvid}",
    }


def fetch_video(bvid: str, *, path: str | None = None, retries: int = 1) -> dict:
    """同步包装；抓取失败重试一次，返回明确错误。"""
    last_error = ""
    for attempt in range(max(1, int(retries) + 1)):
        try:
            return asyncio.run(_fetch_video_async(str(bvid or "").strip(), path))
        except BiliNotLoggedIn as exc:
            return {"ok": False, "bvid": bvid, "error": str(exc),
                    "error_code": "bili_not_logged_in"}
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    return {"ok": False, "bvid": bvid, "error": last_error[:400],
            "error_code": "bili_fetch_failed"}


def _parse_json(text: Any) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            return value
    return None


def _chat_json(purpose: str, prompt: str, *, retries: int = 1, **kwargs) -> tuple[dict, dict]:
    """调路由层并解析 JSON；坏输出/失败重试一次；仍失败抛错，不落半成品。"""
    last = ""
    for attempt in range(max(1, int(retries) + 1)):
        result = llm_chat(purpose, [{"role": "system", "content": "只输出 JSON，不要解释。"},
                                    {"role": "user", "content": prompt}], **kwargs)
        if not result.get("ok"):
            last = str(result.get("error") or "llm_call_failed")
            continue
        parsed = _parse_json(result.get("text"))
        if parsed is not None:
            meta = {"backend": result.get("backend"),
                    "usage": result.get("usage") or {},
                    "latency_ms": result.get("latency_ms")}
            return parsed, meta
        last = "invalid_json"
    raise BiliIngestError(f"{purpose}_failed: {last}")


def _materials(fetched: dict) -> str:
    comments = "\n".join(f"- {item['comment'][:120]}（赞{item['like']}）"
                         for item in (fetched.get("comments") or [])[:8])
    subtitle = str(fetched.get("subtitle") or "")
    text = (
        f"标题：{fetched.get('title')}\n"
        f"UP主：{fetched.get('owner')}\n"
        f"分区：{fetched.get('zone')}\n"
        f"时长：{fetched.get('duration_s')}秒\n"
        f"简介：{str(fetched.get('description') or '')[:1000]}\n"
        f"标签：{'、'.join(fetched.get('tags') or [])}\n"
        f"热评：\n{comments or '（无）'}\n"
    )
    if subtitle:
        text += "字幕：\n" + subtitle[:MAX_SUBTITLE_CHARS]
    else:
        text += "字幕：无（请只基于标题/简介/评论，不得编造视频内容）\n"
    return text[:MAX_MATERIAL_CHARS]


def _json_hint(schema: str) -> str:
    return f"只输出 JSON，不要 Markdown。字段：{schema}。"


def _share_prompt(materials: str, *, strict: bool = False) -> str:
    prompt = ("基于下面视频材料，写一句你主动分享给朋友的话（≤40字，第一人称、口语、"
              "不要复述字幕原文，不要编造）。\n" + materials)
    if strict:
        prompt += ("\n【重要】禁止出现这些词：视频、刷到、B站、bilibili、字幕、链接、"
                   "播放量、UP主、封面、评论区；请写成“看到/发现个事”这种自然表达。")
    prompt += "\n" + _json_hint('{"share_line":"..."}')
    return prompt


def _share_dirty(text: str) -> bool:
    return bool(_SHARE_BANNED_RE.search(str(text or "")))


def _clean_share_line(text: str) -> str:
    """规则清洗：删掉命中词及相邻的“刷/看到…视频”短语，保留自然表达。"""
    value = str(text or "")
    for word in sorted(SHARE_BANNED, key=len, reverse=True):
        value = re.sub(r"(?:刚刚?|最近)?(?:刷到|看到|发现)?[^，。！？!?\s]*?"
                       + re.escape(word) + r"[^，。！？!?\s]*", "", value,
                       flags=re.I)
        value = value.replace(word, "")
    value = re.sub(r"\s+", " ", value).strip(" ，,。.！!？?")
    if not value:
        value = "发现个挺有意思的东西"
    return value[:MAX_SHARE]


def _ensure_share_line(materials: str, steps: dict) -> tuple[str, bool]:
    data, meta = _chat_json("share_copy", _share_prompt(materials), max_tokens=120)
    line = str(data.get("share_line") or "").strip()[:MAX_SHARE]
    steps["share_copy"] = meta
    cleaned = False
    if _share_dirty(line):
        retry, retry_meta = _chat_json("share_copy", _share_prompt(materials, strict=True),
                                       max_tokens=120)
        steps["share_copy_retry"] = retry_meta
        line = str(retry.get("share_line") or "").strip()[:MAX_SHARE]
        if _share_dirty(line):
            line = _clean_share_line(line)
            cleaned = True
    return line, cleaned


def ingest(bvid: str, *, path: Path | str | None = None, dry: bool = False) -> dict:
    """抓取 + 四步生成 + 入库；失败不写半成品。"""
    bvid = str(bvid or "").strip()
    if not bvid:
        return {"ok": False, "error": "bvid_required", "error_code": "invalid_bvid"}
    fetched = fetch_video(bvid)
    if not fetched.get("ok"):
        return {"ok": False, "bvid": bvid,
                "error": fetched.get("error") or "fetch_failed",
                "error_code": fetched.get("error_code") or "bili_fetch_failed"}
    if dry:
        return {"ok": True, "dry": True, "fetched": fetched}

    materials = _materials(fetched)
    steps: dict[str, dict] = {}
    try:
        topics_data, steps["keyword_extract"] = _chat_json(
            "keyword_extract",
            "从下面材料提取 3-6 个话题关键词：\n" + materials +
            "\n" + _json_hint('{"topics":["..."]}'), max_tokens=200)
        topics = [str(item)[:30] for item in topics_data.get("topics") or [] if str(item).strip()][:6]
        summary_data, steps["ingest_summary"] = _chat_json(
            "ingest_summary",
            "用 1-2 句概括这个视频（只基于材料）：\n" + materials +
            "\n" + _json_hint('{"summary":"...","emotion":"neutral|positive|negative"}'),
            max_tokens=400)
        understanding, steps["video_understanding"] = _chat_json(
            "video_understanding",
            "这是一个视频的材料。只基于材料，不要编造视频里没有的内容。\n" + materials +
            "\n给出：value（0-1，值不值得看/分享）、why（一句具体理由）、"
            'my_take（第一人称“我看了觉得…”，≤60字，你自己的看法/感受）。\n'
            + _json_hint('{"value":0.0,"why":"...","my_take":"..."}'))
        share_line, share_line_cleaned = _ensure_share_line(materials, steps)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "bvid": bvid,
                "error": str(exc)[:300], "error_code": "bili_ingest_failed",
                "steps": steps}

    value = max(0.0, min(1.0, float(understanding.get("value") or 0.0)))
    my_take = str(understanding.get("my_take") or "").strip()[:MAX_TAKE]
    share_line = str(share_line or "").strip()[:MAX_SHARE]
    record = {
        "id": "BI-" + hashlib.sha1(bvid.encode("utf-8", "replace")).hexdigest()[:14],
        "bvid": bvid,
        "title": fetched.get("title") or "",
        "owner": fetched.get("owner") or "",
        "owner_mid": fetched.get("owner_mid") or "",
        "duration_s": float(fetched.get("duration_s") or 0),
        "cover_url": fetched.get("cover_url") or "",
        "summary": str(summary_data.get("summary") or "").strip()[:500],
        "topics": topics,
        "emotion": str(summary_data.get("emotion") or "").strip()[:30],
        "value": value,
        "why": str(understanding.get("why") or "").strip()[:300],
        "my_take": my_take,
        "share_line": share_line,
        "share_line_cleaned": bool(share_line_cleaned),
        "subtitle_len": len(fetched.get("subtitle") or ""),
        "subtitle_missing": bool(fetched.get("subtitle_missing")),
        "comments": fetched.get("comments") or [],
        "danmaku": fetched.get("danmaku") or [],
        "meta": {
            "url": fetched.get("url"), "zone": fetched.get("zone"),
            "tags": fetched.get("tags") or [], "view": fetched.get("view"),
            "like": fetched.get("like"), "coin": fetched.get("coin"),
            "favorite": fetched.get("favorite"),
            "subtitle_reason": fetched.get("subtitle_reason"),
            "steps": steps,
            "share_line_cleaned": bool(share_line_cleaned),
        },
        "created_at": time.time(),
    }
    _save(record, path)
    # P12-B：摄取成功后把 video topics 喂给兴趣画像；失败不影响摄取。
    if str(os.environ.get("BILI_FEED_INTERESTS", "1")).strip().lower() not in (
            "0", "false", "off", "no"):
        try:
            from agi.selflearn import interests
            interests.observe(record["topics"], source="video",
                              ref={"bvid": bvid}, path=path)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "impression": record, "steps": steps,
            "subtitle_len": record["subtitle_len"],
            "subtitle_missing": record["subtitle_missing"]}


def _save(record: dict, path: Path | str | None = None) -> None:
    con = db.connect(path or db.DB_PATH)
    try:
        con.execute(
            "INSERT INTO bili_impressions"
            "(id,bvid,title,owner,owner_mid,duration_s,cover_url,summary,topics_json,"
            "emotion,value,why,my_take,share_line,subtitle_len,subtitle_missing,"
            "comments_json,danmaku_json,meta_json,created_at,shared,shared_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL) "
            "ON CONFLICT(bvid) DO UPDATE SET title=excluded.title,owner=excluded.owner,"
            "owner_mid=excluded.owner_mid,duration_s=excluded.duration_s,"
            "cover_url=excluded.cover_url,summary=excluded.summary,"
            "topics_json=excluded.topics_json,emotion=excluded.emotion,"
            "value=excluded.value,why=excluded.why,my_take=excluded.my_take,"
            "share_line=excluded.share_line,subtitle_len=excluded.subtitle_len,"
            "subtitle_missing=excluded.subtitle_missing,comments_json=excluded.comments_json,"
            "danmaku_json=excluded.danmaku_json,meta_json=excluded.meta_json,"
            "created_at=excluded.created_at",
            (record["id"], record["bvid"], record["title"], record["owner"],
             record["owner_mid"], record["duration_s"], record["cover_url"],
             record["summary"], json.dumps(record["topics"], ensure_ascii=False),
             record["emotion"], record["value"], record["why"], record["my_take"],
             record["share_line"], record["subtitle_len"],
             int(record["subtitle_missing"]),
             json.dumps(record["comments"], ensure_ascii=False),
             json.dumps(record["danmaku"], ensure_ascii=False),
             json.dumps(record["meta"], ensure_ascii=False), record["created_at"]))
        con.commit()
    finally:
        con.close()


def _row_to_impression(row) -> dict:
    item = dict(row)
    for key, fallback in (("topics_json", "[]"), ("comments_json", "[]"),
                          ("danmaku_json", "[]"), ("meta_json", "{}")):
        try:
            item[key[:-5]] = json.loads(item.pop(key) or fallback)
        except (TypeError, ValueError):
            item[key[:-5]] = json.loads(fallback)
    item["subtitle_missing"] = bool(item.get("subtitle_missing"))
    return item


def recent_impressions(limit: int = 10, *, path: Path | str | None = None) -> list[dict]:
    con = db.connect(path or db.DB_PATH)
    try:
        rows = con.execute(
            "SELECT * FROM bili_impressions ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return [_row_to_impression(row) for row in rows]
    finally:
        con.close()


def get_impression(bvid: str, *, path: Path | str | None = None) -> dict | None:
    con = db.connect(path or db.DB_PATH)
    try:
        row = con.execute("SELECT * FROM bili_impressions WHERE bvid=?", (str(bvid),)).fetchone()
        return _row_to_impression(row) if row else None
    finally:
        con.close()


def mark_shared(bvid: str, *, path: Path | str | None = None,
                now: float | None = None) -> bool:
    con = db.connect(path or db.DB_PATH)
    try:
        cur = con.execute("UPDATE bili_impressions SET shared=1,shared_at=? WHERE bvid=?",
                          (time.time() if now is None else float(now), str(bvid)))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


async def _search_async(keyword: str, limit: int = 3) -> list[dict]:
    if bili_search is None:
        raise BiliIngestError("bilibili_api unavailable")
    from bilibili_api.search import SearchObjectType
    data = await bili_search.search_by_type(
        keyword, search_type=SearchObjectType.VIDEO, page=1)
    items = []
    for item in (data or {}).get("result") or []:
        bvid = str(item.get("bvid") or "").strip()
        if bvid:
            items.append({"bvid": bvid, "title": str(item.get("title") or "")})
        if len(items) >= max(1, int(limit)):
            break
    return items


def search_videos(keyword: str, limit: int = 3) -> list[dict]:
    return asyncio.run(_search_async(keyword, limit))


def _impression_materials(impression: dict) -> str:
    return (f"标题：{impression.get('title')}\n"
            f"UP主：{impression.get('owner')}\n"
            f"摘要：{impression.get('summary')}\n"
            f"为什么值得：{impression.get('why')}\n"
            f"话题：{'、'.join(impression.get('topics') or [])}\n")


def refresh_share_lines(limit: int = 10, *, path: Path | str | None = None) -> dict:
    """对已有印象重新生成 share_line（清理元话题词），返回前后对比。"""
    rows = recent_impressions(limit, path=path)
    updated = []
    for impression in rows:
        before = str(impression.get("share_line") or "")
        steps: dict = {}
        try:
            after, cleaned = _ensure_share_line(_impression_materials(impression), steps)
        except Exception as exc:  # noqa: BLE001
            updated.append({"bvid": impression.get("bvid"), "ok": False,
                            "error": str(exc)[:200], "before": before, "after": before})
            continue
        meta = impression.get("meta") or {}
        meta["share_line_cleaned"] = bool(cleaned)
        meta["share_line_refreshed_at"] = time.time()
        meta.setdefault("steps", {}).update(steps)
        con = db.connect(path or db.DB_PATH)
        try:
            con.execute("UPDATE bili_impressions SET share_line=?,meta_json=? WHERE bvid=?",
                        (after, json.dumps(meta, ensure_ascii=False),
                         impression.get("bvid")))
            con.commit()
        finally:
            con.close()
        updated.append({"bvid": impression.get("bvid"), "ok": True,
                        "before": before, "after": after,
                        "share_line_cleaned": bool(cleaned)})
    return {"ok": all(item.get("ok") for item in updated), "updated": len(updated),
            "items": updated}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.integrations.bili_ingest")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--bvid", default="")
    group.add_argument("--search", default="")
    group.add_argument("--show", action="store_true")
    group.add_argument("--refresh-share-lines", action="store_true")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--path", default="")
    args = parser.parse_args(argv)
    path = args.path or None
    if args.show:
        print(json.dumps(recent_impressions(args.limit, path=path),
                         ensure_ascii=False, indent=2))
        return 0
    if args.refresh_share_lines:
        result = refresh_share_lines(args.limit, path=path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.search:
        try:
            found = search_videos(args.search, args.limit)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        results = [ingest(item["bvid"], path=path, dry=args.dry)
                   for item in found[:max(1, args.limit)]]
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(item.get("ok") for item in results) else 1
    if args.bvid:
        result = ingest(args.bvid, path=path, dry=args.dry)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
