"""`token-saver review` : revue de code bornée, le modèle relit et l'outil tient les comptes.

Mesuré le 16/09/2026 sur deux projets réels : jusqu'à 8 relectures d'une même PR, chaque relecture relisant
toute la PR (7-8 M tokens, 15-85 min) ; un tour à six spécialistes en parallèle = 116 M tokens ; 710 appels
qui ne faisaient qu'interroger l'état de la CI (3,4 % du volume). Causes : relecture intégrale à chaque tour,
aucun plafond de tours ni seuil de gravité, attente de la CI par sondage.

Ici : `begin` écrit le diff à relire dans un fichier (complet au tour 1, seulement les lignes changées ensuite),
numérote le tour et rappelle les 🔴 à vérifier ; `end` enregistre le verdict (seul un 🔴 bloque) ; au-delà de
`review.max_rounds` tours, plus de tour : la PR revient à l'humain ; `wait-ci` attend la CI en un seul appel.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .paths import ts_dir

MODES = {1: "COMPLET", 2: "DELTA", 3: "VÉRIFICATION"}
RED, ORANGE, GREEN = "🔴", "🟠", "🟢"
REVIEW_NAME = re.compile(r"review|revue|relect|relire|relis", re.I)       # noms de commandes/skills de revue
CODE_WORDS = re.compile(r"\bdiff\b|\bPRs?\b|pull request|branche|branch|commit|\bcode\b|\bfichiers?\b|\bfiles?\b|#\d{1,6}\b", re.I)  # revue de CODE, pas d'écrans
REVIEW_DESC = re.compile(r"revue|review|relect|relire|relis|balayage|v[ée]rif|audit|sanit", re.I)  # descriptions d'agents


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _git(project: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=str(project), capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} : {(r.stderr or r.stdout).strip()[:200]}")
    return r.stdout


def _git_ok(project: Path, *args: str) -> bool:
    return subprocess.run(["git", *args], cwd=str(project), capture_output=True).returncode == 0


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "branche"


def _fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


def state_dir(project: Path) -> Path:
    return ts_dir(project) / "state" / "review"


def state_file(project: Path, branch: str) -> Path:
    return state_dir(project) / f"{_slug(branch)}.json"


def read_state(project: Path, branch: str) -> dict:
    f = state_file(project, branch)
    if not f.is_file():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(project: Path, branch: str, st: dict) -> None:
    f = state_file(project, branch)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(st, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def default_base(project: Path) -> str | None:
    """Branche de base : origin/HEAD si connue, sinon main/master."""
    try:
        ref = _git(project, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD").strip()
        if ref:
            return ref
    except RuntimeError:
        pass
    for b in ("main", "master", "origin/main", "origin/master"):
        if _git_ok(project, "rev-parse", "--verify", "-q", b):
            return b
    return None


def _pr_info(project: Path, pr: int) -> dict:
    if shutil.which("gh") is None:
        raise RuntimeError("`gh` introuvable : lance `review begin` depuis la branche de la PR, sans --pr")
    r = subprocess.run(["gh", "pr", "view", str(pr), "--json", "headRefName,baseRefName,url,number"], cwd=str(project),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"gh pr view {pr} : {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


_ITEM = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|__)?\s*([🔴🟠🟢])")       # un constat = une ligne qui commence par l'emoji
_NEGATION = re.compile(r"aucun|zéro|\b0\b|pas de|sans|verdict|none|\bno\b", re.I)   # « aucun 🔴 » n'est pas un constat


def _parse_report(text: str, max_findings: int) -> tuple[list[str], int, int, list[str]]:
    """Constats 🔴 (texte), nombre de 🟠 et de 🟢, lignes ambiguës (un 🔴 hors constat, ex. un titre « ## 🔴 Bloquants »).
    Leçon de la validation réelle du 16/09 : la ligne « Verdict : aucun 🔴 → FUSIONNABLE » avait été comptée comme un 🔴."""
    red, orange, green, ambiguous = [], 0, 0, []
    for ln in text.splitlines():
        m = _ITEM.match(ln)
        if m:
            if m.group(1) == RED:
                red.append(re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?", "", ln).strip()[:200])
            elif m.group(1) == ORANGE:
                orange += 1
            else:
                green += 1
        elif RED in ln and not _NEGATION.search(ln):
            ambiguous.append(ln.strip()[:120])
    return red[:max_findings], orange, green, ambiguous


# ---------------------------------------------------------------- begin / end / status / reset

def begin(project: Path, cfg: dict, pr: int | None = None, base: str | None = None, fresh: bool = False,
          info: dict | None = None) -> tuple[int, str]:
    """Prépare un tour de revue : diff en fichier, numéro et mode du tour, constats 🔴 à vérifier. Renvoie (code, texte).
    Avec --pr, la branche de la PR est relue telle qu'elle est sur GitHub (fetch), sans jamais changer la copie de travail."""
    rc = cfg.get("review", {})
    max_rounds = int(rc.get("max_rounds", 3))
    if not ts_dir(project).is_dir():
        return 2, "TOKEN SAVER n'est pas installé dans ce projet (`install` d'abord)"
    if not _git_ok(project, "rev-parse", "--git-dir"):
        return 2, "ce projet n'est pas un dépôt git : la revue bornée a besoin de `git diff`"
    pr_meta = None
    branch = _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    head_ref = "HEAD"
    if pr:
        try:
            pr_meta = _pr_info(project, pr)
        except RuntimeError as exc:
            return 2, str(exc)
        if branch != pr_meta["headRefName"]:
            r = subprocess.run(["git", "fetch", "-q", "origin", pr_meta["headRefName"]], cwd=str(project), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=120)
            if r.returncode != 0:
                return 2, f"git fetch origin {pr_meta['headRefName']} : {r.stderr.strip()[:200]}"
            branch, head_ref = pr_meta["headRefName"], "FETCH_HEAD"
    head = _git(project, "rev-parse", head_ref).strip()
    st = {} if fresh else read_state(project, branch)
    base = base or st.get("base") or (pr_meta or {}).get("baseRefName") or rc.get("base") or default_base(project)
    if not base:
        return 2, "branche de base introuvable : `review begin --base <branche>`"
    if base.split("/")[-1] == branch:
        return 2, f"la branche courante ({branch}) est la branche de base : place-toi sur la branche à relire"
    if not _git_ok(project, "rev-parse", "--verify", "-q", base):
        return 2, f"branche de base inconnue : {base}"
    rounds = st.get("rounds", [])
    if rounds and rounds[-1]["sha"] == head:
        last = rounds[-1]
        return 0, (f"TOKEN SAVER — revue : déjà relu à ce commit ({head[:7]}), tour {last['round']} ({last['mode']}), verdict {last['verdict']}, "
                   f"{len(last.get('red', []))} {RED}. Aucun changement depuis : pas de nouveau tour. Restitue ce verdict. "
                   "(`review begin --fresh` pour repartir de zéro sur décision humaine.)")
    n = len(rounds) + 1
    if n > max_rounds:
        last = rounds[-1]
        return 3, (f"TOKEN SAVER — revue : plafond atteint ({max_rounds} tours sur {branch}). Dernier verdict : {last['verdict']} "
                   f"({len(last.get('red', []))} {RED} restants). Pas de nouveau tour : laisse la PR ouverte pour une décision humaine, "
                   "note-le au journal et passe à autre chose. (`review begin --fresh` seulement si un humain l'a demandé.)")
    merge_base = _git(project, "merge-base", base, head).strip()
    note = ""
    if n == 1:
        rng, mode = f"{merge_base}..{head}", MODES[1]
    else:
        last_sha = rounds[-1]["sha"]
        if _git_ok(project, "merge-base", "--is-ancestor", last_sha, head):
            rng, mode = f"{last_sha}..{head}", MODES[min(n, 3)]
        else:
            rng, mode, note = f"{merge_base}..{head}", MODES[1], "historique réécrit depuis le dernier tour : diff complet"
    diff = _git(project, "diff", "--no-color", "--find-renames", rng)
    stat = _git(project, "diff", "--stat=100", rng).rstrip()
    files = [f for f in _git(project, "diff", "--name-only", rng).splitlines() if f.strip()]
    sd = state_dir(project)
    sd.mkdir(parents=True, exist_ok=True)
    diff_file = sd / f"{_slug(branch)}-r{n}.diff"
    report_file = sd / f"{_slug(branch)}-r{n}.md"
    diff_file.write_text(diff, encoding="utf-8", newline="")
    rel_diff, rel_report = _fwd(diff_file.relative_to(project)), _fwd(report_file.relative_to(project))
    lines = diff.count("\n")
    st.update({"branch": branch, "base": base, "pr": (pr_meta or {}).get("number", st.get("pr")), "url": (pr_meta or {}).get("url", st.get("url")),
               "rounds": rounds, "pending": {"round": n, "mode": mode, "sha": head, "diff": rel_diff, "report": rel_report,
                                              "started": _now(), "files": len(files), "lines": lines}})
    _write_state(project, branch, st)
    if info is not None:
        info.update({"branch": branch, "round": n, "mode": mode, "diff": rel_diff, "report": rel_report, "sha": head})
    prev_red = rounds[-1].get("red", []) if rounds else []
    L = [f"TOKEN SAVER — revue : tour {n}/{max_rounds} ({mode}) — {branch} contre {base}" + (f" — PR #{st['pr']}" if st.get("pr") else ""),
         f"diff à relire : {rel_diff} ({lines} lignes, ≈ {max(1, len(diff) // 4000)}K tokens, {len(files)} fichiers)" + (f" — {note}" if note else ""),
         f"rapport à écrire : {rel_report}"]
    if stat:
        L += ["fichiers :"] + [f"  {ln.strip()}" for ln in stat.splitlines()[-40:]]
    if not diff.strip():
        L.append("aucune ligne changée dans cet intervalle : le verdict précédent tient, `review end --verdict <verdict>`")
    if mode == MODES[1]:
        L.append(f"consigne : relis UNIQUEMENT ce fichier de diff, avec la grille de la commande de revue ; au plus {rc.get('max_findings', 12)} constats, "
                 f"une ligne par constat qui COMMENCE par {RED} (empêche la fusion), {ORANGE} (amélioration, ne bloque pas) ou {GREEN}, puis `fichier:ligne` ; "
                 "aucun emoji ailleurs dans le rapport.")
    elif mode == MODES[2]:
        L.append(f"consigne : seules les lignes de ce delta sont à relire. Vérifie d'abord que chaque {RED} du tour précédent est réglé, "
                 f"puis relis les lignes nouvelles avec la grille ; un {RED} n'est possible que sur une ligne de ce delta.")
    else:
        L.append(f"consigne : dernier tour, vérification seulement : chaque {RED} du tour précédent est-il réglé dans ce delta ? "
                 f"Aucun constat nouveau ({ORANGE} interdit). S'il reste un {RED}, verdict BLOQUE et la PR revient à l'humain.")
    if prev_red:
        L += [f"{RED} du tour précédent à vérifier ({len(prev_red)}) :"] + [f"  - {r}" for r in prev_red]
    L.append("ouvre un fichier source seulement pour comprendre une ligne citée par le diff ; ne relance pas ce que la CI a déjà exécuté sur ce commit ; "
             "si aucune CI n'a tourné, lance la commande de test du projet une fois et cite le résultat dans le rapport.")
    L.append(f"puis : python .token-saver/bin/token-saver.pyz review end --report {rel_report}   (verdict déduit des {RED} du rapport)")
    return 0, "\n".join(L)


def _resolve_branch(project: Path, branch: str | None, pr: int | None) -> str | None:
    if branch:
        return branch
    if pr:
        sd = state_dir(project)
        for f in (sorted(sd.glob("*.json")) if sd.is_dir() else []):
            try:
                st = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if st.get("pr") == pr:
                return st.get("branch")
        return None
    return _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()


_NO_FINDING = re.compile(r"FUSIONNABLE|aucun (?:constat|probl[èe]me|blocage|point bloquant|🔴)|(?:\b0|zéro) 🔴|\bRAS\b|LGTM|rien à signaler|"
                         r"rien n'emp[êe]che la fusion|peut être fusionn", re.I)


def end(project: Path, cfg: dict, verdict: str | None = None, report: str | None = None, branch: str | None = None,
        pr: int | None = None, text: str | None = None, merge: bool = False) -> tuple[int, str]:
    """Enregistre le tour (verdict, 🔴 à vérifier au tour suivant) et dit quoi faire ensuite.
    `text` : rapport fourni directement (hook H3) ; `merge` : plusieurs relecteurs en parallèle sur le même commit, leurs 🔴 s'additionnent."""
    rc = cfg.get("review", {})
    max_rounds = int(rc.get("max_rounds", 3))
    if not _git_ok(project, "rev-parse", "--git-dir"):
        return 2, "ce projet n'est pas un dépôt git"
    branch = _resolve_branch(project, branch, pr)
    if not branch:
        return 2, f"aucun tour de revue connu pour la PR #{pr} : commence par `review begin --pr {pr}`"
    st = read_state(project, branch)
    pend = st.get("pending")
    rounds = st.get("rounds", [])
    amend = False
    if not pend:
        current = _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
        head_now = _git(project, "rev-parse", "HEAD").strip() if current == branch else None
        if rounds and (head_now is None or rounds[-1]["sha"] == head_now):   # tour déjà clos au même commit : correction ou fusion des rapports
            last = rounds[-1]
            pend, amend = {**last, "started": last.get("at"), "lines": last.get("diff_lines")}, True
        else:
            return 2, f"aucun tour de revue en cours sur {branch} : commence par `review begin`"
    red, orange, green = [], 0, 0
    rep_path = Path(report) if report else None
    if rep_path and not rep_path.is_absolute():
        rep_path = project / rep_path
    if text is not None and not rep_path:                        # rapport reçu directement : écrit dans le fichier du tour
        rep_path = project / (pend.get("report") or f".token-saver/state/review/{_slug(branch)}-r{pend['round']}.md")
        rep_path.parent.mkdir(parents=True, exist_ok=True)
        rep_path.write_text(text, encoding="utf-8")
    if rep_path and rep_path.is_file():
        body = rep_path.read_text(encoding="utf-8", errors="replace")
        red, orange, green, ambiguous = _parse_report(body, int(rc.get("max_findings", 12)))
        if ambiguous:
            return 2, (f"rapport ambigu : {len(ambiguous)} ligne(s) contiennent un {RED} sans être un constat (un constat = une ligne qui commence par "
                       f"{RED}, {ORANGE} ou {GREEN}, puis `fichier:ligne`). Corrige le rapport puis relance `review end` :\n"
                       + "\n".join(f"  - {a}" for a in ambiguous[:6]))
        if not (red or orange or green) and not _NO_FINDING.search(body):
            rel = _fwd(rep_path.relative_to(project)) if rep_path.is_relative_to(project) else str(rep_path)
            return 2, (f"rapport sans constat formaté ({rel}) : une ligne par constat commençant par {RED}, {ORANGE} ou {GREEN}, ou le mot FUSIONNABLE "
                       f"s'il n'y a rien à signaler. Mets le rapport en forme puis `python .token-saver/bin/token-saver.pyz review end --report {rel} --branch {branch}`.")
        if amend and merge:                                        # relecteurs en parallèle : union des 🔴, sommes des 🟠/🟢
            prev = pend
            red = list(dict.fromkeys(list(prev.get("red", [])) + red))
            orange += int(prev.get("orange") or 0)
            green += int(prev.get("green") or 0)
        derived = "BLOQUE" if red else "FUSIONNABLE"
        note = ""
        if verdict and verdict.upper().replace("É", "E") != derived:
            note = f" (le rapport contient {len(red)} {RED} : verdict déduit du rapport, pas de --verdict)"
        verdict = derived + note
    elif not verdict:
        return 2, "donne le rapport (`--report <fichier>`) ou un verdict (`--verdict FUSIONNABLE|BLOQUE`)"
    else:
        verdict = verdict.upper().replace("É", "E")
    v = verdict.split(" ")[0]
    head = pend.get("sha") or _git(project, "rev-parse", "HEAD").strip()
    entry = {"round": pend["round"], "mode": pend["mode"], "sha": head, "at": _now(), "verdict": v, "red": red, "orange": orange, "green": green,
             "report": _fwd(rep_path.relative_to(project)) if rep_path and rep_path.is_relative_to(project) else pend.get("report"),
             "diff_lines": pend.get("lines"), "files": pend.get("files"), "minutes": pend.get("minutes") if amend else None}
    if not amend:
        try:
            t1 = time.mktime(time.strptime(entry["at"], "%Y-%m-%dT%H:%M:%S"))
            t0 = time.mktime(time.strptime(pend["started"], "%Y-%m-%dT%H:%M:%S"))
            entry["minutes"] = round((t1 - t0) / 60, 1)
        except (ValueError, KeyError, TypeError, OverflowError):
            pass
    st["rounds"] = (rounds[:-1] if amend else rounds) + [entry]
    st["pending"] = None
    _write_state(project, branch, st)
    log = ts_dir(project) / "metrics" / "review-log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": entry["at"], "branch": branch, "pr": st.get("pr"),
                             **{k: entry[k] for k in ("round", "mode", "verdict", "diff_lines", "files", "minutes")},
                             "red": len(red), "orange": orange, **({"amended": True} if amend else {})}, ensure_ascii=False) + "\n")
    n = entry["round"]
    what = "complété" if (amend and merge) else "corrigé" if amend else "enregistré"
    L = [f"TOKEN SAVER — revue : tour {n}/{max_rounds} ({pend['mode']}) {what} sur {branch} @ {head[:7]} : {verdict}, "
         f"{len(red)} {RED}, {orange} {ORANGE}, {green} {GREEN}."]
    if red:
        L += [f"  {RED} " + r.lstrip(RED).strip() for r in red[:12]]
    if v == "FUSIONNABLE":
        L.append(f"suite : fusion possible dès que la CI est verte — `python .token-saver/bin/token-saver.pyz review wait-ci` (un seul appel). "
                 + (f"Les {orange} {ORANGE} se corrigent dans cette PR avant la fusion (avec leur test si c'est du code) ou sont écartés par écrit "
                    f"dans sa description, avec la raison : jamais de carte ni d'issue de suivi, pas de nouveau tour. "
                    f"Un {ORANGE} qui décrit un défaut que l'utilisateur verrait est un {RED} : corrige-le." if orange else ""))
    elif n < max_rounds:
        L.append(f"suite : corriger les {len(red)} {RED} (rien d'autre), commiter, puis `review begin` : le tour {n + 1} ne relira que les lignes changées.")
    else:
        L.append(f"suite : plafond de {max_rounds} tours atteint et {len(red)} {RED} restants : laisser la PR ouverte pour l'humain, l'écrire au journal, "
                 "ne pas relancer de tour.")
    return 0, "\n".join(L)


# ---------------------------------------------------------------- relecteurs lancés par le hook H3 (agents)

def hook_register(project: Path, branch: str, tool_use_id: str, report: str | None = None) -> None:
    st = read_state(project, branch)
    st.setdefault("hook_agents", {})[tool_use_id] = {"at": _now(), "report": report}
    _write_state(project, branch, st)


def hook_unregister(project: Path, branch: str, tool_use_id: str) -> None:
    st = read_state(project, branch)
    if st.get("hook_agents", {}).pop(tool_use_id, None) is not None:
        _write_state(project, branch, st)


def find_hook_agent(project: Path, tool_use_id: str) -> tuple[str, dict] | None:
    """(branche, fiche) du tour préparé par le hook pour ce lancement d'agent, sinon None."""
    sd = state_dir(project)
    for f in (sorted(sd.glob("*.json")) if sd.is_dir() else []):
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rec = (st.get("hook_agents") or {}).get(tool_use_id)
        if rec is not None:
            return st.get("branch", f.stem), (rec if isinstance(rec, dict) else {})
    return None


def has_findings(text: str) -> bool:
    """Vrai si le texte contient au moins un constat formaté ou une conclusion « rien à signaler »."""
    return any(_ITEM.match(ln) for ln in text.splitlines()) or bool(_NO_FINDING.search(text))


def status(project: Path) -> str:
    sd = state_dir(project)
    files = sorted(sd.glob("*.json")) if sd.is_dir() else []
    if not files:
        return "TOKEN SAVER — revue : aucun tour enregistré dans ce projet"
    L = ["TOKEN SAVER — revue : tours par branche"]
    for f in files:
        try:
            st = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        rounds = st.get("rounds", [])
        pend = st.get("pending")
        desc = ", ".join(f"t{r['round']} {r['mode'].lower()} {r['verdict']} ({len(r.get('red', []))} {RED})" for r in rounds) or "aucun tour clos"
        L.append(f"  {st.get('branch', f.stem)}" + (f" (PR #{st['pr']})" if st.get("pr") else "") + f" : {desc}"
                 + (f" ; tour {pend['round']} en cours" if pend else ""))
    return "\n".join(L)


def reset(project: Path) -> str:
    branch = _git(project, "rev-parse", "--abbrev-ref", "HEAD").strip()
    f = state_file(project, branch)
    if f.is_file():
        f.unlink()
        return f"TOKEN SAVER — revue : compteur de tours remis à zéro sur {branch}"
    return f"TOKEN SAVER — revue : rien à remettre à zéro sur {branch}"


# ---------------------------------------------------------------- attente de la CI en un seul appel

def wait_ci(project: Path, pr: int | None = None, timeout_min: int = 45, poll_s: int = 30) -> tuple[int, str]:
    """Attend la fin des vérifications CI d'une PR dans UN processus (au lieu de 20 appels `gh pr checks` à contexte plein)."""
    if shutil.which("gh") is None:
        return 2, "`gh` introuvable : impossible d'interroger la CI (installe GitHub CLI ou regarde la PR dans le navigateur)"
    target = [str(pr)] if pr else []
    deadline = time.monotonic() + timeout_min * 60
    try:
        r = subprocess.run(["gh", "pr", "checks", *target, "--watch", "--fail-fast"], cwd=str(project), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout_min * 60)
        out = (r.stdout + r.stderr).strip()
        low = out.lower()
        if "unknown flag" not in low:
            tail = "\n".join(out.splitlines()[-15:])
            if r.returncode == 0:
                return 0, "TOKEN SAVER — CI : toutes les vérifications sont passées\n" + tail
            if "no checks reported" in low:
                return 0, "TOKEN SAVER — CI : aucune vérification déclarée sur cette PR\n" + tail
            if "no pull requests found" in low or "could not find" in low or "not found" in low:
                return 2, "TOKEN SAVER — CI : PR introuvable pour cette branche\n" + tail
            return 1, "TOKEN SAVER — CI : au moins une vérification a échoué (ne pas fusionner)\n" + tail
    except subprocess.TimeoutExpired:
        return 2, f"TOKEN SAVER — CI : toujours en cours après {timeout_min} min ; relance `review wait-ci` plus tard (un seul appel)"
    while True:                                                   # gh ancien sans --watch : sondage interne, un seul appel côté Claude
        r = subprocess.run(["gh", "pr", "checks", *target], cwd=str(project), capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (r.stdout + r.stderr).strip()
        pending = any(w in out.lower() for w in ("pending", "in_progress", "queued"))
        if not pending or time.monotonic() > deadline:
            tail = "\n".join(out.splitlines()[-15:])
            if pending:
                return 2, f"TOKEN SAVER — CI : toujours en cours après {timeout_min} min\n" + tail
            return (0 if r.returncode == 0 else 1), ("TOKEN SAVER — CI : toutes les vérifications sont passées\n" if r.returncode == 0
                                                    else "TOKEN SAVER — CI : au moins une vérification a échoué (ne pas fusionner)\n") + tail
        time.sleep(poll_s)


# ---------------------------------------------------------------- mesure (doctor R17/R18)

def measure(con, since: str | None) -> dict:
    """Relectures en chaîne (sous-agents de revue par PR) et sondage de la CI, depuis les transcripts collectés."""
    w, args = (" AND t.ts >= ?", (since,)) if since else ("", ())
    rows = con.execute("SELECT t.tool_use_id, t.extra, a.agent_id, a.type FROM tool_uses t JOIN agents a ON a.parent_tool_use_id = t.tool_use_id "
                       f"WHERE t.name IN ('Agent','Task'){w}", args).fetchall()
    per_pr: dict[str, dict] = {}
    n_rev, tok_rev = 0, 0
    for tid, extra, aid, atype in rows:
        try:
            desc = json.loads(extra or "{}").get("description") or ""
        except ValueError:
            desc = ""
        if not REVIEW_DESC.search(desc):
            continue
        tok = con.execute("SELECT COALESCE(SUM(input+cache_write+cache_read),0) FROM calls WHERE agent_id=?", (aid,)).fetchone()[0]
        if not tok:
            continue
        n_rev += 1
        tok_rev += tok
        m = re.search(r"(?:#|PR\s*#?|\bde\s+)(\d{2,6})\b", desc)
        if m:
            d = per_pr.setdefault(m.group(1), {"rounds": 0, "tokens": 0})
            d["rounds"] += 1
            d["tokens"] += tok
    chains = sorted(((pr, d) for pr, d in per_pr.items() if d["rounds"] >= 3), key=lambda kv: -kv[1]["rounds"])
    wc, argc = (" AND c.ts >= ?", (since,)) if since else ("", ())
    poll = con.execute(f"""SELECT COUNT(DISTINCT c.id), COALESCE(SUM(c.input+c.cache_write+c.cache_read),0) FROM calls c WHERE c.id IN (
        SELECT c2.id FROM calls c2 JOIN tool_uses t ON t.file=c2.file AND t.call_seq=c2.seq
        WHERE t.name IN ('Bash','PowerShell') AND (t.arg LIKE '%gh pr checks%' OR t.arg LIKE '%gh run %')){wc}""", argc).fetchone()
    total = con.execute(f"SELECT COALESCE(SUM(c.input+c.cache_write+c.cache_read),0) FROM calls c WHERE 1=1{wc}", argc).fetchone()[0] or 0
    return {"reviews": n_rev, "review_tokens": tok_rev, "chains": chains, "poll_calls": poll[0] or 0, "poll_tokens": poll[1] or 0,
            "total_tokens": total}
