"""Hooks de contrôle TOKEN SAVER (opt-in, fail-open, mesurés).

H1 read-ledger   : PreToolUse/PostToolUse sur Read — refuse une relecture strictement identique
                   d'un fichier inchangé, déjà dans le contexte (un second appel identique passe).
H2 compact-reset : SessionStart(compact|clear|resume) + PreCompact — remet le ledger à zéro,
                   prend un instantané avant compaction, injecte un rappel factuel ≤ 300 tokens après.
Retiré le 14/09/2026 (EXP-04) : H5 « compaction aux fins de cycle », qui abaissait temporairement
`autoCompactWindow` — ce réglage n'est lu qu'au démarrage/à la reprise d'une session, et un hook gardé par
une règle `if` s'exécute aussi sur les commandes que Claude Code ne sait pas analyser (boucles `for`…).
Résultat : fenêtre bloquée à 100K sous le plancher post-compaction (~117K) → boucle de compactions.
Règle : un hook `if` doit toujours revérifier lui-même la commande ; ne jamais piloter la compaction par réglage.
Toute erreur => sortie "{}" (aucun effet). Chaque exécution est journalisée dans
<projet>/.token-saver/metrics/hook-events.jsonl (latence, action, tokens injectés).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from .config import load_config
from .paths import localize, py_cmd, ts_dir

READ_DEFAULT_LINES = 2000          # le Read natif lit au plus 2000 lignes sans `limit`
TAIL_BYTES = 262_144               # portion de transcript relue pour snapshot / détection de nettoyage
BRIEF_MAX_CHARS = 1200             # ≈ 300 tokens


def _norm(p: str | None) -> str | None:
    return p.replace("\\", "/").lower() if p else None


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class Ctx:
    """Contexte d'exécution d'un hook : projet, config, chemins, journal."""

    def __init__(self, payload: dict, project: Path | None = None):
        self.payload = payload
        self.t0 = time.perf_counter()
        self.project = project or self._find_project(payload)
        self.cfg = load_config(self.project)
        self.tsd = ts_dir(self.project) if self.project else None
        self.session = payload.get("session_id")
        self.transcript = payload.get("transcript_path") or ""
        key = hashlib.sha1(self.transcript.encode("utf-8", "replace")).hexdigest()[:12] if self.transcript else "na"
        self.ledger_path = (self.tsd / "state" / f"ledger-{key}.json") if self.tsd else None

    @staticmethod
    def _find_project(payload: dict) -> Path | None:
        env = os.environ.get("CLAUDE_PROJECT_DIR")
        for cand in (env, payload.get("cwd")):
            if cand and (Path(cand) / ".token-saver").is_dir():
                return Path(cand)
        here = Path(__file__).resolve()
        for parent in here.parents:           # .../.token-saver/bin/token-saver.pyz/token_saver/hooks.py
            if parent.name == ".token-saver":
                return parent.parent
        return Path(env) if env else None

    # ----- ledger -----
    def load_ledger(self) -> dict:
        try:
            if self.ledger_path and self.ledger_path.is_file():
                d = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except (OSError, ValueError):
            pass
        return {"transcript": self.transcript, "reads": {}, "read_events": 0, "reset_at": _now_iso()}

    def save_ledger(self, led: dict) -> None:
        if not self.ledger_path:
            return
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ledger_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(led), encoding="utf-8")
            tmp.replace(self.ledger_path)
        except OSError:
            pass

    # ----- journal -----
    def log(self, hook: str, action: str, tool: str | None = None, chars_before: int | None = None,
            chars_after: int | None = None, tokens_injected: int = 0, note: str | None = None) -> None:
        if not self.tsd:
            return
        rec = {"ts": _now_iso(), "hook": hook, "session_id": self.session, "transcript": self.transcript, "tool": tool,
               "action": action, "latency_ms": round((time.perf_counter() - self.t0) * 1000, 1), "chars_before": chars_before,
               "chars_after": chars_after, "tokens_injected": tokens_injected, "note": note}
        self._append(self.tsd / "metrics" / "hook-events.jsonl", rec)

    def saving(self, source: str, registry: str, tokens: int, method: str, confidence: str) -> None:
        if not self.tsd:
            return
        self._append(self.tsd / "metrics" / "savings.jsonl",
                     {"ts": _now_iso(), "session_id": self.session, "source": source, "registry": registry, "tokens": int(tokens),
                      "method": method, "confidence": confidence})

    @staticmethod
    def _append(path: Path, rec: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


# ------------------------------------------------------------------ H1

import re as _re

_READ_CMDS = [
    # (regex, start_group, end_group) — lectures pures d'un seul fichier, sans pipe ni redirection
    (_re.compile(r"^\s*(?:rtk\s+read|cat)(?:\s+-[nbAE]+)*\s+(?P<f>\"[^\"]+\"|'[^']+'|[^\s|;&<>]+)\s*$"), None, None),
    (_re.compile(r"^\s*sed\s+-n\s+['\"]?(?P<a>\d+),(?P<b>\d+)p['\"]?\s+(?P<f>\"[^\"]+\"|'[^']+'|[^\s|;&<>]+)\s*$"), "a", "b"),
    (_re.compile(r"^\s*head\s+(?:-n\s*|-)(?P<b>\d+)\s+(?P<f>\"[^\"]+\"|'[^']+'|[^\s|;&<>]+)\s*$"), None, "b"),
    (_re.compile(r"^\s*(?:Get-Content|gc|type)\s+(?:-Path\s+)?(?P<f>\"[^\"]+\"|'[^']+'|[^\s|;&<>]+)\s*$", _re.I), None, None),
]


def parse_read_command(cmd: str, cwd: str | None) -> tuple[str, int, int | None] | None:
    """(chemin absolu, début, fin|None) si la commande est une lecture pure d'un fichier, sinon None."""
    if not cmd or "\n" in cmd:
        return None
    for rx, ga, gb in _READ_CMDS:
        m = rx.match(cmd)
        if not m:
            continue
        f = m.group("f").strip("\"'")
        if not os.path.isabs(f) and cwd:
            f = os.path.join(cwd, f)
        start = int(m.group(ga)) if ga else 1
        end = int(m.group(gb)) if gb else None
        return f, start, end
    return None


def _requested_range(inp: dict) -> tuple[int, int | None]:
    start = int(inp.get("offset") or 1)
    limit = inp.get("limit")
    return start, (start + int(limit) - 1) if limit else None


def _read_request(p: dict) -> tuple[str, int, int | None, str] | None:
    """Normalise Read / Bash / PowerShell en (chemin, début, fin|None, outil)."""
    tool = p.get("tool_name")
    inp = p.get("tool_input") or {}
    if tool == "Read":
        if not inp.get("file_path"):
            return None
        s, e = _requested_range(inp)
        return inp["file_path"], s, e, "Read"
    if tool in ("Bash", "PowerShell"):
        r = parse_read_command(inp.get("command") or "", p.get("cwd"))
        return (r[0], r[1], r[2], tool) if r else None
    return None


def _file_sig(path: str) -> tuple | None:
    try:
        st = os.stat(path)
        return (int(st.st_mtime), st.st_size)
    except OSError:
        return None


def _tool_results_cleared_since(transcript: str, since_iso: str) -> bool:
    """Vrai si le transcript montre un nettoyage de résultats d'outils (context_management.applied_edits) après since_iso."""
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in tail.splitlines():
        if '"applied_edits":[{' in line or '"applied_edits": [{' in line:
            i = line.find('"timestamp":"')
            ts = line[i + 13:i + 32] if i >= 0 else ""
            if not ts or ts >= since_iso:
                return True
    return False


def h1_pre(ctx: Ctx) -> dict:
    p = ctx.payload
    if not ctx.cfg["features"].get("H1_read_ledger"):
        return {}
    req = _read_request(p)
    if not req:
        return {}
    raw_path, start, end, tool = req
    path = _norm(raw_path)
    if not path or path.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ipynb")):
        return {}
    led = ctx.load_ledger()
    e = led["reads"].get(path)
    if not e:
        ctx.log("H1", "allow", tool, note="first read")
        return {}
    sig = _file_sig(raw_path)
    if sig is None or list(sig) != e.get("sig"):
        ctx.log("H1", "allow", tool, note="file changed")
        return {}
    e_start, e_end = e["start"], e["start"] + e["num"] - 1
    total = e.get("total") or e_end
    req_end = end if end is not None else (min(total, start + READ_DEFAULT_LINES - 1) if tool == "Read" else total)
    if start < e_start or req_end > e_end:
        ctx.log("H1", "allow", tool, note="range not covered")
        return {}
    cfg = ctx.cfg["ledger"]
    age_min = (time.time() - e.get("t", 0)) / 60
    if age_min > cfg["ttl_minutes"] or led["read_events"] - e.get("n", 0) > cfg["ttl_calls"]:
        ctx.log("H1", "allow", tool, note="ledger entry expired")
        return {}
    if e.get("tokens", 0) < int(cfg.get("min_tokens", 0)):
        ctx.log("H1", "allow", tool, note="small file: a refusal would cost more than the re-read")
        return {}
    if led.get("overrides", 0) >= int(cfg.get("max_overrides", 99)):
        ctx.log("H1", "allow", tool, note="refusals disabled for this transcript (too many overrides)")
        return {}
    if ctx.transcript and _tool_results_cleared_since(ctx.transcript, e.get("ts", "")):
        ctx.log("H1", "allow", tool, note="tool results cleared since")
        return {}
    req_sig = f"{start}-{req_end}"
    if e.get("denied") == req_sig:                      # Claude insiste : on laisse passer
        e["denied"] = None
        led["overrides"] = led.get("overrides", 0) + 1
        ctx.save_ledger(led)
        ctx.log("H1", "override", tool, note=f"second identical request allowed (overrides={led['overrides']})")
        return {}
    e["denied"] = req_sig
    ctx.save_ledger(led)
    name = os.path.basename(path)
    ago = led["read_events"] - e.get("n", 0)
    when = "à l'instant" if ago == 0 else f"il y a {ago} lecture{'s' if ago > 1 else ''}"
    how = "précise offset/limit" if tool == "Read" else "utilise sed -n 'a,bp' ou grep"
    reason = (f"TOKEN SAVER: {name} lignes {e_start}-{e_end} sont déjà dans ton contexte (lu {when}, fichier inchangé). "
              f"Réutilise cette version ; pour d'autres lignes {how} ; pour forcer, relance exactement la même commande.")
    ctx.log("H1", "deny", tool, chars_before=e.get("chars"), chars_after=len(reason), tokens_injected=len(reason) // 4,
            note=name)
    ctx.saving("H1", "avoided", e.get("tokens", 0), "taille du résultat de la lecture identique précédente (tool_response)", "high")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _read_response_info(resp) -> tuple[int, int | None, int | None, int | None]:
    """(chars, startLine, numLines, totalLines) depuis la réponse structurée ou textuelle du Read."""
    if isinstance(resp, dict):
        fi = resp.get("file") if isinstance(resp.get("file"), dict) else resp
        content = fi.get("content") or ""
        return len(content), fi.get("startLine"), fi.get("numLines"), fi.get("totalLines")
    if isinstance(resp, list):
        text = "".join(b.get("text", "") for b in resp if isinstance(b, dict))
        return len(text), None, text.count("\n") + 1, None
    if isinstance(resp, str):
        return len(resp), None, resp.count("\n") + 1, None
    return 0, None, None, None


def _count_lines(path: str) -> int | None:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return None


def h1_post(ctx: Ctx) -> dict:
    p = ctx.payload
    if not ctx.cfg["features"].get("H1_read_ledger"):
        return {}
    req = _read_request(p)
    if not req:
        return {}
    raw_path, start, end, tool = req
    path = _norm(raw_path)
    resp = p.get("tool_response")
    if tool == "Read":
        chars, r_start, num, total = _read_response_info(resp)
        if r_start is not None:
            start = r_start
        if num is None:
            num = int((p.get("tool_input") or {}).get("limit") or READ_DEFAULT_LINES)
    else:  # Bash / PowerShell : {stdout, stderr, interrupted, isImage}
        out = resp.get("stdout", "") if isinstance(resp, dict) else (resp if isinstance(resp, str) else "")
        if not isinstance(resp, dict) or resp.get("interrupted") or (resp.get("stderr") and not out):
            return {}
        chars = len(out)
        total = _count_lines(raw_path)
        if total is None:
            return {}
        num = (end - start + 1) if end is not None else max(1, total - start + 1)
        num = min(num, max(1, total - start + 1))
    if not chars:
        return {}
    led = ctx.load_ledger()
    led["read_events"] = led.get("read_events", 0) + 1
    led["reads"][path] = {"start": int(start), "num": int(num), "total": total, "sig": list(_file_sig(raw_path) or []),
                          "chars": chars, "tokens": chars // 4, "t": time.time(), "ts": _now_iso(), "n": led["read_events"], "denied": None}
    ctx.save_ledger(led)
    ctx.log("H1", "log", tool, chars_before=chars, note=os.path.basename(path))
    return {}


# ------------------------------------------------------------------ H2

def _transcript_tail_lines(transcript: str) -> list[dict]:
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines()[1:] if size > TAIL_BYTES else raw.splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def _snapshot_from_transcript(lines: list[dict], led: dict) -> dict:
    tools: list[dict] = []
    names: dict[str, str] = {}
    edits: dict[str, int] = {}
    last_user = ""
    for d in lines:
        msg = d.get("message") or {}
        c = msg.get("content")
        if d.get("type") == "assistant" and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    inp = b.get("input") or {}
                    name = b.get("name")
                    names[b.get("id", "")] = name
                    arg = inp.get("file_path") or inp.get("command") or inp.get("pattern") or inp.get("description") or ""
                    tools.append({"id": b.get("id"), "name": name, "arg": str(arg)[:100], "ok": None})
                    if name in ("Edit", "Write", "NotebookEdit") and inp.get("file_path"):
                        edits[inp["file_path"]] = edits.get(inp["file_path"], 0) + 1
        elif d.get("type") == "user":
            if isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        for t in tools:
                            if t["id"] == b.get("tool_use_id"):
                                t["ok"] = not b.get("is_error")
                    elif b.get("type") == "text" and not d.get("isMeta") and not b.get("text", "").lstrip().startswith("<"):
                        last_user = b.get("text", "")
            elif isinstance(c, str) and not d.get("isMeta") and not c.lstrip().startswith("<"):
                last_user = c
    reads = sorted(led.get("reads", {}).items(), key=lambda kv: -kv[1].get("n", 0))[:8]
    return {"taken_at": _now_iso(), "last_user_prompt": last_user[:300], "edited": edits,
            "read": [{"path": k, "start": v.get("start"), "end": (v.get("start") or 1) + (v.get("num") or 1) - 1} for k, v in reads],
            "last_tools": tools[-20:]}


def _brief(snap: dict, rel_path: str, cfg: dict | None = None, project: Path | None = None) -> str:
    """Rappel factuel après compaction. Jamais de consigne : ce n'est pas une instruction, c'est un état."""
    bc = (cfg or {}).get("h2_brief") or {}
    max_chars = int(bc.get("max_chars") or BRIEF_MAX_CHARS)
    L = ["Rappel factuel TOKEN SAVER après compaction (ce n'est PAS une consigne ; la procédure en cours et les règles du projet priment) :"]
    if snap.get("edited"):
        L.append("- fichiers modifiés avant le résumé : " + ", ".join(f"{os.path.basename(k)} (×{v})" for k, v in list(snap["edited"].items())[:8]))
    if snap.get("read"):
        L.append("- fichiers lus récemment : " + ", ".join(f"{os.path.basename(r['path'])}:{r['start']}-{r['end']}" for r in snap["read"][:6]))
    cmds = [t for t in snap.get("last_tools", []) if t["name"] in ("Bash", "PowerShell")][-4:]
    if cmds:
        L.append("- dernières commandes : " + " ; ".join(f"{t['arg'][:60]} → {'ok' if t['ok'] else ('ÉCHEC' if t['ok'] is False else '?')}" for t in cmds))
    state = [s for s in bc.get("state_files", []) if project is None or (project / s).is_file()]
    if state:
        L.append("- avant de continuer, relis l'état et la procédure en cours : " + ", ".join(state))
    L.append(f"- détail : {rel_path}")
    text = "\n".join(L)
    return text[:max_chars]


def h2_precompact(ctx: Ctx) -> dict:
    if not ctx.cfg["features"].get("H2_compact_reset") or not ctx.tsd:
        return {}
    led = ctx.load_ledger()
    snap = _snapshot_from_transcript(_transcript_tail_lines(ctx.transcript), led)
    snap["trigger"] = ctx.payload.get("trigger")
    path = ctx.tsd / "state" / f"snapshot-{(ctx.session or 'na')[:8]}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
    led["reads"] = {}
    led["reset_at"] = _now_iso()
    ctx.save_ledger(led)
    ctx.log("H2", "snapshot", note=f"trigger={snap['trigger']} edited={len(snap['edited'])} read={len(snap['read'])}")
    return {}


def _night_note(ctx: Ctx) -> str:
    """Une ligne factuelle pour la session de l'app : une nuit tourne (ne pas travailler sur le dépôt) ou vient de finir (rien ne tourne).
    Leçon du 17/09 : sans ça, la session de l'app croyait qu'« un truc tourne encore » des heures après la fin."""
    try:
        from . import nuit
        st = nuit.read_state(ctx.project)
        if not st:
            return ""
        if st.get("status") == "running" and nuit._alive(st.get("pid")):
            return (f"TOKEN SAVER : une autonomie tourne sur ce projet depuis {str(st.get('started', ''))[11:16]} ({st.get('cycles_done', 0)} cycle(s) fait(s)), "
                    "hors de l'app : ne modifie pas le dépôt depuis cette session. Suivi : /token-saver-status · arrêt : /token-saver-autonomie-stop.")
        ended = st.get("ended") or ""
        try:
            age_h = (time.time() - time.mktime(time.strptime(ended, "%Y-%m-%dT%H:%M:%S"))) / 3600
        except (ValueError, OverflowError):
            return ""
        if age_h <= 12:
            return (f"TOKEN SAVER : la dernière autonomie s'est terminée à {ended[11:16]} ({st.get('cycles_done', 0)} cycle(s), statut {st.get('status')}) ; "
                    "rien ne tourne, le dépôt est libre. Bilan : /token-saver-status.")
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _handoff_note(ctx: Ctx, source: str | None) -> str:
    """Passage de relais écrit par `/token-saver-next` dans la conversation précédente : remis aux deux premières sessions
    neuves ouvertes dans les 24 h (startup ou clear), puis oublié. C'est ce qui rend « une session par tâche » indolore."""
    if source not in ("startup", "clear") or not ctx.tsd:
        return ""
    f = ctx.tsd / "state" / "handoff.md"
    try:
        if not f.is_file() or time.time() - f.stat().st_mtime > 24 * 3600:
            return ""
        seen_path = ctx.tsd / "state" / "handoff-seen.json"
        seen = json.loads(seen_path.read_text(encoding="utf-8")) if seen_path.is_file() else {}
        if seen.get("file_mtime") != f.stat().st_mtime:
            seen = {"file_mtime": f.stat().st_mtime, "sessions": []}
        if ctx.session in seen["sessions"]:
            return ""
        if len(seen["sessions"]) >= 2:
            return ""
        seen["sessions"].append(ctx.session)
        seen_path.write_text(json.dumps(seen), encoding="utf-8")
        text = f.read_text(encoding="utf-8", errors="replace").strip()[:2400]
        age = (time.time() - f.stat().st_mtime) / 60
        return (f"TOKEN SAVER — passage de relais de la conversation précédente (il y a {age:.0f} min, fichier .token-saver/state/handoff.md) :\n"
                f"{text}\n(Reprends à partir de « Reste à faire » ; ne relis que « À relire d'abord ».)")
    except (OSError, ValueError):
        return ""


def h2_start(ctx: Ctx) -> dict:
    if not ctx.cfg["features"].get("H2_compact_reset") or not ctx.tsd:
        return {}
    source = ctx.payload.get("source")
    led = ctx.load_ledger()
    led["reads"] = {}
    led["reset_at"] = _now_iso()
    ctx.save_ledger(led)
    note = "\n".join(x for x in (_night_note(ctx), _handoff_note(ctx, source)) if x)
    if source == "compact" and ctx.cfg["features"].get("H2_brief"):
        snap_path = ctx.tsd / "state" / f"snapshot-{(ctx.session or 'na')[:8]}.json"
        try:
            if snap_path.is_file() and time.time() - snap_path.stat().st_mtime < 600:
                snap = json.loads(snap_path.read_text(encoding="utf-8"))
                text = _brief(snap, f".token-saver/state/{snap_path.name}", ctx.cfg, ctx.project)
                if note:
                    text = text.rstrip("\n") + "\n" + note
                ctx.log("H2", "brief", tokens_injected=len(text) // 4, chars_after=len(text), note=source)
                return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
        except (OSError, ValueError):
            pass
    ctx.log("H2", "reset", note=str(source))
    if note:
        ctx.log("H2", "night-note", tokens_injected=len(note) // 4, note=note[:80])
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}}
    return {}


# ------------------------------------------------------------------ H3 : toute relecture confiée à un agent est bornée
# Mesuré le 16/09/2026 : les processus lancent des relecteurs directement (« Relire la PR #260 », « Revue sécurité 4b »), hors
# de la commande de revue du projet ; chaque relecture relisait toute la PR (7-8 M tokens) et une PR a été relue 8 fois.
# PreToolUse(Agent) : si la consigne de l'agent est une relecture et que l'orchestrateur n'a pas déjà préparé le diff,
# le hook lance `review begin` et place le brief (fichier de diff, tour, 🔴 à vérifier, format du rapport) en tête de la
# consigne (`updatedInput`). PostToolUse(Agent) : le rapport rendu par l'agent est enregistré par `review end` et le
# verdict est ajouté au contexte de l'orchestrateur. Jamais de refus : l'agent part toujours, avec la procédure.

_REVIEW_INTENT = _re.compile(r"\b(relire|relis|relisez|relecture|relectures|revue|review|reviewer|code[- ]?review)\b", _re.I)
_PR_NUM = _re.compile(r"(?:\bPR\s*#?|#)(\d{1,6})\b", _re.I)
_AGENT_TOOLS = ("Agent", "Task")


def _response_text(resp) -> str:
    if resp is None:
        return ""
    if isinstance(resp, str):
        return resp
    if isinstance(resp, list):
        return "\n".join(_response_text(x) for x in resp)
    if isinstance(resp, dict):
        for key in ("text", "message", "result", "summary", "report", "content"):
            if key in resp and resp[key]:
                got = _response_text(resp[key])
                if got.strip():
                    return got
        return ""
    return str(resp)


def h3_agent_pre(ctx: Ctx) -> dict:
    if not ctx.cfg["features"].get("H3_review_guard") or not ctx.tsd or ctx.payload.get("tool_name") not in _AGENT_TOOLS:
        return {}
    inp = ctx.payload.get("tool_input") or {}
    desc, prompt = str(inp.get("description") or ""), str(inp.get("prompt") or "")
    text = desc + "\n" + prompt
    from .review import CODE_WORDS
    if not _REVIEW_INTENT.search(text) or not CODE_WORDS.search(text):     # une relecture DE CODE (pas une revue UX d'écrans)
        return {}
    if ".token-saver/state/review/" in prompt or "review begin" in prompt or "review end" in prompt:
        return {}                                         # l'orchestrateur suit déjà la procédure (diff en fichier fourni)
    from . import review
    m = _PR_NUM.search(text)
    info: dict = {}
    rc, brief = review.begin(ctx.project, ctx.cfg, pr=int(m.group(1)) if m else None, info=info)
    grid = next(iter((ctx.cfg.get("review") or {}).get("commands") or []), None)
    grid_txt = f"la grille du projet est dans `{grid}` (lis-la d'abord)" if grid else "la grille : correction, régressions, preuves, sécurité, règles du projet, lisibilité"
    if rc in (0, 3) and not info:                      # déjà relu à ce commit, ou plafond de tours atteint : pas de nouvelle relecture
        ctx.log("H3", "no-round", tool=ctx.payload.get("tool_name"), tokens_injected=len(brief) // 4, note=(brief.splitlines() or [""])[0][:120])
        new_prompt = brief + "\n\nNe relis rien : réponds exactement par ce message, sans autre travail.\n\n---\n\n" + prompt
    elif rc != 0:
        head = (brief.splitlines() or ["?"])[0][:160]
        note = (f"TOKEN SAVER — relecture bornée (diff non préparé : {head}). Relis UNIQUEMENT le diff de la PR ou de la branche "
                f"(`gh pr diff N` ou `git diff <base>...HEAD`), {grid_txt} ; au plus 12 constats, une ligne par constat commençant par 🔴 (empêche la fusion), "
                "🟠 (amélioration, ne bloque pas) ou 🟢, puis `fichier:ligne` ; réponse finale = ces lignes seulement.")
        ctx.log("H3", "rules-only", tool=ctx.payload.get("tool_name"), tokens_injected=len(note) // 4, note=head[:120])
        new_prompt = note + "\n\n---\n\n" + prompt
    else:
        tid = ctx.payload.get("tool_use_id")
        if tid:                                            # rapport propre à cet agent (plusieurs relecteurs en parallèle possibles)
            own = info["report"][:-3] + f"-{str(tid)[-6:]}.md"
            brief = brief.replace(info["report"], own)
            review.hook_register(ctx.project, info["branch"], str(tid), report=own)
        tail = (f"\n\n{grid_txt}. Écris le rapport dans le fichier indiqué ci-dessus et rends-le aussi comme réponse finale : une ligne par constat, "
                "commençant par 🔴, 🟠 ou 🟢 puis `fichier:ligne`, rien d'autre (TOKEN SAVER enregistre le verdict à partir de ces lignes ; "
                "ne lance pas `review end` toi-même).")
        ctx.log("H3", "brief", tool=ctx.payload.get("tool_name"), tokens_injected=(len(brief) + len(tail)) // 4,
                note=f"{info.get('branch')} tour {info.get('round')} {info.get('mode')}")
        new_prompt = brief + tail + "\n\n---\n\n" + prompt
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "TOKEN SAVER : relecture bornée (diff en fichier, tours comptés)",
                                   "updatedInput": {**inp, "prompt": new_prompt}}}


def h3_agent_post(ctx: Ctx) -> dict:
    if not ctx.cfg["features"].get("H3_review_guard") or not ctx.tsd or ctx.payload.get("tool_name") not in _AGENT_TOOLS:
        return {}
    tid = ctx.payload.get("tool_use_id")
    if not tid:
        return {}
    from . import review
    found = review.find_hook_agent(ctx.project, str(tid))
    if not found:
        return {}
    branch, rec = found
    resp = ctx.payload.get("tool_response")
    # 1. le fichier de rapport que l'agent devait écrire ; 2. le rapport remis à l'orchestrateur (handback) ; 3. le texte du résultat.
    # (Depuis Claude Code 2.1.27x, le résultat de l'outil Agent ne contient plus le texte de l'agent : il est remis à part.)
    own = ctx.project / rec["report"] if rec.get("report") else None
    source = "fichier" if own and own.is_file() and review.has_findings(own.read_text(encoding="utf-8", errors="replace")) else None
    if source:
        rc, msg = review.end(ctx.project, ctx.cfg, branch=branch, report=str(own), merge=True)
    else:
        text = _response_text(resp.get("handback")) if isinstance(resp, dict) and resp.get("handback") else ""
        source = "handback" if text and review.has_findings(text) else "résultat"
        text = text if source == "handback" else _response_text(resp)
        rc, msg = review.end(ctx.project, ctx.cfg, branch=branch, text=text, merge=True)
    review.hook_unregister(ctx.project, branch, str(tid))
    kind = f"{type(resp).__name__}:{','.join(list(resp.keys())[:8])}" if isinstance(resp, dict) else type(resp).__name__
    ctx.log("H3", "verdict" if rc == 0 else "unformatted", tool=ctx.payload.get("tool_name"), tokens_injected=len(msg) // 4,
            note=f"{source} | {(msg.splitlines() or [''])[0][:100]} | resp={kind}"[:220])
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}


# La procédure de revue bornée n'est plus écrite dans la commande de revue du projet (jusqu'au 18/09/2026 : bloc marqué, régénéré à
# chaque mise à jour, qui a écrasé une correction locale). Elle est soufflée au démarrage de la revue : commande tapée par
# l'utilisateur (UserPromptSubmit) ou skill lancé par Claude (PostToolUse Skill). Vérifié réel sur le projet d'essai : le contexte
# arrive dans les deux cas. Même texte, mêmes tokens (≈ 450), seulement quand une revue commence ; aucun fichier du projet modifié.

def _review_names(ctx: Ctx) -> set[str]:
    out = set()
    for c in (ctx.cfg.get("review") or {}).get("commands") or []:
        p = Path(str(c))
        out.add((p.parent.name if p.name == "SKILL.md" else p.stem).lower())
    return out


def _review_context() -> str:
    from .install import REVIEW_PROCEDURE
    return ("TOKEN SAVER — procédure de revue bornée pour cette commande (elle fixe ce qui est lu, le nombre de tours et ce qui bloque ; "
            "la grille de la commande reste la référence) :\n" + REVIEW_PROCEDURE)


def h3_prompt(ctx: Ctx) -> dict:
    """UserPromptSubmit : l'utilisateur (ou le prompt d'un cycle) tape la commande de revue du projet → la procédure l'accompagne."""
    if not ctx.cfg["features"].get("H3_review_guard") or not ctx.tsd:
        return {}
    m = _SLASH.match(str(ctx.payload.get("prompt") or ""))
    name = m.group(1).lower() if m else ""
    if not name or name not in _review_names(ctx):
        return {}
    text = _review_context()
    ctx.log("H3", "prompt", tokens_injected=len(text) // 4, note=f"/{name}")
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


def h3_skill(ctx: Ctx) -> dict:
    """PostToolUse Skill : Claude lance lui-même la commande de revue du projet (cycle d'autonomie, par exemple) → même procédure."""
    if not ctx.cfg["features"].get("H3_review_guard") or not ctx.tsd or ctx.payload.get("tool_name") != "Skill":
        return {}
    inp = ctx.payload.get("tool_input") or {}
    name = str(inp.get("skill") or inp.get("name") or "").strip().lstrip("/").split()[0].lower() if str(inp.get("skill") or inp.get("name") or "").strip() else ""
    if not name or name not in _review_names(ctx):
        return {}
    text = _review_context()
    ctx.log("H3", "skill", tool="Skill", tokens_injected=len(text) // 4, note=f"/{name}")
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}}


# ------------------------------------------------------------------ H6 : l'autonomie tapée dans l'app devient le mode nuit
# Mesuré : une session d'autonomie dans l'app relit toute sa conversation à chaque appel (contexte moyen 415K, 79 % du volume).
# UserPromptSubmit : `/autonomie-totale`, `/autonomie N`… (commandes du projet détectées à l'installation) lance le runner de
# nuit (une session neuve par cycle) et le message est refusé avec l'explication ; `/stop-autonomie` arrête la nuit ;
# pendant la nuit, les autres messages de l'app sur ce projet sont refusés (deux équipes sur le même code se gêneraient).
# Les sessions du runner lui-même ne sont jamais touchées. Si la nuit ne peut pas démarrer, le message passe tel quel.

_SLASH = _re.compile(r"^\s*/([A-Za-z0-9_.-]+)(?:\s+(.*))?$", _re.S)


def h6_prompt(ctx: Ctx) -> dict:
    if not ctx.cfg["features"].get("H6_autonomy_redirect") or not ctx.tsd:
        return {}
    from . import nuit
    sid = ctx.session
    if sid and sid in nuit.own_sessions(ctx.project):
        return {}                                          # session du runner : jamais touchée
    prompt = str(ctx.payload.get("prompt") or "")
    m = _SLASH.match(prompt)
    name, arg = (m.group(1).lower(), (m.group(2) or "").strip()) if m else ("", "")
    ncfg = ctx.cfg.get("nuit") or {}
    auto = {c.lower() for c in ncfg.get("autonomy_commands") or []}
    stops = {c.lower() for c in ncfg.get("stop_commands") or []} | {"token-saver-autonomie-stop", "token-saver-nuit-stop"}
    st = nuit.read_state(ctx.project)
    running = st.get("status") == "running" and nuit._alive(st.get("pid"))
    if name in stops:
        if not running:
            return {}
        msg = nuit.stop(ctx.project, now="now" in arg.lower())
        ctx.log("H6", "stop", note=f"/{name} {arg}".strip())
        return {"decision": "block", "reason": "TOKEN SAVER — " + msg}
    if name in auto and not running:
        cycles = int(arg.split()[0]) if arg and arg.split()[0].isdigit() else 0
        if sid:
            os.environ["CLAUDE_CODE_SESSION_ID"] = str(sid)   # cette session rend la main : elle n'est pas « une autre session active »
        pyz = ctx.tsd / "bin" / "token-saver.pyz"
        launcher = [py_cmd(), str(pyz)] if pyz.is_file() else [sys.executable, "-m", "token_saver"]
        msg = nuit.start(ctx.project, ctx.cfg, cycles, None, launcher)
        st2 = nuit.read_state(ctx.project)
        if st2.get("status") == "running" and nuit._alive(st2.get("pid")) and st2.get("started") != st.get("started"):
            ctx.log("H6", "redirect", note=f"/{name} {arg} → nuit {cycles or '∞'}".strip())
            warn = [l.strip() for l in msg.splitlines() if l.strip().startswith("⚠")]
            end_txt = f" Fin prévue : {st2['end_at']} au plus tard." if st2.get("end_at") else ""
            return {"decision": "block", "reason": (
                f"TOKEN SAVER — autonomie lancée en sessions neuves à la place de /{name} : {cycles or 'illimité'} cycle(s) de `{st2.get('prompt') or ncfg.get('prompt')}`, chacun dans une "
                f"conversation neuve, qui ne grossit jamais.{end_txt} Suivi : /token-saver-status · arrêt : "
                + " ou ".join("/" + s for s in sorted(stops)) + ". Cette session ne doit plus travailler sur ce projet pendant ce temps : "
                "ses messages seront refusés jusqu'à l'arrêt." + ("\n" + "\n".join(warn) if warn else ""))}
        why = " | ".join(l.strip() for l in msg.splitlines() if l.strip().startswith("✗")) or (msg.splitlines() or [""])[0]
        ctx.log("H6", "fallback", note=why[:160])
        return {"systemMessage": f"TOKEN SAVER — sessions neuves NON lancées, /{name} démarre dans cette session comme avant. Raison : {why[:400]}"}
    if running and not name.startswith("token-saver"):
        ctx.log("H6", "refused", note=(name or prompt[:40]))
        return {"decision": "block", "reason": (
            f"TOKEN SAVER — une autonomie tourne sur ce projet ({st.get('cycles_done', 0)} cycle(s) fait(s) depuis {str(st.get('started', ''))[11:16]}) : "
            "deux équipes sur le même code se gêneraient. Pour reprendre la main : /token-saver-autonomie-stop (à la fin du cycle en cours) "
            "ou /token-saver-autonomie-stop --now (tout de suite). Suivi : /token-saver-status.")}
    return {}


# ------------------------------------------------------------------ H7 : la conversation devient lourde
# Mesuré le 18/09/2026 : une session guidée de l'app gardée 5 jours = 4 259 appels, contexte moyen 485K, 2 066 M tokens relus.
# Personne ne le voyait. À chaque palier franchi (200K, 400K, 600K, 800K), une ligne à l'utilisateur (coût par message,
# /token-saver-next) et une consigne à Claude : proposer une session neuve quand la tâche en cours est finie. Jamais de blocage.

def _last_context_tokens(transcript: str) -> int:
    """Contexte du dernier appel API de cette conversation, lu dans la fin du transcript (zéro token)."""
    if not transcript or not os.path.isfile(transcript):
        return 0
    try:
        size = os.path.getsize(transcript)
        with open(transcript, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return 0
    ctx = 0
    for line in tail.splitlines():
        if '"usage"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        u = (rec.get("message") or {}).get("usage") or {}
        if u:
            ctx = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
    return ctx


def h7_prompt(ctx: Ctx) -> dict:
    if not ctx.cfg["features"].get("H7_context_meter") or not ctx.tsd:
        return {}
    from . import nuit
    if ctx.session and ctx.session in nuit.own_sessions(ctx.project):
        return {}                                          # sessions neuves du runner : jamais lourdes, jamais dérangées
    levels = sorted(int(x) for x in (ctx.cfg.get("context_meter") or {}).get("levels") or [200000, 400000, 600000, 800000])
    tokens = _last_context_tokens(ctx.transcript)
    level = max([l for l in levels if tokens >= l], default=0)
    if not level:
        return {}
    led = ctx.load_ledger()
    if int(led.get("ctx_level_warned") or 0) >= level:
        return {}
    led["ctx_level_warned"] = level
    ctx.save_ledger(led)
    k = tokens // 1000
    user_line = (f"TOKEN SAVER : cette conversation pèse {k}K tokens, et chaque message la relit entièrement. "
                 "Tâche finie ? Tape /token-saver-next : Claude note l'état et tu continues dans une session neuve, sans rien perdre.")
    claude_note = (f"TOKEN SAVER : le contexte de cette conversation atteint {k}K tokens. Quand la tâche en cours est terminée, "
                   "propose à l'utilisateur /token-saver-next (note d'état puis session neuve) plutôt que d'enchaîner ici. Ne le fais pas au milieu d'une tâche.")
    ctx.log("H7", "warn", tokens_injected=len(claude_note) // 4, note=f"{k}K palier {level // 1000}K")
    return {"systemMessage": user_line, "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": claude_note}}


HANDLERS = {"h1-pre": h1_pre, "h1-post": h1_post, "h2-start": h2_start, "h2-precompact": h2_precompact,
            "h3-agent-pre": h3_agent_pre, "h3-agent-post": h3_agent_post, "h3-prompt": h3_prompt, "h3-skill": h3_skill,
            "h6-prompt": h6_prompt, "h7-prompt": h7_prompt}


def run(name: str, stdin=None, stdout=None) -> int:
    """Point d'entrée `token-saver hook <name>` : lit stdin, écrit la réponse JSON, ne lève jamais."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    try:
        payload = json.loads(stdin.read() or "{}")
        ctx = Ctx(payload)
        out = HANDLERS[name](ctx) if name in HANDLERS else {}
    except Exception as exc:  # noqa: BLE001 — fail-open absolu
        out = {}
        try:
            Ctx({}).log(name, "error", note=str(exc)[:200])
        except Exception:  # noqa: BLE001
            pass
    stdout.write(localize(json.dumps(out, ensure_ascii=False)))
    stdout.flush()
    return 0
