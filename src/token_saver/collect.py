"""Collecteur : parse incrémental des transcripts JSONL de Claude Code vers SQLite.

Chaque transcript (session principale ou sous-agent) est lu à partir de l'offset déjà traité.
Le parser est tolérant : types de lignes inconnus ignorés, champs absents = null.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from .db import delete_file_rows
from .paths import transcript_files

REDUNDANT_WINDOW = 400  # appels d'outils max entre deux lectures pour parler de relecture
PARSER_VERSION = "3"    # à incrémenter quand la sémantique du parser change : force une ré-analyse complète


def _text_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(len(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
    return 0


def _is_meta_text(text: str, line: dict) -> bool:
    if line.get("isMeta") or line.get("isCompactSummary"):
        return True
    t = text.lstrip()
    return t.startswith("<system-reminder>") or t.startswith("<command-") or t.startswith("<local-command")


def _norm_path(p: str | None) -> str | None:
    return p.replace("\\", "/").lower() if p else None


@dataclass
class ParserState:
    """État persistant d'un transcript entre deux collectes (sérialisé dans files.state)."""
    seq: int = 0                       # nombre d'appels API vus
    tool_count: int = 0                # nombre d'appels d'outils vus
    last_key: list = field(default_factory=lambda: [None, None])
    last_model: str | None = None
    pending: dict = field(default_factory=dict)   # chars injectés depuis le dernier appel, par catégorie
    tool_names: dict = field(default_factory=dict)  # tool_use_id -> name (borné)
    tool_args: dict = field(default_factory=dict)   # tool_use_id -> arg (Read path, Agent type)
    reads: dict = field(default_factory=dict)       # path -> [start, num, total, tool_use_id, tool_count]
    last_compact_tool_count: int = -1
    session_id: str | None = None
    agent_id: str | None = None
    cwd: str | None = None
    version: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    title: str | None = None

    def bump(self, cat: str, n: int) -> None:
        if n:
            self.pending[cat] = self.pending.get(cat, 0) + n

    def remember_tool(self, tid: str, name: str, arg: str | None) -> None:
        if len(self.tool_names) > 600:  # borne mémoire : on garde les 300 derniers
            for k in list(self.tool_names)[:300]:
                self.tool_names.pop(k, None)
                self.tool_args.pop(k, None)
        self.tool_names[tid] = name
        if arg:
            self.tool_args[tid] = arg


@dataclass
class Rows:
    calls: list = field(default_factory=list)
    tool_uses: list = field(default_factory=list)
    tool_updates: list = field(default_factory=list)   # (result_chars, is_error, file_path, start, num, total, redundant_of, tool_use_id)
    agents: list = field(default_factory=list)
    events: list = field(default_factory=list)
    turns: list = field(default_factory=list)


class TranscriptParser:
    def __init__(self, file: str, kind: str, st: ParserState):
        self.file, self.kind, self.st, self.rows = file, kind, st, Rows()

    # ----- ligne -----
    def feed(self, d: dict) -> None:
        st = self.st
        t = d.get("type")
        ts = d.get("timestamp")
        if ts:
            st.first_ts = st.first_ts or ts
            st.last_ts = ts
        st.session_id = st.session_id or d.get("sessionId")
        st.agent_id = st.agent_id or d.get("agentId")
        st.cwd = st.cwd or d.get("cwd")
        st.version = st.version or d.get("version")
        if t == "assistant":
            self._assistant(d, ts)
        elif t == "user":
            self._user(d, ts)
        elif t == "system":
            self._system(d, ts)
        elif t in ("custom-title", "ai-title"):
            st.title = d.get("customTitle") or d.get("aiTitle") or st.title

    def _assistant(self, d: dict, ts: str | None) -> None:
        st = self.st
        msg = d.get("message") or {}
        usage = msg.get("usage")
        key = [msg.get("id"), d.get("requestId")]
        if usage and key != st.last_key and key[0]:
            st.seq += 1
            i = usage.get("input_tokens") or 0
            cr = usage.get("cache_read_input_tokens") or 0
            cw = usage.get("cache_creation_input_tokens") or 0
            cc = usage.get("cache_creation") or {}
            out = usage.get("output_tokens") or 0
            model = msg.get("model")
            if st.last_model and model and model != st.last_model and not model.startswith("<"):
                self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "model_switch",
                                         json.dumps({"from": st.last_model, "to": model})))
            if model and not model.startswith("<"):
                st.last_model = model
            self.rows.calls.append((
                self.file, st.session_id, st.agent_id, key[0], key[1], st.seq, ts, model, d.get("effort"),
                usage.get("speed"), i, cr, cw, cc.get("ephemeral_1h_input_tokens") or 0,
                cc.get("ephemeral_5m_input_tokens") or 0, out,
                (usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0, i + cr + cw,
                msg.get("stop_reason"), d.get("attributionSkill"), 1 if d.get("isApiErrorMessage") else 0,
                json.dumps(st.pending) if st.pending else None))
            st.pending = {}
            st.last_key = key
            ql = d.get("quotaLimits")
            if isinstance(ql, dict) and ql.get("status") == "rejected":
                self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "limit_hit",
                                         json.dumps({"type": ql.get("rateLimitType"), "resetsAt": ql.get("resetsAt")})))
            cm = msg.get("context_management") or {}
            if cm.get("applied_edits"):
                self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "tool_clear",
                                         json.dumps(cm.get("applied_edits"))[:2000]))
        elif d.get("isApiErrorMessage"):
            self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "api_error",
                                     json.dumps({"error": str(d.get("error"))[:200]})))
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                st.bump("assistant:text", len(b.get("text", "")))
            elif bt == "tool_use":
                self._tool_use(b, ts)
            # thinking : non renvoyé aux appels suivants -> pas de coût de relecture

    def _tool_use(self, b: dict, ts: str | None) -> None:
        st = self.st
        name = b.get("name") or "?"
        inp = b.get("input") or {}
        raw = json.dumps(inp, ensure_ascii=False)
        st.bump("assistant:write" if name in ("Write", "Edit", "NotebookEdit") else "assistant:tool", len(raw))
        st.tool_count += 1
        arg = None
        extra = None
        if name == "Read":
            arg = inp.get("file_path")
        elif name in ("Edit", "Write", "NotebookEdit"):
            arg = inp.get("file_path")
        elif name in ("Bash", "PowerShell"):
            arg = inp.get("command")
        elif name == "Grep":
            arg = inp.get("pattern")
        elif name == "Glob":
            arg = inp.get("pattern")
        elif name == "Agent":
            arg = inp.get("subagent_type") or "general-purpose"
            extra = json.dumps({"description": (inp.get("description") or "")[:120], "model": inp.get("model"),
                                "prompt_chars": len(inp.get("prompt") or "")})
        elif name == "Skill":
            arg = inp.get("skill")
        else:
            arg = None
        tid = b.get("id") or f"{self.file}:{st.tool_count}"
        st.remember_tool(tid, name, arg if name in ("Read", "Agent") else None)
        self.rows.tool_uses.append((tid, self.file, st.session_id, st.agent_id, st.seq, ts, name,
                                    (arg or "")[:300] or None, len(raw), None, _norm_path(arg) if name == "Read" else None,
                                    None, None, None, 0, None, extra))

    def _user(self, d: dict, ts: str | None) -> None:
        st = self.st
        msg = d.get("message") or {}
        content = msg.get("content")
        tur = d.get("toolUseResult")
        has_tool_result = False
        text_chars = 0
        meta = False
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    has_tool_result = True
                    self._tool_result(b, tur, ts)
                elif b.get("type") == "text":
                    tx = b.get("text", "")
                    if _is_meta_text(tx, d):
                        meta = True
                        st.bump("meta", len(tx))
                    else:
                        text_chars += len(tx)
                        st.bump("user", len(tx))
                elif b.get("type") == "image":
                    st.bump("images", 4000)
        elif isinstance(content, str):
            if _is_meta_text(content, d):
                meta = True
                st.bump("meta", len(content))
            else:
                text_chars = len(content)
                st.bump("user", len(content))
        if d.get("isCompactSummary"):
            self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "compact_summary",
                                     json.dumps({"chars": _text_len(content)})))
            st.last_compact_tool_count = st.tool_count
            st.reads.clear()
        elif text_chars and not has_tool_result and not meta:
            self.rows.turns.append((self.file, st.session_id, st.agent_id, st.seq, ts, text_chars))

    def _tool_result(self, b: dict, tur, ts: str | None) -> None:
        st = self.st
        tid = b.get("tool_use_id")
        name = st.tool_names.get(tid, "?")
        c = b.get("content")
        chars = len(c) if isinstance(c, str) else _text_len(c)
        is_error = 1 if b.get("is_error") else 0
        cat = f"tool:{name}" if not name.startswith("mcp__") else "tool:MCP"
        st.bump(cat, chars)
        file_path = start = num = total = redundant = None
        if name == "Read":
            file_path = _norm_path(st.tool_args.get(tid))
            if isinstance(tur, dict):
                fi = tur.get("file") or {}
                if isinstance(fi, dict):
                    file_path = _norm_path(fi.get("filePath")) or file_path
                    start, num, total = fi.get("startLine"), fi.get("numLines"), fi.get("totalLines")
            if file_path and not is_error:
                prev = st.reads.get(file_path)
                if prev and prev[0] == start and prev[1] == num and prev[2] == total \
                        and prev[4] > st.last_compact_tool_count and st.tool_count - prev[4] <= REDUNDANT_WINDOW:
                    redundant = prev[3]
                st.reads[file_path] = [start, num, total, tid, st.tool_count]
        elif name == "Agent" and isinstance(tur, dict) and tur.get("agentId"):
            self.rows.agents.append((tur.get("agentId"), st.session_id, st.tool_args.get(tid) or "general-purpose",
                                     tur.get("resolvedModel"), (tur.get("description") or "")[:120], tid))
        self.rows.tool_updates.append((chars, is_error, file_path, start, num, total, redundant, tid))

    def _system(self, d: dict, ts: str | None) -> None:
        st = self.st
        sub = d.get("subtype")
        if sub == "compact_boundary":
            cm = d.get("compactMetadata") or {}
            keep = {k: cm.get(k) for k in ("trigger", "preTokens", "postTokens", "durationMs", "cumulativeDroppedTokens")}
            self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "compact", json.dumps(keep)))
            st.last_compact_tool_count = st.tool_count
            st.reads.clear()
        elif sub == "api_error":
            self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "api_error",
                                     json.dumps({"retry": d.get("retryAttempt"), "error": str(d.get("error"))[:200]})))
        elif sub == "stop_hook_summary":
            self.rows.events.append((self.file, st.session_id, st.agent_id, st.seq, ts, "hook_summary",
                                     json.dumps({"count": d.get("hookCount"), "errors": len(d.get("hookErrors") or []),
                                                 "ctx": len(d.get("hookAdditionalContext") or [])})))


# ----- lecture incrémentale -----

def _iter_lines(path: Path, offset: int):
    """Rend (obj, new_offset) pour chaque ligne JSON complète après offset."""
    with open(path, "rb") as f:
        f.seek(offset)
        pos = offset
        for raw in f:
            if not raw.endswith(b"\n"):
                break  # ligne incomplète (écriture en cours) : reprise au prochain passage
            pos += len(raw)
            if len(raw) < 2:
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj, pos


def _flush(con: sqlite3.Connection, rows: Rows) -> None:
    con.executemany("INSERT OR IGNORE INTO calls(file, session_id, agent_id, message_id, request_id, seq, ts, model, effort, "
                    "speed, input, cache_read, cache_write, cw_1h, cw_5m, output, thinking, ctx, stop_reason, skill, is_error, injected) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows.calls)
    con.executemany("INSERT OR IGNORE INTO tool_uses(tool_use_id, file, session_id, agent_id, call_seq, ts, name, arg, input_chars, "
                    "result_chars, file_path, start_line, num_lines, total_lines, is_error, redundant_of, extra) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows.tool_uses)
    con.executemany("UPDATE tool_uses SET result_chars=?, is_error=?, file_path=COALESCE(?, file_path), start_line=?, num_lines=?, "
                    "total_lines=?, redundant_of=? WHERE tool_use_id=?", rows.tool_updates)
    con.executemany("INSERT OR REPLACE INTO agents(agent_id, session_id, type, model, description, parent_tool_use_id) "
                    "VALUES(?,?,?,?,?,?)", rows.agents)
    con.executemany("INSERT INTO events(file, session_id, agent_id, call_seq, ts, kind, data) VALUES(?,?,?,?,?,?,?)", rows.events)
    con.executemany("INSERT INTO user_turns(file, session_id, agent_id, call_seq, ts, chars) VALUES(?,?,?,?,?,?)", rows.turns)


def collect(con: sqlite3.Connection, project: Path, progress=None, ts_logs: bool = True) -> dict:
    """Ingestion incrémentale de tous les transcripts du projet. Retourne des statistiques."""
    t0 = time.time()
    stats = {"files": 0, "files_updated": 0, "lines": 0, "calls": 0, "reset": 0}
    row = con.execute("SELECT value FROM meta WHERE key='parser_version'").fetchone()
    if not row or row[0] != PARSER_VERSION:      # sémantique changée : on repart de zéro
        with con:
            for table in ("calls", "tool_uses", "events", "user_turns", "files", "sessions", "agents"):
                con.execute(f"DELETE FROM {table}")
            con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('parser_version', ?)", (PARSER_VERSION,))
        stats["reset"] = -1  # marqueur : ré-analyse complète
    files = transcript_files(project)
    for path, kind in files:
        stats["files"] += 1
        key = str(path)
        try:
            size, mtime = path.stat().st_size, path.stat().st_mtime
        except OSError:
            continue
        row = con.execute("SELECT size, mtime, offset, state FROM files WHERE path=?", (key,)).fetchone()
        offset = row["offset"] if row else 0
        if row and size == row["size"] and mtime == row["mtime"]:
            continue
        if row and size < offset:            # fichier tronqué/réécrit : on repart de zéro
            delete_file_rows(con, key)
            offset = 0
            row = None
            stats["reset"] += 1
        st = ParserState(**json.loads(row["state"])) if row and row["state"] else ParserState()
        st.last_key = list(st.last_key)
        parser = TranscriptParser(key, kind, st)
        n = 0
        with con:
            for obj, offset in _iter_lines(path, offset):
                parser.feed(obj)
                n += 1
            _flush(con, parser.rows)
            con.execute("INSERT OR REPLACE INTO files(path, kind, session_id, agent_id, size, mtime, offset, state, parsed_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (key, kind, st.session_id, st.agent_id, size, mtime, offset,
                         json.dumps(st.__dict__), time.strftime("%Y-%m-%dT%H:%M:%S")))
            if kind == "main" and st.session_id:
                con.execute("INSERT INTO sessions(session_id, cwd, started_at, last_at, version, title) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(session_id) DO UPDATE SET last_at=excluded.last_at, "
                            "version=COALESCE(excluded.version, version), title=COALESCE(excluded.title, title), "
                            "cwd=COALESCE(cwd, excluded.cwd), started_at=COALESCE(started_at, excluded.started_at)",
                            (st.session_id, st.cwd, st.first_ts, st.last_ts, st.version, st.title))
        stats["files_updated"] += 1
        stats["lines"] += n
        stats["calls"] += len(parser.rows.calls)
        if progress:
            progress(f"{os.path.basename(key)}: {n} lignes")
    stats["ts_logs"] = collect_ts_logs(con, project) if ts_logs else 0
    stats["seconds"] = round(time.time() - t0, 1)
    return stats


def collect_ts_logs(con: sqlite3.Connection, project: Path) -> int:
    """Journaux écrits par les hooks TOKEN SAVER (hook-events.jsonl, savings.jsonl) -> tables, incrémental."""
    from .paths import ts_dir
    n = 0
    metrics = ts_dir(project) / "metrics"
    specs = (("hook-events.jsonl", "hook_events",
              "INSERT INTO hook_events(ts, session_id, transcript, hook, tool, action, latency_ms, chars_before, chars_after, tokens_injected, note) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              ("ts", "session_id", "transcript", "hook", "tool", "action", "latency_ms", "chars_before", "chars_after", "tokens_injected", "note")),
             ("savings.jsonl", "savings",
              "INSERT INTO savings(ts, session_id, source, registry, tokens, token_calls, confidence, method) VALUES(?,?,?,?,?,?,?,?)",
              ("ts", "session_id", "source", "registry", "tokens", "token_calls", "confidence", "method")))
    for fname, kind, sql, cols in specs:
        path = metrics / fname
        if not path.is_file():
            continue
        key = str(path)
        row = con.execute("SELECT size, offset FROM files WHERE path=?", (key,)).fetchone()
        offset = row["offset"] if row else 0
        size = path.stat().st_size
        if row and size == row["size"]:
            continue
        if size < offset:
            con.execute(f"DELETE FROM {kind}")
            offset = 0
        rows = []
        for obj, offset in _iter_lines(path, offset):
            rows.append(tuple(obj.get(c) for c in cols))
        with con:
            con.executemany(sql, rows)
            con.execute("INSERT OR REPLACE INTO files(path, kind, size, mtime, offset, parsed_at) VALUES(?,?,?,?,?,?)",
                        (key, kind, size, path.stat().st_mtime, offset, time.strftime("%Y-%m-%dT%H:%M:%S")))
        n += len(rows)
    return n
