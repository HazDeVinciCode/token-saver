"""Conseiller d'agents (règles R5, R19, R20 du doctor) : mesuré dans les transcripts, générique, il conseille et ne change rien.

- R19 : agents du projet (.claude/agents/*.md) jamais lancés, ou plus lancés depuis 30 jours — leur description est pourtant
  relue à chaque appel de la session principale (liste des agents dans l'outil Agent).
- R20 : coût par lancement de chaque agent (tokens relus, appels, contexte moyen, dernier usage) ; les lourds sont signalés :
  un agent dont chaque appel relit plus que `thresholds.long_context_tokens` coûte autant qu'une session principale.
- R5  : candidats à un modèle moins cher — sur Opus/Fable, lancés assez souvent, et qui ne modifient (presque) jamais de fichier
  (moins d'une édition par lancement) ou dont le nom dit une tâche routinière. Estimation avec un facteur prix non vérifié.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

RO_TOOLS = ("Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "NotebookRead")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SHELL_TOOLS = ("Bash", "PowerShell")
ROUTINE = re.compile(r"explore|search|recherch|test|qa|sanit|review|revue|lint|doc|format|build|check|audit|verif|vérif", re.I)
UNUSED_DAYS = 30


def frontmatter(text: str) -> dict:
    """name / description / model / tools d'un fichier agent ou skill (YAML minimal, blocs `>` et `|` compris)."""
    out: dict = {}
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return out
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        k, sep, v = line.partition(":")
        if not sep or line.startswith((" ", "\t")):
            i += 1
            continue
        k, v = k.strip(), v.strip()
        if v in (">", "|", ">-", "|-"):
            block = []
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            out[k] = (" " if v.startswith(">") else "\n").join(b for b in block if b).strip()
            continue
        out[k] = v.strip("\"'")
        i += 1
    return out


def project_agents(project: Path) -> list[dict]:
    d = project / ".claude" / "agents"
    out = []
    for f in (sorted(d.glob("*.md")) if d.is_dir() else []):
        try:
            fm = frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        desc = fm.get("description") or ""
        out.append({"name": fm.get("name") or f.stem, "file": f".claude/agents/{f.name}", "description": desc, "model": (fm.get("model") or "").lower(),
                    "desc_tokens": len(desc) // 4 + 12})       # + le nom et la liste des outils dans la ligne de l'outil Agent
    return out


def _w(since: str | None, col: str) -> tuple[str, tuple]:
    return (f" AND {col} >= ?", (since,)) if since else ("", ())


def usage(con: sqlite3.Connection, since: str | None) -> dict[str, dict]:
    """Par type d'agent : lancements, appels, tokens relus (ctx), écrits, réflexion, modèles, contexte moyen, mix d'outils, dernier usage."""
    U: dict[str, dict] = {}

    def rec(t: str) -> dict:
        return U.setdefault(t, {"type": t, "launches": 0, "calls": 0, "ctx": 0, "output": 0, "thinking": 0, "models": set(),
                                "ro": 0, "edits": 0, "shell": 0, "tools": 0, "last_ts": "", "first_ts": ""})
    w, p = _w(since, "ts")
    for r in con.execute(f"SELECT arg, COUNT(*) FROM tool_uses WHERE name IN ('Agent','Task') AND arg IS NOT NULL{w} GROUP BY arg", p):
        rec(r[0])["launches"] = r[1]
    w, p = _w(since, "c.ts")
    for r in con.execute(f"SELECT COALESCE(a.type,'?'), COUNT(*), COALESCE(SUM(c.ctx),0), COALESCE(SUM(c.output),0), COALESCE(SUM(c.thinking),0), "
                         f"GROUP_CONCAT(DISTINCT c.model) FROM calls c LEFT JOIN agents a ON a.agent_id=c.agent_id "
                         f"WHERE c.agent_id IS NOT NULL AND c.model NOT LIKE '<%'{w} GROUP BY 1", p):
        x = rec(r[0])
        x.update({"calls": r[1], "ctx": r[2], "output": r[3], "thinking": r[4]})
        x["models"] = {m for m in (r[5] or "").split(",") if m}
    w, p = _w(since, "t.ts")
    for r in con.execute(f"SELECT a.type, t.name, COUNT(*) FROM tool_uses t JOIN agents a ON a.agent_id=t.agent_id WHERE 1=1{w} GROUP BY 1, 2", p):
        x = rec(r[0])
        x["tools"] += r[2]
        if r[1] in RO_TOOLS:
            x["ro"] += r[2]
        elif r[1] in EDIT_TOOLS:
            x["edits"] += r[2]
        elif r[1] in SHELL_TOOLS:
            x["shell"] += r[2]
    for r in con.execute("SELECT arg, MIN(ts), MAX(ts) FROM tool_uses WHERE name IN ('Agent','Task') AND arg IS NOT NULL GROUP BY arg"):
        x = rec(r[0])                                     # dernier usage : toute l'histoire, pas seulement la période
        x["first_ts"], x["last_ts"] = r[1] or "", r[2] or ""
    for x in U.values():
        x["avg_ctx"] = x["ctx"] // x["calls"] if x["calls"] else 0
        x["per_launch"] = x["ctx"] // x["launches"] if x["launches"] else 0
        x["calls_per_launch"] = round(x["calls"] / x["launches"], 1) if x["launches"] else 0
        x["edits_per_launch"] = round(x["edits"] / x["launches"], 2) if x["launches"] else 0
        x["models"] = sorted(x["models"])
    return U


def _f(n: float) -> str:
    n = float(n or 0)
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else f"{n:.0f}"


def _days_ago(ts: str) -> int | None:
    if not ts:
        return None
    try:
        t = time.mktime(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return None
    return int((time.time() - t) // 86400)


def _short_model(models: list[str]) -> str:
    fam = []
    for m in models:
        for k in ("fable", "opus", "sonnet", "haiku"):
            if k in m and k not in fam:
                fam.append(k)
    return "/".join(fam) or "?"


def _finding(rid, level, title, why, advice, impact, registry, confidence, difficulty, data=None) -> dict:
    return {"id": rid, "level": level, "title": title, "why": why, "advice": advice, "impact": impact, "registry": registry,
            "confidence": confidence, "difficulty": difficulty, "data": data or {}}


def advise(con: sqlite3.Connection, project: Path, cfg: dict, since: str | None) -> list[dict]:
    F: list[dict] = []
    U = usage(con, since)
    agents = project_agents(project)
    thr = cfg.get("thresholds") or {}
    long_ctx = int(thr.get("long_context_tokens") or 150000)
    w, p = _w(since, "ts")
    main_calls = con.execute(f"SELECT COUNT(*) FROM calls WHERE agent_id IS NULL AND model NOT LIKE '<%'{w}", p).fetchone()[0] or 0

    # R19 — agents du projet jamais lancés, ou plus depuis UNUSED_DAYS jours
    unused = []
    for a in agents:
        u = U.get(a["name"])
        if not u or not u["last_ts"]:
            unused.append({**a, "why": "jamais lancé"})
        else:
            d = _days_ago(u["last_ts"])
            if d is not None and d > UNUSED_DAYS:
                unused.append({**a, "why": f"dernier lancement il y a {d} j"})
    if agents:
        if unused:
            cost = sum(a["desc_tokens"] for a in unused) * main_calls
            F.append(_finding("R19", "LOW", "Agents du projet qui ne servent plus",
                              f"{len(unused)}/{len(agents)} agents de `.claude/agents/` : " + "; ".join(f"{a['name']} ({a['why']})" for a in unused[:8])
                              + (" …" if len(unused) > 8 else "") + f". Leur description est relue à chaque appel de la session principale ({main_calls} appels sur la période).",
                              "Retire ou fusionne ceux qui ne servent plus (leur fichier dans `.claude/agents/`) ; garde ceux que tu comptes relancer. "
                              "TOKEN SAVER ne modifie rien : c'est ta décision.",
                              f"≈ {_f(cost)} tokens relus sur la période (≈ {sum(a['desc_tokens'] for a in unused)} tokens de descriptions × {main_calls} appels)",
                              "ESTIMÉ", "moyenne", "très faible", {"unused": [a["name"] for a in unused]}))
        else:
            F.append(_finding("R19", "INFO", "Tous les agents du projet ont servi récemment",
                              f"{len(agents)} agents dans `.claude/agents/`, tous lancés depuis moins de {UNUSED_DAYS} jours.", "Rien à faire.",
                              None, "MESURÉ", "haute", "—"))

    # R20 — coût par lancement, agents lourds
    used = sorted([u for u in U.values() if u["launches"] or u["calls"]], key=lambda u: -u["ctx"])
    if used:
        def line(u: dict) -> str:
            last = _days_ago(u["last_ts"])
            return (f"{u['type']} : {u['launches']} lancement(s), {_f(u['per_launch'])} tokens relus par lancement "
                    f"({u['calls_per_launch']} appels, contexte moyen {_f(u['avg_ctx'])}), {_short_model(u['models'])}"
                    + (f", dernier il y a {last} j" if last is not None else ""))
        heavy = [u for u in used if u["avg_ctx"] > long_ctx and u["launches"] >= 2]
        total = sum(u["ctx"] for u in used)
        table = " · ".join(line(u) for u in used[:6]) + (f" · … ({len(used)} agents)" if len(used) > 6 else "")
        if heavy:
            F.append(_finding("R20", "MEDIUM", "Agents lourds : chaque appel relit plus qu'une session principale",
                              ", ".join(f"{u['type']} (contexte moyen {_f(u['avg_ctx'])}, {u['launches']} lancements, {_f(u['ctx'])} relus)" for u in heavy[:4])
                              + f" dépassent {_f(long_ctx)} par appel. Tableau : " + table,
                              "Brief précis (les fichiers exacts, `find` pour les docs), tâche découpée, `maxTurns` dans l'appel ; un agent qui relit tout "
                              "le projet à chaque appel n'économise rien par rapport à la session principale.",
                              f"{_f(sum(u['ctx'] for u in heavy))} tokens relus par ces agents sur la période ({100.0 * sum(u['ctx'] for u in heavy) / max(total, 1):.0f} % du volume des agents)",
                              "MESURÉ", "haute", "moyenne", {"heavy": [u["type"] for u in heavy]}))
        else:
            F.append(_finding("R20", "INFO", "Coût des agents par lancement", table, "Rien d'anormal : aucun agent ne relit plus qu'une session principale.",
                              f"{_f(total)} tokens relus par les agents sur la période", "MESURÉ", "haute", "—"))

    # R5 — candidats à un modèle moins cher (mesuré : sur Opus/Fable, lancés ≥ 3 fois, < 1 édition par lancement, ou nom routinier)
    names = {a["name"] for a in agents}
    cands = []
    for u in used:
        if u["type"] not in names or u["launches"] < 3 or not any(k in m for m in u["models"] for k in ("opus", "fable")):
            continue
        why = []
        if u["edits_per_launch"] < 1:
            why.append(f"{u['edits_per_launch']} édition(s) de fichier par lancement")
        if ROUTINE.search(u["type"]):
            why.append("tâche routinière d'après son nom")
        if why:
            cands.append((u, ", ".join(why)))
    if cands:
        ctx_c = sum(u["ctx"] for u, _ in cands)
        F.append(_finding("R5", "MEDIUM", "Agents sur Opus qui ne modifient (presque) pas de fichiers",
                          "; ".join(f"{u['type']} ({u['launches']} lancements, {_f(u['ctx'])} relus, {_short_model(u['models'])} : {why})" for u, why in cands[:5])
                          + ". Un agent qui lit, analyse et décide sans écrire de code est le premier candidat à un modèle moins cher.",
                          "`model: sonnet` (ou `haiku` pour l'exploration pure) et `effort: medium` dans son fichier `.claude/agents/<nom>.md` ; "
                          "compare la qualité sur trois lancements avant de garder. Garde Opus pour ceux qui écrivent le code.",
                          f"≈ {_f(ctx_c * 0.1 * 0.4)} tokens pondérés (facteur prix Sonnet ≈ 0,6, non vérifié)", "ESTIMÉ", "moyenne", "faible",
                          {"agents": [u["type"] for u, _ in cands]}))
    return F


def render_table(con: sqlite3.Connection, project: Path, since: str | None) -> str:
    """`token-saver agents` : le tableau complet, une ligne par agent, pour l'expert."""
    U = usage(con, since)
    names = {a["name"] for a in project_agents(project)}
    rows = sorted(U.values(), key=lambda u: -u["ctx"])
    if not rows:
        return "aucun agent lancé sur la période"
    L = [f"{'agent':28} {'lanc.':>5} {'appels':>6} {'relus':>7} {'/lanc.':>7} {'ctx moy':>8} {'édit/l':>6} {'modèle':10} dernier"]
    for u in rows:
        last = _days_ago(u["last_ts"])
        L.append(f"{u['type'][:28]:28} {u['launches']:5} {u['calls']:6} {_f(u['ctx']):>7} {_f(u['per_launch']):>7} {_f(u['avg_ctx']):>8} "
                 f"{u['edits_per_launch']:6} {_short_model(u['models']):10} {('il y a ' + str(last) + ' j') if last is not None else '?'}"
                 + ("" if u["type"] in names else "   (agent intégré, pas dans .claude/agents)"))
    return "\n".join(L)
