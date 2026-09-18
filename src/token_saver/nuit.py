"""`token-saver nuit` — une session neuve par cycle, pilotée de l'extérieur (mode headless `claude -p`).

Pourquoi : dans une longue session, chaque appel relit tout l'historique (290-340K tokens en moyenne
mesurés sur les nuits d'autonomie) et la compaction, quand elle vient, perd des étapes du processus.
Ici chaque cycle (une carte) tourne dans une session neuve qui repart des fichiers d'état du projet
(ETAT.md, JOURNAL.md) — le mécanisme de reprise que le processus utilise déjà — sans jamais compacter,
avec un socle de démarrage réduit (pas d'outils de l'app desktop).

Commandes : `nuit start [--cycles N]` (détache le runner), `nuit stop [--now]`, `nuit status`, `nuit run` (boucle, interne).
Tout est journalisé dans .token-saver/state/nuit.log et .token-saver/metrics/nuit-cycles.jsonl.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .paths import ts_dir

DEFAULTS = {
    "claude_cmd": None,                 # liste argv ; None = détection automatique
    "prompt": None,                     # commande du projet qui fait exactement UN cycle (ex. `/autonomie 1`) ; vide = recette TOKEN SAVER (.token-saver/cycle.md)
    "model": "opus[1m]",                # 1M : un cycle entier tient sans compaction
    "permission_mode": "auto",          # même comportement que le mode Auto de l'app
    "cycle_timeout_min": 240,
    "max_hours": 12,
    "pause_between_s": 15,
    "retry_wait_min": 30,               # attente par défaut sur limite de quota si l'heure de reset n'est pas lisible
    "end_at": None,                     # heure de fin locale « HH:MM » (en plus de max_hours) : la nuit finit son cycle en cours puis s'arrête
    "env_checks": [],                   # vérifications d'environnement déclarées par le projet : {name, command, expect, required}
    "env_checks_auto": True,            # proposées à l'installation d'après ce que le projet contient (CI GitHub, appareil Android…)
}
STRIP_ENV_PREFIXES = ("CLAUDE_", "CLAUDECODE", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY")


def cfg_nuit(cfg: dict) -> dict:
    out = dict(DEFAULTS)
    out.update(cfg.get("nuit") or {})
    return out


def cycle_prompt(project: Path, cfg: dict) -> tuple[str | None, str]:
    """(texte envoyé à chaque session neuve, libellé lisible) : la commande du projet si `nuit.prompt` est fixé, sinon la recette
    TOKEN SAVER validée par l'utilisateur (.token-saver/cycle.md) ; (None, …) s'il n'y a ni l'une ni l'autre."""
    from . import recette
    p = cfg.get("prompt")
    if p:
        return p, p
    f = recette.recipe_path(project)
    if f.is_file():
        try:
            return f.read_text(encoding="utf-8"), f"recette TOKEN SAVER ({recette.RECIPE_FILE})"
        except OSError:
            pass
    return None, "aucune recette de cycle"


# ------------------------------------------------------------------ chemins & état

def state_dir(project: Path) -> Path:
    return ts_dir(project) / "state"


def state_file(project: Path) -> Path:
    return state_dir(project) / "nuit.json"


def stop_file(project: Path) -> Path:
    return state_dir(project) / "nuit.stop"


def log_file(project: Path) -> Path:
    return state_dir(project) / "nuit.log"


def cycles_file(project: Path) -> Path:
    return ts_dir(project) / "metrics" / "nuit-cycles.jsonl"


def read_state(project: Path) -> dict:
    try:
        return json.loads(state_file(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(project: Path, st: dict) -> None:
    """Écriture atomique, tolérante aux collisions Windows (un autre processus lit le fichier au même instant)."""
    state_dir(project).mkdir(parents=True, exist_ok=True)
    tmp = state_file(project).with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    for attempt in range(30):
        try:
            tmp.replace(state_file(project))
            return
        except PermissionError:
            time.sleep(0.05 * (attempt + 1))
    try:                                                   # dernier recours : écriture directe
        state_file(project).write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        tmp.unlink(missing_ok=True)


def log(project: Path, msg: str) -> None:
    state_dir(project).mkdir(parents=True, exist_ok=True)
    with open(log_file(project), "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")


# ------------------------------------------------------------------ binaire claude

def find_claude(cfg: dict) -> list[str] | None:
    """argv du CLI : config `nuit.claude_cmd`, sinon `claude` du PATH, sinon le binaire embarqué par l'app desktop."""
    if cfg.get("claude_cmd"):
        return list(cfg["claude_cmd"])
    from shutil import which
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = which(name)
        if p:
            return [p]
    cands = []
    la, ra = os.environ.get("LOCALAPPDATA"), os.environ.get("APPDATA")
    roots = []
    if ra:
        roots.append(ra)                                                   # visible seulement depuis les processus de l'app (virtualisation MSIX)
    if la:
        roots += [os.path.join(p, "LocalCache", "Roaming") for p in glob.glob(os.path.join(la, "Packages", "Claude_*"))]   # emplacement réel
        roots += [os.path.join(p, "LocalCache", "Local") for p in glob.glob(os.path.join(la, "Packages", "Claude_*"))]
        roots.append(la)
    for base in roots:
        cands += glob.glob(os.path.join(base, "Claude", "claude-code", "*", "claude.exe"))
        cands += glob.glob(os.path.join(base, "ClaudeLaunchers", "*", "claude-code", "*", "claude.exe"))
    cands = [c for c in cands if os.path.isfile(c)]
    if not cands:
        return None

    def ver(p: str) -> tuple:
        m = re.search(r"[\\/](\d+)\.(\d+)\.(\d+)[\\/]", p)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)
    return [max(cands, key=ver)]


KEEP_ENV = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")   # authentification par jeton `claude setup-token`, config de test


# CREATE_NO_WINDOW : lancé depuis un hook de l'app (sans console), claude.exe ouvrirait sinon une fenêtre noire (vu le 16/09 au soir).
_NOWIN = {"creationflags": 0x08000000} if os.name == "nt" else {}


def child_env() -> dict:
    """Environnement propre : le CLI autonome ne doit pas hériter du canal d'authentification de l'app."""
    return {k: v for k, v in os.environ.items()
            if k in KEEP_ENV or (not k.startswith(STRIP_ENV_PREFIXES) and k not in STRIP_ENV_PREFIXES)}


# ------------------------------------------------------------------ un cycle

def parse_reset(text: str) -> float | None:
    """« resets 12:30am (Europe/Paris) » / « réinitialisation dans 2 h 16 min » → horodatage epoch local, si lisible."""
    m = re.search(r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I)
    if m:
        h, mn, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        h = (h % 12) + (12 if ap == "pm" else 0)
        now = time.localtime()
        target = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, h, mn, 0, 0, 0, -1))
        if target <= time.time():
            target += 86400
        return target
    m = re.search(r"(\d+)\s*h\s*(\d+)?\s*min", text)
    if m:
        return time.time() + int(m.group(1)) * 3600 + int(m.group(2) or 0) * 60
    return None


def run_cycle(project: Path, cfg: dict, prompt: str, cycle_no: int, claude_cmd: list[str]) -> dict:
    """Lance `claude -p <prompt>` dans le projet et retourne un enregistrement normalisé."""
    sid = new_session_id(project)
    argv = list(claude_cmd) + ["-p", prompt, "--output-format", "json", "--permission-mode", cfg["permission_mode"],
                               "--permission-prompts", "none", "--session-id", sid]
    if cfg.get("model"):
        argv += ["--model", cfg["model"]]
    t0 = time.time()
    rec = {"cycle": cycle_no, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "cmd": " ".join(argv[1:4]), "session_id": sid}
    try:
        p = subprocess.run(argv, cwd=str(project), env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, **_NOWIN,
                           text=True, encoding="utf-8", errors="replace", timeout=cfg["cycle_timeout_min"] * 60)
        rec["exit"] = p.returncode
        try:
            d = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
        except ValueError:
            d = {}
        u = d.get("usage") or {}
        rec.update({
            "session_id": d.get("session_id") or sid, "is_error": bool(d.get("is_error")), "result": (d.get("result") or "")[:400],
            "num_turns": d.get("num_turns"), "cost_usd": d.get("total_cost_usd"),
            "input": u.get("input_tokens"), "cache_read": u.get("cache_read_input_tokens"),
            "cache_write": u.get("cache_creation_input_tokens"), "output": u.get("output_tokens"),
            "stderr": (p.stderr or "")[-400:],
        })
    except subprocess.TimeoutExpired:
        rec.update({"exit": -1, "is_error": True, "result": f"timeout after {cfg['cycle_timeout_min']} min"})
    except OSError as exc:
        rec.update({"exit": -2, "is_error": True, "result": f"cannot start claude: {exc}"})
    rec["duration_min"] = round((time.time() - t0) / 60, 1)
    return rec


def classify(rec: dict) -> str:
    """ok | auth | limit | error"""
    text = f"{rec.get('result', '')} {rec.get('stderr', '')}".lower()
    if not rec.get("is_error") and rec.get("exit") == 0:
        return "ok"
    if "not logged in" in text or "/login" in text or "authentication" in text:
        return "auth"
    if "limit" in text and ("reset" in text or "usage" in text or "rate" in text or "session limit" in text):
        return "limit"
    return "error"


AUTH_HELP = ("le CLI Claude n'est pas connecté hors de l'app. Une fois, dans un terminal : lance le binaire "
             "({claude}) puis tape /login (navigateur, même compte que l'app), puis /exit. Relance ensuite `nuit start`.")


def _sh(project: Path, args: list[str], timeout: int = 30) -> str:
    """Sortie d'une commande dans le projet, chaîne vide en cas d'erreur (aucune exception ne remonte)."""
    try:
        return subprocess.run(args, cwd=str(project), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, **_NOWIN).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


WAIT_WORDS = re.compile(r"j'attends|en attente|waiting for|holding the|attend(?:s|re)? (?:le|la|les|que)", re.I)


def leftover(project: Path, rec: dict) -> str:
    """Trace d'un cycle inachevé, dite au cycle suivant : échec, dernier mot en attente, commits non poussés, fichiers non commités.
    Observé les 16-17/09 : deux cycles arrêtés avant la fin (attente d'une campagne ; commit sans push) — le suivant a dû le deviner."""
    parts = []
    result = (rec.get("result") or "").strip()
    if rec.get("kind") != "ok":
        parts.append(f"il s'est terminé en échec ({result[:80]!r})")
    elif WAIT_WORDS.search(result):
        parts.append(f"son dernier mot était une attente : « {result[:120]} »")
    if (project / ".git").exists():
        branch = _sh(project, ["git", "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        ahead = _sh(project, ["git", "rev-list", "--count", "@{u}..HEAD"]).strip()
        dirty = [l for l in _sh(project, ["git", "status", "--porcelain"]).splitlines() if l.strip() and "token-saver" not in l]
        if ahead.isdigit() and int(ahead) > 0:
            parts.append(f"{ahead} commit(s) non poussé(s) sur la branche {branch}")
        if dirty:
            parts.append(f"{len(dirty)} fichier(s) modifié(s) ou nouveaux non commités sur la branche {branch}")
    return " ; ".join(parts)


def end_time(end_at: str | None, t_start: float) -> float | None:
    """Prochaine occurrence de « HH:MM » après t_start, en secondes époque ; None si absent ou illisible."""
    if not end_at:
        return None
    m = re.match(r"^\s*(\d{1,2})\s*[:hH]\s*(\d{2})?\s*$", str(end_at))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    if not (0 <= h < 24 and 0 <= mi < 60):
        return None
    lt = time.localtime(t_start)
    cand = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, mi, 0, 0, 0, -1))
    return cand + 86400 if cand <= t_start else cand


def run_env_checks(project: Path, cfg: dict) -> list[tuple[str, bool, str, bool]]:
    """Vérifications déclarées par le projet (nuit.env_checks) : (nom, ok, détail, requise). Jamais rien d'intégré en dur :
    un projet sans CI ni appareil n'a simplement aucune vérification."""
    out = []
    for c in cfg.get("env_checks") or []:
        cmd = c.get("command")
        if not cmd:
            continue
        name, expect, req = c.get("name") or cmd, str(c.get("expect") or "exit0"), bool(c.get("required"))
        try:
            p = subprocess.run(cmd, shell=True, cwd=str(project), capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=int(c.get("timeout_s") or 30), **_NOWIN)
            text = (p.stdout or "") + (p.stderr or "")
            if expect == "nonempty":
                ok = bool((p.stdout or "").strip())
            elif expect.startswith("regex:"):
                ok = re.search(expect[6:], text, re.I | re.M) is not None
            else:
                ok = p.returncode == 0
            lines = [l for l in text.strip().splitlines() if l.strip()]
            detail = (lines[-1][:100] if lines else f"code {p.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, detail = False, str(exc)[:80]
        out.append((name, ok, detail, req))
    return out


# ------------------------------------------------------------------ boucle

def run(project: Path, cfg_all: dict, cycles: int, prompt: str | None = None, until: str | None = None) -> int:
    cfg = cfg_nuit(cfg_all)
    claude_cmd = find_claude(cfg)
    if not claude_cmd:
        log(project, "ERREUR : binaire claude introuvable (config nuit.claude_cmd)")
        write_state(project, {**read_state(project), "status": "error", "error": "claude introuvable"})
        return 2
    if prompt:
        label = prompt
    else:
        prompt, label = cycle_prompt(project, cfg)
    if not prompt:
        log(project, "ERREUR : aucune recette de cycle (ni commande du projet, ni .token-saver/cycle.md) — /token-saver-autonomie t'accompagne pour la créer")
        write_state(project, {**read_state(project), "status": "error", "error": "aucune recette de cycle"})
        return 2
    end_at = until or cfg.get("end_at")
    st = {"status": "running", "pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "cycles_target": cycles,
          "cycles_done": 0, "cycles_failed": 0, "current": None, "claude": claude_cmd[0], "prompt": label, "model": cfg.get("model"),
          "end_at": end_at, "sessions": []}
    write_state(project, st)
    stop_file(project).unlink(missing_ok=True)
    log(project, f"démarrage : {cycles or 'illimité'} cycle(s), cycle = « {label} », modèle {cfg.get('model')}, claude = {claude_cmd[0]}"
                 + (f", fin à {end_at}" if end_at else ""))
    t_start = time.time()
    t_end = end_time(end_at, t_start)
    n = 0
    retries = 0
    last_hint = ""
    while True:
        if stop_file(project).exists():
            log(project, "arrêt demandé (nuit.stop) — fin propre")
            st["status"] = "stopped"
            stop_file(project).unlink(missing_ok=True)
            break
        if cycles and n >= cycles:
            st["status"] = "done"
            break
        if time.time() - t_start > cfg["max_hours"] * 3600:
            log(project, f"durée maximale atteinte ({cfg['max_hours']} h) — fin")
            st["status"] = "done"
            break
        if t_end and time.time() >= t_end:
            log(project, f"heure de fin atteinte ({end_at}) — fin")
            st["status"] = "done"
            break
        n_try = n + 1
        st["current"] = {"cycle": n_try, "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        write_state(project, st)
        previous = (f" Les {n_try - 1} cycle(s) précédent(s) de cette nuit sont déjà dans les fichiers d'état et le journal du projet : "
                    f"reprends là où ils se sont arrêtés, ne recommence pas leur numérotation." if n_try > 1 else
                    " C'est le premier cycle de cette nuit.")
        if last_hint:
            previous += (f" ATTENTION : le cycle précédent ({n_try - 1}) semble inachevé : {last_hint}. "
                         "Reprends ce travail et termine-le (push, PR, journal) avant d'en commencer un autre.")
        full_prompt = (f"{prompt}\n\nNote du runner TOKEN SAVER : cycle {n_try} de la nuit du {time.strftime('%Y-%m-%d')} ; "
                       f"chaque cycle tourne dans une session neuve.{previous} "
                       f"Personne ne répondra à une question : décide, note la décision au journal, continue.")
        log(project, f"cycle {n_try} : lancement")
        rec = run_cycle(project, cfg, full_prompt, n_try, claude_cmd)
        kind = classify(rec)
        rec["kind"] = kind
        cycles_file(project).parent.mkdir(parents=True, exist_ok=True)
        with open(cycles_file(project), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if rec.get("session_id"):
            st["sessions"] = (st.get("sessions") or []) + [rec["session_id"]]
        last_hint = leftover(project, rec)
        if last_hint:
            log(project, f"cycle {n_try} : trace d'inachevé notée pour le suivant — {last_hint[:160]}")
        st["last_hint"] = last_hint
        if kind == "ok":
            n += 1
            retries = 0
            st["cycles_done"] = n
            ctx = (rec.get("input") or 0) + (rec.get("cache_read") or 0) + (rec.get("cache_write") or 0)
            log(project, f"cycle {n_try} : terminé en {rec['duration_min']} min, {rec.get('num_turns')} tours, "
                         f"{ctx / 1e6:.1f}M tokens relus, {(rec.get('output') or 0) / 1e3:.0f}K écrits, session {rec.get('session_id')}")
            if "[nuit:fin]" in (rec.get("result") or ""):
                log(project, "le cycle signale qu'il n'y a plus de tâche — fin de la nuit")
                st["status"] = "done"
                break
            time.sleep(cfg["pause_between_s"])
            continue
        if kind == "auth":
            log(project, "ERREUR : " + AUTH_HELP.format(claude=claude_cmd[0]))
            st.update({"status": "error", "error": "auth"})
            break
        if kind == "limit":
            reset = parse_reset(rec.get("result", "") + " " + rec.get("stderr", ""))
            wait = max(60, (reset - time.time()) + 90) if reset else cfg["retry_wait_min"] * 60
            log(project, f"limite de quota atteinte — reprise dans {wait / 60:.0f} min ({rec.get('result', '')[:120]})")
            st["current"] = {"cycle": n_try, "waiting_until": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() + wait))}
            write_state(project, st)
            time.sleep(wait)
            continue
        text = f"{rec.get('result', '')} {rec.get('stderr', '')}".lower()
        if cfg.get("model") and "model" in text and retries == 0:      # variante de modèle refusée (ex. [1m]) : on réessaie sans
            log(project, f"cycle {n_try} : modèle « {cfg['model']} » refusé — nouvel essai avec le modèle par défaut")
            cfg["model"] = None
            st["model"] = None
            continue
        retries += 1
        st["cycles_failed"] += 1
        log(project, f"cycle {n_try} : échec ({rec.get('result', '')[:160]!r}, code {rec.get('exit')}) — tentative {retries}/2")
        if retries >= 2:
            st.update({"status": "error", "error": "cycle failed twice"})
            break
        time.sleep(120)
    st["current"] = None
    st["ended"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    write_state(project, st)
    log(project, f"fin : {st['cycles_done']} cycle(s) terminé(s), statut {st['status']}")
    try:
        b = bilan(project, cfg_all, st)
        log(project, "bilan écrit : " + render_bilan(b).splitlines()[0])
    except Exception as exc:  # noqa: BLE001 — le bilan ne doit jamais faire échouer la fin de nuit
        log(project, f"bilan non calculé : {exc!s:.160}")
    return 0 if st["status"] in ("done", "stopped") else 1


# ------------------------------------------------------------------ bilan de fin de nuit (mesuré, générique : gh et GitHub optionnels)

def bilans_file(project: Path) -> Path:
    return ts_dir(project) / "metrics" / "nuit-bilans.jsonl"


def bilan_md(project: Path) -> Path:
    return state_dir(project) / "nuit-bilan.md"


def _to_utc_iso(local_iso: str) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.mktime(time.strptime(local_iso, "%Y-%m-%dT%H:%M:%S"))))
    except (ValueError, OverflowError):
        return local_iso


def bilan(project: Path, cfg_all: dict, st: dict | None = None) -> dict:
    """Bilan mesuré de la nuit décrite par `st` (défaut : dernier état) : cycles, tokens (transcripts, sous-agents inclus),
    revues, PR fusionnées si le projet est sur GitHub, commits sinon ; comparé au bilan précédent. Écrit jsonl + markdown."""
    import shutil
    import statistics
    st = st if st is not None else read_state(project)
    started, ended = st.get("started") or "", st.get("ended") or time.strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    cf = cycles_file(project)
    if cf.is_file():
        rows = [json.loads(l) for l in cf.read_text(encoding="utf-8").splitlines() if l.strip()]
        known = set(s for s in (st.get("sessions") or []) if s)
        rows = [r for r in rows if r.get("session_id") in known] if known else [r for r in rows if r.get("started", "") >= started]
    sessions = [s for s in (st.get("sessions") or []) if s] or [r.get("session_id") for r in rows if r.get("session_id")]
    b: dict = {"started": started, "ended": ended, "status": st.get("status"), "cycles_ok": sum(1 for r in rows if r.get("kind") == "ok"),
               "cycles_failed": sum(1 for r in rows if r.get("kind") not in ("ok", None)), "cycle_minutes": round(sum(r.get("duration_min") or 0 for r in rows)),
               "cycle_minutes_median": round(statistics.median([r.get("duration_min") or 0 for r in rows])) if rows else 0}
    try:
        b["hours"] = round((time.mktime(time.strptime(ended, "%Y-%m-%dT%H:%M:%S")) - time.mktime(time.strptime(started, "%Y-%m-%dT%H:%M:%S"))) / 3600, 1)
    except (ValueError, OverflowError):
        b["hours"] = None
    # tokens : transcripts (sous-agents inclus) via la base du projet ; repli : usages renvoyés par `claude -p`
    tok = None
    try:
        from .collect import collect
        from .db import connect
        from .paths import default_db_path, transcript_dirs
        if transcript_dirs(project) and sessions:
            con = connect(default_db_path(project))
            collect(con, project, ts_logs=False)
            q = ",".join("?" * len(sessions))
            r = con.execute(f"SELECT COUNT(*), COALESCE(SUM(input+cache_write+cache_read),0), COALESCE(AVG(ctx),0), COALESCE(MAX(ctx),0), "
                            f"COALESCE(SUM(output+COALESCE(thinking,0)),0) FROM calls WHERE session_id IN ({q})", sessions).fetchone()
            if r and r[0]:
                tok = {"calls": r[0], "tokens_read": int(r[1]), "ctx_avg": int(r[2]), "ctx_max": int(r[3]), "output": int(r[4]), "source": "transcripts"}
                ev = dict(con.execute(f"SELECT kind, COUNT(*) FROM events WHERE session_id IN ({q}) GROUP BY kind", sessions).fetchall())
                tok["compactions"] = int(ev.get("compact", 0))
                tok["limits"] = int(ev.get("limit_hit", 0))
    except Exception:  # noqa: BLE001
        tok = None
    if tok is None:
        tok = {"calls": None, "tokens_read": sum((r.get("input") or 0) + (r.get("cache_read") or 0) + (r.get("cache_write") or 0) for r in rows),
               "ctx_avg": None, "ctx_max": None, "output": sum(r.get("output") or 0 for r in rows), "source": "résultats des cycles (sans sous-agents)",
               "compactions": None, "limits": None}
    b.update(tok)
    # revues (revue bornée)
    rl = ts_dir(project) / "metrics" / "review-log.jsonl"
    rounds = []
    if rl.is_file():
        for l in rl.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(l)
            except ValueError:
                continue
            if r.get("ts", "") >= started:
                rounds.append(r)
    per = {}
    for r in rounds:
        per[r.get("branch")] = per.get(r.get("branch"), 0) + 1
    b["reviews"] = {"rounds": len(rounds), "branches": len(per), "max_per_branch": max(per.values()) if per else 0,
                    "blocked": sum(1 for r in rounds if r.get("verdict") == "BLOQUE"), "red": sum(int(r.get("red") or 0) for r in rounds)}
    # livraison : PR fusionnées (GitHub + gh) sinon commits
    b["prs"], b["prs_titles"], b["commits"] = None, [], None
    if (project / ".git").exists():
        remote = _sh(project, ["git", "remote", "get-url", "origin"]).strip()
        if "github.com" in remote and shutil.which("gh"):
            out = _sh(project, ["gh", "pr", "list", "--state", "merged", "--limit", "100", "--json", "number,title,mergedAt"], timeout=60)
            try:
                prs = [p for p in json.loads(out or "[]") if (p.get("mergedAt") or "") >= _to_utc_iso(started)]
                b["prs"], b["prs_titles"] = len(prs), [f"#{p['number']} {p['title'][:70]}" for p in sorted(prs, key=lambda p: p["mergedAt"])]
            except ValueError:
                pass
        if b["prs"] is None:
            _sh(project, ["git", "fetch", "-q"], timeout=60)
            from .review import default_base
            base = default_base(project) or "HEAD"
            cnt = _sh(project, ["git", "rev-list", "--count", f"--since={started}", base]).strip()
            b["commits"] = int(cnt) if cnt.isdigit() else None
    # comparaison avec le bilan précédent
    prev = None
    bf = bilans_file(project)
    if bf.is_file():
        for l in bf.read_text(encoding="utf-8").splitlines():
            try:
                cand = json.loads(l)
            except ValueError:
                continue
            if cand.get("started") and cand["started"] < started:
                prev = cand
    b["previous"] = {k: prev.get(k) for k in ("started", "tokens_read", "cycles_ok", "prs", "commits", "hours")} if prev else None
    bf.parent.mkdir(parents=True, exist_ok=True)
    existing = [l for l in bf.read_text(encoding="utf-8").splitlines() if l.strip()] if bf.is_file() else []
    existing = [l for l in existing if json.loads(l).get("started") != started]          # recalcul de la même nuit : remplace
    bf.write_text("\n".join(existing + [json.dumps(b, ensure_ascii=False)]) + "\n", encoding="utf-8")
    bilan_md(project).parent.mkdir(parents=True, exist_ok=True)
    bilan_md(project).write_text(render_bilan(b) + "\n", encoding="utf-8")
    return b


def _fm(n) -> str:
    return "?" if n is None else (f"{n / 1e9:.2f} Md" if n >= 1e9 else f"{n / 1e6:.0f} M" if n >= 1e6 else f"{n / 1e3:.0f} K" if n >= 1e3 else str(n))


def _delta(cur, prev) -> str:
    if cur is None or not prev:
        return ""
    return f" ({'+' if cur >= prev else ''}{(cur - prev) / prev * 100:.0f} % vs la précédente)"


def render_bilan(b: dict) -> str:
    p = b.get("previous") or {}
    L = [f"BILAN DE L'AUTONOMIE du {b.get('started', '')[:10]} : {b.get('hours') or '?'} h, {b['cycles_ok']} cycle(s) terminé(s), {b['cycles_failed']} échec(s), "
         f"statut {b.get('status')} ; {b['cycle_minutes']} min de travail, {b['cycle_minutes_median']} min par cycle en médiane."]
    if b.get("prs") is not None:
        L.append(f"Livraison : {b['prs']} PR fusionnée(s){_delta(b['prs'], p.get('prs'))}.")
        L += [f"  - {t}" for t in b.get("prs_titles", [])[:12]]
    elif b.get("commits") is not None:
        L.append(f"Livraison : {b['commits']} commit(s) sur la branche principale{_delta(b['commits'], p.get('commits'))} (pas de dépôt GitHub : PR non comptées).")
    else:
        L.append("Livraison : pas de dépôt git, rien à compter.")
    L.append(f"Tokens relus : {_fm(b.get('tokens_read'))}{_delta(b.get('tokens_read'), p.get('tokens_read'))} ; source : {b.get('source')}."
             + (f" Appels : {b['calls']} ; contexte moyen {_fm(b.get('ctx_avg'))}, maximum {_fm(b.get('ctx_max'))}." if b.get("calls") else ""))
    if b.get("compactions") is not None:
        L.append(f"Continuité : {b['compactions']} compaction(s), {b['limits']} limite(s) de quota.")
    r = b.get("reviews") or {}
    if r.get("rounds"):
        L.append(f"Revues : {r['rounds']} tour(s) sur {r['branches']} branche(s), au plus {r['max_per_branch']} par branche, "
                 f"{r['blocked']} tour(s) bloqué(s), {r['red']} 🔴 trouvé(s) et corrigé(s) avant fusion.")
    if p:
        L.append(f"Autonomie précédente ({p.get('started', '')[:10]}) : {p.get('cycles_ok')} cycle(s), {_fm(p.get('tokens_read'))} relus"
                 + (f", {p['prs']} PR" if p.get("prs") is not None else "") + ".")
    return "\n".join(L)


# ------------------------------------------------------------------ contrôle avant départ

def auth_marker(project: Path) -> Path:
    return state_dir(project) / "nuit-auth-ok"


def ping(project: Path, cfg: dict, claude_cmd: list[str]) -> tuple[bool, str]:
    """Un appel minimal (1 tour) pour vérifier la connexion du CLI. Coût : un socle de session (~20K tokens)."""
    argv = list(claude_cmd) + ["-p", "Réponds exactement : OK", "--max-turns", "1", "--output-format", "json", "--permission-mode", "dontAsk",
                               "--session-id", new_session_id(project)]
    try:
        p = subprocess.run(argv, cwd=str(project), env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, **_NOWIN, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        d = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
    except (subprocess.TimeoutExpired, OSError, ValueError) as exc:
        return False, f"impossible d'exécuter le CLI : {exc}"
    rec = {"exit": p.returncode, "is_error": bool(d.get("is_error")), "result": d.get("result") or "", "stderr": p.stderr or ""}
    kind = classify(rec)
    if kind == "ok":
        auth_marker(project).parent.mkdir(parents=True, exist_ok=True)
        auth_marker(project).write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        return True, f"connecté (modèle par défaut : {', '.join((d.get('modelUsage') or {}).keys()) or '?'})"
    if kind == "auth":
        return False, AUTH_HELP.format(claude=claude_cmd[0])
    if kind == "limit":
        return False, f"limite de quota en cours : {rec['result'][:120]}"
    return False, f"erreur : {rec['result'][:160] or rec['stderr'][:160]}"


def own_sessions_file(project: Path) -> Path:
    return state_dir(project) / "nuit-sessions.json"


def own_sessions(project: Path) -> set[str]:
    try:
        return set(json.loads(own_sessions_file(project).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return set()


def new_session_id(project: Path) -> str:
    """Identifiant de session choisi par le runner (ping ou cycle), mémorisé pour ne pas se prendre pour une session étrangère."""
    import uuid
    sid = str(uuid.uuid4())
    ids = own_sessions(project)
    ids.add(sid)
    state_dir(project).mkdir(parents=True, exist_ok=True)
    own_sessions_file(project).write_text(json.dumps(sorted(ids)[-500:]), encoding="utf-8")
    return sid


def last_activity_minutes(project: Path) -> float | None:
    """Minutes depuis la dernière écriture d'un transcript de session du projet, hors sessions du runner (None si aucun)."""
    from .paths import transcript_dirs
    mine = own_sessions(project)
    if os.environ.get("CLAUDE_CODE_SESSION_ID"):          # la session de l'app depuis laquelle on lance la nuit : elle rend la main
        mine.add(os.environ["CLAUDE_CODE_SESSION_ID"])
    newest = 0.0
    for d in transcript_dirs(project):
        for f in d.glob("*.jsonl"):
            if f.stem in mine:
                continue
            try:
                newest = max(newest, f.stat().st_mtime)
            except OSError:
                pass
    return (time.time() - newest) / 60 if newest else None


def check(project: Path, cfg_all: dict, do_ping: bool = True) -> list[tuple[str, bool, str]]:
    """Contrôle avant départ : binaire, commande du cycle présente dans le projet, connexion du CLI, projet libre."""
    cfg = cfg_nuit(cfg_all)
    R: list[tuple[str, bool, str]] = []
    idle = last_activity_minutes(project)
    R.append(("aucune autre session ne travaille sur le projet (silence ≥ 10 min)", idle is None or idle >= 10,
              "aucun transcript" if idle is None else f"dernière activité il y a {idle:.0f} min"
              + ("" if idle >= 10 else " → arrête d'abord le mode autonome de l'app (/stop-autonomie) : deux équipes sur le même code se gêneraient")))
    claude_cmd = find_claude(cfg)
    R.append(("binaire claude trouvé", bool(claude_cmd), claude_cmd[0] if claude_cmd else "indique `nuit.claude_cmd` dans .token-saver/config.json"))
    text, label = cycle_prompt(project, cfg)
    if cfg.get("prompt"):                                   # commande du projet : elle doit exister
        name = (cfg["prompt"].split() or ["/"])[0].lstrip("/")
        has_cmd = (project / ".claude" / "skills" / name / "SKILL.md").is_file() or (project / ".claude" / "commands" / f"{name}.md").is_file()
        R.append(("recette de cycle disponible", has_cmd, f"commande du projet `{cfg['prompt']}`" + ("" if has_cmd else " : skill ou commande absente")))
    else:
        R.append(("recette de cycle disponible", bool(text), label if text else "aucune : /token-saver-autonomie t'accompagne pour la créer"))
    R.append(("mode de permission non interactif", cfg["permission_mode"] in ("auto", "acceptEdits", "dontAsk", "bypassPermissions"), cfg["permission_mode"]))
    if claude_cmd and do_ping:
        ok, detail = ping(project, cfg, claude_cmd)
        R.append(("CLI connecté (appel de test, 1 tour)", ok, detail))
    elif claude_cmd:
        m = auth_marker(project)
        R.append(("CLI connecté (dernier test)", m.is_file(), m.read_text(encoding="utf-8") if m.is_file() else "jamais vérifié : `nuit check`"))
    for name, ok, detail, required in run_env_checks(project, cfg):     # déclarées par le projet ; avertissent, ne bloquent que si « required »
        R.append((f"environnement : {name}", ok or not required,
                  detail if ok else ("⚠ " + detail + (" — requis : l'autonomie ne part pas" if required else " — l'autonomie part quand même, sans cette validation"))))
    return R


# ------------------------------------------------------------------ commandes

def start(project: Path, cfg_all: dict, cycles: int, prompt: str | None, launcher: list[str], until: str | None = None) -> str:
    """Détache la boucle dans un processus indépendant de la session courante — après un contrôle complet."""
    st = read_state(project)
    if st.get("status") == "running" and _alive(st.get("pid")):
        return f"déjà en cours (pid {st['pid']}, {st.get('cycles_done', 0)} cycle(s) fait(s)) — /token-saver-status · /token-saver-autonomie-stop"
    cfg = cfg_nuit(cfg_all)
    if not prompt and cycle_prompt(project, cfg)[0] is None:   # pas de recette : l'accompagnement (inventaire + proposition) avant tout contrôle
        from . import recette
        return recette.propose(project, cfg_all)
    m = auth_marker(project)
    fresh = m.is_file() and (time.time() - m.stat().st_mtime) < 12 * 3600
    results = check(project, cfg_all, do_ping=not fresh)
    if not all(ok for _, ok, _ in results):
        from .install import render_check
        return "autonomie NON lancée — corriger d'abord :\n" + render_check(results)
    warnings = [f"⚠ {label[len('environnement : '):]} : {detail[2:].strip()}" for label, _, detail in results
                if label.startswith("environnement : ") and detail.startswith("⚠")]
    claude_cmd = find_claude(cfg)
    if not claude_cmd:
        return "binaire claude introuvable : indique `nuit.claude_cmd` dans .token-saver/config.json"
    if until and end_time(until, time.time()) is None:
        return f"heure de fin illisible : « {until} » (attendu HH:MM)"
    argv = (list(launcher) + ["nuit", "run", "--project", str(project), "--cycles", str(cycles)] + (["--prompt", prompt] if prompt else [])
            + (["--until", until] if until else []))
    state_dir(project).mkdir(parents=True, exist_ok=True)
    out = open(state_dir(project) / "nuit.runner.out", "a", encoding="utf-8")
    kw: dict = {"cwd": str(project), "env": child_env(), "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen(argv, **kw)
    end_at = until or cfg.get("end_at")
    write_state(project, {"status": "running", "pid": p.pid, "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "cycles_target": cycles,
                          "cycles_done": 0, "cycles_failed": 0, "current": None, "claude": claude_cmd[0],
                          "prompt": prompt or cycle_prompt(project, cfg)[1], "end_at": end_at})
    return (f"autonomie lancée en sessions neuves (pid {p.pid}) : {cycles or 'illimité'} cycle(s), claude = {claude_cmd[0]}"
            + (f", fin à {end_at} au plus tard" if end_at else f", {cfg['max_hours']} h au plus") + "\n"
            f"suivi : /token-saver-status · arrêt à la fin du cycle : /token-saver-autonomie-stop · tout de suite : /token-saver-autonomie-stop --now"
            + ("\n" + "\n".join(warnings) if warnings else ""))


def _alive(pid) -> bool:
    if not pid:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True).stdout
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except Exception:  # noqa: BLE001
        return False


def stop(project: Path, now: bool = False) -> str:
    st = read_state(project)
    stop_file(project).parent.mkdir(parents=True, exist_ok=True)
    stop_file(project).write_text(time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
    if not now:                                            # le runner seul écrit l'état : ici, juste le drapeau
        return "arrêt demandé : l'autonomie s'arrêtera à la fin du cycle en cours (ou /token-saver-autonomie-stop --now pour couper tout de suite)"
    pid = st.get("pid")
    if pid and _alive(pid):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        else:
            import signal
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        log(project, "arrêt immédiat demandé (processus coupé)")
    st.update({"status": "stopped", "current": None, "ended": time.strftime("%Y-%m-%dT%H:%M:%S")})
    write_state(project, st)
    return "autonomie arrêtée immédiatement (le cycle en cours est interrompu : son état est dans le journal du projet)"


def live(project: Path, st: dict | None = None) -> str:
    """Ce que fait le cycle en cours, à l'instant : dernière écriture, appels, contexte, derniers outils, dernier texte de Claude.
    Lu dans le transcript de la session du cycle (aucun appel au modèle : zéro token)."""
    from .paths import transcript_dirs
    st = st if st is not None else read_state(project)
    cur = st.get("current") or {}
    sid = cur.get("session")
    files = []
    for d in transcript_dirs(project):
        if sid and (d / f"{sid}.jsonl").is_file():
            files.append(d / f"{sid}.jsonl")
    if not files:                                          # session inconnue : le transcript le plus récent parmi ceux du runner
        mine = own_sessions(project)
        for d in transcript_dirs(project):
            files += [f for f in d.glob("*.jsonl") if f.stem in mine]
        if not files:
            return ""
    f = max(files, key=lambda p: p.stat().st_mtime)
    calls, tools, last_text, ctx = 0, [], "", 0
    try:
        for line in f.open(encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") or {}
            if rec.get("type") != "assistant":
                continue
            u = msg.get("usage") or {}
            if u:
                calls += 1
                ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    inp = c.get("input") or {}
                    what = inp.get("description") or inp.get("command") or inp.get("file_path") or inp.get("skill") or inp.get("pattern") or ""
                    tools.append(f"{c.get('name')} — {str(what)[:90]}".replace("\n", " "))
                elif c.get("type") == "text" and (c.get("text") or "").strip():
                    last_text = c["text"].strip()
    except OSError:
        return ""
    age = (time.time() - f.stat().st_mtime) / 60
    L = [f"à l'instant : dernière action il y a {age:.0f} min · {calls} appels · contexte {ctx // 1000}K"]
    L += [f"  {t}" for t in tools[-4:]]
    if last_text:
        L.append("  Claude : " + last_text[:280].replace("\n", " ") + ("…" if len(last_text) > 280 else ""))
    return "\n".join(L)


STATUS_FR = {"running": "en cours", "done": "terminée", "stopped": "arrêtée", "error": "en erreur"}


def status(project: Path) -> str:
    st = read_state(project)
    if not st:
        return "autonomie : aucune lancée dans ce projet"
    alive = st.get("status") == "running" and _alive(st.get("pid"))
    L = [f"autonomie : {STATUS_FR.get(st.get('status'), st.get('status'))}{'' if alive or st.get('status') != 'running' else ' (processus absent)'} · démarrée {st.get('started', '?')[:16].replace('T', ' ')}"
         f" · cycles faits {st.get('cycles_done', 0)}/{st.get('cycles_target') or '∞'} · échecs {st.get('cycles_failed', 0)}"]
    if st.get("current"):
        L.append(f"en cours : cycle {st['current'].get('cycle')} depuis {st['current'].get('started', '')[11:16]}"
                 + (f" (attente quota jusqu'à {st['current']['waiting_until'][11:16]})" if st["current"].get("waiting_until") else ""))
        if alive:
            lv = live(project, st)                     # ce que fait le cycle à l'instant, lu dans son transcript (zéro token)
            if lv:
                L.append(lv)
    if st.get("error"):
        L.append(f"erreur : {st['error']}")
    cf = cycles_file(project)
    if cf.is_file():
        rows = [json.loads(l) for l in cf.read_text(encoding="utf-8").splitlines() if l.strip()]
        rows = [r for r in rows if r.get("started", "") >= st.get("started", "")]
        for r in rows[-8:]:
            ctx = (r.get("input") or 0) + (r.get("cache_read") or 0) + (r.get("cache_write") or 0)
            L.append(f"  cycle {r['cycle']:2} {r.get('kind', '?'):5} {r.get('duration_min', 0):5.0f} min  {r.get('num_turns') or 0:4} tours  "
                     f"{ctx / 1e6:5.1f}M relus  {(r.get('output') or 0) / 1e3:4.0f}K écrits  {(r.get('result') or '')[:60]!r}")
    lf = log_file(project)
    if lf.is_file():
        L.append("dernières lignes du journal :")
        L += ["  " + l for l in lf.read_text(encoding="utf-8").splitlines()[-4:]]
    bm = bilan_md(project)
    if not alive and st.get("status") != "running" and bm.is_file():
        try:
            head = bm.read_text(encoding="utf-8").strip()
            if st.get("started", "")[:10] and st["started"][:10] in head.splitlines()[0]:
                L += ["", head]
        except OSError:
            pass
    return "\n".join(L)
