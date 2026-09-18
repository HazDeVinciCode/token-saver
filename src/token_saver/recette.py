"""Recette de cycle : ce qu'une session neuve fait à chaque cycle d'autonomie, quand le projet n'a pas sa propre commande.

Règle (18/09/2026, feedback « outil générique ») : rien n'est supposé. L'inventaire regarde ce que le projet contient
(commandes, agents, tests, source des tâches, dépôt, CI, revue) et la recette est assemblée avec ça ; ce qui manque est
demandé à l'utilisateur (deux questions au plus) ; s'il n'y a rien, la recette de base suffit : une tâche, tests, revue
bornée, commit sur une branche, journal. La recette est un fichier lisible, `.token-saver/cycle.md`, validé par
l'utilisateur avant le premier départ, modifiable à la main, revu par `/token-saver-autonomie config`.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from .paths import ts_dir

RECIPE_FILE = "cycle.md"            # .token-saver/cycle.md : la recette (envoyée telle quelle à chaque session neuve)
ANSWERS_FILE = "recette.json"       # .token-saver/recette.json : réponses de l'utilisateur (tâches, tests), réutilisées par `config`
HEADER_ASK = "RECETTE À VALIDER"     # premier mot de la sortie quand il n'y a pas encore de recette : le raccourci sait quoi faire
HEADER_HAVE = "RECETTE ACTUELLE"

_TEST_NAME = re.compile(r"\btests?\b|\bspec\b|vérif|verif|\bcheck\b|\bqa\b", re.I)
_SHIP_NAME = re.compile(r"\bship\b|\bpr\b|merge|fusion|release|deploy|publish|livr", re.I)
_PLAN_NAME = re.compile(r"\bplan\b|design|spec|story|\bus\b|backlog|cadr", re.I)
_TASK_FILES = ("TODO.md", "TODO", "todo.md", "BACKLOG.md", "TASKS.md", "docs/TODO.md", "docs/BACKLOG.md")
_BOARD_WORDS = re.compile(r"\bboard\b|backlog|kanban|jira|linear|trello|notion|github projects?|\bissues?\b", re.I)
_CMD_IN_LINE = re.compile(r"`([^`\n]{3,80})`")


def recipe_path(project: Path) -> Path:
    return ts_dir(project) / RECIPE_FILE


def answers_path(project: Path) -> Path:
    return ts_dir(project) / ANSWERS_FILE


def load_answers(project: Path) -> dict:
    f = answers_path(project)
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ inventaire (lecture seule, sans hypothèse)

def _frontmatter(text: str) -> dict:
    """name/description d'un SKILL.md ou d'un agent (frontmatter YAML minimal, blocs `>`/`|` compris), sinon la première ligne de texte."""
    from .agents import frontmatter
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    out = {k: v for k, v in frontmatter(text).items() if k in ("name", "description") and v}
    if "description" not in out:
        body = text[m.end():] if m else text
        first = next((l.strip().lstrip("#").strip() for l in body.splitlines() if l.strip() and not l.strip().startswith("<!--")), "")
        if first:
            out["description"] = first
    return out


def _read(f: Path, limit: int = 200_000) -> str:
    try:
        return f.read_text(encoding="utf-8", errors="replace") if f.is_file() and f.stat().st_size < limit else ""
    except OSError:
        return ""


def _git(project: Path, *args: str) -> str:
    if not (project / ".git").exists():
        return ""
    try:
        return subprocess.run(["git", *args], cwd=str(project), capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=15).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _commands(project: Path) -> list[dict]:
    """Commandes et skills du projet (hors TOKEN SAVER), classés d'après leur nom : revue, tests, livraison, cadrage, autonomie, autre."""
    from .install import AUTONOMY_NAME, STOP_NAME, detect_review_commands
    from .review import REVIEW_NAME
    review_files = {str(f) for f in detect_review_commands(project)}
    items: list[dict] = []
    skills = project / ".claude" / "skills"
    files = [(d.name, d / "SKILL.md") for d in (sorted(skills.iterdir()) if skills.is_dir() else []) if d.is_dir() and (d / "SKILL.md").is_file()]
    cmds = project / ".claude" / "commands"
    files += [(f.stem, f) for f in (sorted(cmds.glob("*.md")) if cmds.is_dir() else [])]
    seen: set[str] = set()
    for name, f in files:
        if name.startswith("token-saver") or name in seen:
            continue
        seen.add(name)
        fm = _frontmatter(_read(f, 400_000))
        if str(f) in review_files:
            kind = "revue"
        elif AUTONOMY_NAME.search(name):
            kind = "arrêt" if STOP_NAME.search(name) else "autonomie"
        elif REVIEW_NAME.search(name):
            kind = "autre"                                   # revue d'écrans, d'UX… : pas une revue de code
        elif _TEST_NAME.search(name):
            kind = "tests"
        elif _SHIP_NAME.search(name):
            kind = "livraison"
        elif _PLAN_NAME.search(name):
            kind = "cadrage"
        else:
            kind = "autre"
        items.append({"name": name, "kind": kind, "desc": (fm.get("description") or "")[:110]})
    return items


def _agents(project: Path) -> list[dict]:
    agents = project / ".claude" / "agents"
    out = []
    for f in (sorted(agents.glob("*.md")) if agents.is_dir() else []):
        fm = _frontmatter(_read(f))
        name = fm.get("name") or f.stem
        text = f"{name} {fm.get('description', '')}"
        kind = "revue" if re.search(r"review|revue|relect|audit|critic|contr[ôo]le", text, re.I) else \
               "tests" if _TEST_NAME.search(text) else "autre"
        out.append({"name": name, "kind": kind, "desc": (fm.get("description") or "")[:110]})
    return out


def _tests(project: Path) -> dict:
    """Commande de test : d'abord ce que CLAUDE.md dit, sinon les traces classiques du projet. None si rien."""
    claude_md = _read(project / "CLAUDE.md")
    for line in claude_md.splitlines():
        if re.search(r"\btests?\b", line, re.I):
            for cmd in _CMD_IN_LINE.findall(line):
                if re.search(r"\btest|pytest|unittest|spec|jest|vitest|gut|gdunit|check", cmd, re.I) and not cmd.startswith("/"):
                    return {"command": cmd, "source": "CLAUDE.md"}
    pkg = _read(project / "package.json")
    if pkg:
        try:
            scripts = (json.loads(pkg).get("scripts") or {})
        except ValueError:
            scripts = {}
        if scripts.get("test") and "no test specified" not in scripts["test"]:
            runner = "pnpm" if (project / "pnpm-lock.yaml").is_file() else "yarn" if (project / "yarn.lock").is_file() else "npm"
            return {"command": f"{runner} test", "source": "package.json"}
    mk = _read(project / "Makefile")
    if re.search(r"^test\s*:", mk, re.M):
        return {"command": "make test", "source": "Makefile"}
    if (project / "pytest.ini").is_file() or "[tool.pytest" in _read(project / "pyproject.toml") or (project / "conftest.py").is_file():
        return {"command": "python -m pytest -q", "source": "configuration pytest"}
    if (project / "tests").is_dir() and any((project / "tests").glob("test_*.py")):
        return {"command": "python -m unittest discover -s tests -v", "source": "dossier tests/"}
    if any(project.glob("*.sln")) or any(project.glob("*.csproj")) or any(project.glob("*/*.csproj")):
        return {"command": "dotnet test", "source": "projet .NET"}
    if (project / "gradlew").is_file():
        return {"command": "./gradlew test", "source": "gradlew"}
    if (project / "Cargo.toml").is_file():
        return {"command": "cargo test", "source": "Cargo.toml"}
    if (project / "go.mod").is_file():
        return {"command": "go test ./...", "source": "go.mod"}
    return {"command": None, "source": "rien trouvé (CLAUDE.md, package.json, Makefile, pyproject.toml, tests/, .NET, gradle, cargo, go)"}


def _tasks(project: Path, repo: dict) -> dict:
    """Source des tâches : un fichier de tâches à cases (première trouvée), sinon les issues GitHub si gh et un remote GitHub. None sinon."""
    for rel in _TASK_FILES:
        f = project / rel
        text = _read(f)
        if f.is_file():
            n_open = len(re.findall(r"^\s*[-*]\s+\[ \]", text, re.M))
            return {"kind": "todo", "path": rel, "open": n_open, "source": f"{rel} ({n_open} tâche(s) à faire)"}
    if repo.get("github") and repo.get("gh"):
        return {"kind": "issues", "path": None, "open": None, "source": "issues GitHub (gh prêt)"}
    hint = ""
    for line in _read(project / "CLAUDE.md").splitlines():
        if _BOARD_WORDS.search(line):
            hint = line.strip()[:120]
            break
    return {"kind": None, "path": None, "open": None, "source": "rien trouvé" + (f" — CLAUDE.md mentionne : « {hint} »" if hint else "")}


def _repo(project: Path) -> dict:
    remote = _git(project, "remote", "get-url", "origin")
    base = ""
    if (project / ".git").exists():
        from .review import default_base
        base = (default_base(project) or "").split("/")[-1]
    wf = project / ".github" / "workflows"
    ci = "GitHub Actions" if wf.is_dir() and any(wf.glob("*.y*ml")) else "GitLab CI" if (project / ".gitlab-ci.yml").is_file() else None
    return {"git": (project / ".git").exists(), "remote": remote, "github": "github.com" in remote, "gh": bool(shutil.which("gh")),
            "base": base or "main", "ci": ci}


def inventory(project: Path, cfg_all: dict) -> dict:
    repo = _repo(project)
    rv_paths = list((cfg_all.get("review") or {}).get("commands") or [])
    rv_names = ["/" + (Path(c).parent.name if Path(c).name == "SKILL.md" else Path(c).stem) for c in rv_paths]
    from .install import REVIEW_SKILL_NAME
    review = {"command": rv_names[0] if rv_names else f"/{REVIEW_SKILL_NAME}",
              "source": "commande de revue du projet, procédure bornée installée" if rv_names else "revue bornée générique de TOKEN SAVER"}
    return {"commands": _commands(project), "agents": _agents(project), "tests": _tests(project), "tasks": _tasks(project, repo),
            "repo": repo, "review": review}


# ------------------------------------------------------------------ recette

def questions(inv: dict, answers: dict) -> list[str]:
    q = []
    if not inv["tasks"]["kind"] and not answers.get("tasks"):
        q.append("? tâches : où sont-elles ? (rien trouvé : ni fichier TODO.md à cases ni issues GitHub) — réponses : « todo » (un TODO.md à la racine, "
                 "créé avec les tâches que tu donnes), « issues » (issues GitHub ouvertes), ou décris la source (fichier, colonne d'un board…)")
    if not inv["tests"]["command"] and not answers.get("tests"):
        q.append("? tests : quelle commande les lance ? (rien trouvé dans CLAUDE.md, package.json, Makefile, pyproject.toml, tests/…) — "
                 "réponses : la commande, ou « aucun » si le projet n'a pas de tests automatisés")
    return q


def _task_lines(inv: dict, answers: dict) -> list[str]:
    t = inv["tasks"]
    a = (answers.get("tasks") or "").strip()
    if a.lower() in ("todo", "todo.md"):
        return ["- Source des tâches : `TODO.md` à la racine — prends la **première case non cochée** `- [ ]`. S'il n'existe pas ou n'a plus de case : plus de tâche."]
    if a.lower() in ("issues", "issue", "github"):
        return ["- Source des tâches : les issues GitHub ouvertes — `gh issue list --state open --limit 20` ; prends la plus ancienne non assignée, "
                "assigne-la-toi (`gh issue edit N --add-assignee @me`) pour la marquer prise. Si `gh` échoue : plus de tâche."]
    if a:
        return [f"- Source des tâches, indiquée par l'utilisateur : {a}. Prends la première tâche disponible selon cette source ; si tu ne peux pas la lire : plus de tâche."]
    if t["kind"] == "todo":
        return [f"- Source des tâches : `{t['path']}` — prends la **première case non cochée** `- [ ]`. Plus de case : plus de tâche."]
    if t["kind"] == "issues":
        return ["- Source des tâches : les issues GitHub ouvertes — `gh issue list --state open --limit 20` ; prends la plus ancienne non assignée, "
                "assigne-la-toi (`gh issue edit N --add-assignee @me`) pour la marquer prise. Si `gh` échoue : plus de tâche."]
    return ["- Source des tâches : `TODO.md` à la racine (première case non cochée `- [ ]`), sinon `gh issue list --state open --limit 20` si `gh` fonctionne, sinon rien."]


def _test_lines(inv: dict, answers: dict) -> list[str]:
    a = (answers.get("tests") or "").strip()
    cmd = None if a.lower() in ("aucun", "aucune", "none", "non") else (a or inv["tests"]["command"])
    if cmd:
        return [f"- Tests : lance `{cmd}` ; ajoute des tests pour la logique nouvelle. Tant qu'ils échouent, corrige — au plus **3 tentatives**, "
                "puis note l'échec et passe à la fin du cycle."]
    return ["- Ce projet n'a pas de commande de tests automatisés : vérifie ton changement autrement (exécution, script, lecture attentive) et "
            "écris la preuve dans le journal. N'invente pas de framework de test."]


def build(project: Path, cfg_all: dict, answers: dict | None = None, inv: dict | None = None) -> str:
    """La recette, assemblée avec ce que le projet contient et les réponses de l'utilisateur. Un cycle = une tâche, dans une session neuve."""
    inv = inv or inventory(project, cfg_all)
    answers = answers or {}
    repo, review = inv["repo"], inv["review"]
    cmds = [c for c in inv["commands"] if c["kind"] not in ("autonomie", "arrêt", "revue")]
    agents = inv["agents"]
    L = ["# Recette de cycle TOKEN SAVER — établie le " + time.strftime("%Y-%m-%d %H:%M") + " d'après le contenu du projet",
         "<!-- Un cycle = une session neuve qui fait exactement UNE tâche. Modifie ce fichier à la main, ou /token-saver-autonomie config. -->",
         "",
         "Tu fais **exactement une tâche**, de bout en bout, puis tu rends la main. Tu travailles seul : personne ne répondra à une question ; "
         "décide, note la décision au journal, continue.",
         "",
         "## 1. Reprendre l'état",
         "- Lis `.token-saver/nuit/ETAT.md` s'il existe (tâche en cours, dernières décisions) et les 40 premières lignes de `.token-saver/nuit/JOURNAL.md`.",
         "- Si `ETAT.md` dit `actif: oui` : reprends cette tâche là où elle en est, ne prends pas une autre."]
    L += _task_lines(inv, answers)
    L += ["- S'il ne reste **aucune tâche** : écris `actif: non` et `fin: plus de tâche` dans `ETAT.md`, réponds par la ligne exacte `[nuit:fin] plus de tâche` et arrête-toi.",
          "",
          "## 2. Choisir et cadrer",
          "- Prends la première tâche disponible ; ne prends jamais deux tâches de front.",
          "- Écris dans `ETAT.md` : `actif: oui`, `tâche: <titre>`, `démarrée: <date heure>`, `critère: <ce qui prouve qu'elle est finie>`."]
    if repo["git"]:
        L.append(f"- Crée ou reprends une branche `autonomie/<slug-de-la-tache>` depuis `{repo['base']}` à jour. Ne travaille jamais directement sur `{repo['base']}`.")
    plan_cmds = [c for c in cmds if c["kind"] == "cadrage"]
    if plan_cmds:
        L.append("- Pour cadrer une tâche floue, le projet a : " + " ; ".join(f"`/{c['name']}`" + (f" ({c['desc']})" if c["desc"] else "") for c in plan_cmds[:3]) + ".")
    L += ["",
          "## 3. Réaliser avec preuves",
          "- Lis seulement les fichiers nécessaires ; pour un gros document, `python .token-saver/bin/token-saver.pyz find \"<sujet>\"`.",
          "- Respecte `CLAUDE.md` et les conventions du dépôt."]
    L += _test_lines(inv, answers)
    test_cmds = [c for c in cmds if c["kind"] == "tests"]
    if test_cmds:
        L.append("- Commandes de test du projet : " + " ; ".join(f"`/{c['name']}`" + (f" ({c['desc']})" if c["desc"] else "") for c in test_cmds[:3]) + ".")
    test_agents = [a for a in agents if a["kind"] == "tests"]
    if test_agents:
        L.append("- Agents de test du projet, un appel chacun au plus : " + " ; ".join(f"`{a['name']}`" + (f" ({a['desc']})" if a["desc"] else "") for a in test_agents[:3]) + ".")
    if repo["git"]:
        L.append("- Commits conventionnels sur la branche de la tâche. Pas de `push --force`, pas de suppression de branche, rien d'irréversible, rien hors du projet.")
    else:
        L.append("- Ce dossier n'est pas un dépôt git : garde une copie `.bak` de chaque fichier modifié dans `.token-saver/nuit/backup/`, rien d'irréversible, rien hors du projet.")
    L += ["",
          "## 4. Relire",
          f"- Relis ton changement avec `{review['command']}` ({review['source']}) : le diff seul, 3 tours au plus, seuls les 🔴 empêchent de continuer. "
          "Corrige les 🔴. Les 🟠 se corrigent dans la même PR avant la fusion (avec leur test si c'est du code) ou sont écartés par écrit "
          "dans la description de la PR, avec la raison : jamais de carte ni d'issue de suivi pour un 🟠. "
          "Un 🟠 qui décrit un défaut que l'utilisateur verrait est un 🔴."]
    review_agents = [a for a in agents if a["kind"] == "revue"]
    if review_agents:
        L.append("- Relecteurs du projet, à convoquer au tour 1 seulement, avec le fichier de diff : " + " ; ".join(f"`{a['name']}`" for a in review_agents[:3]) + ".")
    L += ["", "## 5. Clore"]
    if repo["github"] and repo["gh"]:
        L.append(f"- Pousse la branche et ouvre une PR vers `{repo['base']}` (`gh pr create --fill`)"
                 + (f" ; attends la CI ({repo['ci']}) avec `python .token-saver/bin/token-saver.pyz review wait-ci --pr <N>` (un seul appel)" if repo["ci"] else "")
                 + ". Ne fusionne pas toi-même : la PR revient à l'humain.")
    elif repo["remote"]:
        L.append("- Pousse la branche (`git push -u origin autonomie/<slug>`) ; la fusion revient à l'humain.")
    elif repo["git"]:
        L.append("- Laisse la branche commitée ; la fusion revient à l'humain.")
    ship_cmds = [c for c in cmds if c["kind"] == "livraison"]
    if ship_cmds:
        L.append("- Commandes de livraison du projet, si la tâche le demande : " + " ; ".join(f"`/{c['name']}`" + (f" ({c['desc']})" if c["desc"] else "") for c in ship_cmds[:3]) + ".")
    tk = (answers.get("tasks") or "").strip().lower() or inv["tasks"]["kind"] or ""
    if tk in ("todo", "todo.md") or inv["tasks"]["kind"] == "todo" and not answers.get("tasks"):
        L.append("- Coche la case de la tâche (`- [x]`) si elle est finie, sinon ajoute en dessous `  - échec : <raison en une ligne>`.")
    elif tk in ("issues", "issue", "github"):
        L.append("- Commente l'issue avec le lien de la PR (ou la raison de l'échec) ; ne la ferme pas toi-même.")
    L += ["- Ajoute EN TÊTE de `.token-saver/nuit/JOURNAL.md` une entrée en français, sans jargon d'outil : `## <date heure> — <tâche>` puis "
          "**Ce qui a été fait**, **Preuve** (tests, commande, résultat), **Commits** (branche + hachages), **Décisions prises à ta place**, **Ce qui reste**.",
          "- Mets à jour `ETAT.md` : `actif: non`, `dernière tâche: <titre>`, `résultat: fait|échec`.",
          "- Termine par un résumé de 5 lignes maximum. Ne commence pas une autre tâche."]
    others = [c for c in cmds if c["kind"] == "autre"]
    if others or [a for a in agents if a["kind"] == "autre"]:
        L += ["", "## Aussi disponibles dans ce projet (à utiliser seulement si la tâche le demande)"]
        L += [f"- `/{c['name']}`" + (f" — {c['desc']}" if c["desc"] else "") for c in others[:8]]
        L += [f"- agent `{a['name']}`" + (f" — {a['desc']}" if a["desc"] else "") for a in agents if a["kind"] == "autre"][:6]
    return "\n".join(L) + "\n"


def render_found(inv: dict) -> str:
    """Ce que l'inventaire a trouvé, en quelques lignes lisibles."""
    repo, t = inv["repo"], inv["tasks"]
    cmds = [c for c in inv["commands"] if c["kind"] not in ("arrêt",)]
    L = ["Trouvé dans le projet :",
         f"  tâches    : {t['source']}",
         f"  tests     : {inv['tests']['command'] + ' (' + inv['tests']['source'] + ')' if inv['tests']['command'] else inv['tests']['source']}",
         f"  revue     : {inv['review']['command']} ({inv['review']['source']})",
         "  commandes : " + (", ".join(f"/{c['name']} [{c['kind']}]" for c in cmds[:12]) + (" …" if len(cmds) > 12 else "") if cmds else "aucune"),
         "  agents    : " + (", ".join(f"{a['name']} [{a['kind']}]" for a in inv["agents"][:12]) if inv["agents"] else "aucun"),
         "  dépôt     : " + ("GitHub" + (", gh prêt" if repo["gh"] else ", gh absent") if repo["github"] else "git avec remote" if repo["remote"]
                            else "git local" if repo["git"] else "pas de git") + (f", CI {repo['ci']}" if repo["ci"] else "")]
    return "\n".join(L)


def propose(project: Path, cfg_all: dict) -> str:
    """Sortie de `nuit recette propose` et de `nuit start` sans recette : inventaire, recette (actuelle ou proposée), questions, marche à suivre."""
    inv = inventory(project, cfg_all)
    answers = load_answers(project)
    cur = recipe_path(project)
    own = _own_command(project, cfg_all)
    L = []
    if cur.is_file():
        L += [f"{HEADER_HAVE} — ce projet a une recette de cycle validée ({_rel(project, cur)}), en voici une version régénérée d'après le projet d'aujourd'hui ; "
              "« Valider » l'enregistre à la place de l'actuelle, « Annuler » garde l'actuelle."]
    elif own:
        L += [f"{HEADER_HAVE} — ce projet a sa propre commande de cycle, `{own}` : TOKEN SAVER l'utilise telle quelle. "
              "Ci-dessous, la recette que TOKEN SAVER proposerait à la place ; « Valider » la met à la place de la commande du projet "
              "(`nuit.prompt` fixé), « Annuler » ne change rien."]
    else:
        L += [f"{HEADER_ASK} — aucune recette de cycle dans ce projet. Ce que TOKEN SAVER a trouvé, puis la recette qu'il propose."]
    L += ["", render_found(inv), "", "Recette proposée :", "-----", build(project, cfg_all, answers, inv).rstrip(), "-----"]
    qs = questions(inv, answers)
    if qs:
        L += ["", "Questions (sans réponse, la recette garde la ligne générique) :"] + qs
    L += ["", "Ensuite : `python .token-saver/bin/token-saver.pyz nuit recette save` (options : `--tasks \"…\"`, `--tests \"…\"`) écrit "
          f"{_rel(project, cur)} ; puis `nuit start` lance l'autonomie avec."]
    return "\n".join(L)


def _own_command(project: Path, cfg_all: dict) -> str | None:
    p = (cfg_all.get("nuit") or {}).get("prompt")
    return p if p else None


def _rel(project: Path, f: Path) -> str:
    try:
        return f.relative_to(project).as_posix()
    except ValueError:
        return str(f)


def save(project: Path, cfg_all: dict, tasks: str | None = None, tests: str | None = None, replace: bool = False) -> str:
    """Écrit la recette (et les réponses) ; `replace` la met aussi à la place de la commande du projet (nuit.prompt fixé à vide)."""
    answers = load_answers(project)
    if tasks:
        answers["tasks"] = tasks.strip()
    if tests:
        answers["tests"] = tests.strip()
    answers["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    text = build(project, cfg_all, answers)
    f = recipe_path(project)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    answers_path(project).write_text(json.dumps(answers, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    msg = [f"recette enregistrée : {_rel(project, f)} ({len(text.splitlines())} lignes) — modifiable à la main, revue par /token-saver-autonomie config"]
    own = _own_command(project, cfg_all)
    if own:
        if replace:
            _set_prompt(project, None)
            msg.append(f"elle remplace la commande du projet `{own}` (nuit.prompt vidé, prompt_auto désactivé)")
        else:
            msg.append(f"note : ce projet garde sa commande de cycle `{own}` ; pour utiliser la recette à la place : `nuit recette save --replace`")
    return "\n".join(msg)


def _set_prompt(project: Path, value: str | None) -> None:
    cfgf = ts_dir(project) / "config.json"
    try:
        cfg = json.loads(cfgf.read_text(encoding="utf-8")) if cfgf.is_file() else {}
    except (OSError, ValueError):
        cfg = {}
    n = cfg.setdefault("nuit", {})
    n["prompt"] = value
    n["prompt_auto"] = False
    cfgf.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def show(project: Path, cfg_all: dict) -> str:
    own = _own_command(project, cfg_all)
    f = recipe_path(project)
    if own:
        return f"recette de cycle : la commande du projet `{own}`" + (f" (une recette TOKEN SAVER existe aussi, inactive : {_rel(project, f)})" if f.is_file() else "")
    if f.is_file():
        return f"recette de cycle : {_rel(project, f)}\n\n" + f.read_text(encoding="utf-8").rstrip()
    return "aucune recette de cycle : `/token-saver-autonomie` t'accompagne pour la créer"
