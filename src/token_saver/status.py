"""Rendu texte de `token-saver status` : chaque bloc porte son registre."""
from __future__ import annotations


def fmt(n: float | int) -> str:
    n = float(n or 0)
    if abs(n) >= 1e9:
        return f"{n / 1e9:.2f}B"
    if abs(n) >= 1e6:
        return f"{n / 1e6:.1f}M"
    if abs(n) >= 1e3:
        return f"{n / 1e3:.0f}K"
    return f"{n:.0f}"


def pct(x: float) -> str:
    return f"{100 * (x or 0):.0f}%"


def render(snap: dict, project: str, installed: bool, features: dict, period_label: str) -> str:
    t = snap["totals"]
    a, m, s = t["all"], t["main"], t["subagent"]
    ctx, turns, cold, rd, ev, sess = snap["context"], snap["turns"], snap["cold"], snap["reads"], snap["events"], snap["sessions"]
    w = snap["weighted"]
    ts0 = snap.get("ts") or {}
    cd0 = ev["compact_detail"]
    L: list[str] = []
    L.append(f"TOKEN SAVER — {project}")
    L.append(f"Statut : {'INSTALLÉ' if installed else 'NON INSTALLÉ (lecture seule des transcripts)'}  ·  période : {period_label}"
             f"  ·  sessions : {sess['sessions']}  ·  appels API : {a['calls']:,}".replace(",", " "))
    L.append("")
    L.append("── EN CLAIR ──")
    L.append(f"  Dépensé : Claude a relu {fmt(a['ctx'])} tokens de contexte en {a['calls']:,} appels et en a écrit {fmt(a['output'])}.".replace(",", " "))
    L.append(f"  Économisé par TOKEN SAVER (mesuré) : {fmt(ts0.get('avoided_tokens', 0))} tokens ({ts0.get('avoided_events', 0)} relectures évitées). "
             f"Ce que TOKEN SAVER a coûté : {fmt(ts0.get('injected_tokens', 0))} tokens de rappels, 0 programme en arrière-plan.")
    warn = []
    if cd0["n"] >= 5:
        warn.append(f"{cd0['n']} compactions ({cd0['total_min']} min passées à résumer, {fmt(cd0['summary_tokens'])} tokens de résumés)")
    if ev["limit_hit"]:
        warn.append(f"{ev['limit_hit']} arrêts pour limite de quota")
    if ctx["p90"] > 600000:
        warn.append(f"des sessions très lourdes (p90 {fmt(ctx['p90'])} tokens par appel)")
    L.append("  Continuité du travail : " + ("; ".join(warn) if warn else "rien d'anormal") + f" · spécialistes lancés : {', '.join(ev.get('agent_types') or []) or 'aucun'}.")
    L.append("── MESURÉ (usage rapporté par l'API dans les transcripts Claude Code) ──")
    L.append(f"  Σ contexte relu : {fmt(a['ctx'])}   cache read {fmt(a['cache_read'])} ({pct(a['hit'])})   "
             f"cache write {fmt(a['cache_write'])} (1h {fmt(a['cw_1h'])} / 5m {fmt(a['cw_5m'])})   input {fmt(a['input'])}   "
             f"output {fmt(a['output'])} (thinking {pct(a['thinking'] / a['output'] if a['output'] else 0)})")
    L.append(f"  Session principale : {m['calls']:,} appels, {fmt(m['ctx'])}   ·   Sous-agents : {s['calls']:,} appels, {fmt(s['ctx'])} "
             f"({pct(s['ctx'] / a['ctx'] if a['ctx'] else 0)})".replace(",", " "))
    L.append(f"  Contexte / appel : moyenne {fmt(ctx['mean'])}  p50 {fmt(ctx['p50'])}  p90 {fmt(ctx['p90'])}  max {fmt(ctx['max'])}"
             f"   ·   appels > 150K : {pct(ctx['share_over_long'])}")
    L.append(f"  Appels par tour utilisateur : p50 {turns['p50']}  p90 {turns['p90']}  max {turns['max']}  ({turns['turns']} tours)"
             f"   ·   sessions > 400K : {sess['over_400k']}, > 800K : {sess['over_800k']}")
    L.append(f"  Reconstructions à froid (cache write ≥ {fmt(cold['threshold'])}) : {cold['events']} appels, {fmt(cold['tokens'])} "
             f"= {pct(cold['share_of_cache_writes'])} des cache writes, contexte moyen {fmt(cold['avg_ctx'])}")
    L.append(f"  Lectures (Read) : {rd['total']}  dont relectures identiques {rd['redundant']} ({pct(rd['redundant'] / rd['total'] if rd['total'] else 0)}, "
             f"~{fmt(rd['redundant_tokens'])} tokens injectés)  ·  fichiers lus par ≥ 2 agents d'une même session : {rd['cross_agent_files']}")
    cd = ev["compact_detail"]
    L.append(f"  Compactions : {ev['compact']} (moy. {fmt(cd['pre_avg'])} → {fmt(cd['post_avg'])}, {cd['duration_avg_s']} s)   "
             f"limites atteintes : {ev['limit_hit']}   changements de modèle : {ev['model_switch']}   erreurs API : {ev['api_error']}   "
             f"nettoyages d'outils : {ev['tool_clear']}   erreurs de hooks : {ev['hook_errors']}")
    if snap["models"]:
        L.append("  Par modèle : " + "  ·  ".join(f"{r['model'].replace('claude-', '')} {fmt(r['ctx'])} ({r['n']} appels)"
                                                 for r in snap["models"][:5]))
    if rd["top"]:
        L.append("  Fichiers les plus coûteux en lecture : " + " · ".join(
            f"{r['file_path'].rsplit('/', 1)[-1]} ×{r['n']} ({fmt(r['tok'])} tok, {r['red']} relect.)" for r in rd["top"][:5]))
    if snap["agents"]:
        L.append("  Agents : " + " · ".join(f"{r['type']} {r['calls']} appels {fmt(r['ctx'])} [{(r['models'] or '').replace('claude-', '')}]"
                                            for r in snap["agents"][:6]))
    att = snap["attribution"]["shares"]
    if att:
        L.append("  Où partent les tokens relus : " + " · ".join(f"{k} {pct(v)}" for k, v in list(att.items())[:7]))
    L.append("")
    L.append("── ÉVITÉ-MESURÉ (actions de TOKEN SAVER) ──")
    active = [k for k, v in features.items() if v]
    ts = snap.get("ts") or {}
    if not active:
        L.append("  aucune feature active — rien à compter")
    else:
        L.append(f"  actives : {', '.join(active)}   ·   relectures évitées : {ts.get('avoided_events', 0)} = {fmt(ts.get('avoided_tokens', 0))} tokens "
                 f"(taille mesurée des lectures identiques)   ·   ≈ {fmt(ts.get('estimated_token_calls', 0))} token-relectures évitées "
                 f"[ESTIMÉ : × {ts.get('median_remaining_calls', 0)} appels par tour, p50]")
        L.append(f"  gain net = évité − injecté = {fmt(ts.get('net_tokens', 0))} tokens   ·   actions hooks : {ts.get('actions', {})}")
    L.append("")
    L.append("── ESTIMÉ (contre-factuels, hypothèses explicites) ──")
    cc = ctx["cap_counterfactual"]
    L.append(f"  Plafond de contexte (borne haute, trajectoire inchangée) : 400K → −{pct(cc[400000])}  200K → −{pct(cc[200000])}  "
             f"150K → −{pct(cc[150000])}  100K → −{pct(cc[100000])} de tokens relus")
    parts = w["parts"]
    tot = w["total"] or 1
    L.append(f"  Tokens pondérés prix API{' (facteurs modèle NON vérifiés)' if not w['verified'] else ''} : {fmt(w['total'])}  = "
             + "  ".join(f"{k} {pct(v / tot)}" for k, v in parts.items()))
    L.append("")
    L.append("── OVERHEAD TOKEN SAVER ──")
    L.append(f"  Processus résidents : 0   ·   exécutions de hooks : {ts.get('hook_events', 0)} (latence p50 {ts.get('latency_p50', 0)} ms, "
             f"p95 {ts.get('latency_p95', 0)} ms, erreurs {ts.get('errors', 0)})   ·   tokens injectés par TS : {fmt(ts.get('injected_tokens', 0))}"
             f"   ·   calcul de ce rapport : {snap['computed_in_s']} s")
    return "\n".join(L)
