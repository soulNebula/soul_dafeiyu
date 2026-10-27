# -*- coding: utf-8 -*-
"""03 防幻觉：记忆引用（[M-...]）的生成与校验。

prompt 里的每条记忆都要带一个稳定引用，模型只能复制、不能编造；
回复生成后再用 ``validate_citations`` 把库里不存在（或已失效）的引用删掉。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from . import db
from .config import DB_PATH

#: 引用标记；只认 M- 前缀，与 guard.CITATION 保持同一形状。
CITATION = re.compile(r"\[(M-[0-9a-zA-Z_-]+)\]")
EMPTY_MEMORY = "（暂无长期记忆）"


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def citation_id(memory: dict) -> str:
    """一条记忆的稳定引用 ID：优先真实 message_id（通常已是 M- 前缀），否则 memory_id。"""
    message_id = _text(memory.get("evidence_message_id"))
    if message_id:
        return message_id if message_id.startswith("M-") else "M-" + message_id
    memory_id = _text(memory.get("id"))
    if not memory_id:
        return ""
    return memory_id if memory_id.startswith("M-") else "M-" + memory_id


def enabled() -> bool:
    """开关 memory_citation_enabled（默认 true；env 优先，其次 selflearn config/wechat config）。"""
    raw = os.environ.get("MEMORY_CITATION_ENABLED")
    if raw is not None:
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    try:
        from . import config as sl_config
        if hasattr(sl_config, "MEMORY_CITATION_ENABLED"):
            value = getattr(sl_config, "MEMORY_CITATION_ENABLED")
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
    except Exception:  # noqa: BLE001
        pass
    # 运行期 wechat config（与 trace_enabled 等同一份本机配置）
    try:
        config_path = (Path(__file__).resolve().parents[2] / "agi" / "data"
                       / "wechat" / "config.json")
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "memory_citation_enabled" in payload:
            value = payload.get("memory_citation_enabled")
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
    except Exception:  # noqa: BLE001
        pass
    return True


def short_citation_id(memory: dict, length: int = 8) -> str:
    """一条记忆的**短**稳定引用：``M-`` + sha1(真实 id) 前 8 位。

    - 用 memory 表 id（没有则 evidence_message_id）做 hash，稳定且不会泄露完整 id；
    - 同一输入重复调用结果一致；无 id 返回空串（该条不注入引用）。
    """
    source = _text((memory or {}).get("id")) or _text(
        (memory or {}).get("evidence_message_id"))
    if not source:
        return ""
    digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:max(4, int(length))]
    return "M-" + digest


def injected_ids(mems: Iterable[dict] | None, limit: int = 10) -> set[str]:
    """真正注入 prompt 的记忆短引用集合（≤limit 条，给 guard 校验用）。"""
    ids: set[str] = set()
    for memory in list(mems or [])[:max(0, int(limit))]:
        if not isinstance(memory, dict):
            continue
        cid = short_citation_id(memory)
        if cid:
            ids.add(cid)
    return ids


def inject_lines(lines: Iterable[str], mems: Iterable[dict] | None,
                 allowed: set[str] | None = None) -> list[str]:
    """把 ``[M-xxxx]`` 插到 memory.render_memories 的每行开头（``- `` 之后）。

    - 只注入 ``allowed`` 里的短引用；没有 id/不在 allowed 的行原样返回；
    - lines 与 mems 一一对应；长度不一致时只处理共同前缀。
    """
    line_list = list(lines or [])
    mem_list = list(mems or [])
    out: list[str] = []
    for index, line in enumerate(line_list):
        memory = mem_list[index] if index < len(mem_list) else None
        cid = short_citation_id(memory) if isinstance(memory, dict) else ""
        if not cid or (allowed is not None and cid not in allowed):
            out.append(line)
            continue
        if line.startswith("- "):
            out.append("- [" + cid + "] " + line[2:])
        else:
            out.append("[" + cid + "] " + line)
    return out


def format_memory_context(mems: Iterable[dict] | None) -> str:
    """把检索到的记忆渲染成带引用的 prompt 片段。

    每行形如 ``- [M-<message_id|memory_id>] [user/fact] 内容（依据：原话）``。
    引用只由记忆行本身决定，同一输入重复调用结果一致；没有 id 的条目不给引用，
    只作背景，避免模型凭空造出引用。
    """
    lines: list[str] = []
    for memory in mems or []:
        if not isinstance(memory, dict):
            continue
        content = _text(memory.get("content"))
        if not content:
            continue
        cite = citation_id(memory)
        line = f"- [{cite}]" if cite else "- （不可引用）"
        label = "/".join(
            part for part in (_text(memory.get("scope")), _text(memory.get("kind"))) if part
        )
        if label:
            line += f" [{label}]"
        line += f" {content}"
        evidence = _text(memory.get("evidence"))
        if evidence and evidence != content:
            line += f"（依据：{evidence}）"
        lines.append(line)
    return "\n".join(lines) or EMPTY_MEMORY


def _resolvable(ids: list[str], path: Path | str,
                allowed: Iterable[str] | None = None) -> set[str]:
    """返回 ids 中在库里有真实来源的引用：message 存在，或 memory 存在且 active。

    ``allowed``：本次实际注入 prompt 的短引用（``M-<hash8>``）；命中即视为有效
    （短 hash 无法直接按库 id 反查，必须由调用方提供本次注入集合）。
    """
    if not ids:
        return set()
    allowed_set = set()
    for item in allowed or []:
        value = _text(item)
        if value:
            allowed_set.add(value if value.startswith("M-") else "M-" + value)
    valid: set[str] = {cid for cid in ids if cid in allowed_set}
    marks = ",".join("?" for _ in ids)
    con = db.connect(path)
    try:
        for row in con.execute(f"SELECT id FROM messages WHERE id IN ({marks})", ids):
            valid.add(str(row["id"]))
        for row in con.execute(
                f"SELECT id FROM user_memories WHERE status='active' AND id IN ({marks})", ids):
            valid.add(str(row["id"]))
        # 形如 [M-UM-xxx] 的记忆引用：去掉 M- 后匹配 memory.id
        payloads = sorted({cid[2:] for cid in ids if cid.startswith("M-") and len(cid) > 2})
        if payloads:
            pmarks = ",".join("?" for _ in payloads)
            for row in con.execute(
                    f"SELECT id FROM user_memories WHERE status='active' AND id IN ({pmarks})",
                    payloads):
                valid.add("M-" + str(row["id"]))
    finally:
        con.close()
    return valid


def validate_citations(reply: str, path: Path | str = DB_PATH,
                       allowed: Iterable[str] | None = None) -> dict[str, Any]:
    """删掉回复里指向不存在 / 已失效 message 或 memory 的 ``[M-...]`` 引用。

    返回 ``{"ok": bool, "reply": str, "removed": list[str]}``：

    - ``ok``：没有未知引用（回复里没有引用也算 ok：纯情绪回应不需要引用）；
    - ``reply``：清理后的回复。只删引用标记本身、不删整句，是否降级由调用方决定；
    - ``removed``：按首次出现顺序去重后的未知引用 id（不含方括号，如 ``"M-nope"``）。

    重复引用不算错误：同一个合法引用出现多次会原样保留；同一个坏引用出现多次
    也只报告一次。没有需要删除的引用时，回复逐字不变。
    """
    text = str(reply or "")
    ids: list[str] = []
    for cid in CITATION.findall(text):
        if cid not in ids:
            ids.append(cid)
    if not ids:
        return {"ok": True, "reply": text, "removed": []}
    valid = _resolvable(ids, path, allowed)
    removed = [cid for cid in ids if cid not in valid]
    out = text
    if removed:
        for cid in removed:
            out = out.replace(f"[{cid}]", "")
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+(?=\n)", "", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
        out = out.strip()
    return {"ok": not removed, "reply": out, "removed": removed}
