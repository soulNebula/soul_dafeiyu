# -*- coding: utf-8 -*-
"""S8 检索：SQLite FTS5 + 中文 bigram；FTS5 不可用时降级为 bigram 扫描。

存储：
- ``search_documents``：原文，``(source_type, source_id)`` 唯一，重复索引只更新；
- ``search_fts``：FTS5 虚表，只存预切分好的 token 串，rowid 与文档 id 对齐；
- ``search_meta``：记录 FTS 是否可用，降级后 ``search`` 直接走扫描。

切分：
- 中文按 bigram（两字窗口）切分；文档额外保留单字，便于单字查询；
- ASCII 词整体保留（小写化），不拆 bigram；
- 查询与索引使用同一套切分规则。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from pathlib import Path

DOCS_TABLE = "search_documents"
FTS_TABLE = "search_fts"
META_TABLE = "search_meta"

_DOCS_SQL = """
CREATE TABLE IF NOT EXISTS search_documents(
  id INTEGER PRIMARY KEY,
  source_type TEXT NOT NULL,
  source_id TEXT NOT NULL,
  text TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(source_type, source_id)
);
CREATE INDEX IF NOT EXISTS ix_search_documents_type
  ON search_documents(source_type);
CREATE TABLE IF NOT EXISTS search_meta(
  key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""

_FTS_SQL = "CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(tokens)"

# CJK 基本区/扩展 A/兼容区/扩展 B；ASCII 词允许内部出现 + # . _ -（如 C++、v1.2）
_CJK = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002a6df"
_TOKEN_RE = re.compile(rf"([{_CJK}]+)|([A-Za-z0-9][A-Za-z0-9_+#.\-]*)")
_TRIM = ".-"


def _connect(path: Path | str) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=5.0)
    con.row_factory = sqlite3.Row
    return con


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def _is_fts_table(con: sqlite3.Connection) -> bool:
    """确认 search_fts 是 FTS5 虚表，而不是恰好同名的普通表。"""
    row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)
    ).fetchone()
    sql = str(row["sql"] or "") if row is not None else ""
    return "fts5" in sql.lower()


def _iter_parts(text):
    for match in _TOKEN_RE.finditer(str(text or "")):
        cjk = match.group(1)
        if cjk:
            yield "cjk", cjk
        else:
            word = match.group(2).lower().rstrip(_TRIM)
            if word:
                yield "ascii", word


def _doc_tokens(text: str) -> list[str]:
    """文档 token：中文单字 + bigram，ASCII 词原样保留。"""
    out: list[str] = []
    for kind, value in _iter_parts(text):
        if kind == "ascii":
            out.append(value)
        else:
            out.extend(value)
            out.extend(value[i:i + 2] for i in range(len(value) - 1))
    return out


def _query_tokens(text: str) -> list[str]:
    """查询 token：中文 bigram（单字查询保留单字），ASCII 词原样保留。"""
    out: list[str] = []
    for kind, value in _iter_parts(text):
        if kind == "ascii" or len(value) == 1:
            out.append(value)
        else:
            out.extend(value[i:i + 2] for i in range(len(value) - 1))
    return out


def _tokens_join(text: str) -> str:
    return " ".join(_doc_tokens(text))


def _set_meta(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO search_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _fts_disabled(con: sqlite3.Connection) -> bool:
    if not _table_exists(con, META_TABLE):
        return False
    row = con.execute("SELECT value FROM search_meta WHERE key='fts_ok'").fetchone()
    return row is not None and str(row["value"]) == "0"


def _try_create_fts(con: sqlite3.Connection) -> bool:
    """独立成函数，便于测试模拟“没有 FTS5”。"""
    try:
        con.execute(_FTS_SQL)
        con.commit()
        return True
    except sqlite3.Error:
        return False


def _rebuild_fts(con: sqlite3.Connection) -> None:
    con.execute(f"DELETE FROM {FTS_TABLE}")
    rows = con.execute(
        f"SELECT id, text FROM {DOCS_TABLE} ORDER BY id"
    ).fetchall()
    con.executemany(
        f"INSERT INTO {FTS_TABLE}(rowid, tokens) VALUES(?,?)",
        [(int(r["id"]), _tokens_join(r["text"])) for r in rows],
    )


def _ensure_fts(con: sqlite3.Connection) -> bool:
    """确保 FTS 表可用；不可用（或同名表结构不对）时返回 False 并记 meta。"""
    if _table_exists(con, FTS_TABLE) and not _is_fts_table(con):
        _set_meta(con, "fts_ok", "0")
        con.commit()
        return False
    if not _table_exists(con, FTS_TABLE):
        if not _try_create_fts(con):
            _set_meta(con, "fts_ok", "0")
            con.commit()
            return False
        try:
            _rebuild_fts(con)
        except sqlite3.Error:
            _set_meta(con, "fts_ok", "0")
            con.commit()
            return False
    _set_meta(con, "fts_ok", "1")
    con.commit()
    return True


def _heal_fts(con: sqlite3.Connection) -> None:
    """文档数与 FTS 行数不一致时全量重建，修复半途失败留下的空洞。"""
    doc_count = int(con.execute(f"SELECT count(*) FROM {DOCS_TABLE}").fetchone()[0])
    fts_count = int(con.execute(f"SELECT count(*) FROM {FTS_TABLE}").fetchone()[0])
    if doc_count != fts_count:
        _rebuild_fts(con)


def _clean_item(item) -> tuple[str, str, str] | None:
    if not isinstance(item, dict):
        return None
    text = item.get("text")
    if text is None:
        text = item.get("content")
    if text is None:
        text = item.get("body")
    text = str(text or "").strip()
    if not text:
        return None
    source_type = str(item.get("source_type") or item.get("type") or "text").strip()
    if not source_type:
        source_type = "text"
    source_id = item.get("source_id")
    if source_id is None:
        source_id = item.get("id")
    source_id = str(source_id).strip() if source_id is not None else ""
    if not source_id:
        # 没给主键时用内容指纹，保证重复调用仍然幂等
        source_id = "auto-" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]
    return source_type, source_id, text


def _upsert_doc(con: sqlite3.Connection, source_type: str, source_id: str,
                text: str, now: float) -> int:
    row = con.execute(
        f"SELECT id, text FROM {DOCS_TABLE} WHERE source_type=? AND source_id=?",
        (source_type, source_id),
    ).fetchone()
    if row is None:
        cur = con.execute(
            f"INSERT INTO {DOCS_TABLE}(source_type,source_id,text,updated_at) "
            "VALUES(?,?,?,?)",
            (source_type, source_id, text, now),
        )
        return int(cur.lastrowid)
    doc_id = int(row["id"])
    if str(row["text"]) != text:
        con.execute(
            f"UPDATE {DOCS_TABLE} SET text=?, updated_at=? WHERE id=?",
            (text, now, doc_id),
        )
    return doc_id


def index_texts(path: Path | str, items: list[dict]) -> int:
    """建立/更新 FTS 索引，返回本次写入的文档数（按 source+id 去重）。

    可重复调用：相同 ``(source_type, source_id)`` 只更新原文和 token，不追加行。
    FTS5 不可用时仍然把原文写入 ``search_documents``，供 ``search`` 扫描降级。
    """
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(db_path)
    try:
        con.executescript(_DOCS_SQL)
        docs: dict[tuple[str, str], str] = {}
        for item in items or []:
            parsed = _clean_item(item)
            if parsed is not None:
                docs[(parsed[0], parsed[1])] = parsed[2]
        if not docs:
            return 0

        fts_ok = _ensure_fts(con)
        now = time.time()
        written = 0
        for (source_type, source_id), text in docs.items():
            doc_id = _upsert_doc(con, source_type, source_id, text, now)
            written += 1
            if not fts_ok:
                continue
            try:
                con.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid=?", (doc_id,))
                con.execute(
                    f"INSERT INTO {FTS_TABLE}(rowid, tokens) VALUES(?,?)",
                    (doc_id, _tokens_join(text)),
                )
            except sqlite3.Error:
                # 单条写入失败就整体降级，搜索仍然能扫描原文
                fts_ok = False
                _set_meta(con, "fts_ok", "0")

        if fts_ok:
            try:
                _heal_fts(con)
                _set_meta(con, "fts_ok", "1")
            except sqlite3.Error:
                _set_meta(con, "fts_ok", "0")
        con.commit()
        return written
    finally:
        con.close()


def _match_expr(tokens: list[str], joiner: str) -> str:
    quoted = []
    for token in tokens:
        quoted.append('"' + token.replace('"', '""') + '"')
    return joiner.join(quoted)


def _fts_search(con: sqlite3.Connection, tokens: list[str], limit: int) -> list[dict]:
    # 先 AND（精确），无结果再 OR（召回），避免中文标点把连续短语切断后查不到
    passes = (" AND ",) if len(tokens) == 1 else (" AND ", " OR ")
    for joiner in passes:
        match = _match_expr(tokens, joiner)
        rows = con.execute(
            f"SELECT d.source_type, d.source_id, d.text, bm25({FTS_TABLE}) AS rank "
            f"FROM {FTS_TABLE} JOIN {DOCS_TABLE} d ON d.id={FTS_TABLE}.rowid "
            f"WHERE {FTS_TABLE} MATCH ? "
            "ORDER BY rank ASC, d.id ASC LIMIT ?",
            (match, int(limit)),
        ).fetchall()
        if rows:
            return [
                {
                    "source_type": str(r["source_type"]),
                    "source_id": str(r["source_id"]),
                    "text": str(r["text"]),
                    "score": round(-float(r["rank"]), 9),
                }
                for r in rows
            ]
    return []


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").lower(), flags=re.UNICODE)


def _scan_search(con: sqlite3.Connection, tokens: list[str], query: str,
                 limit: int) -> list[dict]:
    query_set = set(tokens)
    if not query_set:
        return []
    query_norm = _norm(query)
    rows = con.execute(
        f"SELECT id, source_type, source_id, text FROM {DOCS_TABLE} ORDER BY id"
    ).fetchall()
    scored: list[tuple[float, sqlite3.Row]] = []
    for row in rows:
        doc_set = set(_doc_tokens(row["text"]))
        overlap = len(query_set & doc_set)
        if not overlap:
            continue
        score = overlap / len(query_set)
        if query_norm and query_norm in _norm(row["text"]):
            score += 0.5
        scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], int(pair[1]["id"])))
    return [
        {
            "source_type": str(row["source_type"]),
            "source_id": str(row["source_id"]),
            "text": str(row["text"]),
            "score": round(float(score), 6),
        }
        for score, row in scored[:int(limit)]
    ]


def search(path: Path | str, query: str, limit: int = 8) -> list[dict]:
    """检索，按相关度降序返回 ``source_type/source_id/text/score``。

    FTS5 不可用、虚表不存在或 MATCH 报错时自动降级为 bigram 扫描；
    空查询、无索引、DB 不存在都返回空列表，不抛异常。
    """
    if limit is None:
        limit = 8
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 8
    if limit <= 0:
        return []
    tokens = list(dict.fromkeys(_query_tokens(query)))
    if not tokens:
        return []

    db_path = Path(path)
    if not db_path.exists() or db_path.is_dir():
        return []
    try:
        con = _connect(db_path)
    except (sqlite3.Error, OSError):
        return []
    try:
        if not _table_exists(con, DOCS_TABLE):
            return []
        if not _fts_disabled(con) and _is_fts_table(con):
            try:
                return _fts_search(con, tokens, limit)
            except sqlite3.Error:
                pass  # 虚表损坏/查询语法异常：退回扫描
        return _scan_search(con, tokens, str(query or ""), limit)
    except sqlite3.Error:
        return []
    finally:
        con.close()


def delete(path: Path | str, source_type: str, source_id: str | None = None) -> int:
    """删除检索文档及其 FTS 行；找不到表时返回 0，不抛异常。"""
    con = _connect(path)
    try:
        if not _table_exists(con, DOCS_TABLE):
            return 0
        if source_id is None:
            rows = con.execute("SELECT id FROM search_documents WHERE source_type=?",
                               (str(source_type),)).fetchall()
        else:
            rows = con.execute("SELECT id FROM search_documents WHERE source_type=? AND source_id=?",
                               (str(source_type), str(source_id))).fetchall()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return 0
        if _table_exists(con, FTS_TABLE):
            con.executemany("DELETE FROM search_fts WHERE rowid=?", [(i,) for i in ids])
        con.executemany("DELETE FROM search_documents WHERE id=?", [(i,) for i in ids])
        con.commit()
        return len(ids)
    except sqlite3.Error:
        return 0
    finally:
        con.close()
