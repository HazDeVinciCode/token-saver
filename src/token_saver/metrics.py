"""Métriques dérivées (définitions figées, cf. PHASE5-CONCEPTION §7.3) et attribution exacte."""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from .config import model_factor

PERIODS = {"today": 1, "7d": 7, "30d": 30, "all": None}


def since_ts(period: str) -> str | None:
    days = PERIODS.get(period)
    if days is None:
        return None
    if period == "today":
        return datetime.now().strftime("%Y-%m-%dT00:00:00")
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


def _w(since: str | None, col: str = "ts") -> tuple[str, tuple]:
    return (f" AND {col} >= ?", (since,)) if since else ("", ())


def _pct(values: list[int], q: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[min(len(s) - 1, int(len(s) * q))]


def totals(con: sqlite3.Connection, since: str | None) -> dict:
    """Totaux MESURÉS par scope (main / subagent / all)."""
    w, p = _w(since)
    out = {}
    for scope, cond in (("main", "agent_id IS NULL"), ("subagent", "agent_id IS NOT NULL"), ("all", "1=1")):
        r = con.execute(f"SELECT COUNT(*) n, COALESCE(SUM(input),0) i, COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(cache_write),0) cw, "
                        f"COALESCE(SUM(cw_1h),0) c1, COALESCE(SUM(cw_5m),0) c5, COALESCE(SUM(output),0) o, COALESCE(SUM(thinking),0) th, "
                        f"COALESCE(SUM(ctx),0) ctx FROM calls WHERE {cond}{w}", p).fetchone()
        out[scope] = {"calls": r["n"], "input": r["i"], "cache_read": r["cr"], "cache_write": r["cw"], "cw_1h": r["c1"],
                      "cw_5m": r["c5"], "output": r["o"], "thinking": r["th"], "ctx": r["ctx"],
                      "hit": (r["cr"] / r["ctx"]) if r["ctx"] else 0.0}
    return out


def by_model(con: sqlite3.Connection, since: str | None) -> list[dict]:
    w, p = _w(since)
    rows = con.execute(f"SELECT model, COUNT(*) n, SUM(ctx) ctx, SUM(cache_read) cr, SUM(cache_write) cw, SUM(output) o, "
                       f"SUM(CASE WHEN agent_id IS NULL THEN 0 ELSE 1 END) sub FROM calls WHERE model NOT LIKE '<%'{w} "
                       f"GROUP BY model ORDER BY ctx DESC", p).fetchall()
    return [dict(r) for r in rows if r["model"]]


def weighted(con: sqlite3.Connection, since: str | None, cfg: dict) -> dict:
    """Tokens pondérés au prix API (ESTIMÉ) : Σ facteur_modèle × Σ poids × tokens."""
    wts = cfg["cost_model"]["weights"]
    w, p = _w(since)
    rows = con.execute(f"SELECT model, SUM(input) i, SUM(cache_read) cr, SUM(cw_1h) c1, SUM(cache_write)-SUM(cw_1h) c5, SUM(output) o "
                       f"FROM calls WHERE 1=1{w} GROUP BY model", p).fetchall()
    total = 0.0
    parts = Counter()
    for r in rows:
        f = model_factor(cfg, r["model"])
        parts["input"] += f * wts["input"] * (r["i"] or 0)
        parts["cache_read"] += f * wts["cache_read"] * (r["cr"] or 0)
        parts["cache_write"] += f * (wts["cache_write_1h"] * (r["c1"] or 0) + wts["cache_write_5m"] * max(0, r["c5"] or 0))
        parts["output"] += f * wts["output"] * (r["o"] or 0)
    total = sum(parts.values())
    return {"total": total, "parts": dict(parts), "verified": cfg["cost_model"].get("model_factors_verified", False)}


def context_stats(con: sqlite3.Connection, since: str | None, cfg: dict) -> dict:
    w, p = _w(since)
    ctx = [r[0] for r in con.execute(f"SELECT ctx FROM calls WHERE 1=1{w}", p)]
    thr = cfg["thresholds"]
    caps = {}
    tot = sum(ctx)
    for cap in (400000, 200000, 150000, 100000):
        s = sum(min(c, cap) for c in ctx)
        caps[cap] = (1 - s / tot) if tot else 0.0
    return {"n": len(ctx), "mean": int(statistics.mean(ctx)) if ctx else 0, "p50": _pct(ctx, 0.5), "p90": _pct(ctx, 0.9),
            "max": max(ctx) if ctx else 0,
            "share_over_long": (sum(1 for c in ctx if c > thr["long_context_tokens"]) / len(ctx)) if ctx else 0.0,
            "cap_counterfactual": caps}


def calls_per_turn(con: sqlite3.Connection, since: str | None) -> dict:
    """Appels API entre deux prompts utilisateur (sessions principales)."""
    w, p = _w(since)
    rows = con.execute(f"SELECT file, call_seq FROM user_turns WHERE agent_id IS NULL{w} ORDER BY file, call_seq", p).fetchall()
    last_seq = con.execute("SELECT file, MAX(seq) m FROM calls WHERE agent_id IS NULL GROUP BY file").fetchall()
    last = {r["file"]: r["m"] for r in last_seq}
    per_file = defaultdict(list)
    for r in rows:
        per_file[r["file"]].append(r["call_seq"])
    vals = []
    for f, seqs in per_file.items():
        seqs = sorted(set(seqs)) + [last.get(f, seqs[-1])]
        vals.extend(b - a for a, b in zip(seqs, seqs[1:]) if b >= a)
    return {"turns": len(rows), "p50": _pct(vals, 0.5), "p90": _pct(vals, 0.9), "max": max(vals) if vals else 0}


def cold_rebuilds(con: sqlite3.Connection, since: str | None, cfg: dict) -> dict:
    thr = cfg["thresholds"]["cold_rebuild_tokens"]
    w, p = _w(since)
    r = con.execute(f"SELECT COUNT(*) n, COALESCE(SUM(cache_write),0) t, COALESCE(AVG(ctx),0) c FROM calls WHERE cache_write>=?{w}",
                    (thr, *p)).fetchone()
    total_cw = con.execute(f"SELECT COALESCE(SUM(cache_write),0) FROM calls WHERE 1=1{w}", p).fetchone()[0]
    return {"threshold": thr, "events": r["n"], "tokens": r["t"], "avg_ctx": int(r["c"]),
            "share_of_cache_writes": (r["t"] / total_cw) if total_cw else 0.0}


def reads(con: sqlite3.Connection, since: str | None) -> dict:
    w, p = _w(since)
    total = con.execute(f"SELECT COUNT(*) FROM tool_uses WHERE name='Read'{w}", p).fetchone()[0]
    redundant = con.execute(f"SELECT COUNT(*) FROM tool_uses WHERE name='Read' AND redundant_of IS NOT NULL{w}", p).fetchone()[0]
    red_tokens = con.execute(f"SELECT COALESCE(SUM(result_chars),0)/4 FROM tool_uses WHERE name='Read' AND redundant_of IS NOT NULL{w}",
                             p).fetchone()[0]
    cross = con.execute(f"SELECT COUNT(*) FROM (SELECT session_id, file_path, COUNT(DISTINCT COALESCE(agent_id,'main')) a "
                        f"FROM tool_uses WHERE name='Read' AND file_path IS NOT NULL{w} GROUP BY session_id, file_path HAVING a>1)",
                        p).fetchone()[0]
    top = con.execute(f"SELECT file_path, COUNT(*) n, SUM(CASE WHEN redundant_of IS NULL THEN 0 ELSE 1 END) red, "
                      f"COALESCE(SUM(result_chars),0)/4 tok FROM tool_uses WHERE name='Read' AND file_path IS NOT NULL{w} "
                      f"GROUP BY file_path ORDER BY tok DESC LIMIT 8", p).fetchall()
    return {"total": total, "redundant": redundant, "redundant_tokens": red_tokens, "cross_agent_files": cross,
            "top": [dict(r) for r in top]}


def tools(con: sqlite3.Connection, since: str | None) -> list[dict]:
    w, p = _w(since)
    rows = con.execute(f"SELECT name, COUNT(*) n, COALESCE(SUM(result_chars),0) rc, COALESCE(SUM(input_chars),0) ic, "
                       f"SUM(is_error) err FROM tool_uses WHERE 1=1{w} GROUP BY name ORDER BY n DESC LIMIT 12", p).fetchall()
    return [dict(r) for r in rows]


def events_summary(con: sqlite3.Connection, since: str | None) -> dict:
    w, p = _w(since)
    out = {}
    for kind in ("compact", "compact_summary", "limit_hit", "model_switch", "api_error", "tool_clear", "hook_summary"):
        out[kind] = con.execute(f"SELECT COUNT(*) FROM events WHERE kind=?{w}", (kind, *p)).fetchone()[0]
    comps = con.execute(f"SELECT data FROM events WHERE kind='compact'{w}", p).fetchall()
    pre, post, dur = [], [], []
    for r in comps:
        try:
            d = json.loads(r["data"])
        except ValueError:
            continue
        if d.get("preTokens"):
            pre.append(d["preTokens"])
            post.append(d.get("postTokens") or 0)
            dur.append(d.get("durationMs") or 0)
    out["compact_detail"] = {"n": len(pre), "pre_avg": int(statistics.mean(pre)) if pre else 0,
                             "post_avg": int(statistics.mean(post)) if post else 0,
                             "duration_avg_s": round(statistics.mean(dur) / 1000, 1) if dur else 0,
                             "total_min": round(sum(dur) / 60000, 1) if dur else 0,
                             "summary_tokens": sum(post)}
    # continuité : diversité des sous-agents (un processus qui perd ses étapes cesse de lancer ses spécialistes)
    out["agent_types"] = [r[0] for r in con.execute(
        f"SELECT DISTINCT COALESCE(a.type,'?') FROM calls c LEFT JOIN agents a ON a.agent_id=c.agent_id WHERE c.agent_id IS NOT NULL{w.replace('ts', 'c.ts')}", p)]
    hooks_err = con.execute(f"SELECT data FROM events WHERE kind='hook_summary'{w}", p).fetchall()
    out["hook_errors"] = sum(json.loads(r["data"]).get("errors", 0) for r in hooks_err)
    return out


def agents_summary(con: sqlite3.Connection, since: str | None) -> list[dict]:
    w, p = _w(since)
    rows = con.execute(
        f"SELECT COALESCE(a.type, '?') type, COUNT(DISTINCT c.agent_id) agents, COUNT(*) calls, SUM(c.ctx) ctx, SUM(c.output) o, "
        f"GROUP_CONCAT(DISTINCT c.model) models FROM calls c LEFT JOIN agents a ON a.agent_id=c.agent_id "
        f"WHERE c.agent_id IS NOT NULL AND c.model NOT LIKE '<%'{w.replace('ts', 'c.ts')} "
        f"GROUP BY COALESCE(a.type,'?') ORDER BY ctx DESC LIMIT 15", p).fetchall()
    return [dict(r) for r in rows]


def sessions_summary(con: sqlite3.Connection, since: str | None) -> dict:
    w, p = _w(since)
    rows = con.execute(f"SELECT file, COUNT(*) n, MAX(ctx) m FROM calls WHERE agent_id IS NULL{w} GROUP BY file", p).fetchall()
    n = [r["n"] for r in rows]
    m = [r["m"] for r in rows]
    return {"sessions": len(rows), "calls_p50": _pct(n, 0.5), "calls_max": max(n) if n else 0,
            "over_400k": sum(1 for x in m if x > 400000), "over_800k": sum(1 for x in m if x > 800000)}


def attribution(con: sqlite3.Connection, since: str | None) -> dict:
    """Attribution exacte par construction des tokens de contexte relus (token-calls).

    Pour chaque transcript, dans l'ordre des appels : le contexte mesuré de l'appel k est la somme
    d'items « vivants » ; le delta avec l'appel k-1 est réparti entre les contenus injectés entre
    les deux (proportionnellement aux caractères) ; remise à zéro à chaque compaction ; chaque
    appel « relit » tous les items vivants.
    """
    w, p = _w(since)
    files = [r[0] for r in con.execute(f"SELECT DISTINCT file FROM calls WHERE 1=1{w}", p)]
    total = Counter()
    for f in files:
        calls = con.execute("SELECT seq, ctx, injected, agent_id FROM calls WHERE file=? ORDER BY seq", (f,)).fetchall()
        compacts = {r[0] for r in con.execute("SELECT call_seq FROM events WHERE file=? AND kind IN ('compact','compact_summary')", (f,))}
        live: Counter = Counter()
        prev = None
        is_sub = bool(calls[0]["agent_id"]) if calls else False
        for c in calls:
            ctx = c["ctx"] or 0
            pending = json.loads(c["injected"]) if c["injected"] else {}
            if prev is None:
                live = Counter({"startup(subagent)" if is_sub else "startup(main)": ctx})
            elif c["seq"] in compacts or (prev and ctx < prev * 0.5 and ctx < 60000):
                live = Counter({"post-compaction": ctx})
            else:
                delta = ctx - prev
                if delta < 0:
                    s = sum(live.values())
                    if s:
                        for k in list(live):
                            live[k] = int(live[k] * ctx / s)
                else:
                    ps = sum(pending.values())
                    if ps == 0:
                        live["unattributed"] += delta
                    else:
                        for k, v in pending.items():
                            live[k] += int(delta * v / ps)
            prev = ctx
            for k, v in live.items():
                total[k] += v
    tot = sum(total.values())
    return {"total": tot, "shares": {k: v / tot for k, v in total.most_common()} if tot else {}}


def ts_actions(con: sqlite3.Connection, since: str | None) -> dict:
    """Registres ÉVITÉ-MESURÉ et OVERHEAD issus des journaux des hooks TOKEN SAVER."""
    w, p = _w(since)
    avoided = con.execute(f"SELECT COALESCE(SUM(tokens),0), COUNT(*) FROM savings WHERE registry='avoided'{w}", p).fetchone()
    lat = [r[0] for r in con.execute(f"SELECT latency_ms FROM hook_events WHERE latency_ms IS NOT NULL{w}", p)]
    inj = con.execute(f"SELECT COALESCE(SUM(tokens_injected),0) FROM hook_events WHERE 1=1{w}", p).fetchone()[0]
    actions = {r[0]: r[1] for r in con.execute(f"SELECT action, COUNT(*) FROM hook_events WHERE 1=1{w} GROUP BY action", p)}
    by_hook = {r[0]: r[1] for r in con.execute(f"SELECT hook, COUNT(*) FROM hook_events WHERE 1=1{w} GROUP BY hook", p)}
    # multiplicateur ESTIMÉ prudent : un token évité aurait été relu à chaque appel du tour en cours (p50 des appels par tour, ≤ 30)
    rem = min(30, calls_per_turn(con, since)["p50"] or 0)
    return {"avoided_tokens": avoided[0], "avoided_events": avoided[1], "estimated_token_calls": avoided[0] * rem,
            "median_remaining_calls": rem, "injected_tokens": inj, "hook_events": sum(actions.values()), "actions": actions,
            "by_hook": by_hook, "latency_p50": _pct(sorted(lat), 0.5) if lat else 0, "latency_p95": _pct(sorted(lat), 0.95) if lat else 0,
            "errors": actions.get("error", 0), "net_tokens": avoided[0] - inj}


def snapshot(con: sqlite3.Connection, period: str, cfg: dict) -> dict:
    since = since_ts(period)
    t0 = time.time()
    snap = {
        "period": period, "since": since,
        "totals": totals(con, since), "models": by_model(con, since), "weighted": weighted(con, since, cfg),
        "context": context_stats(con, since, cfg), "turns": calls_per_turn(con, since),
        "cold": cold_rebuilds(con, since, cfg), "reads": reads(con, since), "tools": tools(con, since),
        "events": events_summary(con, since), "agents": agents_summary(con, since),
        "sessions": sessions_summary(con, since), "attribution": attribution(con, since), "ts": ts_actions(con, since),
    }
    snap["computed_in_s"] = round(time.time() - t0, 2)
    return snap
