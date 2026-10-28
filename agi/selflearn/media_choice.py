# -*- coding: utf-8 -*-
"""P5-b：媒体解析与安全上限（**是否发媒体由大模型决定**）。

本地不做概率决策；只做：
1. 把可选素材清单（name（tags），最多 40 条）写进生成 prompt；
2. 解析模型输出末尾的指令行：
   - ``[[media:sticker:亲亲]]`` → 第二个气泡发表情包
   - ``[[media:emoji:😄]]`` → 句末 1 个 emoji
   - 不输出 → 不发媒体
3. 安全上限：连续 2 条媒体后第 3 条强制不带；mood<0.35 禁搞笑贴纸；crisis 禁一切；
   同一贴纸 7 天不重复；不在素材库的名字拒绝；剥离指令后正文不留残渣。

未开启 media 时 prompt 与返回结构逐字节不变。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

from . import config
from . import emoji as emoji_archive

ROOT = Path(__file__).resolve().parents[2]
ASSET_DIRS = (
    ROOT / "wx_core" / "emoji_assets",
    ROOT / "media" / "stickers",
    config.DATA_DIR / "media",
)
TAGS_PATH = _default_data_dir() / "media_tags.json"
STICKER_WINDOW_S = 7 * 24 * 3600
FUNNY_TAGS = {"好笑", "搞笑", "恶搞", "赞", "嘲讽"}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".wxgf"}
_HASH_NAME_RE = re.compile(r"[0-9a-f]{16,}")
_QUOTE_CHARS = "\"'“”"

_MEDIA_ANY_RE = re.compile(r"\[\[media:[^\]]*\]\]")
_MEDIA_VALID_RE = re.compile(r"\[\[media:(sticker|emoji|wish):([^\]\n]+)\]\]")
_INLINE_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")

MEDIA_INSTRUCTION = (
    "\n可选媒体素材（按需使用，不是每条都要发；不想发就完全不输出这一段）：\n"
    "- 有贴切素材时发 0–1 个贴纸（最多 3 个），不要每条都发；\n"
    "  [[media:sticker:名字]]  或  [[media:emoji:😄]]\n"
    "- emoji 也可以直接写在正文里。\n"
    "- 心情差/严肃/负面话题不要发搞笑贴纸；不要每条都发；只能用下面清单里的名字。\n"
    "- 如果清单里没有你想发的表情，输出 "
    "[[media:wish:想表达\"无语但好笑\"]]，本次不会发，会登记愿望给后续补素材。\n"
    "- **正文绝对不能出现**：表情包/素材/清单/表情库/系统/指令/代码块/[[media "
    "这些元话题；想发就只输出指令行，不要用文字描述“我要发表情包/那个表情”。\n"
    "素材：\n"
)
META_TOPIC_RE = re.compile(r"表情包|素材|清单|表情库|系统|指令|\[\[media")
STICKER_REFERENCE_RE = re.compile(
    r"(那个|这个|刚才那个).{0,5}(表情|贴纸)|这个表情|发个表情|发个贴纸|表情包|贴纸")
MEDIA_DESCRIPTION_RE = re.compile(
    r"(我要|我给你|给你|下面|准备)(发|丢|甩).{0,4}(表情|贴纸)|发个(表情|贴纸)")
ANCHOR_RE = re.compile(
    r"资料|项目|工作|片子|电影|专辑|歌|代码|论文|考试|加班|吃饭|咖啡|天气|今天|昨天|"
    r"刚才|刚|看到|翻到|想到|那批|这件事|朋友|同事|周末|早上|晚上|聊|说|早点|休息|"
    r"在听|在看|在写|在做|在吃|在喝|在走|在睡|听|看|写|做|吃|喝|走|睡|回|家|路|"
    r"手机|电脑|书|剧|游戏|照片|消息|话|事|次|遍|顿|句|夜|小时|分钟|杯|件")
DANGLING_REF_RE = re.compile(r"那个|它|这事|这个|那些|那批")
FEELING_ONLY_RE = re.compile(r"心里|心情|感觉|整个人|一下子|轻了|松了|舒服|踏实|空落|安稳")
# P6-a：内心独白/书面抒情句式（真人微信几乎不说）
INNER_MONOLOGUE_RE = re.compile(
    r"心里.{0,4}(轻了|暖|一暖|发暖|凉|一沉|一紧|松了|空了|堵)|"
    r"悬着的?心.{0,4}放下|心头.{0,4}(一暖|一热)|眼眶.{0,3}(热|湿)|"
    r"百感交集|说不出.{0,2}(滋味|感觉)")
NEGATIVE_WORDS = ("烦", "累", "难受", "失望", "难过", "闷", "焦虑", "委屈",
                  "崩溃", "心累", "没劲", "丧", "不想动")
LAUGH_WORDS = ("哈哈哈", "哈哈", "笑死", "笑死我了", "hhh", "233")
_TAGS_CACHE: dict[str, Any] = {"mtime": None, "tags": {}}
_RECENT_MEDIA: dict[str, deque] = {}
REGEN_FAILED_COUNTS: dict[str, int] = {}
INDEXED: dict[str, Any] = {"done": False}


def media_settings() -> dict:
    """读取媒体配置：默认值 + web_config + 环境变量。"""
    values = {
        "enabled": bool(getattr(config, "MEDIA_ENABLED", False)),
        "sticker_in_reply": bool(getattr(config, "MEDIA_STICKER_IN_REPLY", False)),
        "material_limit": int(getattr(config, "MEDIA_MATERIAL_LIMIT", 40)),
        "max_stickers": int(getattr(config, "MEDIA_MAX_STICKERS", 3)),
        "sticker_cooldown_days": int(getattr(config, "MEDIA_STICKER_COOLDOWN_DAYS", 7)),
        "max_consecutive": int(getattr(config, "MEDIA_MAX_CONSECUTIVE", 2)),
        "tag_file": str(getattr(config, "MEDIA_TAG_FILE", TAGS_PATH)),
        "wish_file": str(getattr(config, "MEDIA_WISH_FILE", config.DATA_DIR / "media_wishes.jsonl")),
    }
    for cfg_path in (Path(config.WEB_CONFIG),
                     ROOT / "agi" / "selflearn" / "web_config.json"):
        try:
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for key in ("media_enabled", "media_sticker_in_reply", "media_material_limit",
                            "media_max_stickers", "media_sticker_cooldown_days",
                            "media_max_consecutive", "media_tag_file", "media_wish_file"):
                    if key in raw:
                        values[key[len("media_"):]] = raw[key]
        except Exception:  # noqa: BLE001
            continue
    env = os.environ
    if env.get("MEDIA_ENABLED") is not None:
        values["enabled"] = str(env["MEDIA_ENABLED"]).strip().lower() in ("1", "true", "yes", "on")
    if env.get("MEDIA_STICKER_IN_REPLY") is not None:
        values["sticker_in_reply"] = str(env["MEDIA_STICKER_IN_REPLY"]).strip().lower() in (
            "1", "true", "yes", "on")
    for env_key, field in (("MEDIA_MATERIAL_LIMIT", "material_limit"),
                           ("MEDIA_MAX_STICKERS", "max_stickers"),
                           ("MEDIA_STICKER_COOLDOWN_DAYS", "sticker_cooldown_days"),
                           ("MEDIA_MAX_CONSECUTIVE", "max_consecutive")):
        if env.get(env_key) is not None:
            try:
                values[field] = int(env[env_key])
            except ValueError:
                pass
    # 测试隔离：未显式设置 MEDIA_ENABLED 时忽略本地 web_config，避免生产开关污染套件
    if ("PYTEST_CURRENT_TEST" in env or "pytest" in sys.modules) and env.get("MEDIA_ENABLED") is None:
        values["enabled"] = False
    if env.get("MEDIA_TAG_FILE"):
        values["tag_file"] = env["MEDIA_TAG_FILE"]
    if env.get("MEDIA_WISH_FILE"):
        values["wish_file"] = env["MEDIA_WISH_FILE"]
    return values


def _load_tags() -> dict[str, list[str]]:
    path = Path(media_settings().get("tag_file") or TAGS_PATH)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _TAGS_CACHE.get("mtime") == mtime and _TAGS_CACHE.get("tags"):
        return dict(_TAGS_CACHE["tags"])
    tags: dict[str, list[str]] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        table = raw.get("tags", raw) if isinstance(raw, dict) else {}
        if isinstance(table, dict):
            for name, value in table.items():
                if isinstance(value, list):
                    tags[str(name)] = [str(item) for item in value if str(item).strip()]
    except Exception:  # noqa: BLE001
        pass
    _TAGS_CACHE.update({"mtime": mtime, "tags": tags})
    return dict(tags)


def ensure_indexed(path: Path | str | None = None, *, force: bool = False) -> dict:
    """首次使用索引本地资产并写 tags（幂等；只在启动/首次使用扫盘）。"""
    if INDEXED.get("done") and not force:
        return {"ok": True, "cached": True}
    tags = _load_tags()
    stats = {"ok": True, "indexed": 0, "tagged": 0}
    for asset_dir in ASSET_DIRS:
        if not Path(asset_dir).is_dir():
            continue
        result = emoji_archive.index_assets(path, str(asset_dir), limit=500)
        if result.get("ok"):
            stats["indexed"] += int(result.get("indexed") or 0)
    con = None
    try:
        from . import db
        con = db.connect(path if path is not None else config.DB_PATH)
        with con:
            rows = con.execute("SELECT id,name FROM emoji_archive").fetchall()
            for row in rows:
                name = str(row["name"] or "")
                tags_for_name = tags.get(name)
                if tags_for_name is None:
                    tags_for_name = ["表情包"] if _HASH_NAME_RE.fullmatch(name) else [name]
                con.execute("UPDATE emoji_archive SET tags_json=? WHERE id=?",
                            (json.dumps(tags_for_name, ensure_ascii=False), row["id"]))
                stats["tagged"] += 1
    except Exception:  # noqa: BLE001
        stats["ok"] = False
    finally:
        if con is not None:
            con.close()
    INDEXED["done"] = True
    return stats


def list_assets(path: Path | str | None = None) -> list[dict]:
    """素材清单：media_assets（控制台「素材库」）优先，空表时回退 emoji_archive。

    每次调用都重读 DB（不缓存死），所以改名/改类型/禁用后下一次生成立即生效。
    """
    ensure_indexed(path)
    con = None
    try:
        from . import db
        con = db.connect(path if path is not None else config.DB_PATH)
        has_table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='media_assets'"
        ).fetchone() is not None
        if has_table:
            rows = con.execute(
                "SELECT id,name,path,tags_json,enabled FROM media_assets "
                "WHERE enabled = 1 ORDER BY builtin DESC, created_at DESC, rowid DESC"
            ).fetchall()
            if rows:
                items = []
                for row in rows:
                    try:
                        tags = json.loads(row["tags_json"] or "[]")
                    except (TypeError, ValueError):
                        tags = []
                    items.append({"id": row["id"], "name": str(row["name"]),
                                  "path": str(row["path"]),
                                  "tags": [str(t) for t in tags],
                                  "extension": Path(str(row["path"] or "")).suffix.lower(),
                                  "usage_count": 0, "source": "media_assets"})
                return items
        rows = con.execute(
            "SELECT id,name,path,tags_json,usage_count FROM emoji_archive "
            "WHERE path LIKE '%.png' OR path LIKE '%.jpg' OR path LIKE '%.jpeg' "
            "OR path LIKE '%.webp' OR path LIKE '%.gif' OR path LIKE '%.wxgf'"
        ).fetchall()
        items = []
        for row in rows:
            try:
                tags = json.loads(row["tags_json"] or "[]")
            except (TypeError, ValueError):
                tags = []
            items.append({"id": row["id"], "name": str(row["name"]),
                          "path": str(row["path"]),
                          "tags": [str(t) for t in tags],
                          "extension": Path(str(row["path"] or "")).suffix.lower(),
                          "usage_count": int(row["usage_count"] or 0)})
        return items
    except Exception:  # noqa: BLE001
        return []
    finally:
        if con is not None:
            con.close()


def find_asset(name: str, path: Path | str | None = None) -> dict | None:
    name = str(name or "").strip().lstrip(":").strip()
    for item in list_assets(path):
        if item.get("name") == name:
            return item
    return None


def media_materials(path: Path | str | None = None, limit: int | None = None) -> list[str]:
    """给模型看的素材清单：``name（tags）``，过滤不可用/哈希名，最多 limit 条。"""
    settings = media_settings()
    limit = int(limit if limit is not None else settings.get("material_limit") or 40)
    materials: list[str] = []
    for item in list_assets(path):
        name = str(item.get("name") or "").strip()
        if not name or item.get("extension") not in _IMAGE_SUFFIXES:
            continue
        if _HASH_NAME_RE.fullmatch(name):
            continue
        tags = "、".join(item.get("tags") or [])
        label = f"{name}（{tags}）" if tags else name
        if label not in materials:
            materials.append(label)
        if len(materials) >= limit:
            break
    return materials


def media_prompt_block(path: Path | str | None = None) -> str:
    """media 开启时才返回；关闭时返回空串（prompt 逐字节不变）。"""
    settings = media_settings()
    if not settings.get("enabled"):
        return ""
    materials = media_materials(path, settings.get("material_limit"))
    if not materials:
        return MEDIA_INSTRUCTION + "（暂无可用素材，不要输出 media 指令）\n"
    return MEDIA_INSTRUCTION + "\n".join(materials) + "\n"


def _recent_sticker_names(path: Path | str | None = None, now: float | None = None,
                          days: int | None = None) -> set[str]:
    now = time.time() if now is None else float(now)
    days = int(days if days is not None else media_settings().get("sticker_cooldown_days") or 7)
    con = None
    try:
        from . import db
        con = db.connect(path if path is not None else config.DB_PATH)
        rows = con.execute(
            "SELECT DISTINCT a.name FROM emoji_usage u "
            "JOIN emoji_archive a ON a.id = u.emoji_id WHERE u.created_at >= ?",
            (now - days * 86400,)).fetchall()
        return {str(row["name"]) for row in rows}
    except Exception:  # noqa: BLE001
        return set()
    finally:
        if con is not None:
            con.close()


def _clean_directive_value(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] in _QUOTE_CHARS and text[-1] in _QUOTE_CHARS:
        text = text[1:-1].strip()
    return text


def parse_media_directive(reply: Any) -> tuple[str, dict | None]:
    """去掉所有 media 指令行，返回 (clean_reply, plan|None)。

    plan 结构：``{"stickers": [名字...], "emoji": "😄"|"", "wishes": [文本...]}``；
    多条指令全部收集（贴纸上限在 build_plan 里截断）。
    """
    text = str(reply or "")
    stickers: list[str] = []
    wishes: list[str] = []
    emoji_value = ""
    lines: list[str] = []
    for line in text.splitlines():
        for match in _MEDIA_VALID_RE.finditer(line):
            kind = match.group(1).lower()
            value = _clean_directive_value(match.group(2))
            if kind == "sticker":
                stickers.append(value)
            elif kind == "emoji":
                if not emoji_value:
                    emoji_value = value
            elif kind == "wish" and value:
                wishes.append(value)
        cleaned = _MEDIA_ANY_RE.sub("", line).strip()
        if cleaned:
            lines.append(cleaned)
    clean = "\n".join(lines).strip()
    if not stickers and not emoji_value and not wishes:
        return clean, None
    return clean, {"stickers": stickers, "emoji": emoji_value, "wishes": wishes}


def record_wish(wish: Any, context: str = "", path: Path | str | None = None,
                source: str = "model") -> dict:
    """把模型缺素材的愿望写进 media_wishes.jsonl（本次不发）。"""
    settings = media_settings()
    value = _clean_directive_value(wish)
    if not value:
        return {"ok": False, "reason": "empty_wish"}
    target = Path(str(settings.get("wish_file") or config.MEDIA_WISH_FILE))
    entry = {"ts": round(time.time(), 3), "wish": value[:500],
             "context": str(context or "")[:500], "source": str(source or "model")[:40]}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "entry": entry, "path": str(target)}


def _consecutive_media(recent_media: list | None) -> int:
    count = 0
    for item in reversed(list(recent_media or [])):
        if item:
            count += 1
        else:
            break
    return count


def validate_plan(plan: dict | None, state: dict | None,
                  recent_media: list | None = None, *,
                  path: Path | str | None = None,
                  allow_sticker: bool = True,
                  now: float | None = None) -> dict | None:
    """单条指令的安全校验（build_plan 内部逐条调用；也方便单测）。"""
    settings = media_settings()
    if not settings.get("enabled") or not isinstance(plan, dict):
        return None
    state = dict(state or {})
    now = time.time() if now is None else float(now)
    if state.get("crisis"):
        return None
    kind = str(plan.get("type") or "").lower()
    value = str(plan.get("value") or "").strip()
    if not value:
        return None
    if kind == "wish":
        return {"type": "wish", "value": value}
    if kind == "emoji":
        return {"type": "emoji", "value": value}
    if kind != "sticker" or not allow_sticker:
        return None
    asset = find_asset(value, path)
    if not asset:
        return None
    mood = float(state.get("mood", 0.55) or 0.55)
    tags = set(asset.get("tags") or [])
    if mood < 0.35 and tags & FUNNY_TAGS:
        return None
    if value in _recent_sticker_names(path, now,
                                      settings.get("sticker_cooldown_days")):
        return None
    return {"type": "sticker", "value": value, "name": value,
            "path": asset.get("path"), "tags": sorted(tags)}


def build_plan(path: Path | str | None, directives: dict | None, text: str, *,
               state: dict | None = None, recent_media: list | None = None,
               allow_sticker: bool = True, now: float | None = None) -> dict | None:
    """把模型的多条指令变成 media bubbles；无指令/全被拦 → None。"""
    if not directives or not isinstance(directives, dict):
        return None
    settings = media_settings()
    if not settings.get("enabled"):
        return None
    state = dict(state or {})
    if state.get("crisis"):
        return None
    text = str(text or "").strip()
    wishes_raw = list(directives.get("wishes") or [])
    recorded_wishes = []
    for wish in wishes_raw:
        result = record_wish(wish, context=text[:120], path=path)
        if result.get("ok"):
            recorded_wishes.append(result["entry"])
    if not text:
        return ({"bubbles": [], "wishes": recorded_wishes, "reason": "model:wish"}
                if recorded_wishes else None)
    consecutive = _consecutive_media(recent_media)
    sticker_cap = 1 if consecutive >= int(settings.get("max_consecutive") or 2) else int(
        settings.get("max_stickers") or 3)
    accepted: list[dict] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for name in list(directives.get("stickers") or []):
        if len(accepted) >= sticker_cap:
            dropped.append(f"over_cap:{name}")
            continue
        name = _clean_directive_value(name)
        if not name or name in seen:
            dropped.append(f"duplicate_or_empty:{name}")
            continue
        single = validate_plan({"type": "sticker", "value": name}, state,
                               recent_media, path=path,
                               allow_sticker=allow_sticker, now=now)
        if not single:
            dropped.append(f"rejected:{name}")
            # 库外/被拦的名字不进素材，但保留为 wish 供后续补库
            if not find_asset(name, path):
                result = record_wish(f"想发贴纸:{name}", context=text[:120], path=path)
                if result.get("ok"):
                    recorded_wishes.append(result["entry"])
            continue
        seen.add(name)
        accepted.append(single)
    emoji_value = str(directives.get("emoji") or "").strip()
    emoji_ok = bool(emoji_value and _INLINE_EMOJI_RE.search(emoji_value)
                    and not _INLINE_EMOJI_RE.search(text))
    if consecutive >= int(settings.get("max_consecutive") or 2):
        if accepted:
            emoji_ok = False
        else:
            accepted = []
    if not accepted and not emoji_ok:
        return ({"bubbles": [], "wishes": recorded_wishes, "reason": "model:wish",
                 "dropped": dropped} if recorded_wishes else None)
    bubbles: list[dict] = []
    reason_parts: list[str] = []
    if emoji_ok:
        bubbles.append({"type": "emoji_text", "text": text + emoji_value})
        reason_parts.append("model:emoji")
    else:
        bubbles.append({"type": "text", "text": text})
    for item in accepted:
        name = str(item.get("name") or "")
        try:
            emoji_archive.record_use(path, name, context="proactive")
        except Exception:  # noqa: BLE001
            pass
        kind = "gif" if str(item.get("path") or "").lower().endswith(
            (".gif", ".wxgf")) else "sticker"
        bubbles.append({"type": kind, "name": name, "path": item.get("path")})
        reason_parts.append(f"model:sticker:{name}")
    reason = ",".join(reason_parts) if reason_parts else "model:wish"
    return {"bubbles": bubbles, "wishes": recorded_wishes,
            "reason": reason, "dropped": dropped,
            "stickers": [b.get("name") for b in bubbles[1:]]}


def _has_sticker(plan: dict | None) -> bool:
    return bool(plan and any(
        isinstance(item, dict) and item.get("type") in ("sticker", "gif", "image")
        for item in plan.get("bubbles") or []))


def _split_sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[。！？!?])", str(text or "")) if part.strip()]


def polarity_conflict(text: Any) -> bool:
    value = str(text or "")
    return (any(word in value for word in NEGATIVE_WORDS)
            and any(word in value for word in LAUGH_WORDS))


def has_concrete_anchor(text: Any, anchors: str = "") -> bool:
    """第一条是否含具体事由/对象；允许引用用户上条消息里的具体短语。"""
    value = str(text or "").strip()
    if not value:
        return False
    if ANCHOR_RE.search(value):
        return True
    context = str(anchors or "")
    for match in re.finditer(r"(?:那条|这个|那个|那批)([一-鿿]{2,6})", value):
        if match.group(1) and match.group(1) in context:
            return True
    return False


def _segments(text: Any) -> list[str]:
    return [part.strip() for part in str(text or "").splitlines() if part.strip()]


def coherence_issues(text: Any, anchors: str = "") -> list[str]:
    """多气泡自足性：第一条要事由；后续指代词要有可见锚点。"""
    issues: list[str] = []
    parts = _segments(text)
    if not parts:
        return issues
    # 只对“纯内心感受/多气泡首条”强制具体事由；普通短句不误伤
    if (len(parts[0]) >= 5 and not has_concrete_anchor(parts[0], anchors)
            and (len(parts) > 1 or FEELING_ONLY_RE.search(parts[0]))):
        issues.append("first_bubble_no_anchor")
    if len(parts) > 1:
        context = parts[0] + "\n" + str(anchors or "")
        for part in parts[1:]:
            if DANGLING_REF_RE.search(part) and not ANCHOR_RE.search(context):
                issues.append("dangling_reference")
                break
    return issues


def text_media_issues(text: Any, plan: dict | None, *,
                      anchors: str = "") -> list[str]:
    """文本—媒体一致性/元话题/极性/多气泡自足检测。"""
    issues: list[str] = []
    value = str(text or "")
    if not value:
        return issues
    if META_TOPIC_RE.search(value):
        issues.append("meta_topic")
    has_sticker = _has_sticker(plan)
    if STICKER_REFERENCE_RE.search(value) and not has_sticker:
        issues.append("sticker_ref_without_media")
    if has_sticker and MEDIA_DESCRIPTION_RE.search(value):
        issues.append("media_description_without_need")
    if polarity_conflict(value):
        issues.append("polarity_conflict")
    if INNER_MONOLOGUE_RE.search(value):
        issues.append("inner_monologue")
    issues.extend(coherence_issues(value, anchors))
    return issues


def _strip_sentences(text: str, predicate) -> str:
    kept = [sentence for sentence in _split_sentences(text) if not predicate(sentence)]
    return "".join(kept).strip()


def sanitize_text_media(text: Any, plan: dict | None, *,
                        anchors: str = "") -> str:
    """确定性兜底：删元话题/未发贴纸引用/发贴纸描述；极性冲突去笑词。"""
    value = str(text or "").strip()
    if not value:
        return value
    parts = _segments(value)
    if (parts and len(parts[0]) >= 5 and not has_concrete_anchor(parts[0], anchors)
            and (len(parts) > 1 or FEELING_ONLY_RE.search(parts[0]))):
        return ""
    if parts and len(parts) > 1:
        context = parts[0] + "\n" + str(anchors or "")
        kept = [parts[0]]
        for part in parts[1:]:
            if DANGLING_REF_RE.search(part) and not ANCHOR_RE.search(context):
                continue
            kept.append(part)
        value = "\n".join(kept)
    if META_TOPIC_RE.search(value):
        value = _strip_sentences(value, lambda s: bool(META_TOPIC_RE.search(s)))
    if INNER_MONOLOGUE_RE.search(value):
        value = _strip_sentences(value, lambda s: bool(INNER_MONOLOGUE_RE.search(s)))
    has_sticker = _has_sticker(plan)
    if STICKER_REFERENCE_RE.search(value) and not has_sticker:
        value = _strip_sentences(value, lambda s: bool(STICKER_REFERENCE_RE.search(s)))
    if has_sticker and MEDIA_DESCRIPTION_RE.search(value):
        value = _strip_sentences(value, lambda s: bool(MEDIA_DESCRIPTION_RE.search(s)))
    if polarity_conflict(value):
        for word in sorted(LAUGH_WORDS, key=len, reverse=True):
            value = value.replace(word, "")
        value = re.sub(r"[，,；;、]+$", "", value).strip()
    return value.strip("，,；; ")


def sync_plan_text(plan: dict | None, text: str) -> dict | None:
    """文本被收口后同步 media.bubbles[0] 的文本。"""
    if not plan or not plan.get("bubbles"):
        return plan
    first = plan["bubbles"][0]
    if isinstance(first, dict) and first.get("type") in ("text", "emoji_text"):
        if first.get("type") == "emoji_text":
            first["text"] = str(text or "") + str(plan.get("emoji") or "")
        else:
            first["text"] = str(text or "")
    return plan


def record_media(path: Path | str | None, plan: dict | None) -> None:
    """记录本地连续媒体历史（进程内；供“连续 2 条后强制不带”上限）。"""
    history = _RECENT_MEDIA.setdefault(str(path or ""), deque(maxlen=5))
    history.append(bool(plan))


def recent_media(path: Path | str | None) -> list[bool]:
    return list(_RECENT_MEDIA.get(str(path or ""), []))


def record_regen_failed(issues: list[str], reason: str = "still_invalid") -> dict:
    """P6-c：重生成后仍命中的规则计数（按规则分类，不静默吞）。"""
    for issue in list(issues or []):
        key = f"{reason}:{issue}"
        REGEN_FAILED_COUNTS[key] = int(REGEN_FAILED_COUNTS.get(key) or 0) + 1
    return dict(REGEN_FAILED_COUNTS)


def regen_failed_snapshot() -> dict:
    return dict(REGEN_FAILED_COUNTS)


def reset_regen_failed() -> None:
    REGEN_FAILED_COUNTS.clear()


def reset_history() -> None:
    _RECENT_MEDIA.clear()
