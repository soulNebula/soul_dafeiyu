# -*- coding: utf-8 -*-
"""03 防幻觉：生成后的轻量规则校验，不重调大模型。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from . import citation, db
from .config import DB_PATH

ABSOLUTE = {
    "一定": "可能",
    "从来": "印象里",
    "永远": "通常",
    "所有人": "很多人",
    "绝对": "很可能",
}
TOOL_CLAIMS = ("我查了", "我搜了", "我看了资料", "我上网查")
NUMBER = re.compile(r"(?<!\d)(?:\d{2,4}年|\d{1,2}月|\d{1,2}日|\d+(?:\.\d+)?\s*(?:元|块|岁|点|个|次|天|小时|分钟|%|公里|km|kg))")
CITATION = re.compile(r"\[(M-[0-9a-zA-Z_-]+)\]")
TIME_CLAIM = re.compile(r"(今天|昨天|明天|前天|后天|上周|下周|上个月|下个月|去年|明年|刚刚|刚才)")
FORBIDDEN = ("保证收益", "包治", "百分之百治好", "你必须", "你一定", "绝对安全")
#: P27：网络梗/比赛比喻/总结式评价（命中后重写或删分句）
SLANG_RE = re.compile(
    r"赢了|输了|这波|拿捏|绝了|笑死|栓Q|666|开摆|绷不住|"
    r"血压(?:飙升|升高)?|算是.{0,6}(?:赢|输)|只能算")
#: P28：金句腔/伪深刻短评（命中后重写或删分句）
CANNED_RE = re.compile(
    r"不高级.{0,8}耐看|耐看|挺稳|蛮稳|不会出错|默契|收下了|接住了|记下了|存着|"
    r"踏实了|心里舒坦|花在刀刃上|这波不亏|值了|算下来")
#: P28：主动结束话题；用户明确说要走/忙/睡/再见时例外
CLOSING_RE = re.compile(r"那先这样|忙你的去|你先忙|回聊|改天聊|不打扰了")
#: P36：无依据的能力故障（网络/页面/B站）
FALSE_FAILURE_RE = re.compile(
    r"连不上网|网络出不去|网出不去|页面(?:一直)?转圈|刷不了B站|"
    r"打不开(?:网页|网站|页面)|没网|没刷成|刷不成|刷不出来|还是没成")
_NET_CACHE = {"at": 0.0, "failed": False}


def net_probe(now: float | None = None, timeout: float = 2.0) -> bool:
    """真实探测网络（HEAD baidu，2s，5min 缓存）；True=网络失败。"""
    import time as _time
    import urllib.request
    now = _time.time() if now is None else float(now)
    if _NET_CACHE["at"] and now - _NET_CACHE["at"] < 300:
        return bool(_NET_CACHE["failed"])
    failed = False
    try:
        req = urllib.request.Request("https://www.baidu.com", method="HEAD",
                                     headers={"User-Agent": "Mozilla/5.0"})
        urllib.request.urlopen(req, timeout=max(0.2, float(timeout))).close()
    except Exception:  # noqa: BLE001
        failed = True
    _NET_CACHE.update({"at": now, "failed": failed})
    return failed


def false_failure_check(text: Any, now: float | None = None) -> dict:
    value = str(text or "")
    value = re.sub(r"\[\[media:[^\]]*\]\]", "", value)
    hits = list(dict.fromkeys(FALSE_FAILURE_RE.findall(value)))
    if not hits:
        return {"hit": False, "issues": [], "hits": [], "net_failed": False}
    failed = net_probe(now)
    return {"hit": not failed, "issues": ([] if failed else [f"false_failure:{x}" for x in hits]),
            "hits": hits, "net_failed": failed}


def strip_false_failure_clauses(text: Any) -> str:
    value = str(text or "")
    parts = re.split(r"([，,。！!？?；;])", value)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index]
        sep = parts[index + 1] if index + 1 < len(parts) else ""
        if clause and FALSE_FAILURE_RE.search(clause) and not net_probe():
            continue
        if clause or sep:
            kept.append(clause + sep)
    return "".join(kept).strip("，,。；; ")
_CLOSING_ALLOW_RE = re.compile(r"忙|睡|晚安|再见|bye|出门|上班|下班|走")


def slang_check(text: Any) -> dict:
    """返回 {hit, issues}；纯函数，不写 DB。"""
    value = str(text or "")
    # 媒体指令不是正文，尤其 [[media:sticker:666]] 里的 666 不能当网感词
    value = re.sub(r"\[\[media:[^\]]*\]\]", "", value)
    hits = list(dict.fromkeys(SLANG_RE.findall(value)))
    return {"hit": bool(hits), "issues": [f"slang:{x}" for x in hits], "hits": hits}


def strip_slang_clauses(text: Any) -> str:
    """保底：删掉命中禁用词的短句/分句；删空返回空串。"""
    value = str(text or "")
    parts = re.split(r"([，,。！!？?；;])", value)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index]
        sep = parts[index + 1] if index + 1 < len(parts) else ""
        if clause and SLANG_RE.search(clause):
            continue
        if clause or sep:
            kept.append(clause + sep)
    result = "".join(kept).strip("，,。；; ")
    return result


def canned_check(text: Any, user_text: Any = "") -> dict:
    """返回 {hit, issues, canned_hits, closing_hits, closing_allowed}。"""
    value = str(text or "")
    value = re.sub(r"\[\[media:[^\]]*\]\]", "", value)
    canned_hits = list(dict.fromkeys(CANNED_RE.findall(value)))
    allowed = bool(_CLOSING_ALLOW_RE.search(str(user_text or "")))
    closing_hits = [] if allowed else list(dict.fromkeys(CLOSING_RE.findall(value)))
    issues = [f"canned:{x}" for x in canned_hits] + [f"closing:{x}" for x in closing_hits]
    return {"hit": bool(issues), "issues": issues, "canned_hits": canned_hits,
            "closing_hits": closing_hits, "closing_allowed": allowed}


def strip_canned_clauses(text: Any, user_text: Any = "") -> str:
    """保底：删掉金句/不被允许的收尾分句；删空返回空串。"""
    value = str(text or "")
    allowed = bool(_CLOSING_ALLOW_RE.search(str(user_text or "")))
    parts = re.split(r"([，,。！!？?；;])", value)
    kept: list[str] = []
    for index in range(0, len(parts), 2):
        clause = parts[index]
        sep = parts[index + 1] if index + 1 < len(parts) else ""
        bad = bool(CANNED_RE.search(clause)) or (
            not allowed and CLOSING_RE.search(clause))
        if bad:
            continue
        if clause or sep:
            kept.append(clause + sep)
    return "".join(kept).strip("，,。；; ")


def _rewrite(reply: str) -> tuple[str, list[str]]:
    issues = []
    out = reply
    for word, soft in ABSOLUTE.items():
        if word in out:
            issues.append(f"absolute:{word}")
            out = out.replace(word, soft)
    if any(claim in out for claim in TOOL_CLAIMS):
        issues.append("tool_claim_without_tool")
        out = "我不太确定，先不装作查过。" + re.sub(
            "|".join(re.escape(x) for x in TOOL_CLAIMS), "", out)
    if len(out) > 800:
        issues.append("too_long")
        out = out[:800].rstrip() + "…"
    return out, issues


def _record_citation_invalid(path: Path | str, removed: Iterable[str],
                             reply: str) -> None:
    """审计坏引用：raw_events(kind=citation_invalid)；失败不影响主链。"""
    removed_list = [str(item) for item in (removed or [])]
    if not removed_list:
        return
    try:
        now = time.time()
        rid = "RE-cite-" + hashlib.sha1(
            (str(removed_list) + str(now)).encode("utf-8")).hexdigest()[:14]
        payload = {"removed": removed_list, "reply": str(reply or "")[:300]}
        con = db.connect(path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO raw_events(id,source,source_id,friend,"
                "sender,kind,text,ts,meta_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (rid, "selflearn", rid, "", "system", "citation_invalid",
                 str(reply or "")[:500], now,
                 json.dumps(payload, ensure_ascii=False)))
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 - 审计失败不影响回复
        pass


def check(reply: str, path: Path | str = DB_PATH, *, context: str = "",
          citation_ids: Iterable[str] | None = None) -> dict[str, Any]:
    """返回 {level, reply, issues}；L2 用安全回复，不重试。"""
    text = str(reply or "").strip()
    if not text:
        return {"level": "L2", "reply": "我不太确定该怎么说。你愿意再多告诉我一点吗？",
                "issues": ["empty"]}
    rewritten, issues = _rewrite(text)
    # P36：无依据的能力故障（网络正常时）自动清理；真实断网时放行
    ff = false_failure_check(rewritten)
    if ff.get("hit"):
        issues.extend(ff.get("issues") or [])
        cleaned = strip_false_failure_clauses(rewritten)
        if cleaned:
            rewritten = cleaned
        else:
            rewritten = "这个我查不到，先不编原因。"
    ctext = str(context or "")
    if ctext:
        for num in NUMBER.findall(rewritten):
            if num not in ctext:
                issues.append(f"number_not_in_context:{num}")
                break
        for word in TIME_CLAIM.findall(rewritten):
            if word not in ctext:
                issues.append(f"time_not_in_context:{word}")
                break
    for word in FORBIDDEN:
        if word in rewritten:
            issues.append(f"forbidden:{word}")
            rewritten = rewritten.replace(word, "我不能保证")
    # 引用必须对应真实 message/memory；未知/失效引用删掉
    try:
        checked = citation.validate_citations(
            rewritten, path, allowed=citation_ids)
        if not checked.get("ok"):
            issues.append("bad_citation")
            rewritten = checked.get("reply") or rewritten
            if citation.enabled():
                _record_citation_invalid(path, checked.get("removed") or [],
                                         rewritten)
    except Exception:  # noqa: BLE001
        pass
    if any(i.startswith("tool_claim") for i in issues) or len(issues) >= 2:
        return {"level": "L2",
                "reply": "这个我不太确定，先不瞎说。你可以再告诉我一点吗？",
                "issues": issues}
    if issues:
        return {"level": "L1", "reply": rewritten, "issues": issues}
    return {"level": "L0", "reply": text, "issues": []}


def open_conflict_question(path: Path | str = DB_PATH) -> str:
    """把未解决记忆冲突变成一句自然确认，不审问。"""
    from . import memory
    conflicts = memory.open_conflicts(path)
    if not conflicts:
        return ""
    con = db.connect(path)
    try:
        a = con.execute("SELECT content FROM user_memories WHERE id=?",
                        (conflicts[0]["mem_a"],)).fetchone()
        b = con.execute("SELECT content FROM user_memories WHERE id=?",
                        (conflicts[0]["mem_b"],)).fetchone()
    finally:
        con.close()
    if not a or not b:
        return ""
    return f"我好像记得「{a['content']}」，又记得「{b['content']}」，哪个才是现在的？"
