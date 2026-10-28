# -*- coding: utf-8 -*-
"""P6-e：主动消息 simple 链路（B 方案）。

- ``proactive_style`` 默认 legacy；运行期 ``agi/data/wechat/config.json``
  或环境变量 ``PROACTIVE_STYLE=simple`` 切到本链路。
- simple：prompt 只留人格边界 + 不编造 + 像微信 + 禁元话题；生成一次 →
  解析 media → 硬检查；违规重生成 1 次；仍违规丢弃本条。
- **不调用** legacy 的开头池/转折词/口语/截断/连续性/兜底/style_profile 后处理。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

ROOT = Path(__file__).resolve().parents[2]
WECHAT_CONFIG = _default_data_dir() / "wechat" / "config.json"
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

# --------------------------------------------------------------------------- #
# P12-C：主动分享「刷到的视频」（文字 + 封面图）
# --------------------------------------------------------------------------- #
#: 与其它 kind 竞争时命中视频分享的概率（低，避免刷屏）
VIDEO_SHARE_PROB = 0.18
#: 只有「值得看」的印象才分享
VIDEO_MIN_VALUE = 0.6
#: 只分享近 7 天刷到的
VIDEO_WINDOW_S = 7 * 86400
#: 频控：每天最多 2 条，两条间隔 ≥3 小时
VIDEO_DAILY_LIMIT = 2
VIDEO_MIN_GAP_S = 3 * 3600
#: 文案长度：主气泡 ≤45 字，追问短气泡 ≤12 字
VIDEO_MAIN_MAX = 45
VIDEO_FOLLOWUP_MAX = 12
#: 封面图上限 2MB（媒体白名单目录内，扩展名 jpg/png/webp）
VIDEO_MAX_BYTES = 2 * 1024 * 1024
VIDEO_FOLLOWUPS = ("一起看看？", "你肯定喜欢", "你肯定感兴趣")
#: 视频元话题词：分享文案里不许出现（"我刷到一条视频"这种话不要）
VIDEO_META_RE = re.compile(r"视频|刷到|B站|b站|哔哩|字幕|链接|播放量|弹幕|up主|UP主|封面")
#: 已发送记录：worker 写 `proactive_send` + `intent_kind=share_video`，频控据此判断
GATEWAY_LOG = _default_data_dir() / "wechat" / "gateway.log"
#: 封面落盘目录（`agi/wechat_gateway/media.py` 的白名单目录之一）
VIDEO_MEDIA_DIR = _default_data_dir() / "selflearn" / "media"


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
    try:
        from . import guard as _guard
        if _guard.false_failure_check(value).get("hit"):
            reasons.append("false_failure")
    except Exception:  # noqa: BLE001
        pass
    return reasons


def today_activity_context(now: float | None = None) -> str:
    """P36：主动消息注入真实的今日 B站/冲浪状态，避免用网络故障解释没分享。"""
    now = time.time() if now is None else float(now)
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    ran, picked, report_date = 0, 0, ""
    try:
        state_path = Path(os.environ.get("BILI_DUTY_STATE")
                          or (Path.home() / ".dsh" / "bili-duty" / "state.json"))
        raw = json.loads(state_path.read_text(encoding="utf-8"))
        if str(raw.get("day") or "") == day:
            ran = int(raw.get("count") or 0)
            picked = int(raw.get("picked") or 0)
            report_date = str(raw.get("day") or "")
    except Exception:  # noqa: BLE001
        pass
    sessions, items = 0, 0
    try:
        from . import surf
        status = surf.status()
        today = status.get("today") or {}
        sessions = int(today.get("executed") or today.get("sessions") or 0)
        for session in status.get("recent_sessions") or []:
            if time.strftime("%Y-%m-%d", time.localtime(
                    float(session.get("started_at") or 0))) == day:
                items += int(session.get("items_seen") or 0)
    except Exception:  # noqa: BLE001
        pass
    return (
        f"\n今天真实后台状态（不是借口，不要拿它编故障）："
        f"bili_today: ran={ran}, picked={picked}, report_date={report_date or day}; "
        f"surf_today: sessions={sessions}, items={items}。"
        "如果今天没刷到可分享的内容，就说没看到什么；"
        "不要说网络坏了/页面打不开/刷不了B站；被问就老实说后台刷到的东西还没整理好。")


def _simple_prompt(kind: str, dialog: str, media_ctx: str,
                   fix_reason: str = "", topic_ctx: str = "",
                   anchor: str = "", activity_ctx: str = "") -> str:
    from . import proactive
    time_line = ""
    try:
        from . import config as _config
        if _config.time_context_enabled():
            time_line = _config.time_context() + "\n"
    except Exception:  # noqa: BLE001 - 时间注入失败不影响主链
        time_line = ""
    prompt = (time_line +
              proactive.PERSONA_CONSTRAINTS + "\n" + proactive.GROUNDING_CONSTRAINTS +
              "\n像真人微信聊天：短句、口语、不加句号；只说一句，不写作文/独白。"
              "\n一条消息只说一件事，尽量 30 字以内；不要括号动作/旁白；"
              "不要问对方为什么不回。"
              "\n正文禁止出现：表情包/素材/清单/表情库/系统/指令/记忆库/记忆/[[media："
              "想发贴纸就单独输出 [[media:...]] 指令行，不要用文字描述。")
    if activity_ctx:
        prompt += activity_ctx
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


def video_share_enabled() -> bool:
    """``proactive_video_share`` 开关（默认 true）。

    与 :func:`proactive_style` 同样的 pytest 隔离：测试进程里读不到生产 config 时
    默认关，避免既有测试误读生产印象库 / 生产 gateway.log；测试要打开就设
    ``PROACTIVE_VIDEO_SHARE=1`` 或把 ``WECHAT_CONFIG`` 指到临时文件。
    """
    raw = os.environ.get("PROACTIVE_VIDEO_SHARE")
    if raw is not None:
        return str(raw).strip().lower() not in ("0", "false", "off", "no", "")
    if (("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules)
            and Path(WECHAT_CONFIG) == DEFAULT_WECHAT_CONFIG):
        return False
    try:
        conf = json.loads(WECHAT_CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return True
    value = conf.get("proactive_video_share", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "off", "no", "")
    return bool(value)


def _video_send_times(now: float) -> list[float]:
    """读 gateway.log 里**已经发出**的 share_video 主动消息时间戳。"""
    try:
        text = Path(GATEWAY_LOG).read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[float] = []
    for line in text.splitlines():
        line = line.strip()
        # 便宜的子串预筛，避免逐行 json.loads 整个大日志
        if not line or "proactive_send" not in line or "share_video" not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("kind") != "proactive_send":
            continue
        if str(entry.get("intent_kind") or "") != "share_video":
            continue
        try:
            ts = float(entry.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        if 0 < ts <= now:
            out.append(ts)
    return out


def video_quota(now: float) -> tuple[bool, str]:
    """频控：每天 ≤2 条、间隔 ≥3 小时；返回 ``(是否可发, 原因)``。"""
    times = _video_send_times(now)
    if sum(1 for ts in times if now - ts < 86400) >= VIDEO_DAILY_LIMIT:
        return False, "daily_limit"
    last = max(times) if times else 0.0
    if last and now - last < VIDEO_MIN_GAP_S:
        return False, "gap"
    return True, "ok"


def recent_video_candidates(now: float, *, limit: int = 50) -> list[dict]:
    """value≥0.6、未分享、近 7 天的印象，按 ``value × 新近度`` 降序。"""
    try:
        from agi.integrations import bili_ingest
        rows = bili_ingest.recent_impressions(max(1, int(limit)))
    except Exception:  # noqa: BLE001 - 印象库不可用不影响主动链
        return []
    out: list[dict] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        try:
            value = float(row.get("value") or 0.0)
            created = float(row.get("created_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if value < VIDEO_MIN_VALUE or int(row.get("shared") or 0):
            continue
        if created and created < now - VIDEO_WINDOW_S:
            continue
        item = dict(row)
        recency = 1.0 if not created else max(0.0, 1.0 - (now - created) / VIDEO_WINDOW_S)
        item["_rank"] = value * recency
        out.append(item)
    out.sort(key=lambda item: (-float(item.get("_rank") or 0.0),
                               str(item.get("bvid") or "")))
    return out


def video_share_candidate(now: float) -> dict | None:
    """满足「开关 + 有候选 + 频控 + 概率」时返回 1 条印象，否则 None（不参与选择）。"""
    if not video_share_enabled():
        return None
    rows = recent_video_candidates(now)
    if not rows:
        return None
    allowed, _reason = video_quota(now)
    if not allowed:
        return None
    if random.random() >= VIDEO_SHARE_PROB:
        return None
    return rows[0]


def _gen_video_copy(client, impression: dict) -> str:
    """``share_line`` 缺失时用 ``share_copy`` purpose 现生成一句（≤40 字）。"""
    if client is None or not getattr(client, "available", lambda: False)():
        return ""
    try:
        result = client.chat(
            [{"role": "system", "content": "只输出一句微信口语，不要解释。"},
             {"role": "user", "content": (
                 "基于下面的视频印象，写一句你主动分享给朋友的话：≤40字、第一人称、"
                 "口语自然；不复述字幕原文、不编造；**不要出现「视频/刷到/B站/字幕/"
                 "链接/播放量」这些词**。\n"
                 f"标题：{str(impression.get('title') or '')[:60]}\n"
                 f"摘要：{str(impression.get('summary') or '')[:200]}\n"
                 f"我的看法：{str(impression.get('my_take') or '')[:60]}")}],
            purpose="share_copy", max_tokens=120, temperature=0.7)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(result, dict) or not result.get("ok"):
        return ""
    return str(result.get("text") or "").strip()


def _video_followup(bvid: str, recent: list[str] | None = None) -> str:
    """追问短气泡（≤12 字）：同一 bvid 从固定起点轮转，跳过硬检查不过的。"""
    digest = hashlib.sha1(str(bvid).encode("utf-8")).hexdigest()[:8]
    start = int(digest, 16) % len(VIDEO_FOLLOWUPS)
    order = list(VIDEO_FOLLOWUPS[start:]) + list(VIDEO_FOLLOWUPS[:start])
    for text in order:
        if not _violations(text, None, [], list(recent or []), kind="share_video"):
            return text[:VIDEO_FOLLOWUP_MAX]
    return ""


def _text_similarity(left: str, right: str) -> float:
    """两句话的相似度（0–1，difflib）。用于「和 share_line 差太多」判定。"""
    import difflib
    return float(difflib.SequenceMatcher(None, str(left or ""), str(right or "")).ratio())


def _prefer_share_line(text: str, share_line: str, recent: list[str]) -> str:
    """文案与 share_line 差异 >80% 且 share_line 本身合规 → 用 share_line 兜底。"""
    reference = str(share_line or "").strip()
    value = str(text or "").strip()
    if not reference or not value:
        return value
    if _video_violations(reference, recent):
        return value
    if _text_similarity(value, reference) >= 0.2:
        return value
    return reference


def _video_violations(text: str, recent: list[str]) -> list[str]:
    """share_video 的硬检查：沿用 simple 通用检查 + 视频专属（元话题/长度）。"""
    value = str(text or "").strip()
    reasons = _violations(value, None, [], recent, kind="share_video")
    if VIDEO_META_RE.search(value):
        reasons.append("video_meta_topic")
    if len(value) > VIDEO_MAIN_MAX:
        reasons.append("video_too_long")
    return reasons


def _fetch_bytes(url: str, *, timeout: float = 8.0,
                 limit: int = VIDEO_MAX_BYTES + 1) -> bytes:
    """下载封面原始字节（可被测试替换；不跟随奇怪 scheme）。"""
    import urllib.request
    request = urllib.request.Request(
        str(url), headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                           "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read(int(limit))


def _sniff_image_ext(data: bytes) -> str:
    """按魔数判断图片类型；非图片返回空串（防止把错误页当封面发出去）。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ""


def download_cover(impression: dict, now: float | None = None) -> Path | None:
    """把封面下载到媒体白名单目录；失败返回 None（只发文字，不阻断）。

    落盘：``agi/data/selflearn/media/video_cover_<bvid>.<ext>``（≤2MB，已存在则复用）。
    """
    url = str(impression.get("cover_url") or "").strip()
    bvid = str(impression.get("bvid") or "").strip()
    if not url or not bvid:
        return None
    safe = re.sub(r"[^0-9A-Za-z_-]", "", bvid) or "video"
    target_dir = Path(VIDEO_MEDIA_DIR)
    for existing in sorted(target_dir.glob(f"video_cover_{safe}.*")):
        try:
            if existing.is_file() and 0 < existing.stat().st_size <= VIDEO_MAX_BYTES:
                return existing
        except OSError:
            continue
    try:
        data = _fetch_bytes(url)
    except Exception:  # noqa: BLE001 - 网络/超时/4xx 一律降级成纯文字
        return None
    if not data or len(data) > VIDEO_MAX_BYTES:
        return None
    ext = _sniff_image_ext(data)
    if not ext:
        return None
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"video_cover_{safe}{ext}"
        temp = target.with_suffix(target.suffix + ".part")
        temp.write_bytes(data)
        temp.replace(target)
        return target
    except OSError:
        return None


def _tick_video_share(path, impression: dict, *, client, now: float,
                      recent_texts: list[str]) -> dict | None:
    """生成并记录一条 share_video；没有可用文案时返回 None（让普通链路继续）。"""
    bvid = str(impression.get("bvid") or "").strip()
    title = str(impression.get("title") or "").strip()
    share_line = str(impression.get("share_line") or "").strip()
    try:
        value = float(impression.get("value") or 0.0)
    except (TypeError, ValueError):
        value = 0.0

    main = share_line or _gen_video_copy(client, impression)
    main = _light_polish(main)
    if not main:
        return None   # 既无 share_line 又生成不出：不抢这个 kind

    reasons = _video_violations(main, recent_texts)
    regen_reason = ",".join(reasons)
    if reasons:
        # 违规：重生成 1 次（LLM），仍不合规才丢；重生成结果与 share_line 差太多则回退
        alt = _light_polish(_gen_video_copy(client, impression))
        if alt:
            alt = _prefer_share_line(alt, share_line, recent_texts)
            if not _video_violations(alt, recent_texts):
                main, reasons = alt, []
    if reasons and share_line and share_line != main:
        fallback = _light_polish(share_line)
        if not _video_violations(fallback, recent_texts):
            main, reasons = fallback, []
    if reasons:
        return {"ok": True, "available": False, "reason": "discarded",
                "discard_reason": ",".join(reasons), "regen_reason": regen_reason,
                "style": "simple", "kind": "share_video", "media": None,
                "video_share": {"bvid": bvid, "title": title,
                                "value": round(value, 3),
                                "share_line": share_line, "cover_sent": False}}
    if len(main) > VIDEO_MAIN_MAX:   # 双保险：兜底句也不许超长
        main = main[:VIDEO_MAIN_MAX].rstrip("，, ")

    cover = download_cover(impression, now)
    media = None
    if cover is not None:
        media = {"bubbles": [{"type": "image", "path": str(cover),
                              "reason": "video_cover", "bvid": bvid}],
                 "reason": "video_cover"}
    messages = [main]
    followup = _video_followup(bvid, recent_texts) if bvid else ""
    if followup:
        messages.append(followup)

    recorded = _record(path, "share_video", main,
                       f"B站印象 value={value:.2f}", value, now, media)
    return {
        "ok": True, "available": True, "style": "simple",
        "regen_reason": regen_reason, "discard_reason": "",
        "topic_seed": {"picked": "", "candidates": [], "reason": "video_share"},
        "progress_check": None,
        "kind": "share_video", "action": "simple", "score": round(value, 3),
        "reason": f"B站印象 value={value:.2f}",
        "video_share": {"bvid": bvid, "title": title, "value": round(value, 3),
                        "share_line": share_line or main,
                        "cover_sent": cover is not None},
        "message": main, "messages": messages,
        "message_id": recorded.get("message_id"),
        "turn_id": recorded.get("turn_id"),
        "media": media,
    }


def tick_simple(path: Path | str, client=None, *, now: float | None = None,
                idle_seconds: float = 30, cooldown: float = 900,
                min_score: float | None = None, respect_state: bool = True,
                inner_client=None, drive: float | None = None) -> dict:
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
    if drive is None:
        best = proactive.choose_candidate(candidates, path, now)
    else:
        best = proactive.choose_candidate(candidates, path, now, drive=drive)
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
    # P12-C：刷到的视频分享。与其它 kind 竞争（独立低概率）；开关关/无候选/频控不过
    # 时返回 None，之后的 prompt 与行为与之前完全一致。
    picked_video = video_share_candidate(now)
    if picked_video is not None:
        video_row = _tick_video_share(path, picked_video, client=client, now=now,
                                      recent_texts=recent_texts)
        if video_row is not None:
            return video_row
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
    activity_ctx = today_activity_context(now)
    prompt = _simple_prompt(kind, dialog, media_ctx,
                            topic_ctx="" if kind == "progress_check" else topic_ctx,
                            anchor=progress_anchor, activity_ctx=activity_ctx)
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
                                    anchor=str(best.get("anchor") or ""),
                                    activity_ctx=activity_ctx)
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
        fallback_text = f"你之前说的{progress_anchor}，后来弄到哪一步了？"
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
