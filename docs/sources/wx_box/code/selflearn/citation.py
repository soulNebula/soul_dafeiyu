# -*- coding: utf-8 -*-
"""03 防幻觉：记忆引用（[M-...]）的生成与校验。

prompt 里的每条记忆都要带一个稳定引用，模型只能复制、不能编造；
回复生成后再用 ``validate_citations`` 把库里不存在（或已失效）的引用删掉。
"""
from __future__ import annotations

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


def _resolvable(ids: list[str], path: Path | str) -> set[str]:
    """返回 ids 中在库里有真实来源的引用：message 存在，或 memory 存在且 active。"""
    if not ids:
        return set()
    marks = ",".join("?" for _ in ids)
    valid: set[str] = set()
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


def validate_citations(reply: str, path: Path | str = DB_PATH) -> dict[str, Any]:
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
    valid = _resolvable(ids, path)
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
