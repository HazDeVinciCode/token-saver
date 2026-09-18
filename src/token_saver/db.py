"""Schéma SQLite du cache de métriques (usage.sqlite)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, kind TEXT, session_id TEXT, agent_id TEXT,
  size INTEGER, mtime REAL, offset INTEGER DEFAULT 0, state TEXT, parsed_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, cwd TEXT, started_at TEXT, last_at TEXT, version TEXT, title TEXT);
CREATE TABLE IF NOT EXISTS calls(
  id INTEGER PRIMARY KEY, file TEXT, session_id TEXT, agent_id TEXT,
  message_id TEXT, request_id TEXT, seq INTEGER, ts TEXT, model TEXT, effort TEXT, speed TEXT,
  input INTEGER, cache_read INTEGER, cache_write INTEGER, cw_1h INTEGER, cw_5m INTEGER,
  output INTEGER, thinking INTEGER, ctx INTEGER, stop_reason TEXT, skill TEXT, is_error INTEGER,
  injected TEXT, UNIQUE(message_id, request_id));
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);
CREATE INDEX IF NOT EXISTS calls_file_seq ON calls(file, seq);
CREATE INDEX IF NOT EXISTS calls_session ON calls(session_id);
CREATE TABLE IF NOT EXISTS tool_uses(
  tool_use_id TEXT PRIMARY KEY, file TEXT, session_id TEXT, agent_id TEXT, call_seq INTEGER, ts TEXT,
  name TEXT, arg TEXT, input_chars INTEGER, result_chars INTEGER, file_path TEXT,
  start_line INTEGER, num_lines INTEGER, total_lines INTEGER, is_error INTEGER, redundant_of TEXT, extra TEXT);
CREATE INDEX IF NOT EXISTS tool_uses_ts ON tool_uses(ts);
CREATE INDEX IF NOT EXISTS tool_uses_name ON tool_uses(name);
CREATE INDEX IF NOT EXISTS tool_uses_path ON tool_uses(file_path);
CREATE TABLE IF NOT EXISTS agents(
  agent_id TEXT PRIMARY KEY, session_id TEXT, type TEXT, model TEXT, description TEXT, parent_tool_use_id TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, file TEXT, session_id TEXT, agent_id TEXT, call_seq INTEGER, ts TEXT, kind TEXT, data TEXT);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts);
CREATE TABLE IF NOT EXISTS user_turns(
  id INTEGER PRIMARY KEY, file TEXT, session_id TEXT, agent_id TEXT, call_seq INTEGER, ts TEXT, chars INTEGER);
CREATE INDEX IF NOT EXISTS user_turns_file ON user_turns(file, call_seq);
CREATE TABLE IF NOT EXISTS savings(
  id INTEGER PRIMARY KEY, ts TEXT, session_id TEXT, source TEXT, registry TEXT,
  tokens INTEGER, token_calls INTEGER, confidence TEXT, method TEXT);
CREATE TABLE IF NOT EXISTS hook_events(
  id INTEGER PRIMARY KEY, ts TEXT, session_id TEXT, transcript TEXT, hook TEXT, tool TEXT,
  action TEXT, latency_ms REAL, chars_before INTEGER, chars_after INTEGER, tokens_injected INTEGER, note TEXT);
"""


def connect(path: Path | None) -> sqlite3.Connection:
    """path=None -> base en mémoire (projet non instrumenté)."""
    if path is None:
        con = sqlite3.connect(":memory:")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(path))
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA temp_store=MEMORY")
    con.executescript(SCHEMA)
    con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    con.row_factory = sqlite3.Row
    return con


def delete_file_rows(con: sqlite3.Connection, file: str) -> None:
    for table in ("calls", "tool_uses", "events", "user_turns"):
        con.execute(f"DELETE FROM {table} WHERE file=?", (file,))
