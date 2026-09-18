"""`token-saver find` : recherche locale par sections dans les docs du projet (SQLite FTS5, sans processus résident).

Index : <projet>/.token-saver/metrics/docs.sqlite. Unité = section Markdown (titre + texte jusqu'au titre
suivant), découpée si > max_section_tokens. Rafraîchissement paresseux à chaque requête (stat des fichiers).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from .config import load_config
from .paths import ts_dir

HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
STOP = {"le", "la", "les", "de", "des", "du", "un", "une", "et", "ou", "en", "au", "aux", "the", "a", "an", "of", "to", "in",
        "and", "or", "for", "on", "with", "pour", "dans", "sur", "par", "avec", "est", "ce", "cette", "ces", "se", "sa", "son", "ses"}


def db_path(project: Path) -> Path:
    return ts_dir(project) / "metrics" / "docs.sqlite"


def _connect(project: Path) -> sqlite3.Connection:
    p = db_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p))
    con.executescript("""
    CREATE TABLE IF NOT EXISTS docs(path TEXT PRIMARY KEY, mtime REAL, size INTEGER, sections INTEGER, indexed_at TEXT);
    CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(path UNINDEXED, title, crumbs, body, start_line UNINDEXED,
        end_line UNINDEXED, tokens UNINDEXED, tokenize='unicode61 remove_diacritics 2 tokenchars ''_-''');
    """)
    return con


def _candidates(project: Path, cfg: dict) -> list[Path]:
    pats = cfg["find"]["paths"]
    excl = set(cfg["find"]["exclude"])
    out: set[Path] = set()
    for pat in pats:
        for p in project.glob(pat):
            if p.is_file() and p.suffix.lower() in (".md", ".txt", ".rst") and not (set(p.relative_to(project).parts[:-1]) & excl):
                out.add(p)
    return sorted(out)


def _split_sections(text: str, max_tokens: int) -> list[dict]:
    """Découpe un Markdown en sections (titre, fil d'Ariane, corps, lignes)."""
    lines = text.splitlines()
    sections: list[dict] = []
    stack: list[tuple[int, str]] = []
    cur = {"title": "(début)", "crumbs": "", "start": 1, "lines": []}
    in_code = False

    def close(end_line: int) -> None:
        body = "\n".join(cur["lines"]).strip()
        if body or cur["title"] != "(début)":
            sections.append({"title": cur["title"], "crumbs": cur["crumbs"], "body": body, "start": cur["start"], "end": end_line})

    for i, line in enumerate(lines, 1):
        if line.strip().startswith("```"):
            in_code = not in_code
        m = HEADING.match(line) if not in_code else None
        if m:
            close(i - 1)
            level, title = len(m.group(1)), m.group(2).strip("# ")
            while stack and stack[-1][0] >= level:
                stack.pop()
            crumbs = " > ".join(t for _, t in stack)
            stack.append((level, title))
            cur = {"title": title, "crumbs": crumbs, "start": i, "lines": []}
        else:
            cur["lines"].append(line)
    close(len(lines))
    # découpage des sections trop longues (par paragraphes)
    out: list[dict] = []
    limit = max_tokens * 4
    for s in sections:
        if len(s["body"]) <= limit:
            out.append(s)
            continue
        paras, buf, start, n = s["body"].split("\n\n"), [], s["start"], 1
        line_cursor = s["start"]
        for para in paras:
            if buf and sum(len(b) + 2 for b in buf) + len(para) > limit:
                chunk = "\n\n".join(buf)
                end = line_cursor - 1
                out.append({**s, "title": f"{s['title']} ({n})", "body": chunk, "start": start, "end": end})
                n += 1
                buf, start = [], line_cursor
            buf.append(para)
            line_cursor += para.count("\n") + 2
        if buf:
            out.append({**s, "title": f"{s['title']} ({n})" if n > 1 else s["title"], "body": "\n\n".join(buf), "start": start, "end": s["end"]})
    return out


def refresh(project: Path, cfg: dict | None = None, rebuild: bool = False) -> dict:
    """(Ré)indexe les fichiers nouveaux/modifiés ; retire les disparus. Retourne des statistiques."""
    cfg = cfg or load_config(project)
    con = _connect(project)
    stats = {"files": 0, "indexed": 0, "removed": 0, "sections": 0}
    known = {r[0]: (r[1], r[2]) for r in con.execute("SELECT path, mtime, size FROM docs")}
    seen = set()
    t0 = time.time()
    with con:
        if rebuild:
            con.execute("DELETE FROM docs")
            con.execute("DELETE FROM sections")
            known = {}
        for f in _candidates(project, cfg):
            rel = str(f.relative_to(project)).replace("\\", "/")
            seen.add(rel)
            stats["files"] += 1
            st = f.stat()
            if rel in known and known[rel] == (st.st_mtime, st.st_size):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            secs = _split_sections(text, cfg["find"]["max_section_tokens"])
            con.execute("DELETE FROM sections WHERE path=?", (rel,))
            con.executemany("INSERT INTO sections(path, title, crumbs, body, start_line, end_line, tokens) VALUES(?,?,?,?,?,?,?)",
                            [(rel, s["title"], s["crumbs"], s["body"], s["start"], s["end"], len(s["body"]) // 4) for s in secs])
            con.execute("INSERT OR REPLACE INTO docs(path, mtime, size, sections, indexed_at) VALUES(?,?,?,?,?)",
                        (rel, st.st_mtime, st.st_size, len(secs), time.strftime("%Y-%m-%dT%H:%M:%S")))
            stats["indexed"] += 1
            stats["sections"] += len(secs)
        for rel in set(known) - seen:
            con.execute("DELETE FROM sections WHERE path=?", (rel,))
            con.execute("DELETE FROM docs WHERE path=?", (rel,))
            stats["removed"] += 1
    stats["seconds"] = round(time.time() - t0, 2)
    stats["total_sections"] = con.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    con.close()
    return stats


def _fts_query(q: str) -> str:
    words = [w for w in re.findall(r"[\w\-]+", q.lower()) if w not in STOP and len(w) > 1]
    if not words:
        return ""
    terms = [f'"{w}"*' if len(w) >= 4 else f'"{w}"' for w in words]
    return " OR ".join(terms)


def search(project: Path, query: str, top: int = 3, max_tokens: int = 2000, within: str | None = None, cfg: dict | None = None) -> str:
    cfg = cfg or load_config(project)
    refresh(project, cfg)
    con = _connect(project)
    fq = _fts_query(query)
    if not fq:
        return "find: requête vide"
    sql = ("SELECT path, title, crumbs, body, start_line, end_line, tokens, bm25(sections, 0, 6.0, 2.0, 1.0) AS score "
           "FROM sections WHERE sections MATCH ?")
    args: list = [fq]
    if within:
        sql += " AND path LIKE ?"
        args.append(f"%{within.replace(chr(92), '/')}%")
    sql += " ORDER BY score LIMIT ?"
    args.append(top * 3)
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.OperationalError as exc:
        return f"find: requête invalide ({exc})"
    words = set(re.findall(r"[\w\-]+", query.lower())) - STOP
    # bonus : nombre de termes distincts présents (évite qu'un seul mot fréquent domine)
    scored = []
    for r in rows:
        text = f"{r[1]} {r[2]} {r[3]}".lower()
        hits = sum(1 for w in words if w in text)
        scored.append((r[7] - 2.0 * hits, r))
    scored.sort(key=lambda x: x[0])
    out, budget, n = [], max_tokens * 4, 0
    for _, r in scored:
        if n >= top:
            break
        path, title, crumbs, body, s, e = r[0], r[1], r[2], r[3], r[4], r[5]
        header = f"== {path}:{s}-{e} — {crumbs + ' > ' if crumbs else ''}{title}"
        if len(body) > budget:
            cut = body.rfind("\n", 0, max(0, budget))
            body = body[: cut if cut > 200 else budget].rstrip() + f"\n[… suite : Read {path} --offset {s} --limit {e - s + 1}]"
        out.append(header + "\n" + body)
        budget -= len(body)
        n += 1
        if budget <= 200:
            break
    con.close()
    result = "\n\n".join(out) if out else f"find: aucune section pour « {query} » (index : {db_path(project).name})"
    _log(project, query, n, len(result) // 4)
    return result


def _log(project: Path, query: str, hits: int, tokens: int) -> None:
    try:
        p = ts_dir(project) / "metrics" / "find-log.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "query": query[:200], "hits": hits, "tokens": tokens},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
