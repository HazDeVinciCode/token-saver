#!/usr/bin/env python3
"""TOKEN SAVER — statusline conseillère (0 token).

Script autonome (aucun import du paquet) copié dans <projet>/.token-saver/bin/statusline.py.
Lit le JSON que Claude Code envoie sur stdin, affiche une ligne d'état + une ligne de conseil,
et journalise chaque changement d'état dans <projet>/.token-saver/metrics/statusline-log.jsonl.
"""
import json
import os
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parents[1]              # <projet>/.token-saver
LOG = ROOT / "metrics" / "statusline-log.jsonl"
TURN = ROOT / "state" / "statusline-turn.json"
DEFAULTS = {"ctx_warn_pct": 70, "ctx_crit_pct": 85, "cache_expiry_warn_min": 10, "calls_per_turn_warn": 25,
            "quota_5h_warn_pct": 80, "quota_7d_warn_pct": 90, "color": True}
YEL, RED, DIM, RST = "\x1b[33m", "\x1b[31m", "\x1b[2m", "\x1b[0m"


def g(d, *keys, default=None):
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k)
    return default if d is None else d


def fmt_k(n):
    try:
        n = int(n)
    except Exception:  # noqa: BLE001
        return "?"
    return f"{n / 1000:.0f}K" if n < 1_000_000 else f"{n / 1_000_000:.2f}M"


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        user = json.loads((ROOT / "config.json").read_text(encoding="utf-8")).get("statusline") or {}
        cfg.update({k: v for k, v in user.items() if k in cfg})
    except Exception:  # noqa: BLE001
        pass
    return cfg


def turn_counter(prompt_id, usage_sig):
    """Compte les appels API du tour courant (changements de current_usage pour un même prompt_id)."""
    try:
        st = json.loads(TURN.read_text(encoding="utf-8")) if TURN.exists() else {}
    except Exception:  # noqa: BLE001
        st = {}
    if not prompt_id:
        return st.get("n", 0)
    if st.get("prompt_id") != prompt_id:
        st = {"prompt_id": prompt_id, "n": 0, "sig": None}
    if usage_sig and usage_sig != st.get("sig"):
        st["n"] = st.get("n", 0) + 1
        st["sig"] = usage_sig
    try:
        TURN.parent.mkdir(parents=True, exist_ok=True)
        TURN.write_text(json.dumps(st), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return st.get("n", 0)


def main():
    cfg = load_cfg()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:  # noqa: BLE001
        data = {}
    c = (lambda code, s: f"{code}{s}{RST}") if cfg.get("color") else (lambda code, s: s)
    model = g(data, "model", "display_name", default="?")
    cw = g(data, "context_window", default={}) or {}
    cu = cw.get("current_usage") or {}
    pc = g(data, "prompt_cache", default={}) or {}
    rl = g(data, "rate_limits", default={}) or {}
    ctx_pct = cw.get("used_percentage")
    sig = json.dumps([cu.get("input_tokens"), cu.get("output_tokens"), cu.get("cache_creation_input_tokens"),
                      cu.get("cache_read_input_tokens")]) if cu else None
    n_turn = turn_counter(data.get("prompt_id"), sig)
    now = time.time()
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "session_id": data.get("session_id"), "prompt_id": data.get("prompt_id"),
        "version": data.get("version"), "model": g(data, "model", "id"), "effort": g(data, "effort", "level"),
        "ctx_used_pct": ctx_pct, "ctx_size": cw.get("context_window_size"), "ctx_total_input": cw.get("total_input_tokens"),
        "ctx_total_output": cw.get("total_output_tokens"), "exceeds_200k": data.get("exceeds_200k_tokens"),
        "cu_input": cu.get("input_tokens"), "cu_output": cu.get("output_tokens"),
        "cu_cache_write": cu.get("cache_creation_input_tokens"), "cu_cache_read": cu.get("cache_read_input_tokens"),
        "pc_requests": pc.get("requests"), "pc_misses": pc.get("misses"), "pc_hit_ratio": pc.get("hit_ratio"),
        "pc_warm": pc.get("warm"), "pc_ttl": pc.get("ttl"), "pc_expires_at": pc.get("expires_at"),
        "pc_recache_if_cold": pc.get("recache_tokens_if_cold"), "pc_last_miss_causes": g(pc, "last_miss_cause", "causes"),
        "rl_5h_pct": g(rl, "five_hour", "used_percentage"), "rl_5h_resets": g(rl, "five_hour", "resets_at"),
        "rl_7d_pct": g(rl, "seven_day", "used_percentage"), "rl_7d_resets": g(rl, "seven_day", "resets_at"),
        "cost_usd": g(data, "cost", "total_cost_usd"), "calls_this_turn": n_turn,
    }
    # journal : uniquement quand l'état observable change
    keys = ("session_id", "cu_input", "cu_output", "cu_cache_write", "cu_cache_read", "pc_requests", "rl_5h_pct", "rl_7d_pct")
    try:
        last = ""
        if LOG.exists() and LOG.stat().st_size:
            with open(LOG, "rb") as f:
                f.seek(max(0, LOG.stat().st_size - 4096))
                tail = f.read().decode("utf-8", "replace").strip().splitlines()
                last = tail[-1] if tail else ""
        lastsig = ""
        if last:
            try:
                lr = json.loads(last)
                lastsig = json.dumps([lr.get(k) for k in keys])
            except Exception:  # noqa: BLE001
                pass
        if data and json.dumps([rec[k] for k in keys]) != lastsig:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass

    # ----- ligne d'état -----
    parts = [f"[{model}]"]
    advice = []
    if ctx_pct is not None:
        seg = f"ctx {ctx_pct:.0f}% ({fmt_k(cw.get('total_input_tokens'))}/{fmt_k(cw.get('context_window_size'))})"
        if ctx_pct >= cfg["ctx_crit_pct"]:
            seg = c(RED, seg)
            advice.append(c(RED, f"ctx {ctx_pct:.0f}% → compaction imminente : termine l'étape en cours, puis /compact"))
        elif ctx_pct >= cfg["ctx_warn_pct"]:
            seg = c(YEL, seg)
            advice.append(c(YEL, f"ctx {ctx_pct:.0f}% → /compact entre deux tâches (ou /clear si la tâche est finie)"))
        parts.append(seg)
    if data.get("exceeds_200k_tokens"):
        parts.append(c(RED, ">200K"))
    if pc:
        hr = pc.get("hit_ratio")
        seg = f"cache {hr * 100:.0f}%" if hr is not None else "cache ?"
        if pc.get("warm"):
            exp = pc.get("expires_at")
            if exp:
                mins = (exp - now) / 60
                seg += f" warm→{time.strftime('%H:%M', time.localtime(exp))}"
                if 0 < mins < cfg["cache_expiry_warn_min"]:
                    advice.append(c(YEL, f"cache expire dans {mins:.0f} min → continue avant, ou reprends plus tard depuis un résumé "
                                        f"(re-cache ≈ {fmt_k(pc.get('recache_tokens_if_cold'))})"))
            else:
                seg += " warm"
        else:
            seg += c(YEL, f" FROID (re-cache ≈ {fmt_k(pc.get('recache_tokens_if_cold'))})")
        parts.append(seg)
    q = []
    p5, p7 = g(rl, "five_hour", "used_percentage"), g(rl, "seven_day", "used_percentage")
    if p5 is not None:
        s = f"5h {p5:.0f}%"
        if p5 >= cfg["quota_5h_warn_pct"]:
            r5 = g(rl, "five_hour", "resets_at")
            s = c(YEL, s + (f" (reset {time.strftime('%H:%M', time.localtime(r5))})" if r5 else ""))
        q.append(s)
    if p7 is not None:
        s = f"7d {p7:.0f}%"
        if p7 >= cfg["quota_7d_warn_pct"]:
            s = c(RED, s)
        q.append(s)
    if q:
        parts.append(" ".join(q))
    if n_turn:
        seg = f"tour {n_turn}"
        if n_turn >= cfg["calls_per_turn_warn"]:
            seg = c(YEL, seg)
            advice.append(c(YEL, f"tour à {n_turn} appels → donne une cible de vérification ou découpe la tâche"))
        parts.append(seg)
    if rec["cost_usd"] is not None:
        parts.append(f"${rec['cost_usd']:.2f}")
    print(" | ".join(parts))
    if advice:
        print("→ " + "  ·  ".join(advice[:2]))


if __name__ == "__main__":
    main()
