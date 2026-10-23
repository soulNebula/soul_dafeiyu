# -*- coding: utf-8 -*-
"""selflearn 独立 SQLite。用户记忆与 Agent 自人格分表。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, role TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL,
  proactive INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS user_memories(
  id TEXT PRIMARY KEY, scope TEXT NOT NULL DEFAULT 'user', kind TEXT NOT NULL,
  content TEXT NOT NULL, evidence TEXT NOT NULL,
  evidence_message_id TEXT, confidence REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  conflict_group TEXT, created_at REAL NOT NULL, UNIQUE(content)
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
  turns_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL
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
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS feedback(
  id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, reaction TEXT NOT NULL, score REAL NOT NULL,
  note TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
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
"""


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # 兼容早期 selflearn 文件：补 scope 列
    cols = {r[1] for r in con.execute("PRAGMA table_info(user_memories)").fetchall()}
    if "scope" not in cols:
        con.execute("ALTER TABLE user_memories ADD COLUMN scope TEXT NOT NULL DEFAULT 'user'")
    if "conflict_group" not in cols:
        con.execute("ALTER TABLE user_memories ADD COLUMN conflict_group TEXT")
    mcols = {r[1] for r in con.execute("PRAGMA table_info(messages)").fetchall()}
    if "proactive" not in mcols:
        con.execute("ALTER TABLE messages ADD COLUMN proactive INTEGER NOT NULL DEFAULT 0")
    con.commit()
    return con


def counts(path: Path | str = DB_PATH) -> dict[str, int]:
    con = connect(path)
    try:
        names = ["messages", "user_memories", "turns", "feedback", "persona", "persona_candidates", "skills", "variants", "reflections", "trust_events", "errors", "lessons", "weak_checks", "proactive", "intents", "conflicts", "attachments", "summaries", "skill_nodes", "learn_tasks", "claims", "skill_cards", "practice_runs", "raw_events", "sessions", "session_segments", "media_understanding", "user_profile", "emoji_archive", "emoji_usage", "score_records", "learning_proposals", "weak_rechecks", "wake_runs"]
        return {name: int(con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]) for name in names}
    finally:
        con.close()
