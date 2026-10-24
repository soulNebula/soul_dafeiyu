"""L2「能查」：判定要不要联网、跑工具循环、把资料注入提示词并留痕。

设计（对应路线图 M2/M3）：
1. ``should_lookup`` 只做**便宜的前置判定**（问句/时效词/明确要求查），避免每句话都联网；
2. ``research`` 交给 ``agent.run_agent`` 做多步工具循环（search → fetch → 汇总）；
3. 资料以「可引用上下文」返回，由 ``chat`` 注入到人格提示词里——**最终仍由人格层写回复**，
   这样不会为了联网牺牲说话风格；
4. ``record_sources`` 把查过的 URL/标题落到 ``research_sources`` 表，供后续核对与引用。

刻意保持纯逻辑 + 少量 SQLite 写入；不 import ``wx_core``（网关注释里写明要独立于 wx_core）。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path

from . import agent as agent_mod
from ..textguard import strip_internal_ids

#: 明确要查/查时效信息时直接触发（不含「今天/现在」这类太常见的词，
#: 那几个只在「问句 + 时效」组合分支里用，避免「今天有点累」也去联网）
LOOKUP_HINTS = (
    "查一下", "查查", "帮我查", "搜一下", "搜搜", "百度", "谷歌", "bing",
    "最新", "近期", "新闻", "热搜", "价格", "多少钱", "汇率", "天气", "气温",
    "比分", "排名", "股价", "油价", "哪一年", "哪年", "什么时候", "多久",
    "上映", "发布", "官网", "网址", "链接", "查证", "核实", "有没有这回事",
)

#: 纯私人的话术：出现这些时即使像问句也不联网（省钱、也更像真人）
_PERSONAL = (
    "你", "我", "咱", "我们", "心情", "感觉", "喜欢", "讨厌", "累", "开心", "难过",
    "吃饭", "睡觉", "想你", "干嘛", "在吗", "陪我", "抱抱",
)

#: 身份类问题不能当联网问题（"你是谁"里也有"是谁"）
_SELF_KEYS = ("你是谁", "我是谁", "你是ai", "你是 AI", "真人吗")

#: 允许的时间敏感句式（配合问句一起判断）
_TIME_SENSITIVE = ("今年", "现在", "今天", "最近", "最新", "刚", "目前", "这几天", "这几天")

_QUESTION = re.compile(r"[?？]|吗[\s。！!]*$|呢[\s。！!]*$|怎么|如何|为什么|啥|什么|多少|几点|哪")

#: 资料注入上限（字符）
MAX_CONTEXT_CHARS = 1600


def _mode() -> str:
    """auto（默认）/ always / off，可用环境变量覆盖，便于测试与运维。"""
    return str(os.environ.get("SELFLEARN_LOOKUP", "auto") or "auto").strip().lower()


def should_lookup(text: str) -> bool:
    """是否需要先联网查一下再回答（便宜的启发式，宁可漏查也不要乱查）。"""
    mode = _mode()
    if mode in ("off", "0", "false", "no"):
        return False
    raw = strip_internal_ids(str(text or ""))
    if not raw.strip() or len(raw) > 300:
        return False
    if mode == "always":
        return True
    if any(k in raw for k in _SELF_KEYS):
        return False
    if any(h in raw for h in LOOKUP_HINTS):
        return True
    # 问句 + 时效词，且不是纯私人寒暄
    if _QUESTION.search(raw) and any(t in raw for t in _TIME_SENSITIVE):
        if not any(p in raw for p in _PERSONAL):
            return True
    return False


def _context_from(result: dict) -> str:
    """把 agent 的结果整理成可注入的「资料」块（含来源）。"""
    reply = strip_internal_ids(str(result.get("reply") or "")).strip()
    sources = result.get("sources") or []
    lines = []
    if reply:
        lines.append(reply[:MAX_CONTEXT_CHARS])
    if sources:
        lines.append("来源：")
        for src in sources[:5]:
            title = str(src.get("title") or "").strip()
            url = str(src.get("url") or "").strip()
            if not url:
                continue
            lines.append(f"- {title} {url}".strip())
    return "\n".join(lines)[:MAX_CONTEXT_CHARS]


def research(client, text: str, *, db_path: Path | str | None = None,
             max_steps: int = 2, max_tool_calls: int = 3,
             deadline_s: float = 45.0) -> dict:
    """跑一次工具循环，返回 ``{"used", "context", "sources", "error"}``。

    任何异常都不外抛（联网失败不能让聊天挂掉），只返回 used=False。
    """
    out = {"used": False, "context": "", "sources": [], "error": ""}
    if not should_lookup(text):
        return out
    if client is None or not getattr(client, "available", lambda: False)():
        out["error"] = "client 不可用"
        return out
    try:
        result = agent_mod.run_agent(
            client, text, db_path=db_path, max_steps=max_steps,
            max_tool_calls=max_tool_calls, deadline_s=deadline_s,
        )
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return out
    if not result.get("ok"):
        out["error"] = str(result.get("error") or "工具循环失败")[:200]
        return out
    context = _context_from(result)
    if not context.strip():
        out["error"] = "工具循环没有产出"
        return out
    out.update({"used": True, "context": context,
                "sources": result.get("sources") or [],
                "tool_calls": int(result.get("tool_calls") or 0),
                "steps": result.get("steps") or []})
    return out


# ---------------------------------------------------------------- 留痕
_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_sources(
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  query TEXT NOT NULL,
  url TEXT NOT NULL,
  title TEXT DEFAULT '',
  snippet TEXT DEFAULT '',
  turn_id TEXT DEFAULT ''
);
"""


def record_sources(db_path: Path | str, query: str, research: dict,
                   *, turn_id: str = "") -> int:
    """把本次查到的来源落库；返回写入条数。失败静默（不影响回复）。"""
    sources = (research or {}).get("sources") or []
    if not db_path or not sources:
        return 0
    try:
        con = sqlite3.connect(str(db_path))
        try:
            con.executescript(_SCHEMA)
            now = time.time()
            n = 0
            seen: set[str] = set()
            for src in sources[:10]:
                url = str(src.get("url") or "").strip()
                if not url or url in seen:
                    continue
                seen.add(url)
                rid = "RS-" + hashlib.md5(url.encode("utf-8", "replace")).hexdigest()[:16]
                try:
                    con.execute(
                        "INSERT OR REPLACE INTO research_sources"
                        "(id, ts, query, url, title, snippet, turn_id) VALUES(?,?,?,?,?,?,?)",
                        (rid, now, str(query or "")[:200], url,
                         str(src.get("title") or "")[:200],
                         str(src.get("snippet") or "")[:500], str(turn_id or "")),
                    )
                    n += 1
                except sqlite3.Error:
                    continue
            con.commit()
            return n
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return 0
