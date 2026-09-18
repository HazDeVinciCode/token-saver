"""`token-saver doctor` : audit du projet et opportunités classées par impact (règles R1-R14).

Chaque constat porte : données (MESURÉ), suggestion, impact (valeur + registre), confiance, difficulté.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from .metrics import _pct, since_ts, snapshot

IMPACT_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "INFO": 3}
ROUTINE = re.compile(r"explore|search|test|qa|sanit|review|lint|doc|format|build|check|audit|verif", re.I)


def _f(n: float) -> str:
    n = float(n or 0)
    return f"{n / 1e9:.2f}B" if n >= 1e9 else f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else f"{n:.0f}"


def _finding(rid: str, level: str, title: str, why: str, advice: str, impact: str | None, registry: str | None,
             confidence: str, difficulty: str, data: dict | None = None) -> dict:
    return {"id": rid, "level": level, "title": title, "why": why, "advice": advice, "impact": impact, "registry": registry,
            "confidence": confidence, "difficulty": difficulty, "data": data or {}}


def _referenced_docs(project: Path) -> list[Path]:
    """Fichiers .md référencés par CLAUDE.md, les agents et les rules."""
    srcs = [project / "CLAUDE.md", project / ".claude" / "CLAUDE.md"]
    srcs += list((project / ".claude" / "agents").glob("*.md")) + list((project / ".claude" / "rules").glob("**/*.md"))
    found: set[Path] = set()
    for s in srcs:
        if not s.is_file():
            continue
        text = s.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"[\w./\\-]+\.md", text):
            cand = (project / m.group(0).replace("\\", "/")).resolve()
            if cand.is_file() and cand != s.resolve():
                found.add(cand)
    return sorted(found)


def run(con: sqlite3.Connection, project: Path, cfg: dict, period: str = "7d") -> dict:
    since = since_ts(period)
    S = snapshot(con, period, cfg)
    t = S["totals"]["all"]
    thr = cfg["thresholds"]
    F: list[dict] = []
    settings = {}
    sf = project / ".claude" / "settings.local.json"
    if sf.is_file():
        try:
            settings = json.loads(sf.read_text(encoding="utf-8"))
        except ValueError:
            settings = {}
    capped = (settings.get("env") or {}).get("CLAUDE_CODE_DISABLE_1M_CONTEXT") == "1"

    # R16 — compactions trop fréquentes (leçon EXP-02 : perte d'étapes de process, temps et tokens de résumés)
    ev0 = S["events"]
    cd0 = ev0["compact_detail"]
    days = {"today": 1, "7d": 7, "30d": 30}.get(period, 30)
    if cd0["n"] >= 5 and (cd0["n"] / days >= 3 or cd0["total_min"] >= 30):
        F.append(_finding("R16", "HIGH", "Compactions trop fréquentes : le travail perd ses étapes",
                          f"{cd0['n']} compactions sur la période ({cd0['n'] / days:.1f}/jour), {cd0['total_min']} min passées à résumer, "
                          f"{_f(cd0['summary_tokens'])} tokens de résumés écrits (output ×5). Chaque résumé perd des détails de procédure.",
                          "Relever `autoCompactWindow` (ou le retirer) et laisser la fenêtre par défaut pour les longs processus autonomes ; "
                          "garder les compactions pour les frontières entre tâches ; ajouter des « Compact instructions » dans CLAUDE.md (`doctor --fix find`).",
                          f"{cd0['total_min']} min et {_f(cd0['summary_tokens'] * 5)} tokens pondérés de résumés", "MESURÉ", "haute", "très faible"))
    # R1 — contexte long (seulement si les compactions ne sont pas déjà le problème)
    c = S["context"]
    if c["n"] and c["share_over_long"] > 0.3 and not (cd0["n"] >= 5 and cd0["n"] / days >= 3):
        gain = c["cap_counterfactual"][150000]
        F.append(_finding("R1", "MEDIUM", "Les appels tournent avec un contexte très grand",
                          f"{c['share_over_long'] * 100:.0f} % des appels dépassent {_f(thr['long_context_tokens'])} tokens (p50 {_f(c['p50'])}, p90 {_f(c['p90'])}). "
                          f"Chaque appel relit tout le contexte.",
                          ("Plafond 200K en place : effet visible sur les nouvelles sessions." if capped else
                           "Pour le travail interactif : `/clear` entre deux tâches, `/compact` aux frontières naturelles. "
                           "Pour les longs processus autonomes : ne PAS forcer de compaction fréquente (voir R16) — le gain de contexte se paie en étapes perdues."),
                          f"jusqu'à −{gain * 100:.0f} % des tokens relus ({_f(t['ctx'] * gain)}) — borne haute qui ignore le coût des compactions supplémentaires",
                          "ESTIMÉ (borne haute)", "basse", "très faible", {"share": c["share_over_long"], "p90": c["p90"], "capped": capped}))
    # R2 — reconstructions à froid
    cold = S["cold"]
    if cold["events"] >= 3:
        F.append(_finding("R2", "HIGH" if cold["share_of_cache_writes"] > 0.4 else "MEDIUM", "Reconstructions du cache à froid",
                          f"{cold['events']} appels ont réécrit ≥ {_f(cold['threshold'])} tokens en cache ({_f(cold['tokens'])} = {cold['share_of_cache_writes'] * 100:.0f} % des cache writes, "
                          f"contexte moyen {_f(cold['avg_ctx'])}). Causes typiques : reprise après > 1 h, changement de modèle, mise à jour, compaction.",
                          "Termine ou compacte avant une pause > 1 h ; reprends depuis un résumé ; ne change pas de modèle en cours de tâche ; la statusline avertit 10 min avant l'expiration du cache.",
                          f"{_f(cold['tokens'] * 1.25)} tokens pondérés (cache write 1,25×)", "MESURÉ", "haute", "faible (habitudes)"))
    # R3 — relectures
    rd = S["reads"]
    if rd["total"] and (rd["redundant"] / rd["total"] > 0.15 or rd["cross_agent_files"] > 20):
        F.append(_finding("R3", "MEDIUM", "Fichiers relus inutilement",
                          f"{rd['redundant']} relectures strictement identiques sur {rd['total']} Read ({rd['redundant'] / rd['total'] * 100:.0f} %, {_f(rd['redundant_tokens'])} tokens réinjectés) ; "
                          f"{rd['cross_agent_files']} fichiers lus par ≥ 2 agents d'une même session.",
                          "H1 (read-ledger) refuse les relectures identiques ; pour les docs partagés entre agents, `find` + pack de contexte.",
                          f"{_f(rd['redundant_tokens'])} tokens injectés (× appels restants pour le coût de relecture)", "MESURÉ / ESTIMÉ", "haute", "faible"))
    # R4 — gros docs référencés
    big = []
    for doc in _referenced_docs(project):
        size = doc.stat().st_size
        if size // 4 >= 10000:
            rel = str(doc.relative_to(project)).replace("\\", "/").lower()
            reads = con.execute("SELECT COUNT(*), COALESCE(SUM(result_chars),0)/4 FROM tool_uses WHERE name='Read' AND file_path LIKE ?",
                                (f"%{rel}",)).fetchone()
            big.append({"path": str(doc.relative_to(project)).replace("\\", "/"), "tokens": size // 4, "reads": reads[0], "injected": reads[1]})
    if big:
        big.sort(key=lambda d: -d["injected"])
        F.append(_finding("R4", "HIGH" if sum(d["injected"] for d in big) > 200000 else "MEDIUM", "Gros documents cités comme référence",
                          "; ".join(f"{d['path']} ≈ {_f(d['tokens'])} tokens, lu {d['reads']}× ({_f(d['injected'])} tokens injectés)" for d in big[:4]),
                          "Indexer avec `find` et dire à Claude de l'utiliser (`token-saver doctor --fix find`) ; garder un résumé ≤ 2K tokens toujours chargé ; "
                          "découper en `.claude/rules/*.md` avec `paths:` pour un chargement paresseux.",
                          f"{_f(sum(d['injected'] for d in big))} tokens injectés sur l'historique", "MESURÉ", "haute", "moyenne", {"docs": big}))
    # R5 / R19 / R20 — conseiller d'agents (voir agents.py) : inutilisés, coût par lancement, candidats à un modèle moins cher
    from .agents import advise as _agents_advise
    F += _agents_advise(con, project, cfg, since)
    # R6 — boucles
    tu = S["turns"]
    if tu["p90"] > thr["calls_per_turn_alert"]:
        F.append(_finding("R6", "MEDIUM", "Tours très longs (boucles agentiques)",
                          f"p90 = {tu['p90']} appels API par tour utilisateur (max {tu['max']}). Chaque appel relit le contexte.",
                          "Donner une cible de vérification (test, commande, capture), découper la tâche, `maxTurns` sur les agents, plan mode pour les tâches larges.",
                          None, None, "basse", "moyenne"))
    # R7 — Write volumineux
    w = con.execute("SELECT COUNT(*), COALESCE(SUM(input_chars),0)/4 FROM tool_uses WHERE name='Write' AND input_chars > ?" + (" AND ts >= ?" if since else ""),
                    (thr["big_write_tokens"] * 4, *([since] if since else []))).fetchone()
    if w[0] >= 3:
        F.append(_finding("R7", "MEDIUM", "Gros fichiers écrits d'un bloc",
                          f"{w[0]} appels Write > {_f(thr['big_write_tokens'])} tokens ({_f(w[1])} tokens) : le contenu complet reste dans le contexte jusqu'à la compaction.",
                          "Préférer `Edit` pour modifier ; faire écrire les gros fichiers par un sous-agent (le contenu reste dans son contexte).",
                          f"{_f(w[1])} tokens injectés (× appels restants)", "MESURÉ / ESTIMÉ", "moyenne", "faible"))
    # R8 — compactions
    ev = S["events"]
    cd = ev["compact_detail"]
    if ev["compact"] >= 3 or (cd["n"] and cd["pre_avg"] > 800000):
        F.append(_finding("R8", "HIGH" if cd["pre_avg"] > 800000 else "MEDIUM", "Compactions tardives et coûteuses",
                          f"{ev['compact']} compactions, en moyenne {_f(cd['pre_avg'])} → {_f(cd['post_avg'])} tokens, {cd['duration_avg_s']} s chacune.",
                          "`/clear` entre deux tâches sans lien ; `/compact` aux frontières naturelles ; autocompact plus bas (150K) ; `/rewind` plutôt que compaction pour abandonner une piste.",
                          f"{cd['n'] * cd['duration_avg_s']:.0f} s d'attente et {_f(cd['pre_avg'] * cd['n'])} tokens résumés", "MESURÉ", "haute", "faible"))
    # R9 — limites atteintes
    if ev["limit_hit"]:
        rows = con.execute("SELECT ts, data FROM events WHERE kind='limit_hit'" + (" AND ts >= ?" if since else "") + " ORDER BY ts DESC LIMIT 3",
                           ([since] if since else [])).fetchall()
        F.append(_finding("R9", "INFO", "Limites de quota atteintes",
                          f"{ev['limit_hit']} rejets sur la période (derniers : {', '.join(r['ts'][:16] for r in rows)}).",
                          "Voir la trajectoire des sessions concernées dans le dashboard (contexte, agents actifs) ; la statusline affiche le quota 5 h / 7 j.",
                          None, "MESURÉ", "haute", "—"))
    # R10 — changements de modèle
    if ev["model_switch"]:
        cw = con.execute("SELECT COALESCE(SUM(c.cache_write),0) FROM events e JOIN calls c ON c.file=e.file AND c.seq=e.call_seq WHERE e.kind='model_switch'"
                         + (" AND e.ts >= ?" if since else ""), ([since] if since else [])).fetchone()[0]
        F.append(_finding("R10", "LOW", "Changements de modèle en cours de session",
                          f"{ev['model_switch']} changements ; le cache est reconstruit à chaque fois ({_f(cw)} tokens écrits en cache juste après).",
                          "Choisir le modèle et l'effort au début d'une tâche ; éviter `opusplan` (bascule à chaque plan).",
                          f"{_f(cw * 1.25)} tokens pondérés", "MESURÉ", "haute", "faible"))
    # R11 — skills jamais invoqués
    skills = [p.parent.name for p in (project / ".claude" / "skills").glob("*/SKILL.md")]
    used = {r[0] for r in con.execute("SELECT DISTINCT skill FROM calls WHERE skill IS NOT NULL")}
    unused = [s for s in skills if s not in used]
    if len(skills) >= 5 and unused:
        F.append(_finding("R11", "LOW", "Skills jamais invoqués",
                          f"{len(unused)}/{len(skills)} skills n'apparaissent dans aucun appel : {', '.join(unused[:8])}{'…' if len(unused) > 8 else ''}.",
                          "Pour ceux que tu lances toi-même : `disable-model-invocation: true` (description hors contexte) ; supprimer les obsolètes ; `/skill-doctor` dans Claude Code.",
                          "quelques centaines de tokens par session", "ESTIMÉ", "basse", "faible", {"unused": unused}))
    # R12 — hooks
    tsx = S["ts"]
    if ev["hook_errors"] or tsx.get("latency_p95", 0) > 400:
        F.append(_finding("R12", "LOW", "Hooks en erreur ou lents",
                          f"{ev['hook_errors']} erreurs de hooks signalées par Claude Code ; latence TS p95 {tsx.get('latency_p95', 0)} ms.",
                          "Vérifier `.token-saver/metrics/hook-events.jsonl` ; désactiver la feature fautive dans config.json.", None, "MESURÉ", "haute", "faible"))
    # R13 — thinking sur agents routiniers
    th = con.execute("SELECT COALESCE(a.type,'?'), SUM(c.thinking), SUM(c.output) FROM calls c LEFT JOIN agents a ON a.agent_id=c.agent_id "
                     "WHERE c.agent_id IS NOT NULL" + (" AND c.ts >= ?" if since else "") + " GROUP BY 1", ([since] if since else [])).fetchall()
    heavy = [r for r in th if r[2] and r[1] / r[2] > 0.6 and ROUTINE.search(r[0] or "")]
    if heavy:
        F.append(_finding("R13", "LOW", "Beaucoup de réflexion (thinking) sur des agents routiniers",
                          ", ".join(f"{r[0]} ({r[1] / r[2] * 100:.0f} % de l'output)" for r in heavy[:4]),
                          "`effort: medium` (ou `low`) dans la définition de ces agents.", f"{_f(sum(r[1] for r in heavy))} tokens de thinking (output ×5)", "MESURÉ", "moyenne", "faible"))
    # R14 — CLAUDE.md
    cm = project / "CLAUDE.md"
    if cm.is_file():
        text = cm.read_text(encoding="utf-8", errors="replace")
        lines = text.count("\n") + 1
        imports = re.findall(r"(?m)(?<![`\w])@([\w./\\-]+)", text)
        startup = con.execute("SELECT AVG(ctx) FROM calls WHERE seq=1 AND agent_id IS NULL" + (" AND ts >= ?" if since else ""),
                              ([since] if since else [])).fetchone()[0] or 0
        if lines > 200 or imports:
            F.append(_finding("R14", "MEDIUM" if lines > 300 else "LOW", "CLAUDE.md volumineux ou avec imports",
                              f"{lines} lignes{', imports : ' + ', '.join(imports[:4]) if imports else ''} ; contexte de démarrage mesuré ≈ {_f(startup)} tokens par session, relu à chaque appel.",
                              "Viser < 200 lignes : procédures → skills, règles par zone → `.claude/rules/*.md` avec `paths:`, `/doctor` de Claude Code propose des coupes.",
                              f"{_f(startup)} tokens × chaque appel", "MESURÉ", "haute", "moyenne"))
        else:
            F.append(_finding("R14", "INFO", "CLAUDE.md compact", f"{lines} lignes, contexte de démarrage ≈ {_f(startup)} tokens.", "Rien à faire.", None, "MESURÉ", "haute", "—"))
    # R15 — find installé mais inutilisé
    flog = project / ".token-saver" / "metrics" / "find-log.jsonl"
    nfind = sum(1 for _ in open(flog, encoding="utf-8")) if flog.is_file() else 0
    from .install import RULES_FILE
    has_rules = (project / RULES_FILE).is_file()
    if cfg["features"].get("find") and not has_rules:
        F.append(_finding("R15", "MEDIUM", "`find` est installé mais Claude ne sait pas l'utiliser",
                          f"{nfind} requêtes `find` journalisées ; aucun fichier de règles TOKEN SAVER ({RULES_FILE}).",
                          f"`token-saver doctor --fix find` écrit {RULES_FILE} (lu par Claude Code comme CLAUDE.md, retiré par uninstall) : "
                          "il dit à Claude d'utiliser `find` avant de lire les docs.",
                          None, None, "haute", "très faible"))
    # R17/R18 — relectures en chaîne et CI attendue par sondage (mesuré le 16/09/2026 : jusqu'à 8 relectures d'une même PR,
    # 7-8 M tokens chacune ; 710 appels de sondage = 3,4 % du volume). Voir review.py.
    from .review import measure as _review_measure
    rv = _review_measure(con, since)
    if rv["chains"]:
        worst = rv["chains"][:4]
        F.append(_finding("R17", "HIGH" if any(d["rounds"] >= 5 for _, d in worst) else "MEDIUM",
                          "Relectures en chaîne : la même PR est relue en entier à chaque tour",
                          ", ".join(f"PR #{pr} relue {d['rounds']} fois ({_f(d['tokens'])} tokens)" for pr, d in worst)
                          + f" ; {rv['reviews']} relectures par sous-agent au total sur la période, {_f(rv['review_tokens'])} tokens lus.",
                          "Revue bornée de TOKEN SAVER, installée dans la commande de revue du projet : diff écrit dans un fichier, tours suivants "
                          "limités aux lignes changées, 3 tours maximum, seuls les 🔴 bloquent (`review begin` / `review end`).",
                          f"≈ {_f(sum(d['tokens'] for _, d in rv['chains']) * 0.6)} tokens relus évitables (hypothèse : tours 2 et 3 sur le delta seulement)",
                          "ESTIMÉ", "moyenne", "faible", {"chains": {pr: d for pr, d in rv["chains"]}}))
    if rv["poll_calls"] >= 20:
        F.append(_finding("R18", "MEDIUM", "CI attendue par sondage : chaque `gh pr checks` est un appel à contexte plein",
                          f"{rv['poll_calls']} appels n'ont fait qu'interroger l'état de la CI : {_f(rv['poll_tokens'])} tokens relus, "
                          f"{100.0 * rv['poll_tokens'] / max(rv['total_tokens'], 1):.1f} % du total.",
                          "`python .token-saver/bin/token-saver.pyz review wait-ci` : un seul appel qui attend la fin des vérifications.",
                          f"{_f(rv['poll_tokens'])} tokens relus", "MESURÉ", "haute", "très faible"))
    F.sort(key=lambda f: IMPACT_ORDER.get(f["level"], 9))
    return {"project": str(project), "period": period, "generated": time.strftime("%Y-%m-%d %H:%M"), "findings": F,
            "summary": {k: sum(1 for f in F if f["level"] == k) for k in IMPACT_ORDER}}


def render(rep: dict) -> str:
    L = [f"TOKEN SAVER — doctor — {rep['project']} — période : {rep['period']} — {rep['generated']}",
         "  ".join(f"{k}: {v}" for k, v in rep["summary"].items() if v), ""]
    for f in rep["findings"]:
        L.append(f"[{f['level']}] {f['id']} — {f['title']}")
        L.append(f"   pourquoi   : {f['why']}")
        L.append(f"   conseil    : {f['advice']}")
        if f.get("impact"):
            L.append(f"   impact     : {f['impact']}  [{f.get('registry') or '—'}]")
        L.append(f"   confiance  : {f['confidence']}   ·   difficulté : {f['difficulty']}")
        L.append("")
    return "\n".join(L)
