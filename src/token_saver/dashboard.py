"""Dashboard HTML statique mono-fichier (aucun CDN, aucun serveur)."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

from . import __version__
from .collect import collect
from .config import load_config
from .db import connect
from .metrics import snapshot
from .paths import claude_config_dir, default_db_path, ts_dir


def _daily(con: sqlite3.Connection, days: int = 90) -> list[dict]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = con.execute(
        "SELECT substr(ts,1,10) d, COUNT(*) calls, SUM(ctx) ctx, SUM(cache_read) cr, SUM(cache_write) cw, SUM(output) o, "
        "SUM(CASE WHEN agent_id IS NULL THEN ctx ELSE 0 END) ctx_main FROM calls WHERE ts >= ? GROUP BY d ORDER BY d", (since,)).fetchall()
    by_day = defaultdict(list)
    for r in con.execute("SELECT substr(ts,1,10) d, ctx FROM calls WHERE ts >= ?", (since,)):
        by_day[r[0]].append(r[1] or 0)
    out = []
    for r in rows:
        v = sorted(by_day[r["d"]])
        out.append({"d": r["d"], "calls": r["calls"], "ctx": r["ctx"], "ctx_main": r["ctx_main"], "cr": r["cr"], "cw": r["cw"], "o": r["o"],
                    "p90": v[min(len(v) - 1, int(len(v) * 0.9))] if v else 0})
    return out


def _sessions(con: sqlite3.Connection, limit: int = 40) -> list[dict]:
    files = con.execute("SELECT file, session_id, COUNT(*) n, SUM(ctx) s, MAX(ctx) m, MIN(ts) t0, MAX(ts) t1 FROM calls "
                        "WHERE agent_id IS NULL GROUP BY file ORDER BY t1 DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for f in files:
        sess = con.execute("SELECT title FROM sessions WHERE session_id=?", (f["session_id"],)).fetchone()
        traj = [r[0] or 0 for r in con.execute("SELECT ctx FROM calls WHERE file=? ORDER BY seq", (f["file"],))]
        step = max(1, len(traj) // 150)
        comp = [r[0] for r in con.execute("SELECT call_seq FROM events WHERE file=? AND kind='compact'", (f["file"],))]
        ev = {k: con.execute("SELECT COUNT(*) FROM events WHERE file=? AND kind=?", (f["file"], k)).fetchone()[0]
              for k in ("compact", "limit_hit", "model_switch", "api_error")}
        subs = con.execute("SELECT COUNT(DISTINCT agent_id), COALESCE(SUM(ctx),0) FROM calls WHERE session_id=? AND agent_id IS NOT NULL",
                           (f["session_id"],)).fetchone()
        turns = con.execute("SELECT COUNT(*) FROM user_turns WHERE file=?", (f["file"],)).fetchone()[0]
        out.append({"id": (f["session_id"] or "")[:8], "title": (sess["title"] if sess and sess["title"] else "")[:60],
                    "t0": f["t0"], "t1": f["t1"], "calls": f["n"], "ctx": f["s"], "max": f["m"], "turns": turns,
                    "traj": traj[::step], "step": step, "compacts": [c // step for c in comp], "events": ev,
                    "agents": subs[0], "agents_ctx": subs[1]})
    return out


WHY = {
    "assistant:tool": "les arguments des appels d'outils (commandes Bash, scripts passés en heredoc, prompts d'agents) restent dans l'historique",
    "assistant:write": "le contenu complet des fichiers passés à Write/Edit reste dans l'historique jusqu'à la compaction",
    "assistant:text": "les explications de Claude s'accumulent",
    "tool:Bash": "sorties de commandes (builds, tests, git) — rtk réduit déjà, le reste s'empile",
    "tool:Read": "contenu des fichiers lus (et relus)",
    "user": "tes prompts (les longs briefs sont relus à chaque appel de la session)",
    "meta": "rappels système, résumés, sorties de commandes locales",
    "post-compaction": "résumé + fichiers réinjectés après compaction",
    "startup(main)": "system prompt + outils + CLAUDE.md + mémoire, relus à chaque appel",
    "startup(subagent)": "bootstrap de chaque sous-agent (~20K : system prompt + CLAUDE.md + tâche)",
    "tool:Grep": "résultats de recherche", "tool:PowerShell": "sorties PowerShell", "images": "captures d'écran",
    "tool:Agent": "résumés retournés par les sous-agents", "unattributed": "croissance non expliquée (contexte système, images)",
}


def build_data(con: sqlite3.Connection, cfg: dict, project: str, installed: bool) -> dict:
    from .doctor import run as doctor_run
    periods = {p: snapshot(con, p, cfg) for p in ("today", "7d", "30d", "all")}
    return {"project": project, "installed": installed, "generated": time.strftime("%Y-%m-%d %H:%M"), "version": __version__,
            "features": cfg["features"], "periods": periods, "daily": _daily(con), "sessions": _sessions(con), "why": WHY,
            "doctor": doctor_run(con, Path(project), cfg, "30d")}


def render_html(data: dict) -> str:
    tpl = resources.files(__package__).joinpath("dashboard.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, default=str).replace("</", "<\\/")
    return tpl.replace("/*__DATA__*/null", payload)


def all_projects(cfg: dict) -> list[dict]:
    """Vue multi-projets : chaque dossier de ~/.claude/projects (worktrees regroupés), base en mémoire."""
    base = claude_config_dir() / "projects"
    groups: dict[str, list[Path]] = defaultdict(list)
    for d in sorted(base.iterdir()) if base.is_dir() else []:
        if d.is_dir() and any(d.glob("*.jsonl")):
            groups[re.sub(r"--claude-worktrees-.*$", "", d.name)].append(d)
    rows = []
    for name, dirs in groups.items():
        con = connect(None)
        t0 = time.time()
        _collect_dirs(con, dirs)
        snap7, snapall = snapshot(con, "7d", cfg), snapshot(con, "all", cfg)
        last = con.execute("SELECT MAX(ts) FROM calls").fetchone()[0]
        rows.append({"name": name, "dirs": len(dirs), "last": last, "seconds": round(time.time() - t0, 1),
                     "all": _brief(snapall), "d7": _brief(snap7)})
        con.close()
    rows.sort(key=lambda r: -(r["all"]["ctx"]))
    return rows


def _brief(s: dict) -> dict:
    t = s["totals"]["all"]
    return {"calls": t["calls"], "ctx": t["ctx"], "cache_read": t["cache_read"], "cache_write": t["cache_write"], "output": t["output"],
            "weighted": s["weighted"]["total"], "p90": s["context"]["p90"], "cold": s["cold"]["tokens"],
            "limit_hits": s["events"]["limit_hit"], "compacts": s["events"]["compact"], "redundant": s["reads"]["redundant"]}


def _collect_dirs(con: sqlite3.Connection, dirs: list[Path]) -> None:
    """Collecte sur une liste de dossiers de transcripts (sans passer par un projet installé)."""
    from . import paths as _p
    real = _p.transcript_files

    def fake(_project):
        out = []
        for d in dirs:
            out.extend((f, "main") for f in sorted(d.glob("*.jsonl")))
            out.extend((f, "subagent") for f in sorted(d.glob("*/subagents/*.jsonl")))
        return out
    import token_saver.collect as c
    c.transcript_files = fake
    try:
        collect(con, Path("."), ts_logs=False)
    finally:
        c.transcript_files = real


def generate(project: Path | None, out: Path | None, cfg: dict, all_projects_view: bool, open_browser: bool) -> Path:
    if all_projects_view:
        data = {"mode": "all", "generated": time.strftime("%Y-%m-%d %H:%M"), "version": __version__, "projects": all_projects(cfg)}
        target = out or (ts_dir(project) / "reports" / "dashboard-all.html" if project and ts_dir(project).is_dir()
                         else Path.cwd() / "token-saver-dashboard-all.html")
    else:
        assert project is not None
        installed = ts_dir(project).is_dir()
        con = connect(default_db_path(project) if installed else None)
        collect(con, project)
        data = build_data(con, cfg, str(project), installed)
        data["mode"] = "project"
        target = out or (ts_dir(project) / "reports" / "dashboard.html" if installed else Path.cwd() / "token-saver-dashboard.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_html(data), encoding="utf-8")
    if open_browser:
        webbrowser.open(target.resolve().as_uri())
    return target
