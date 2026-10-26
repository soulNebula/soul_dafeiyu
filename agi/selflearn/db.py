# -*- coding: utf-8 -*-
"""selflearn 独立 SQLite。用户记忆与 Agent 自人格分表。"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL,
  proactive INTEGER NOT NULL DEFAULT 0,
  -- P1-d：>90 天原文冷归档标记（只标记，不删不改写；旧库 connect() 幂等 ALTER）
  archived_at REAL
);
CREATE TABLE IF NOT EXISTS user_memories(
  id TEXT PRIMARY KEY, scope TEXT NOT NULL DEFAULT 'user', kind TEXT NOT NULL,
  content TEXT NOT NULL, evidence TEXT NOT NULL,
  evidence_message_id TEXT, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  conflict_group TEXT, created_at REAL NOT NULL,
  -- P1-a：记忆重要性 / 访问统计 / 历史证据（旧库由 connect() 幂等 ALTER 补齐）
  importance REAL NOT NULL DEFAULT 0.5,
  access_count INTEGER NOT NULL DEFAULT 0,
  last_accessed REAL NOT NULL DEFAULT 0,
  evidence_history TEXT NOT NULL DEFAULT '',
  UNIQUE(content)
);
CREATE TABLE IF NOT EXISTS emoji_archive(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '[]', usage_count INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS emoji_usage(
  id TEXT PRIMARY KEY, emoji_id TEXT NOT NULL, message_id TEXT,
  context TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS score_records(
  id TEXT PRIMARY KEY, turn_id TEXT, bucket TEXT NOT NULL,
  raw REAL NOT NULL, score REAL NOT NULL, breakdown_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS weak_rechecks(
  id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, variant_id TEXT,
  passed INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS wake_runs(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
  detail_json TEXT NOT NULL DEFAULT '{}', started_at REAL NOT NULL,
  finished_at REAL
);

CREATE TABLE IF NOT EXISTS share_candidates(
  id TEXT PRIMARY KEY, level TEXT NOT NULL, kind TEXT NOT NULL,
  text TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '', source_id TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0.0, status TEXT NOT NULL DEFAULT 'candidate',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS share_events(
  id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, action TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS goals(
  id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  priority REAL NOT NULL DEFAULT 0.5, progress REAL NOT NULL DEFAULT 0.0,
  parent_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS learning_proposals(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
  payload_json TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft', created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_events(
  id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT, friend TEXT,
  sender TEXT, kind TEXT, text TEXT, ts REAL NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, friend TEXT NOT NULL, start_ts REAL NOT NULL,
  end_ts REAL NOT NULL, message_count INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS session_segments(
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL, seq INTEGER NOT NULL,
  start_ts REAL NOT NULL, end_ts REAL NOT NULL, text TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS media_understanding(
  id TEXT PRIMARY KEY, attachment_id TEXT, path TEXT NOT NULL,
  caption TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
  emotion TEXT NOT NULL DEFAULT 'neutral', confidence REAL NOT NULL DEFAULT 0.6,
  status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_profile(
  id TEXT PRIMARY KEY, dimension TEXT NOT NULL, value TEXT NOT NULL,
  evidence_json TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 0.6,
  status TEXT NOT NULL DEFAULT 'active', updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS claims(
  id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
  object TEXT NOT NULL, polarity INTEGER NOT NULL DEFAULT 1,
  time_ref TEXT NOT NULL DEFAULT '', claim_type TEXT NOT NULL,
  evidence_span TEXT NOT NULL, evidence_message_id TEXT,
  confidence REAL NOT NULL DEFAULT 0.6, source_level TEXT NOT NULL DEFAULT 'A',
  status TEXT NOT NULL DEFAULT 'active', conflict_group TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_claims_status ON claims(status,claim_type);

CREATE TABLE IF NOT EXISTS skill_nodes(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'locked', provenance TEXT NOT NULL DEFAULT '[]',
  practice_log TEXT NOT NULL DEFAULT '[]', fail_count INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS learn_tasks(
  id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, goal TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS skill_cards(
  id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, content TEXT NOT NULL,
  example TEXT NOT NULL, provenance_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS practice_runs(
  id TEXT PRIMARY KEY, task_id TEXT NOT NULL, attempt TEXT NOT NULL,
  passed INTEGER NOT NULL, error TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS summaries(
  id TEXT PRIMARY KEY, content TEXT NOT NULL, evidence_json TEXT NOT NULL,
  turns_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
  -- P1-c/P1-d：阶段摘要 / 月度 gist 标记（旧库 connect() 幂等 ALTER）
  kind TEXT NOT NULL DEFAULT 'stage',
  year_month TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'llm'
);
CREATE TABLE IF NOT EXISTS attachments(
  id TEXT PRIMARY KEY, message_id TEXT, kind TEXT NOT NULL, mime TEXT NOT NULL,
  path TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS conflicts(
  id TEXT PRIMARY KEY, mem_a TEXT NOT NULL, mem_b TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', resolution TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns(
  id TEXT PRIMARY KEY, user_message_id TEXT NOT NULL, assistant_message_id TEXT NOT NULL,
  reply TEXT NOT NULL, skill_id TEXT, variant_id TEXT, feedback TEXT, score REAL,
  latency_ms REAL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pairs(
  id TEXT PRIMARY KEY,
  turn_a TEXT NOT NULL, turn_b TEXT NOT NULL,
  reply_a TEXT NOT NULL, reply_b TEXT NOT NULL,
  model_a TEXT, model_b TEXT,
  choice TEXT,                 -- a | b | tie
  note TEXT NOT NULL DEFAULT '',
  rater TEXT NOT NULL DEFAULT 'user',
  context_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inner_states(
  id TEXT PRIMARY KEY,
  turn_id TEXT,
  mode TEXT NOT NULL,          -- rule | model | shadow
  state_json TEXT NOT NULL,
  block TEXT NOT NULL DEFAULT '',
  injected INTEGER NOT NULL DEFAULT 0,
  -- P3-a：内状态可观测性（本地小模型延迟 / 回退原因 / 实际来源 local|rule|static）
  latency_ms REAL,
  fallback TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
-- P15-A：每条回复的完整推理过程（只读采集；memory 域）。字段均按 trace.py 截断后写入。
CREATE TABLE IF NOT EXISTS turn_traces(
  id TEXT PRIMARY KEY,
  ts REAL NOT NULL,
  contact TEXT NOT NULL DEFAULT '',
  user_text TEXT NOT NULL DEFAULT '',
  mode TEXT NOT NULL DEFAULT 'chat',   -- chat | proactive
  system_prompt TEXT NOT NULL DEFAULT '',
  prompt_user TEXT NOT NULL DEFAULT '',
  memories_json TEXT NOT NULL DEFAULT '[]',
  inner_json TEXT NOT NULL DEFAULT '{}',
  mood_json TEXT NOT NULL DEFAULT '{}',
  generation_json TEXT NOT NULL DEFAULT '{}',
  polish_json TEXT NOT NULL DEFAULT '{}',
  guard_json TEXT NOT NULL DEFAULT '{}',
  final_reply TEXT NOT NULL DEFAULT '',
  skipped INTEGER NOT NULL DEFAULT 0,
  skip_reason TEXT NOT NULL DEFAULT '',
  crisis INTEGER NOT NULL DEFAULT 0,
  latency_ms REAL,
  proactive_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_turn_traces_ts ON turn_traces(ts);
CREATE INDEX IF NOT EXISTS ix_turn_traces_mode ON turn_traces(mode, ts);
CREATE TABLE IF NOT EXISTS feedback(
  id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, reaction TEXT NOT NULL, score REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS persona_layers(
  id TEXT PRIMARY KEY, layer TEXT NOT NULL, key TEXT NOT NULL,
  value TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
  evidence_json TEXT NOT NULL DEFAULT '[]', version INTEGER NOT NULL DEFAULT 1,
  approved_by TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS persona_changelog(
  id TEXT PRIMARY KEY, key TEXT NOT NULL, action TEXT NOT NULL,
  old_value TEXT NOT NULL DEFAULT '', new_value TEXT NOT NULL DEFAULT '',
  layer TEXT NOT NULL, created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS persona(
  id TEXT PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL, evidence_json TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'active', updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS skills(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, level INTEGER DEFAULT 1,
  alpha REAL DEFAULT 1, beta REAL DEFAULT 1, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS variants(
  id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, hint TEXT NOT NULL,
  alpha REAL DEFAULT 1, beta REAL DEFAULT 1, status TEXT DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS persona_candidates(
  id TEXT PRIMARY KEY, key TEXT NOT NULL, value TEXT NOT NULL, evidence_json TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'candidate',
  updated_at REAL NOT NULL, UNIQUE(key,value)
);
CREATE TABLE IF NOT EXISTS reflections(
  id TEXT PRIMARY KEY, summary TEXT NOT NULL, evidence_turns TEXT NOT NULL, created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trust_events(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, delta REAL NOT NULL, reason TEXT NOT NULL,
  turn_id TEXT, resolved INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS errors(
  id TEXT PRIMARY KEY, turn_id TEXT, category TEXT NOT NULL, detail TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lessons(
  id TEXT PRIMARY KEY, category TEXT NOT NULL, lesson TEXT NOT NULL,
  evidence TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS weak_checks(
  id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, variant_id TEXT, passed INTEGER NOT NULL,
  note TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS proactive(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, reason TEXT NOT NULL,
  score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'proposed', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS intents(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, text TEXT NOT NULL, wake_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS state(
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL
);

-- P9：心情判定（移植「鱼的心情」）。mood=0-100 基调；7 情绪 dims 0-100。
-- mood_state 单行（id='self'）：当前心情 + 上次判定/注入时间；mood_timeline 每次有效判定一行。
CREATE TABLE IF NOT EXISTS mood_state(
  id TEXT PRIMARY KEY, mood REAL NOT NULL DEFAULT 70,
  dims_json TEXT NOT NULL DEFAULT '{}',
  last_decay_at REAL NOT NULL DEFAULT 0,
  last_reason TEXT NOT NULL DEFAULT '',
  last_confidence REAL NOT NULL DEFAULT 0,
  last_mode TEXT NOT NULL DEFAULT '',
  last_at REAL NOT NULL DEFAULT 0,
  last_injected_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mood_timeline(
  id TEXT PRIMARY KEY, at REAL NOT NULL, mood REAL NOT NULL,
  dims_delta_json TEXT NOT NULL DEFAULT '{}',
  reason TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'user',
  user_text TEXT NOT NULL DEFAULT '', reply_text TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# P7-e：记忆库 / 人格库拆分（透明路由 + 旧库迁移 + 可回滚）
# --------------------------------------------------------------------------- #
logger = logging.getLogger("agi.selflearn.db")

MEMORY_TABLES = frozenset({
    "messages", "user_memories", "claims", "summaries", "attachments",
    "media_understanding", "turns", "feedback", "pairs", "inner_states",
    "raw_events", "sessions", "session_segments", "emoji_archive", "emoji_usage",
    "user_profile", "score_records", "weak_rechecks", "conflicts",
    "search_documents", "search_fts", "search_meta", "research_sources",
    "bili_impressions",
    # P15-A：推理过程 trace（每条回复的上下文/生成/守卫等，memory 域）
    "turn_traces",
})
PERSONA_TABLES = frozenset({
    "persona", "persona_candidates", "persona_layers", "persona_changelog",
    "skills", "skill_nodes", "skill_cards", "variants", "learn_tasks",
    "practice_runs", "reflections", "lessons", "errors", "weak_checks",
    "trust_events", "state", "goals", "intents", "proactive",
    "share_candidates", "share_events", "wake_runs", "learning_proposals",
    # P9：心情判定状态 / 时间线（属于「人格库」：它自己的心情）
    "mood_state", "mood_timeline",
    "interests", "interest_events",
})
TABLE_DOMAIN = {name: "memory" for name in MEMORY_TABLES}
TABLE_DOMAIN.update({name: "persona" for name in PERSONA_TABLES})
_FTS_TABLES = frozenset({"search_fts"})
_KNOWN_TABLES = frozenset(TABLE_DOMAIN)

_EXTRA_MEMORY_SCHEMA = """
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
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(tokens);
CREATE TABLE IF NOT EXISTS research_sources(
  id TEXT PRIMARY KEY, ts REAL NOT NULL, query TEXT NOT NULL, url TEXT NOT NULL,
  title TEXT DEFAULT '', snippet TEXT DEFAULT '', turn_id TEXT DEFAULT ''
);
"""


class CrossDomainQuery(RuntimeError):
    """同一条 SQL 同时引用 memory/persona 两个域。"""


def _split_schema(schema: str) -> tuple[str, str]:
    statements = re.findall(r"CREATE\s+(?:TABLE|INDEX).*?;", schema, re.S | re.I)
    memory: list[str] = []
    persona: list[str] = []
    for statement in statements:
        table_match = re.search(
            r"CREATE\s+(?:TABLE|INDEX)\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_]\w*)",
            statement, re.I)
        name = table_match.group(1) if table_match else ""
        if re.match(r"CREATE\s+INDEX", statement, re.I):
            on_match = re.search(r"\bON\s+([A-Za-z_]\w*)", statement, re.I)
            name = on_match.group(1) if on_match else name
        domain = TABLE_DOMAIN.get(name, "memory")
        (persona if domain == "persona" else memory).append(statement)
    return "\n".join(memory) + "\n", "\n".join(persona) + "\n"


MEMORY_SCHEMA, PERSONA_SCHEMA = _split_schema(SCHEMA)
PERSONA_SCHEMA += """
CREATE TABLE IF NOT EXISTS interests(
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL UNIQUE,
  weight REAL NOT NULL DEFAULT 0.3,
  source TEXT NOT NULL DEFAULT 'chat',
  evidence_json TEXT NOT NULL DEFAULT '[]',
  hits INTEGER NOT NULL DEFAULT 0,
  last_seen_at REAL NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS interest_events(
  id TEXT PRIMARY KEY,
  topic TEXT NOT NULL,
  delta REAL NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  ref_json TEXT NOT NULL DEFAULT '{}',
  at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_interests_weight ON interests(weight);
CREATE INDEX IF NOT EXISTS ix_interest_events_topic ON interest_events(topic);
"""
MEMORY_SCHEMA += """
CREATE TABLE IF NOT EXISTS bili_impressions(
  id TEXT PRIMARY KEY,
  bvid TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  owner TEXT NOT NULL DEFAULT '',
  owner_mid TEXT NOT NULL DEFAULT '',
  duration_s REAL NOT NULL DEFAULT 0,
  cover_url TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  topics_json TEXT NOT NULL DEFAULT '[]',
  emotion TEXT NOT NULL DEFAULT '',
  value REAL NOT NULL DEFAULT 0,
  why TEXT NOT NULL DEFAULT '',
  my_take TEXT NOT NULL DEFAULT '',
  share_line TEXT NOT NULL DEFAULT '',
  subtitle_len INTEGER NOT NULL DEFAULT 0,
  subtitle_missing INTEGER NOT NULL DEFAULT 0,
  comments_json TEXT NOT NULL DEFAULT '[]',
  danmaku_json TEXT NOT NULL DEFAULT '[]',
  meta_json TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  shared INTEGER NOT NULL DEFAULT 0,
  shared_at REAL
);
CREATE INDEX IF NOT EXISTS ix_bili_impressions_created ON bili_impressions(created_at);
"""
# 注意：FTS/search_documents 由 search/tool_reply 按需创建（memory 域），
# 不放进核心 schema，避免 connect() 每次把 drop 掉的 FTS 表重建。


def _base_path(path: Path | str = DB_PATH) -> Path:
    text = str(Path(path))
    for suffix in (".memory.sqlite3", ".persona.sqlite3", ".legacy.sqlite3"):
        if text.endswith(suffix):
            return Path(text[: -len(suffix)])
    return Path(path)


def _with_suffix(path: Path | str, suffix: str) -> Path:
    text = str(_base_path(path))
    if text.endswith(".sqlite3"):
        text = text[:-len(".sqlite3")]
    return Path(text + suffix)


def memory_path(path: Path | str = DB_PATH) -> Path:
    return _with_suffix(path, ".memory.sqlite3")


def persona_path(path: Path | str = DB_PATH) -> Path:
    return _with_suffix(path, ".persona.sqlite3")


def legacy_path(path: Path | str = DB_PATH) -> Path:
    return _with_suffix(path, ".legacy.sqlite3")


_TABLE_PATTERNS = (
    re.compile(r"\bFROM\s+([`\"\[]?[A-Za-z_][\w.]*)", re.I),
    re.compile(r"\bJOIN\s+([`\"\[]?[A-Za-z_][\w.]*)", re.I),
    re.compile(r"\bINTO\s+([`\"\[]?[A-Za-z_][\w.]*)", re.I),
    re.compile(r"\bTABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\"\[]?[A-Za-z_][\w.]*)", re.I),
    re.compile(r"\bUPDATE\s+(?!SET\b)([`\"\[]?[A-Za-z_][\w.]*)", re.I),
    re.compile(r"PRAGMA\s+table_info\s*\(\s*([`\"\[]?[A-Za-z_][\w.]*)", re.I),
)
_route_warned: set[str] = set()


def _clean_identifier(raw: str) -> str:
    value = str(raw or "").strip().strip("`\"[]")
    if "." in value:
        value = value.rsplit(".", 1)[-1]
    return value.strip("`\"[]")


def _tables_in_sql(sql: str) -> set[str]:
    found: set[str] = set()
    for pattern in _TABLE_PATTERNS:
        for match in pattern.finditer(str(sql or "")):
            name = _clean_identifier(match.group(1))
            if name:
                found.add(name)
    return found


def _domains_in_sql(sql: str) -> set[str]:
    domains: set[str] = set()
    for name in _tables_in_sql(sql):
        domain = TABLE_DOMAIN.get(name)
        if domain is None:
            domain = "memory"
            if name not in _route_warned:
                _route_warned.add(name)
                logger.warning("db_route_unknown table=%s sql=%s", name, str(sql)[:200])
        domains.add(domain)
    return domains


def _ensure_columns(con: sqlite3.Connection, persona: sqlite3.Connection) -> None:
    cols = {r[1] for r in con.execute("PRAGMA table_info(user_memories)").fetchall()}
    if "scope" not in cols:
        con.execute("ALTER TABLE user_memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'user'")
    if "conflict_group" not in cols:
        con.execute("ALTER TABLE user_memories ADD COLUMN conflict_group TEXT")
    added: list[str] = []
    for col, ddl in (
        ("importance", "importance REAL NOT NULL DEFAULT 0.5"),
        ("access_count", "access_count INTEGER NOT NULL DEFAULT 0"),
        ("last_accessed", "last_accessed REAL NOT NULL DEFAULT 0"),
        ("evidence_history", "evidence_history TEXT NOT NULL DEFAULT ''"),
    ):
        if col not in cols:
            con.execute(f"ALTER TABLE user_memories ADD COLUMN {ddl}")
            added.append(col)
    if "importance" in added:
        con.execute(
            "UPDATE user_memories SET importance = CASE lower(kind) "
            "WHEN 'value' THEN 0.8 WHEN 'plan' THEN 0.8 WHEN 'preference' THEN 0.7 "
            "WHEN 'insight' THEN 0.6 WHEN 'fact' THEN 0.5 WHEN 'event' THEN 0.4 "
            "ELSE 0.5 END")
    if "evidence_history" in added:
        for row in con.execute(
                "SELECT id, evidence FROM user_memories WHERE evidence_history=''").fetchall():
            snippet = re.sub(r"\s+", " ", str(row[1] or "")).strip()[:120]
            history = json.dumps([snippet], ensure_ascii=False) if snippet else "[]"
            con.execute("UPDATE user_memories SET evidence_history=? WHERE id=?",
                        (history, row[0]))
    con.execute("CREATE INDEX IF NOT EXISTS ix_user_memories_kind "
                "ON user_memories(status, scope, kind)")
    ecols = {r[1] for r in persona.execute("PRAGMA table_info(errors)").fetchall()}
    for col, ddl in (("root_cause", "root_cause TEXT NOT NULL DEFAULT ''"),
                     ("fix_verified", "fix_verified INTEGER NOT NULL DEFAULT 0"),
                     ("hit_count", "hit_count INTEGER NOT NULL DEFAULT 1")):
        if col not in ecols:
            persona.execute(f"ALTER TABLE errors ADD COLUMN {ddl}")
    persona.commit()
    mcols = {r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()}
    if "proactive" not in mcols:
        con.execute("ALTER TABLE messages ADD COLUMN proactive INTEGER NOT NULL DEFAULT 0")
    if "archived_at" not in mcols:
        con.execute("ALTER TABLE messages ADD COLUMN archived_at REAL")
    con.execute("CREATE INDEX IF NOT EXISTS ix_messages_archived ON messages(archived_at)")
    tcols = {r[1] for r in con.execute("PRAGMA table_info(turns)").fetchall()}
    if "latency_ms" not in tcols:
        con.execute("ALTER TABLE turns ADD COLUMN latency_ms REAL")
    icols = {r[1] for r in con.execute("PRAGMA table_info(inner_states)").fetchall()}
    for col, ddl in (("latency_ms", "latency_ms REAL"),
                     ("fallback", "fallback TEXT NOT NULL DEFAULT ''"),
                     ("source", "source TEXT NOT NULL DEFAULT ''")):
        if col not in icols:
            con.execute(f"ALTER TABLE inner_states ADD COLUMN {ddl}")
    scols = {r[1] for r in con.execute("PRAGMA table_info(summaries)").fetchall()}
    for col, ddl in (("kind", "kind TEXT NOT NULL DEFAULT 'stage'"),
                     ("year_month", "year_month TEXT NOT NULL DEFAULT ''"),
                     ("source", "source TEXT NOT NULL DEFAULT 'llm'")):
        if col not in scols:
            con.execute(f"ALTER TABLE summaries ADD COLUMN {ddl}")
    con.commit()


def _schema_con(path: Path, schema: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(schema)
    con.commit()
    return con


def _copy_legacy_tables(source: sqlite3.Connection, target: sqlite3.Connection,
                        tables: list[str]) -> tuple[int, int]:
    total_tables = total_rows = 0
    for table in tables:
        # FTS5 shadow 表由虚表自动维护，不能逐行拷贝（否则会破坏索引）
        if table.startswith("search_fts_"):
            continue
        try:
            info = source.execute(f"PRAGMA table_info({table})").fetchall()
            columns = [str(row[1]) for row in info]
            if not columns:
                continue
            table_sql_row = source.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            table_sql = str(table_sql_row[0] or "") if table_sql_row else ""
            if table_sql:
                try:
                    target.execute(table_sql)
                except sqlite3.Error:
                    pass
            target.execute(f"INSERT OR REPLACE INTO {table}({', '.join(columns)}) "
                           f"SELECT {', '.join(columns)} FROM legacy.{table}")
            total_tables += 1
            total_rows += len(columns)
        except sqlite3.Error as exc:
            logger.warning("db_split_migrate_table_failed table=%s error=%s", table, exc)
    return total_tables, total_rows


def _migrate_legacy(base: Path) -> None:
    text = str(base)
    old = base if text.endswith(".sqlite3") else Path(text + ".sqlite3")
    if not old.exists() or memory_path(base).exists() or persona_path(base).exists():
        return
    logger.info("db_split_migration_started old=%s", old)
    mem = _schema_con(memory_path(base), MEMORY_SCHEMA)
    per = _schema_con(persona_path(base), PERSONA_SCHEMA)
    # errors 的额外诊断列在旧 connect 里是 ALTER 补的；迁移前先补，否则拷贝旧列失败
    ecols = {r[1] for r in per.execute("PRAGMA table_info(errors)").fetchall()}
    for col, ddl in (("root_cause", "root_cause TEXT NOT NULL DEFAULT ''"),
                     ("fix_verified", "fix_verified INTEGER NOT NULL DEFAULT 0"),
                     ("hit_count", "hit_count INTEGER NOT NULL DEFAULT 1")):
        if col not in ecols:
            per.execute(f"ALTER TABLE errors ADD COLUMN {ddl}")
    per.commit()
    legacy = sqlite3.connect(old)
    legacy.row_factory = sqlite3.Row
    mem.execute("ATTACH DATABASE ? AS legacy", (str(old),))
    per.execute("ATTACH DATABASE ? AS legacy", (str(old),))
    try:
        rows = legacy.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()
        table_names = [str(row[0]) for row in rows]
        for name in table_names:
            domain = TABLE_DOMAIN.get(name, "memory")
            target = per if domain == "persona" else mem
            _copy_legacy_tables(legacy, target, [name])
        for row in legacy.execute(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('index','trigger') "
                "AND sql IS NOT NULL").fetchall():
            sql = str(row[1] or "")
            domains = _domains_in_sql(sql)
            target = per if domains == {"persona"} else mem
            try:
                target.execute(sql)
            except sqlite3.Error as exc:
                logger.warning("db_split_migrate_ddl_failed sql=%s error=%s", sql[:200], exc)
        # 旧库缺列时：迁移到带默认值的新表后按 kind 回填（与旧 connect 行为一致）
        try:
            old_cols = {r[1] for r in legacy.execute(
                "PRAGMA table_info(user_memories)").fetchall()}
        except sqlite3.Error:
            old_cols = set()
        if old_cols:
            if "importance" not in old_cols:
                mem.execute(
                    "UPDATE user_memories SET importance = CASE lower(kind) "
                    "WHEN 'value' THEN 0.8 WHEN 'plan' THEN 0.8 "
                    "WHEN 'preference' THEN 0.7 WHEN 'insight' THEN 0.6 "
                    "WHEN 'fact' THEN 0.5 WHEN 'event' THEN 0.4 ELSE 0.5 END")
            if "evidence_history" not in old_cols:
                for row in mem.execute(
                        "SELECT id, evidence FROM user_memories "
                        "WHERE evidence_history=''").fetchall():
                    snippet = re.sub(r"\s+", " ", str(row[1] or "")).strip()[:120]
                    history = json.dumps([snippet], ensure_ascii=False) if snippet else "[]"
                    mem.execute("UPDATE user_memories SET evidence_history=? WHERE id=?",
                                (history, row[0]))
        mem.commit()
        per.commit()
    finally:
        legacy.close()
        mem.close()
        per.close()
    migrated = legacy_path(base)
    try:
        if migrated.exists():
            logger.warning("db_split_legacy_exists path=%s", migrated)
        else:
            old.replace(migrated)
    except OSError as exc:
        logger.warning("db_split_legacy_rename_failed old=%s error=%s", old, exc)
    logger.info("db_split_migrated old=%s legacy=%s memory=%s persona=%s",
                old.name, migrated.name, memory_path(base).name, persona_path(base).name)


class RouterConnection:
    """按 SQL 中的表名透明路由到 memory/persona 两个连接。"""

    def __init__(self, memory: sqlite3.Connection, persona: sqlite3.Connection,
                 *, base_path: Path | str = ""):
        self._memory = memory
        self._persona = persona
        self.base_path = str(base_path)
        self.memory = memory
        self.persona = persona

    @property
    def row_factory(self):
        return self._memory.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._memory.row_factory = value
        self._persona.row_factory = value

    @property
    def in_transaction(self) -> bool:
        return self._memory.in_transaction or self._persona.in_transaction

    def _target(self, sql: str) -> sqlite3.Connection:
        domains = _domains_in_sql(sql)
        if len(domains) > 1:
            message = ("CrossDomainQuery: SQL touches both memory and persona; "
                       f"sql={sql}")
            print(message)
            raise CrossDomainQuery(message)
        return self._persona if domains == {"persona"} else self._memory

    def execute(self, sql: str, parameters=()):
        return self._target(sql).execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters):
        return self._target(sql).executemany(sql, seq_of_parameters)

    def executescript(self, statements: str):
        domains = _domains_in_sql(statements)
        if len(domains) > 1:
            message = ("CrossDomainQuery: executescript touches both domains; "
                       f"sql={statements[:500]}")
            print(message)
            raise CrossDomainQuery(message)
        target = self._persona if domains == {"persona"} else self._memory
        return target.executescript(statements)

    def commit(self) -> None:
        self._memory.commit()
        self._persona.commit()

    def rollback(self) -> None:
        self._memory.rollback()
        self._persona.rollback()

    def close(self) -> None:
        self._memory.close()
        self._persona.close()

    def __getattr__(self, name):
        return getattr(self._memory, name)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def connect(path: Path | str = DB_PATH) -> RouterConnection:
    if Path(path).exists() and Path(path).is_dir():
        raise sqlite3.OperationalError(f"DB 路径是目录：{path}")
    base = _base_path(path)
    _migrate_legacy(base)
    mem = _schema_con(memory_path(base), MEMORY_SCHEMA)
    per = _schema_con(persona_path(base), PERSONA_SCHEMA)
    _ensure_columns(mem, per)
    return RouterConnection(mem, per, base_path=base)


def memory_connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    base = _base_path(path)
    _migrate_legacy(base)
    if not memory_path(base).exists():
        connect(base)
    con = sqlite3.connect(memory_path(base))
    con.row_factory = sqlite3.Row
    return con


def persona_connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    base = _base_path(path)
    _migrate_legacy(base)
    if not persona_path(base).exists():
        connect(base)
    con = sqlite3.connect(persona_path(base))
    con.row_factory = sqlite3.Row
    return con


def counts(path: Path | str = DB_PATH) -> dict[str, int]:
    con = connect(path)
    try:
        names = ["messages", "user_memories", "turns", "feedback", "persona",
                 "persona_candidates", "skills", "variants", "reflections",
                 "trust_events", "errors", "lessons", "weak_checks", "proactive",
                 "intents", "conflicts", "attachments", "summaries", "skill_nodes",
                 "learn_tasks", "claims", "skill_cards", "practice_runs", "raw_events",
                 "sessions", "session_segments", "media_understanding", "user_profile",
                 "emoji_archive", "emoji_usage", "score_records", "learning_proposals",
                 "weak_rechecks", "wake_runs", "goals", "persona_layers",
                 "persona_changelog", "share_candidates", "share_events", "pairs",
                 "inner_states", "mood_state", "mood_timeline", "turn_traces"]
        return {name: int(con.execute(f"SELECT count(*) FROM {name}").fetchone()[0])
                for name in names}
    finally:
        con.close()
