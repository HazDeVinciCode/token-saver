"""Installation / désinstallation project-local, pilotées par un manifest.

Règles : tout fichier créé et toute clé ajoutée sont inscrits dans manifest.json avec leur valeur
antérieure ; rien d'autre n'est jamais supprimé ni modifié ; idempotence par marqueur.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sys
import time
import zipapp
from pathlib import Path

from . import __version__
from .paths import TS_DIRNAME, localize, py_cmd, ts_dir

MARK = "token-saver:"          # préfixe des marqueurs (champ officiel statusMessage des hooks)
def _skill(name: str, description: str, command: str, restitution: str) -> str:
    return (f"---\nname: {name}\ndescription: {description}\ndisable-model-invocation: true\n"
            f"allowed-tools: Bash(python .token-saver/bin/token-saver.pyz *)\n---\n"
            f"Exécute via Bash : `python .token-saver/bin/token-saver.pyz {command}`.\n{restitution}\nNe relance pas la commande.\n")


_PLAIN = ("Restitue UNIQUEMENT la section « EN CLAIR » de la sortie, telle quelle, en français, sans jargon technique "
          "(pas de « cache read », « token-relectures », « registre ») et sans commentaire supplémentaire.")
_PLAIN_ALL = "Restitue la sortie telle quelle, sans commentaire."
# Sept raccourcis visibles (18/09/2026, à la demande de l'utilisateur : « trop de commandes, et plus jamais le mot nuit »).
# `disable-model-invocation` = invisible pour le modèle (0 token de description), visible pour l'utilisateur.
SKILLS: dict[str, str] = {
    "token-saver-status": _skill("token-saver-status", "TOKEN SAVER — tout l'état sur un écran : installation, ce qui tourne maintenant, bilan de la dernière autonomie, dépense du jour et de la semaine",
                                 "overview", _PLAIN_ALL),
    "token-saver-doctor": _skill("token-saver-doctor", "TOKEN SAVER — les conseils pour dépenser moins, classés par impact et chiffrés",
                                 "doctor --since 30d", "Restitue les constats HIGH et MEDIUM tels quels (titre, pourquoi, conseil), en français, sans ajouter de commentaire."),
    "token-saver-dashboard": _skill("token-saver-dashboard", "TOKEN SAVER — génère et ouvre le tableau de bord HTML",
                                    "dashboard --open", "Indique seulement le chemin du fichier HTML généré."),
    "token-saver-autonomie-stop": _skill("token-saver-autonomie-stop", "TOKEN SAVER — arrête l'autonomie en sessions neuves à la fin du cycle en cours (argument --now : tout de suite)",
                                         "nuit stop $ARGUMENTS", _PLAIN_ALL),
    "token-saver-off": _skill("token-saver-off", "TOKEN SAVER — retire tout de ce projet (réglages, hooks, raccourcis, blocs ; mesures conservées)",
                              "off", "Restitue le rapport tel quel."),
    "token-saver-prune": _skill("token-saver-prune", "TOKEN SAVER — expert : retire du contexte les outils et plugins que ce projet n'utilise jamais (aperçu ; --apply pour écrire ; --undo pour les remettre)",
                                "prune $ARGUMENTS", _PLAIN_ALL),
}
# Passage de relais : la tâche est finie, on continue dans une session neuve qui reçoit la note toute seule (hook SessionStart).
NEXT_SKILL = """---
name: token-saver-next
description: TOKEN SAVER — la tâche en cours est finie : note l'état en quelques lignes et continue dans une session neuve, qui recevra la note toute seule
disable-model-invocation: true
allowed-tools: Read, Write, Bash(git *)
---
La tâche en cours est terminée, ou l'utilisateur veut repartir sur une conversation neuve. Fais exactement ceci, rien d'autre :
1. Écris le fichier `.token-saver/state/handoff.md` (crée-le ou remplace-le), en français, 25 lignes au plus :
   - `## Passage de relais — <date et heure>`
   - **Fait** : ce qui a été livré dans cette conversation (branche, commits, PR, fichiers principaux).
   - **Reste à faire** : la prochaine étape précise, et ce qui n'est pas fini.
   - **Décisions prises** et leurs raisons ; **pièges rencontrés**.
   - **À relire d'abord** : trois fichiers au plus, avec le pourquoi.
2. Réponds en trois lignes : « Note écrite. Ouvre une nouvelle session sur ce projet : elle recevra cette note automatiquement au démarrage et repartira légère. » Ne commence aucune nouvelle tâche.
"""
SKILLS["token-saver-next"] = NEXT_SKILL

# Autonomie en sessions neuves. Si le projet n'a pas de recette de cycle (ni commande à lui, ni .token-saver/cycle.md), `nuit start`
# répond « RECETTE À VALIDER » avec l'inventaire du projet et une proposition (recette.py) : le raccourci la montre, pose les questions,
# enregistre après accord, puis lance. `config` : revoir la recette. Les tâches et tests viennent du projet ou de l'utilisateur, jamais d'une supposition.
AUTONOMIE_SKILL = """---
name: token-saver-autonomie
description: TOKEN SAVER — lance l'autonomie en sessions neuves (argument : nombre de cycles ; rien = jusqu'à l'arrêt ; `config` = revoir la recette du cycle) ; la première fois, propose la recette d'après le projet
disable-model-invocation: true
allowed-tools: Bash(python .token-saver/bin/token-saver.pyz *), Read(.token-saver/cycle.md), Edit(.token-saver/cycle.md)
---
Argument reçu : `$ARGUMENTS` (vide = `0` = jusqu'à l'arrêt ; un nombre = autant de cycles ; `config` = revoir la recette du cycle).

1. Si l'argument est `config` : exécute via Bash `python .token-saver/bin/token-saver.pyz nuit recette propose`, puis va à l'étape 3.
   Sinon exécute via Bash `python .token-saver/bin/token-saver.pyz nuit start --cycles <argument, ou 0>`.
2. Si la sortie ne commence pas par `RECETTE` : restitue-la telle quelle, sans commentaire, puis rends la main. Ne lance rien d'autre ; rappelle en une ligne qu'il ne faut plus travailler dans cette session sur ce projet tant que l'autonomie tourne (`/token-saver-status` pour suivre).
3. La sortie commence par `RECETTE` : le projet n'a pas encore de recette de cycle validée (ou l'utilisateur veut la revoir). La sortie contient ce que TOKEN SAVER a trouvé dans le projet, la recette proposée entre les lignes `-----`, et parfois des questions (lignes qui commencent par `?`). Fais exactement ceci :
   - Montre à l'utilisateur, telles quelles, la section « Trouvé dans le projet » et la recette proposée.
   - Pose UNE fois l'outil AskUserQuestion : d'abord chaque ligne `?` (avec les réponses qu'elle indique, plus « Autre »), puis « Cette recette te va ? » avec les choix « Valider », « Modifier » (il dira quoi changer), « Annuler ».
   - « Annuler » : réponds « Rien n'a été enregistré » et arrête-toi.
   - Exécute via Bash `python .token-saver/bin/token-saver.pyz nuit recette save`, avec pour chaque question posée l'option correspondante : `--tasks "<réponse>"`, `--tests "<commande, ou aucun>"`. Si l'utilisateur a choisi « todo » et que `TODO.md` n'existe pas à la racine : demande-lui ses tâches et écris `TODO.md`, une ligne `- [ ] <tâche>` par tâche, rien d'autre.
   - « Modifier » : applique exactement les changements demandés dans `.token-saver/cycle.md` (outil Edit), rien d'autre. N'invente aucune commande, aucun agent, aucune source de tâches.
   - Puis, sauf si l'argument était `config` : relance via Bash `python .token-saver/bin/token-saver.pyz nuit start --cycles <argument, ou 0>` et applique l'étape 2.
"""
SKILLS["token-saver-autonomie"] = AUTONOMIE_SKILL

# ---------------------------------------------------------------- revue bornée (voir review.py)
# La procédure vaut pour toute commande de revue du projet, quel que soit son nom (/review-pr, /code-review, /revue…), mais n'est
# plus écrite dedans (jusqu'au 18/09/2026 : bloc marqué, régénéré à chaque mise à jour — il a écrasé une correction locale).
# Elle est soufflée par hook au démarrage de la revue (hooks.h3_prompt / h3_skill) ; les anciens blocs sont retirés à la mise à jour.
# La grille du projet reste la référence ; la procédure fixe seulement ce qui est lu (le diff, en fichier), le nombre de tours (3)
# et ce qui bloque (🔴).
REVIEW_BLOCK_BEGIN, REVIEW_BLOCK_END = "<!-- token-saver:review:begin -->", "<!-- token-saver:review:end -->"
REVIEW_SKILL_NAME = "token-saver-review"
REVIEW_PROCEDURE = """## Procédure TOKEN SAVER — la grille de la commande de revue reste la référence
Cette procédure fixe **ce qui est lu**, **le nombre de tours** et **ce qui bloque** ; elle ne change pas la grille.
1. Commence par `python .token-saver/bin/token-saver.pyz review begin` (Bash ; `--pr N` pour une PR). Il écrit dans un fichier le diff à relire et indique le tour : **tour 1 = diff complet** ; **tour 2 = seulement les lignes changées depuis le tour 1**, avec les 🔴 précédents à vérifier ; **tour 3 = vérification des 🔴 restants uniquement**. Jamais de 4ᵉ tour : la PR revient à l'humain.
2. Relis **uniquement ce fichier de diff** avec la grille. Ouvre un fichier source seulement pour comprendre une ligne que le diff cite. Ne relance pas ce que la CI a déjà exécuté sur ce commit : lis ses résultats. Si aucune CI n'a tourné, lance la commande de test du projet une fois et cite le résultat dans le rapport.
3. Les relecteurs ou spécialistes que cette commande prévoit sont convoqués au tour 1 ; aux tours suivants, seulement ceux qui ont émis un 🔴. Chacun reçoit le chemin du fichier de diff et cette consigne, jamais la PR entière. Une vérification croisée des constats, si elle est prévue, ne porte que sur les 🔴.
4. Rapport : au plus 12 constats, **une ligne par constat, qui commence par 🔴, 🟠 ou 🟢**, puis `fichier:ligne` et une phrase ; aucun emoji ailleurs dans le rapport (l'outil compte les lignes qui commencent par 🔴). 🔴 = empêche la fusion (bug, perte de données, faille, régression, règle du projet violée, **tout défaut que l'utilisateur final verrait**) ; 🟠 = amélioration, ne bloque pas ; 🟢 = point positif. Écris-le dans le fichier que `review begin` indique.
5. Termine par `python .token-saver/bin/token-saver.pyz review end --report <fichier>` et restitue le verdict avec la liste des 🔴. Sans 🔴, le verdict est FUSIONNABLE. Les 🟠 restants **se corrigent dans la même PR, avant la fusion** (avec leur test si c'est du code) ou sont **écartés par écrit dans la description de la PR, avec leur raison** ; on n'ouvre jamais de carte ni d'issue de suivi pour un 🟠, et on ne relance pas de tour pour eux. Un 🟠 qui décrit un défaut que l'utilisateur verrait n'est pas un 🟠 : c'est un 🔴.
6. Pour attendre la CI : `python .token-saver/bin/token-saver.pyz review wait-ci` (un seul appel), jamais `gh pr checks` répété.
"""
REVIEW_SKILL = f"""---
name: {REVIEW_SKILL_NAME}
description: TOKEN SAVER — revue de code bornée de la branche courante ou d'une PR (le diff seul, 3 tours maximum, seuls les 🔴 bloquent) ; usage /{REVIEW_SKILL_NAME} [numéro de PR]
---
Tu relis un changement de code ; tu n'édites rien. Argument éventuel : un numéro de PR (`review begin --pr N`).

## Grille
- **Correction** : le code fait ce que la tâche annonce ; cas limites ; erreurs gérées, jamais avalées.
- **Régressions** : le comportement existant est préservé ; les tests existants restent valides.
- **Preuves** : chaque logique nouvelle a un test qui échouerait sans elle.
- **Sécurité** : entrées non fiables validées ; aucun secret ; aucune injection ; aucune donnée personnelle superflue.
- **Règles du projet** : `CLAUDE.md` et conventions du dépôt.
- **Lisibilité** : noms, duplication, complexité inutile.

{REVIEW_BLOCK_BEGIN}
{REVIEW_PROCEDURE}{REVIEW_BLOCK_END}
"""


def detect_review_commands(project: Path) -> list[Path]:
    """Commandes/skills de revue de CODE du projet, quel que soit leur nom (review-pr, code-review, revue…), hors TOKEN SAVER.
    Le nom doit évoquer une revue et le contenu parler de code (diff, PR, branche, commit, code) : une « revue UX » d'écrans n'est pas concernée."""
    from .review import CODE_WORDS, REVIEW_NAME
    out: list[Path] = []
    skills = project / ".claude" / "skills"
    cands = [d / "SKILL.md" for d in (sorted(skills.iterdir()) if skills.is_dir() else [])
             if d.is_dir() and (d / "SKILL.md").is_file() and REVIEW_NAME.search(d.name) and not d.name.startswith("token-saver")]
    cmds = project / ".claude" / "commands"
    cands += [f for f in (sorted(cmds.glob("*.md")) if cmds.is_dir() else []) if REVIEW_NAME.search(f.stem) and not f.stem.startswith("token-saver")]
    for f in cands:
        try:
            body = _strip_marked(f.read_bytes().decode("utf-8", "replace"), REVIEW_BLOCK_BEGIN, REVIEW_BLOCK_END)
        except OSError:
            continue
        if CODE_WORDS.search(body):
            out.append(f)
    return out


def _read_nl(f: Path) -> tuple[str, bool]:
    """Texte en \\n + indicateur CRLF, pour réécrire le fichier avec ses fins de ligne d'origine."""
    raw = f.read_bytes().decode("utf-8")
    return raw.replace("\r\n", "\n"), "\r\n" in raw


def _write_nl(f: Path, text: str, crlf: bool) -> None:
    f.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))


def _strip_marked(content: str, begin: str, end: str) -> str:
    i, j = content.find(begin), content.find(end)
    if i < 0 or j < 0:
        return content
    head = content[:i].rstrip("\n")
    tail = content[j + len(end):].lstrip("\n")
    return (head + "\n" if head.strip() else "") + tail


def remove_review_blocks(project: Path, manifest: dict) -> list[str]:
    """Retire les blocs de procédure qu'une version antérieure avait écrits dans les commandes de revue du projet (retour à l'original,
    octet pour octet quand la sauvegarde existe) ; le manifest ne les suit plus. La procédure est désormais soufflée par hook."""
    removed: list[str] = []
    for b in [b for b in manifest.get("blocks", []) if b.get("block") == "review"]:
        f = project / b["file"]
        if f.is_file():
            _remove_review_block(project, f, b)
            removed.append(b["file"])
    for f in detect_review_commands(project):             # bloc orphelin (manifest perdu) : retiré aussi
        if REVIEW_BLOCK_BEGIN in _read_nl(f)[0]:
            _remove_review_block(project, f, {})
            removed.append(_entry_path(project, f))
    manifest["blocks"] = [b for b in manifest.get("blocks", []) if b.get("block") != "review"]
    return sorted(set(removed))


def _remove_review_block(project: Path, f: Path, b: dict) -> None:
    """Retire le bloc ; restaure l'original octet pour octet depuis la plus ancienne sauvegarde compatible (la pose du bloc avait
    retiré les lignes vides de fin : mesuré sur un projet réel, 194 octets → 193, `git status` sale)."""
    text, crlf = _read_nl(f)
    stripped = _strip_marked(text, REVIEW_BLOCK_BEGIN, REVIEW_BLOCK_END)
    if stripped == text:
        return
    rel = _entry_path(project, f) or ""
    cands = sorted((ts_dir(project) / "backups").glob(f"review-block.{rel.replace('/', '__')}.*.bak"))   # la plus ancienne d'abord
    if b.get("backup") and (project / b["backup"]).is_file():
        cands.append(project / b["backup"])
    for bak in cands:
        try:
            bt = _read_nl(bak)[0]
        except (OSError, UnicodeDecodeError):
            continue
        if REVIEW_BLOCK_BEGIN not in bt and bt.rstrip("\n") == stripped.rstrip("\n"):
            shutil.copy2(bak, f)                          # restauration octet pour octet (fins de ligne et lignes vides de fin comprises)
            return
    _write_nl(f, stripped, crlf)


AUTONOMY_NAME = re.compile(r"autonom|totale|boucle|\bloop\b", re.I)
STOP_NAME = re.compile(r"stop|arr[eê]t", re.I)


def detect_autonomy_commands(project: Path) -> tuple[list[str], list[str]]:
    """Commandes du projet qui lancent le mode autonome (autonomie, autonomie-totale…) et celles qui l'arrêtent (stop-autonomie)."""
    names: list[str] = []
    skills = project / ".claude" / "skills"
    names += [d.name for d in (sorted(skills.iterdir()) if skills.is_dir() else []) if d.is_dir() and (d / "SKILL.md").is_file()]
    cmds = project / ".claude" / "commands"
    names += [f.stem for f in (sorted(cmds.glob("*.md")) if cmds.is_dir() else [])]
    names = [n for n in dict.fromkeys(names) if not n.startswith("token-saver") and AUTONOMY_NAME.search(n)]
    return [n for n in names if not STOP_NAME.search(n)], [n for n in names if STOP_NAME.search(n)]


def propose_env_checks(project: Path) -> list[dict]:
    """Vérifications d'environnement proposées d'après ce que le projet contient — jamais d'hypothèse en dur :
    un workflow GitHub + un remote GitHub + `gh` → état de la CI ; des scripts/skills qui parlent d'adb/Android + `adb` → appareil branché."""
    import subprocess
    checks: list[dict] = []
    remote = ""
    if (project / ".git").exists():
        try:
            remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(project), capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=15).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            remote = ""
    workflows = list((project / ".github" / "workflows").glob("*.y*ml")) if (project / ".github" / "workflows").is_dir() else []
    if workflows and "github.com" in remote and shutil.which("gh"):
        from .review import default_base
        base = (default_base(project) or "main").split("/")[-1]
        checks.append({"name": "CI GitHub : dernière exécution sur la branche principale", "expect": "regex:success", "required": False,
                       "command": f"gh run list --branch {base} --limit 1 --json conclusion --jq .[0].conclusion"})
    texts = []
    for f in list((project / ".claude" / "skills").glob("*/SKILL.md")) + list(project.glob("*.ps1")) + list(project.glob("*.sh")) \
            + list((project / "scripts").glob("*")) + list((project / "tools").glob("*")):
        if f.is_file() and f.stat().st_size < 200_000:
            try:
                texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    if re.search(r"\badb\b|build-android|\bandroid\b", "\n".join(texts), re.I) and shutil.which("adb"):
        checks.append({"name": "appareil Android branché (adb)", "command": "adb devices", "expect": r"regex:^\S+\s+device\s*$", "required": False})
    return checks


def default_cycle_prompt(project: Path) -> str | None:
    """Commande de cycle : celle du projet si elle existe (`/autonomie 1`, `/cycle 1`) ; sinon None = la recette TOKEN SAVER
    (.token-saver/cycle.md), proposée d'après le projet et validée par l'utilisateur au premier `/token-saver-autonomie`."""
    for name in ("autonomie", "cycle"):
        if (project / ".claude" / "skills" / name / "SKILL.md").is_file() or (project / ".claude" / "commands" / f"{name}.md").is_file():
            return f"/{name} 1"
    return None


SKILL_TEXT = SKILLS["token-saver-status"]
SETTINGS_KEYS_DOC = "https://code.claude.com/docs/en/settings-reference"

# Leçons mesurées (expériences de l'auteur, EXP-01 à EXP-09) :
# - EXP-02 : forcer la compaction à 150K sur une nuit autonome = 34 compactions, 88 min de résumés, étapes perdues.
#   Cause de fond : le contexte juste après compaction (résumé + system prompt + CLAUDE.md + skills) vaut déjà
#   ~110-120K sur un gros projet ; toute fenêtre < ~3× ce plancher laisse trop peu de place de travail.
# - EXP-04 : `autoCompactWindow` n'est lu qu'au démarrage ou à la reprise d'une session, jamais à chaud ; un hook
#   gardé par une règle `if` s'exécute aussi sur les commandes que Claude Code ne sait pas analyser.
# Les profils ne fixent donc jamais une fenêtre < MIN_COMPACT_WINDOW, ne plafonnent plus la fenêtre à 200K, et aucun
# hook ne modifie un réglage de contexte pendant une session.
MIN_COMPACT_WINDOW = 400000
PROFILES: dict[str, dict] = {
    "observe": {"settings": {}, "env": {}, "features": {"statusline": True}},
    "light": {"settings": {}, "env": {},
              "features": {"statusline": True, "H1_read_ledger": True, "H2_compact_reset": True, "H2_brief": True, "H3_review_guard": True,
                           "H6_autonomy_redirect": True, "H7_context_meter": True, "find": True}},
    # `autonome` : fenêtre 1M conservée, compaction naturelle vers 600K au lieu de ~970K (résumés en français avec la
    # checklist du cycle, rappel factuel + fichier d'état). Lu à la prochaine (re)prise de session.
    "autonome": {"settings": {"autoCompactWindow": 600000}, "env": {},
                 "features": {"statusline": True, "H1_read_ledger": True, "H2_compact_reset": True, "H2_brief": True, "H3_review_guard": True,
                              "H6_autonomy_redirect": True, "H7_context_meter": True, "find": True}},
}
DEFAULT_PROFILE = "light"

# Hooks par feature : (événement, matcher, sous-commande). Enregistrés seulement si la sous-commande
# `hook` existe dans le CLI installé (sinon un hook manquant renverrait exit 2 = blocage).
READ_RULES = ["Bash(cat *)", "Bash(sed *)", "Bash(head *)", "Bash(rtk read *)", "PowerShell(Get-Content *)", "PowerShell(type *)"]
HOOK_SPECS: dict[str, list[tuple]] = {
    # (événement, matcher, sous-commande, règles `if` optionnelles : une entrée par règle => le hook ne se lance
    #  que pour ces commandes, zéro overhead sur les autres)
    "H1_read_ledger": [("PreToolUse", "Read", "h1-pre"), ("PostToolUse", "Read", "h1-post"),
                       ("PreToolUse", "Bash|PowerShell", "h1-pre", READ_RULES), ("PostToolUse", "Bash|PowerShell", "h1-post", READ_RULES)],
    "H2_compact_reset": [("SessionStart", "startup|compact|clear|resume", "h2-start"), ("PreCompact", "", "h2-precompact")],
    # H3 : tout agent lancé pour relire reçoit la procédure bornée (diff en fichier, tour) ; son rapport est enregistré au retour.
    # Depuis le 18/09/2026 la procédure n'est plus écrite dans la commande de revue du projet : elle est soufflée quand la revue
    # démarre — commande tapée par l'utilisateur (UserPromptSubmit) ou skill lancé par Claude (PostToolUse Skill). Vérifié réel.
    "H3_review_guard": [("PreToolUse", "Agent|Task", "h3-agent-pre"), ("PostToolUse", "Agent|Task", "h3-agent-post"),
                        ("UserPromptSubmit", "", "h3-prompt"), ("PostToolUse", "Skill", "h3-skill")],
    "H4_loop_guard": [("PostToolUse", "Bash|PowerShell", "h4-post")],
    # H6 : `/autonomie-totale` (ou toute commande d'autonomie du projet) tapée dans l'app lance le mode nuit à la place ;
    # `/stop-autonomie` l'arrête ; pendant la nuit, les autres messages de l'app sur ce projet sont refusés.
    "H6_autonomy_redirect": [("UserPromptSubmit", "", "h6-prompt")],
    # H7 : la conversation devient lourde → une ligne à l'utilisateur et une consigne à Claude, une fois par palier.
    "H7_context_meter": [("UserPromptSubmit", "", "h7-prompt")],
}
# Les règles `if` ne sont qu'un filtre d'économie : Claude Code exécute quand même le hook sur une commande qu'il
# ne sait pas analyser (boucle `for`, sous-shell…). Chaque hook doit donc revérifier lui-même la commande (H1 le fait).


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%S")


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _hooks_available() -> bool:
    try:
        from . import hooks  # noqa: F401
        return True
    except ImportError:
        return False


def _fwd(p: Path) -> str:
    return str(p).replace("\\", "/")


# ---------------------------------------------------------------- plan

def plan(project: Path, profile: str, shared: bool = False, force: bool = False) -> dict:
    """Calcule le plan d'installation sans rien écrire."""
    if profile not in PROFILES:
        raise ValueError(f"profil inconnu : {profile} (choix : {', '.join(PROFILES)})")
    prof = PROFILES[profile]
    win = prof["settings"].get("autoCompactWindow")
    if win is not None and win < MIN_COMPACT_WINDOW:      # garde-fou EXP-02/04 : jamais une fenêtre proche du plancher
        raise ValueError(f"autoCompactWindow {win} < {MIN_COMPACT_WINDOW} : refusé (boucle de compactions possible)")
    _cleanup_stale_h5(project)
    tsd = ts_dir(project)
    settings_file = project / ".claude" / ("settings.json" if shared else "settings.local.json")
    current = _load_json(settings_file)
    old_manifest = _load_json(tsd / "manifest.json")
    owned = set(_owned_keys(old_manifest))
    bin_dir = tsd / "bin"
    launcher = _fwd(bin_dir / "token-saver.pyz")
    hooks_ok = _hooks_available()
    features = {k: bool(v) for k, v in prof["features"].items()}
    if not hooks_ok:
        for k in HOOK_SPECS:
            features[k] = False
    wanted: dict[str, object] = {f"env.{k}": v for k, v in prof["env"].items()}
    wanted.update(prof["settings"])
    if features.get("statusline"):
        wanted["statusLine"] = {"type": "command", "command": f"{py_cmd()} {_fwd(bin_dir / 'statusline.py')}"}
    owned_prev = _owned_previous(old_manifest)
    keys = []
    for path, value in wanted.items():
        prev = _get(current, path)
        if prev is None:
            keys.append({"path": path, "action": "add", "previous": None, "value": value})
        elif path in owned:                      # posé par une installation précédente : on met à jour
            keys.append({"path": path, "action": "keep_ours", "previous": owned_prev.get(path), "value": value})
        elif prev == value:                      # déjà là avant nous, même valeur : pas à nous, jamais retiré
            keys.append({"path": path, "action": "preexisting", "previous": prev, "value": prev})
        elif force:
            keys.append({"path": path, "action": "override", "previous": prev, "value": value})
        else:
            keys.append({"path": path, "action": "keep_user", "previous": prev, "value": prev})
    for path in sorted(owned):                      # posé par nous avant, plus voulu par ce profil : on retire
        if path not in wanted and _get(current, path) is not None:
            keys.append({"path": path, "action": "remove_ours", "previous": owned_prev.get(path), "value": None})
    hooks = []
    for feat, specs in HOOK_SPECS.items():
        if features.get(feat):
            for spec in specs:
                event, matcher, sub = spec[:3]
                rules = spec[3] if len(spec) > 3 else [None]
                for rule in rules:
                    hooks.append({"event": event, "matcher": matcher, "marker": f"{MARK}{feat}", "if": rule,
                                  "command": f"{py_cmd()} {launcher} hook {sub}",
                                  "timeout": 180 if sub.startswith("h6") else 60 if sub.startswith("h3") else 5})   # h6 : contrôle + appel de test ; h3 : git fetch
    created = [f".claude/skills/{{{', '.join(SKILLS)}}}/SKILL.md  (raccourcis /token-saver… dans Claude Code)",
               f"{TS_DIRNAME}/.gitignore", f"{TS_DIRNAME}/config.json", f"{TS_DIRNAME}/manifest.json",
               f"{TS_DIRNAME}/bin/token-saver.pyz", f"{TS_DIRNAME}/bin/token-saver.cmd", f"{TS_DIRNAME}/bin/token-saver.sh",
               f"{TS_DIRNAME}/bin/statusline.py", f"{TS_DIRNAME}/state/", f"{TS_DIRNAME}/metrics/", f"{TS_DIRNAME}/reports/",
               f"{TS_DIRNAME}/backups/"]
    review_cmds = [_entry_path(project, f) for f in detect_review_commands(project)]
    if not review_cmds:
        created.append(f".claude/skills/{REVIEW_SKILL_NAME}/SKILL.md  (revue bornée générique : le projet n'a pas de commande de revue)")
    if features.get("find"):
        created.append(f"{RULES_FILE}  (règles lues par Claude Code : find, résumés en français, revue bornée — aucun bloc dans CLAUDE.md)")
    return {"project": _fwd(project), "profile": profile, "settings_file": _fwd(settings_file), "settings_exists": settings_file.is_file(),
            "keys": keys, "hooks": hooks, "features": features, "created": created, "hooks_available": hooks_ok,
            "already_installed": bool(old_manifest), "old_manifest": old_manifest, "review_commands": review_cmds}


def _context_settings(settings: dict) -> dict:
    """Les clés lues seulement au démarrage d'une session (fenêtre, plafond 1M, modèle des sous-agents)."""
    env = settings.get("env") or {}
    return {"autoCompactWindow": settings.get("autoCompactWindow"),
            "cap": env.get("CLAUDE_CODE_DISABLE_1M_CONTEXT"), "sub": env.get("CLAUDE_CODE_SUBAGENT_MODEL")}


def last_session_start(project: Path) -> str | None:
    """Horodatage (local) de la dernière (re)prise de session journalisée par H2 (startup ou resume)."""
    ev = ts_dir(project) / "metrics" / "hook-events.jsonl"
    if not ev.is_file():
        return None
    last = None
    try:
        with open(ev, "rb") as f:
            f.seek(max(0, ev.stat().st_size - 2_000_000))
            for line in f.read().decode("utf-8", "replace").splitlines():
                if '"H2"' not in line or '"reset"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("hook") == "H2" and e.get("action") == "reset" and str(e.get("note")) in ("startup", "resume"):
                    last = e.get("ts")
    except OSError:
        return None
    return last


def last_autocompact_command(project: Path) -> tuple[str, str] | None:
    """Dernière commande `/autocompact` tapée dans une session du projet : (horodatage local, argument).
    Elle s'applique à chaud à la session ouverte, mais aucun hook ne la voit : on la lit dans le transcript."""
    import calendar
    from .paths import transcript_dirs
    best = None
    for d in transcript_dirs(project):
        for f in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime)[-3:]:
            try:
                with open(f, "rb") as fh:
                    fh.seek(max(0, f.stat().st_size - 4_000_000))
                    lines = fh.read().decode("utf-8", "replace").splitlines()
            except OSError:
                continue
            for line in lines:
                if "<command-name>/autocompact</command-name>" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                c = (rec.get("message") or {}).get("content")
                txt = c if isinstance(c, str) else json.dumps(c)
                i = txt.find("<command-args>")
                arg = txt[i + 14: txt.find("</command-args>", i)].strip() if i >= 0 else ""
                try:
                    epoch = calendar.timegm(time.strptime((rec.get("timestamp") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
                except ValueError:
                    continue
                local = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))
                if best is None or local > best[0]:
                    best = (local, arg)
    return best


def _cleanup_stale_h5(project: Path) -> None:
    """Si l'ancien hook H5 (retiré le 14/09/2026) a laissé une fenêtre abaissée, la remettre. Idempotent."""
    st = ts_dir(project) / "state" / "h5-pending.json"
    if not st.is_file():
        return
    try:
        pending = json.loads(st.read_text(encoding="utf-8"))
        sf = project / ".claude" / "settings.local.json"
        settings = _load_json(sf)
        if settings.get("autoCompactWindow") == pending.get("low"):
            if pending.get("previous") is None:
                settings.pop("autoCompactWindow", None)
            else:
                settings["autoCompactWindow"] = pending["previous"]
            _dump_json(sf, settings)
    except (OSError, ValueError):
        pass
    st.unlink(missing_ok=True)


def _owned_keys(manifest: dict) -> list[str]:
    out = []
    for m in manifest.get("modified", []):
        out.extend(k["path"] for k in m.get("keys", []) if k.get("action") in ("add", "override", "keep_ours"))
        out.extend(m.get("keys_added", []))          # format EXP-01
    return out


def _owned_previous(manifest: dict) -> dict:
    out = {}
    for m in manifest.get("modified", []):
        for k in m.get("keys", []):
            if k.get("action") in ("add", "override", "keep_ours"):
                out[k["path"]] = k.get("previous")
    return out


def _created_entries(manifest: dict) -> list[dict]:
    return [c if isinstance(c, dict) else {"path": c, "sha256": None} for c in manifest.get("created", [])]


def _get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(d: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def _delete(d: dict, path: str) -> None:
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)
    # nettoie les conteneurs vides que nous avons créés (env vide, hooks vides)
    if len(parts) > 1:
        parent = _get(d, ".".join(parts[:-1]))
        if parent == {}:
            _delete(d, ".".join(parts[:-1]))


def _strip_marked_hooks(settings: dict) -> None:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not any(str(h.get("statusMessage", "")).startswith(MARK) for h in (e.get("hooks") or []))]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)


def render_plan(p: dict, dry_run: bool) -> str:
    L = [f"TOKEN SAVER INSTALLATION PLAN — profil : {p['profile']}{'  (dry-run)' if dry_run else ''}", f"Project: {p['project']}",
         f"{'Déjà installé : mise à jour idempotente' if p['already_installed'] else 'Nouvelle installation'}", "",
         "Will create:", *[f"  {c}" for c in p["created"]], "",
         f"Will modify: {p['settings_file']}{'' if p['settings_exists'] else ' (nouveau fichier)'}"]
    sym = {"add": "+", "override": "!", "keep_user": "=", "keep_ours": "=", "preexisting": "=", "remove_ours": "-"}
    for k in p["keys"]:
        note = {"add": "absent", "override": f"remplace {json.dumps(k['previous'])} (--force)",
                "keep_user": f"conservé (valeur utilisateur {json.dumps(k['previous'])})", "keep_ours": "déjà en place (à nous)",
                "preexisting": "déjà en place avant TOKEN SAVER (jamais retiré)",
                "remove_ours": "retiré (posé par un profil précédent)" + (f", valeur antérieure restaurée : {json.dumps(k['previous'])}" if k.get("previous") is not None else "")}[k["action"]]
        L.append(f"  {sym[k['action']]} {k['path']} = {json.dumps(k['value'], ensure_ascii=False)[:80]}   ({note})")
    if p["hooks"]:
        seen = set()
        for h in p["hooks"]:
            key = (h["event"], h["matcher"], h["marker"])
            if key in seen:
                continue
            seen.add(key)
            n = sum(1 for x in p["hooks"] if (x["event"], x["matcher"], x["marker"]) == key)
            L.append(f"  + hooks.{h['event']}[{h['matcher'] or '*'}] {h['marker']}" + (f"  ({n} règles `if` : lectures seulement)" if n > 1 else ""))
    elif not p["hooks_available"]:
        L.append("  (hooks non enregistrés : le module de hooks n'est pas présent dans cette version)")
    if p.get("review_commands"):
        L += ["", "Revue bornée soufflée par hook au démarrage de (fichiers non modifiés) :", *[f"  {c}" for c in p["review_commands"]]]
    L += ["", "Will NOT modify: CLAUDE.md, .claude/agents, .claude/skills et .claude/commands du projet, ~/.claude/*, PATH, services, autres projets",
          f"Overhead attendu : statusline ~150 ms (débounce 300 ms){', hooks Python ~60-150 ms par Read' if p['hooks'] else ''}, 0 processus résident"]
    return "\n".join(L)


# ---------------------------------------------------------------- install

def install(project: Path, profile: str = DEFAULT_PROFILE, shared: bool = False, force: bool = False, dry_run: bool = False,
            fail_after_backup: bool = False) -> dict:
    p = plan(project, profile, shared, force)
    if dry_run:
        return p
    tsd = ts_dir(project)
    settings_file = Path(p["settings_file"])
    pending = tsd / "manifest.pending.json"
    manifest = {"token_saver_version": __version__, "profile": profile, "installed_at": p["old_manifest"].get("installed_at") or _now(),
                "updated_at": _now(), "project": p["project"], "created": [], "modified": [], "features": p["features"],
                "untouched": ["CLAUDE.md", ".claude/agents", ".claude/skills et .claude/commands du projet", "~/.claude/settings.json", "autres projets"],
                "blocks": p["old_manifest"].get("blocks", [])}         # blocs posés par une installation précédente : toujours suivis
    if p["old_manifest"].get("prune"):
        manifest["prune"] = p["old_manifest"]["prune"]              # idem pour l'élagage des outils
    # 1. dossiers + fichiers de TS
    for sub in ("bin", "state", "metrics", "reports", "backups"):
        (tsd / sub).mkdir(parents=True, exist_ok=True)
    _write_created(manifest, project, tsd / ".gitignore", "*\n")
    cfg = _load_json(tsd / "config.json")
    cfg["profile"] = profile
    from .config import DEFAULT_CONFIG
    known = set(DEFAULT_CONFIG["features"])
    cfg["features"] = {k: v for k, v in cfg.get("features", {}).items() if k in known}   # purge des features retirées
    cfg["features"].update({k: v for k, v in p["features"].items() if k in known})
    for stale in [k for k in cfg if k not in DEFAULT_CONFIG and k != "profile"]:          # sections de config disparues
        cfg.pop(stale, None)
    nuit_cfg = cfg.setdefault("nuit", {})
    if nuit_cfg.get("prompt_auto", True) or nuit_cfg.get("prompt") == "/token-saver-cycle":   # commande de cycle détectée, sauf si fixée à la main
        nuit_cfg["prompt"] = default_cycle_prompt(project)                                    # (l'ancien cycle générique /token-saver-cycle → recette)
        nuit_cfg["prompt_auto"] = True
    if nuit_cfg.get("commands_auto", True):                                               # commandes d'autonomie du projet (H6)
        nuit_cfg["autonomy_commands"], nuit_cfg["stop_commands"] = detect_autonomy_commands(project)
        nuit_cfg["commands_auto"] = True
    if nuit_cfg.get("env_checks_auto", True):                                             # vérifications d'environnement proposées d'après le projet
        nuit_cfg["env_checks"] = propose_env_checks(project)
        nuit_cfg["env_checks_auto"] = True
    _bundle_self(tsd / "bin" / "token-saver.pyz")
    manifest["created"].append(_entry(project, tsd / "bin" / "token-saver.pyz"))
    _write_created(manifest, project, tsd / "bin" / "token-saver.cmd", "@echo off\r\npython \"%~dp0token-saver.pyz\" %*\r\n")
    _write_created(manifest, project, tsd / "bin" / "token-saver.sh", "#!/bin/sh\npython \"$(dirname \"$0\")/token-saver.pyz\" \"$@\"\n")
    from importlib import resources                       # lisible aussi depuis l'archive .pyz
    _write_created(manifest, project, tsd / "bin" / "statusline.py",
                   resources.files(__package__).joinpath("statusline.py").read_text(encoding="utf-8"))
    ours = {c.get("path") for c in _created_entries(p["old_manifest"])}
    wanted_skills = dict(SKILLS)
    for name, text in wanted_skills.items():                       # raccourcis /token-saver… dans Claude Code
        skill = project / ".claude" / "skills" / name / "SKILL.md"
        rel = f".claude/skills/{name}/SKILL.md"
        if not skill.is_file() or rel in ours or skill.read_text(encoding="utf-8") == text:
            _write_created(manifest, project, skill, text)
    skills_dir = project / ".claude" / "skills"
    for d in (sorted(skills_dir.glob("token-saver*")) if skills_dir.is_dir() else []):   # raccourcis d'une version précédente : retirés
        if d.is_dir() and d.name not in wanted_skills and d.name != REVIEW_SKILL_NAME:
            rel = f".claude/skills/{d.name}/SKILL.md"
            f = d / "SKILL.md"
            if f.is_file() and (rel in ours or "token-saver.pyz" in f.read_text(encoding="utf-8", errors="replace")):
                shutil.rmtree(d, ignore_errors=True)
    remove_review_blocks(project, manifest)                         # blocs d'une version antérieure : retirés (la procédure passe par hook)
    review_files = [_entry_path(project, f) for f in detect_review_commands(project)]
    cfg["review"] = {**DEFAULT_CONFIG["review"], **(cfg.get("review") or {})}
    if cfg["review"].get("commands_auto", True):
        cfg["review"]["commands"] = review_files
    cm = project / "CLAUDE.md"
    if any(b.get("file") == "CLAUDE.md" for b in manifest.get("blocks", [])) or (cm.is_file() and CLAUDE_MD_BEGIN in cm.read_text(encoding="utf-8", errors="replace")):
        claude_md_block(project, add=False)                          # bloc CLAUDE.md d'une version antérieure : retiré (remplacé par le fichier de règles)
        manifest["blocks"] = [b for b in manifest["blocks"] if b.get("file") != "CLAUDE.md"]
    rules_file = project / RULES_FILE
    rules_prev = next((c for c in _created_entries(p["old_manifest"]) if c.get("path") == RULES_FILE), None)
    if p["features"].get("find"):
        text = rules_text(project)
        untouched = not rules_file.is_file() or (rules_prev and rules_prev.get("sha256") == sha256(rules_file)) \
            or rules_file.read_text(encoding="utf-8", errors="replace") == text
        if untouched:
            _write_created(manifest, project, rules_file, text)
        else:                                                       # modifié à la main : conservé tel quel, signalé par `check`
            manifest["created"].append({"path": RULES_FILE, "sha256": rules_prev.get("sha256") if rules_prev else None, "user_modified": True})
    elif rules_file.is_file() and rules_prev and rules_prev.get("sha256") == sha256(rules_file):
        rules_file.unlink()                                          # profil sans `find` : notre fichier de règles s'efface
    rskill = project / ".claude" / "skills" / REVIEW_SKILL_NAME / "SKILL.md"
    r_rel = f".claude/skills/{REVIEW_SKILL_NAME}/SKILL.md"
    r_ours = rskill.is_file() and (r_rel in ours or rskill.read_text(encoding="utf-8") == REVIEW_SKILL)
    if not review_files:                                            # aucune commande de revue : la générique de TOKEN SAVER
        if not rskill.is_file() or r_ours:
            _write_created(manifest, project, rskill, REVIEW_SKILL)
    elif r_ours:                                                    # le projet a maintenant sa propre commande : la générique s'efface
        rskill.unlink()
        if not any(rskill.parent.iterdir()):
            rskill.parent.rmdir()
    _write_created(manifest, project, tsd / "config.json", json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    if p["hooks"]:
        t0 = time.perf_counter()
        _smoke_test_hooks(tsd / "bin" / "token-saver.pyz", project)
        manifest["hook_startup_ms"] = round((time.perf_counter() - t0) * 1000)   # coût réel d'un hook (démarrage Python inclus)
    # 2. settings : sauvegarde, journal d'intention, fusion, écriture, vérification
    backup = None
    if settings_file.is_file():
        backup = tsd / "backups" / f"{settings_file.name}.{_stamp()}.bak"
        shutil.copy2(settings_file, backup)
    _dump_json(pending, {"step": "settings", "file": p["settings_file"], "backup": _entry_path(project, backup) if backup else None,
                         "started": _now()})
    if fail_after_backup:  # injection de panne pour les tests (T4)
        raise RuntimeError("interrupted after backup")
    settings = _load_json(settings_file)
    before_ctx = _context_settings(settings)
    for k in p["keys"]:
        if k["action"] in ("add", "override", "keep_ours"):
            _set(settings, k["path"], k["value"])
        elif k["action"] == "remove_ours":
            if k.get("previous") is not None:
                _set(settings, k["path"], k["previous"])
            else:
                _delete(settings, k["path"])
    _strip_marked_hooks(settings)
    for h in p["hooks"]:
        settings.setdefault("hooks", {}).setdefault(h["event"], []).append(
            {**({"matcher": h["matcher"]} if h["matcher"] else {}),
             "hooks": [{"type": "command", **({"if": h["if"]} if h.get("if") else {}), "command": h["command"],
                        "timeout": h["timeout"], "statusMessage": h["marker"]}]})
    _dump_json(settings_file, settings)
    # Les réglages de contexte ne sont lus qu'à la (re)prise d'une session : on date leur dernier changement
    # pour que `check` puisse dire s'ils sont déjà appliqués à la session en cours.
    manifest["context_settings_changed_at"] = (_now() if _context_settings(settings) != before_ctx
                                               else p["old_manifest"].get("context_settings_changed_at"))
    manifest["modified"].append({"file": _entry_path(project, settings_file), "backup": _entry_path(project, backup) if backup else None,
                                 "existed_before": backup is not None, "content_sha256_after": sha256(settings_file),
                                 "keys": [k for k in p["keys"] if k["action"] in ("add", "override", "keep_ours")],
                                 "hooks": [h["marker"] for h in p["hooks"]]})
    manifest["created"].append({"path": _entry_path(project, tsd / "manifest.json"), "sha256": None})
    _dump_json(tsd / "manifest.json", manifest)
    pending.unlink(missing_ok=True)
    return {**p, "manifest": manifest}


def _entry_path(project: Path, path: Path | None) -> str | None:
    return _fwd(path.relative_to(project)) if path else None


def _entry(project: Path, path: Path) -> dict:
    return {"path": _entry_path(project, path), "sha256": sha256(path)}


def _write_created(manifest: dict, project: Path, path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(localize(content) if path.suffix == ".md" else content, encoding="utf-8", newline="")
    manifest["created"].append(_entry(project, path))


def _smoke_test_hooks(pyz: Path, project: Path) -> None:
    """Avant d'enregistrer un hook, vérifie que `python <pyz> hook h1-pre` répond `{}` en < 10 s
    (un hook défaillant renverrait un code ≠ 0, interprété comme un blocage par Claude Code)."""
    import subprocess
    py = py_cmd()
    if shutil.which(py) is None:
        raise RuntimeError(f"`{py}` introuvable dans le PATH : Claude Code ne pourrait pas lancer les hooks")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    r = subprocess.run([py, str(pyz), "hook", "h1-pre"], input="{}", capture_output=True, text=True, timeout=10, env=env)
    if r.returncode != 0 or r.stdout.strip() != "{}":
        raise RuntimeError(f"smoke test du hook échoué (code {r.returncode}) : {r.stderr.strip()[:200] or r.stdout.strip()[:200]}")


def _bundle_self(dest: Path) -> None:
    """Copie locale du CLI : le .pyz courant, ou un zipapp construit depuis les sources."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.suffix == ".pyz" and parent.is_file():
            shutil.copy2(parent, dest)
            return
    src = here.parents[1]            # .../src
    tmp = dest.with_suffix(".tmp")
    zipapp.create_archive(str(src), str(tmp), main="token_saver.cli:entry", compressed=True,
                          filter=lambda p: "__pycache__" not in p.parts and not str(p).endswith(".pyc"))
    tmp.replace(dest)


# ---------------------------------------------------------------- uninstall

def uninstall(project: Path, purge: bool = False, dry_run: bool = False) -> dict:
    tsd = ts_dir(project)
    manifest = _load_json(tsd / "manifest.json")
    report = {"settings": [], "hooks": 0, "files_removed": [], "files_kept": [], "purged": False, "not_installed": not manifest,
              "night_stopped": False}
    if not dry_run:
        from . import nuit                                # une nuit en cours est arrêtée avant de retirer ses fichiers
        st = nuit.read_state(project)
        if st.get("status") == "running" and nuit._alive(st.get("pid")):
            nuit.stop(project, now=True)
            report["night_stopped"] = True
    if not manifest:
        if purge and tsd.is_dir() and not dry_run:
            shutil.rmtree(tsd)
            report["purged"] = True
        return report
    # 1. settings : retirer nos clés / restaurer les valeurs antérieures / retirer les hooks marqués
    for m in manifest.get("modified", []):
        f = project / m["file"]
        if not f.is_file():
            continue
        settings = _load_json(f)
        for k in m.get("keys", []) + [{"path": p, "value": None, "previous": None} for p in m.get("keys_added", [])]:
            cur = _get(settings, k["path"])
            if k.get("value") is not None and cur != k["value"]:
                report["settings"].append((k["path"], "kept: modifié par l'utilisateur"))
                continue
            if k.get("previous") is not None:
                _set(settings, k["path"], k["previous"])
                report["settings"].append((k["path"], "restored"))
            else:
                _delete(settings, k["path"])
                report["settings"].append((k["path"], "removed"))
        _strip_marked_hooks(settings)
        report["hooks"] += len(m.get("hooks", []))
        if not dry_run:
            backup = project / m["backup"] if m.get("backup") else None
            if not settings and not m.get("existed_before", True):
                f.unlink(missing_ok=True)
            elif backup and backup.is_file() and _load_json(backup) == settings:
                shutil.copy2(backup, f)          # restauration octet pour octet
            else:
                _dump_json(f, settings)
    # 1a. règles d'élagage des outils (prune) posées par nous
    if manifest.get("prune"):
        from . import prune as _prune
        n = _prune.remove(project, manifest) if not dry_run else len(manifest["prune"].get("deny", [])) + len(manifest["prune"].get("plugins", []))
        report["settings"].append((f"prune ({n} règles)", "removed"))
    # 1b. blocs marqués dans CLAUDE.md
    for b in manifest.get("blocks", []):
        f = project / b["file"]
        if b.get("block") == "review":                    # procédure de revue dans une commande du projet : retrait à l'identique
            if f.is_file() and not dry_run:
                _remove_review_block(project, f, b)
            report["settings"].append((f"{b['file']} (procédure de revue)", "removed"))
            continue
        if f.is_file() and not dry_run:
            f.write_text(_strip_block(f.read_text(encoding="utf-8")), encoding="utf-8")
        report["settings"].append((f"{b['file']} (bloc)", "removed"))
    # 2. fichiers créés : suppression si hash inchangé
    for c in _created_entries(manifest):
        path = project / c["path"]
        if not path.is_file():
            continue
        if (c.get("user_modified") or (c.get("sha256") and c["sha256"] != sha256(path))) and not purge:
            report["files_kept"].append(c["path"])      # modifié par l'utilisateur depuis : conservé sauf --purge
            continue
        if not dry_run:
            path.unlink()
        report["files_removed"].append(c["path"])
    if not dry_run:
        if purge:
            shutil.rmtree(tsd, ignore_errors=True)
            for name in (*SKILLS, REVIEW_SKILL_NAME):
                sk = project / ".claude" / "skills" / name
                if sk.is_dir():
                    shutil.rmtree(sk, ignore_errors=True)
            report["purged"] = True
        else:
            for sub in ("bin", "state", "reports", "backups"):
                d = tsd / sub
                if d.is_dir() and not any(d.iterdir()):
                    d.rmdir()
            for name in (*SKILLS, REVIEW_SKILL_NAME):
                sk = project / ".claude" / "skills" / name
                if sk.is_dir() and not any(sk.iterdir()):
                    sk.rmdir()
            rules_dir = project / ".claude" / "rules"
            if rules_dir.is_dir() and not any(rules_dir.iterdir()):
                rules_dir.rmdir()
            (tsd / "manifest.json").unlink(missing_ok=True)
            if tsd.is_dir() and not any(x for x in tsd.iterdir() if x.name not in (".gitignore", "metrics")):
                pass  # metrics/ conservé volontairement (sans --purge)
    return report


def render_uninstall(r: dict, dry_run: bool) -> str:
    if r["not_installed"]:
        return "TOKEN SAVER n'est pas installé dans ce projet" + (" (dossier .token-saver purgé)" if r["purged"] else "")
    removed = sum(1 for _, a in r["settings"] if a == "removed")
    restored = sum(1 for _, a in r["settings"] if a == "restored")
    kept = [k for k, a in r["settings"] if a.startswith("kept")]
    L = [f"TOKEN SAVER REMOVAL{'  (dry-run)' if dry_run else ''}"] + (["Night runner: ✓ stopped"] if r.get("night_stopped") else []) + [
         f"Settings keys: ✓ {removed} removed, {restored} restored" + (f", {len(kept)} kept (user-modified: {', '.join(kept)})" if kept else ""),
         f"Hooks: ✓ {r['hooks']} removed", f"Files: ✓ {len(r['files_removed'])} removed" + (f", kept: {', '.join(r['files_kept'])}" if r["files_kept"] else ""),
         f"Metrics: {'✓ purged' if r['purged'] else '✓ kept (.token-saver/metrics — use --purge)'}",
         "Global config: ✓ unchanged", "Other projects: ✓ untouched"]
    return "\n".join(L)


# ---------------------------------------------------------------- contrôle pré-vol (lecture seule)

KNOWN_HOOKS = ("h1-pre", "h1-post", "h2-start", "h2-precompact", "h3-agent-pre", "h3-agent-post", "h3-prompt", "h3-skill", "h6-prompt", "h7-prompt")


def check(project: Path) -> list[tuple[str, bool, str]]:
    """Vérifie l'installation sans rien modifier : réglages, hooks, intégrité du CLI, config, résidus, CLAUDE.md, raccourcis."""
    import subprocess
    from .config import DEFAULT_CONFIG
    tsd = ts_dir(project)
    R: list[tuple[str, bool, str]] = []
    manifest = _load_json(tsd / "manifest.json")
    if not manifest:
        return [("TOKEN SAVER installé dans ce projet", False, "manifest.json absent")]
    sf = project / ".claude" / ("settings.json" if manifest.get("shared") else "settings.local.json")
    try:
        s = _load_json(sf)
        R.append(("fichier de réglages lisible", True, sf.name))
    except ValueError as exc:
        return R + [("fichier de réglages lisible", False, str(exc)[:80])]
    win = s.get("autoCompactWindow")
    R.append(("fenêtre de compaction absente ou ≥ 400 000", win is None or win >= MIN_COMPACT_WINDOW, str(win)))
    R.append(("aucun plafond de fenêtre ni modèle forcé par TOKEN SAVER",
              not (s.get("env") or {}).get("CLAUDE_CODE_DISABLE_1M_CONTEXT") and "model" not in _owned_keys(manifest), ""))
    from .prune import CORE
    denied = set((s.get("permissions") or {}).get("deny") or [])
    R.append(("aucun outil de base retiré du contexte", not (denied & CORE), ", ".join(sorted(denied & CORE)) or
              (f"{len(manifest.get('prune', {}).get('deny', []))} outils inutilisés retirés" if manifest.get("prune") else "aucun élagage")))
    ts_hooks = [h for lst in (s.get("hooks") or {}).values() for e in lst for h in e.get("hooks", [])
                if str(h.get("statusMessage", "")).startswith(MARK)]
    pyz = tsd / "bin" / "token-saver.pyz"
    R.append(("hooks TOKEN SAVER : lecture (H1), compaction (H2), relecture bornée (H3), autonomie (H6), contexte lourd (H7) uniquement",
              all(any(f"hook {k}" in h.get("command", "") for k in KNOWN_HOOKS) for h in ts_hooks),
              f"{len(ts_hooks)} entrées"))
    R.append(("tous les hooks pointent vers la copie locale du CLI, présente",
              pyz.is_file() and all(_fwd(pyz) in h.get("command", "") for h in ts_hooks), ""))
    entry = next((c for c in _created_entries(manifest) if c.get("path", "").endswith("token-saver.pyz")), None)
    R.append(("copie locale du CLI intègre (hash du manifest)", bool(entry) and pyz.is_file() and sha256(pyz) == entry.get("sha256"), ""))
    if pyz.is_file():
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
        for name in ("h1-pre", "h1-post", "h3-agent-pre"):
            try:
                t0 = time.perf_counter()
                r = subprocess.run([py_cmd(), str(pyz), "hook", name], input="{}", capture_output=True, text=True, timeout=10, env=env)
                R.append((f"hook {name} répond {{}} sans erreur", r.returncode == 0 and r.stdout.strip() == "{}", f"{(time.perf_counter() - t0) * 1000:.0f} ms"))
            except (OSError, subprocess.TimeoutExpired) as exc:
                R.append((f"hook {name} répond {{}} sans erreur", False, str(exc)[:60]))
    cfg = _load_json(tsd / "config.json")
    feats = {k for k, v in cfg.get("features", {}).items() if v}
    R.append(("features connues uniquement (aucun résidu)", feats <= set(DEFAULT_CONFIG["features"]) and not (set(cfg) - set(DEFAULT_CONFIG) - {"profile"}),
              ", ".join(sorted(feats))))
    R.append(("aucun fichier d'état résiduel", not (tsd / "state" / "h5-pending.json").exists() and not (tsd / "manifest.pending.json").exists(), ""))
    changed_at = manifest.get("context_settings_changed_at")
    if changed_at:                                   # un réglage de contexte a été posé OU retiré : la session ouverte garde l'ancien
        started = last_session_start(project)
        remedy = f"`/autocompact {win // 1000}k`" if win else "`/autocompact 1m` (retour au comportement par défaut)"
        if started is None:
            R.append(("réglage de contexte appliqué à la session en cours", True, "non vérifiable : aucune (re)prise de session journalisée"))
        else:
            applied = started >= changed_at
            ac = None if applied else last_autocompact_command(project)
            if ac and ac[0] >= changed_at:                   # appliqué à chaud par l'utilisateur
                expected = f"{win // 1000}k" if win else None
                ok_val = expected is None or ac[1].lower().replace(" ", "") in (expected, str(win))
                R.append(("réglage de contexte appliqué à la session en cours", ok_val,
                          f"via `/autocompact {ac[1]}` le {ac[0][:16].replace('T', ' à ')}" + ("" if ok_val else f" — valeur attendue : {expected}")))
            else:
                R.append(("réglage de contexte appliqué à la session en cours", applied,
                          f"réglage modifié le {changed_at[:16].replace('T', ' à ')}, dernière (re)prise le {started[:16].replace('T', ' à ')}"
                          + ("" if applied else f" → dans la session : {remedy}, immédiat ; ou fermer complètement l'app "
                                                  "(vérifier qu'aucun processus Claude ne reste) puis rouvrir la session")))
    cm = project / "CLAUDE.md"
    text = cm.read_text(encoding="utf-8", errors="replace") if cm.is_file() else ""
    if feats & {"find"}:
        ok, detail = rules_file_state(project, manifest)
        R.append((f"règles TOKEN SAVER lues par Claude Code ({RULES_FILE} : find, résumés en français, revue de code)", ok, detail))
    rv_files = [project / c for c in (cfg.get("review") or {}).get("commands") or []]
    stray = ([f"CLAUDE.md"] if CLAUDE_MD_BEGIN in text else []) + [c.name for c in rv_files if c.is_file() and REVIEW_BLOCK_BEGIN in c.read_text(encoding="utf-8", errors="replace")]
    R.append(("aucun fichier du projet modifié (CLAUDE.md, commandes de revue : aucun bloc TOKEN SAVER)", not stray,
              ("bloc d'une version antérieure dans " + ", ".join(stray) + " : réinstaller") if stray else ""))
    skills = [d for d in (project / ".claude" / "skills").glob("token-saver*") if d.is_dir() and d.name != REVIEW_SKILL_NAME]
    R.append(("raccourcis /token-saver… invisibles pour le modèle (0 token)",      # sauf la revue générique, que le modèle doit pouvoir invoquer
              bool(skills) and all("disable-model-invocation: true" in (d / "SKILL.md").read_text(encoding="utf-8", errors="replace") for d in skills),
              f"{len(skills)} raccourcis"))
    rv_cmds = list((cfg.get("review") or {}).get("commands") or [])
    if rv_cmds:
        hook_cmds = {h.get("command", "").rsplit("hook ", 1)[-1] for h in ts_hooks}
        R.append(("revue bornée : procédure soufflée par hook au démarrage des commandes de revue du projet",
                  all((project / c).is_file() for c in rv_cmds) and {"h3-prompt", "h3-skill", "h3-agent-pre"} <= hook_cmds,
                  ", ".join("/" + (Path(c).parent.name if Path(c).name == "SKILL.md" else Path(c).stem) for c in rv_cmds)
                  + ("" if {"h3-prompt", "h3-skill"} <= hook_cmds else " — hooks h3-prompt/h3-skill absents : réinstaller")))
    else:
        R.append(("revue bornée : commande générique /token-saver-review fournie (aucune commande de revue dans le projet)",
                  (project / ".claude" / "skills" / REVIEW_SKILL_NAME / "SKILL.md").is_file(), ""))
    ncfg = cfg.get("nuit") or {}
    auto_cmds = list(ncfg.get("autonomy_commands") or [])
    if feats & {"H6_autonomy_redirect"}:
        R.append(("autonomie en sessions neuves (H6)", True,
                  (", ".join("/" + c for c in auto_cmds) + " → session neuve par cycle ; arrêt : " + ", ".join("/" + c for c in ncfg.get("stop_commands") or ["token-saver-autonomie-stop"]))
                  if auto_cmds else "aucune commande d'autonomie dans ce projet : rien à détourner"))
    from .recette import recipe_path
    if ncfg.get("prompt"):
        name = (str(ncfg["prompt"]).split() or ["/"])[0].lstrip("/")
        has_cmd = (project / ".claude" / "skills" / name / "SKILL.md").is_file() or (project / ".claude" / "commands" / f"{name}.md").is_file()
        R.append(("recette de cycle", has_cmd, f"commande du projet `{ncfg['prompt']}`" + ("" if has_cmd else " : absente du projet")))
    else:
        R.append(("recette de cycle", True, "recette TOKEN SAVER validée (.token-saver/cycle.md)" if recipe_path(project).is_file()
                  else "aucune encore : /token-saver-autonomie la propose d'après le projet, tu valides"))
    R.append(("manifest complet pour un retrait exact", bool(manifest.get("created")) and bool(manifest.get("modified")) and manifest.get("profile") in PROFILES,
              f"profil {manifest.get('profile')}"))
    return R


def render_check(results: list[tuple[str, bool, str]]) -> str:
    lines = [("  ✓ " if ok else "  ✗ ") + label + (f" — {detail}" if detail else "") for label, ok, detail in results]
    n_ok = sum(1 for _, ok, _ in results if ok)
    verdict = "installation saine" if n_ok == len(results) else "ANOMALIE : corriger avant de lancer un long processus (ou `token-saver off`)"
    return "TOKEN SAVER — contrôle pré-vol (lecture seule)\n" + "\n".join(lines) + f"\n{n_ok}/{len(results)} contrôles validés — {verdict}"


# ---------------------------------------------------------------- amorçage global (un seul fichier : ~/.claude/skills/token-saver-install)

def bootstrap(remove: bool = False) -> str:
    """Crée (ou retire) le raccourci global /token-saver-install qui pointe vers ce .pyz. Unique modification globale, opt-in."""
    from .paths import claude_config_dir
    skill_dir = claude_config_dir() / "skills" / "token-saver-install"
    if remove:
        if skill_dir.is_dir():
            shutil.rmtree(skill_dir, ignore_errors=True)
            return f"retiré : {skill_dir}"
        return "aucun raccourci global à retirer"
    here = Path(__file__).resolve()
    pyz = next((p for p in here.parents if p.suffix == ".pyz"), None)
    launcher = _fwd(pyz) if pyz else f"-m token_saver"  # depuis les sources : nécessite PYTHONPATH=src
    text = (f"---\nname: token-saver-install\ndescription: TOKEN SAVER — installe l'outil dans le projet courant (argument : profil autonome ou light)\n"
            f"disable-model-invocation: true\nallowed-tools: Bash(python {launcher} *)\n---\n"
            f"Profil demandé : `$ARGUMENTS` (si vide, utilise `light`). Exécute via Bash, dans le dossier du projet courant :\n"
            f"1. `python {launcher} install --profile <profil> -y`\n"
            f"2. `python {launcher} doctor --fix find`\n"
            f"Puis restitue la dernière ligne de chaque commande telle quelle, et indique que les raccourcis `/token-saver…` "
            f"apparaissent après `/clear` ou une nouvelle session. Ne relance pas les commandes.\n")
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    return f"créé : {skill_dir / 'SKILL.md'} (pointe vers {launcher}) — dans tout projet : /token-saver-install autonome|light"


# ---------------------------------------------------------------- bloc CLAUDE.md (marqué, 0 token pour les marqueurs)

CLAUDE_MD_BEGIN, CLAUDE_MD_END = "<!-- token-saver:begin -->", "<!-- token-saver:end -->"
CLAUDE_MD_FIND = (
    "## TOKEN SAVER\n"
    "- Pour chercher une information dans les docs (`docs/`, gros fichiers Markdown), utilise d'abord "
    "`python .token-saver/bin/token-saver.pyz find \"<sujet>\"` (Bash) : il renvoie les sections utiles avec `chemin:lignes`. "
    "Cela ne remplace jamais la lecture complète d'un document qu'un skill, une règle ou une procédure demande de lire en entier.\n"
    "\n"
    "## Compact instructions\n"
    "Rédige le résumé en français. Conserve impérativement : la procédure ou le cycle en cours avec la liste de ses étapes et "
    "l'étape atteinte, les vérifications restant à faire (tests, builds, campagnes, relectures), les fichiers modifiés, les décisions "
    "prises et leurs raisons, les règles du projet en vigueur. Ne transforme jamais une ancienne consigne en consigne active.\n")


def claude_md_text(project: Path) -> str:
    """Texte des règles TOKEN SAVER : find + consignes de résumé + revue de code (commande du projet, sinon la générique)."""
    cmds = detect_review_commands(project)
    names = ", ".join(dict.fromkeys("`/" + (f.parent.name if f.name == "SKILL.md" else f.stem) + "`" for f in cmds)) or f"`/{REVIEW_SKILL_NAME}`"
    return CLAUDE_MD_FIND + (
        "\n## Revue de code\n"
        f"Toute relecture d'une PR ou d'une branche passe par la commande de revue du projet ({names}) : TOKEN SAVER lui souffle sa procédure "
        "au démarrage (diff écrit dans un fichier, tours suivants limités aux lignes changées, 3 tours maximum, seuls les 🔴 empêchent "
        "la fusion ; les 🟠 se corrigent dans la même PR ou sont écartés par écrit, jamais de carte de suivi). Ne lance pas de relecture "
        "par un agent en dehors de cette commande. Pour attendre la CI : `python .token-saver/bin/token-saver.pyz review wait-ci` "
        "(un seul appel), jamais `gh pr checks` répété.\n")


# Depuis le 18/09/2026 les consignes ne sont plus un bloc dans CLAUDE.md mais un fichier de règles à nous, lu par Claude Code
# comme CLAUDE.md (.claude/rules/*.md sans `paths` : chargé à chaque session, vérifié réel sur le projet d'essai). Aucun fichier
# du projet n'est modifié ; le commentaire HTML est retiré avant injection (0 token).
RULES_FILE = ".claude/rules/token-saver.md"
RULES_NOTE = ("<!-- Fichier géré par TOKEN SAVER : régénéré à la mise à jour tant qu'il n'est pas modifié à la main, retiré par /token-saver-off. "
              "Si tu le modifies, ta version est conservée (signalé par /token-saver-status). -->\n")


def rules_text(project: Path) -> str:
    return RULES_NOTE + claude_md_text(project)


def rules_block(project: Path, add: bool) -> str:
    """`doctor --fix find` / `--unfix find` : écrit (ou retire) le fichier de règles TOKEN SAVER, manifest tenu à jour."""
    tsd = ts_dir(project)
    manifest = _load_json(tsd / "manifest.json")
    f = project / RULES_FILE
    manifest["created"] = [c for c in manifest.get("created", []) if c.get("path") != RULES_FILE]
    if add:
        _write_created(manifest, project, f, rules_text(project))
        msg = f"règles TOKEN SAVER écrites : {RULES_FILE} (lues par Claude Code à chaque session, comme CLAUDE.md ; CLAUDE.md n'est pas modifié)"
    else:
        f.unlink(missing_ok=True)
        if f.parent.is_dir() and not any(f.parent.iterdir()):
            f.parent.rmdir()
        msg = f"règles TOKEN SAVER retirées : {RULES_FILE}"
    if manifest:
        _dump_json(tsd / "manifest.json", manifest)
    return msg


def rules_file_state(project: Path, manifest: dict) -> tuple[bool, str]:
    """(présent et à jour, détail) pour `check` : à nous, modifié à la main, ou absent."""
    f = project / RULES_FILE
    if not f.is_file():
        return False, "absent (réinstaller : `install`)"
    entry = next((c for c in _created_entries(manifest) if c.get("path") == RULES_FILE), None)
    if entry and entry.get("user_modified"):
        return True, "modifié à la main : ta version est conservée"
    text = f.read_text(encoding="utf-8", errors="replace")
    return ("Compact instructions" in text and "Revue de code" in text), ""


def claude_md_block(project: Path, add: bool, text: str | None = None) -> str:
    """Ajoute (ou retire) le bloc marqué dans CLAUDE.md ; sauvegarde et manifest tenus à jour."""
    text = text if text is not None else claude_md_text(project)
    cm = project / "CLAUDE.md"
    tsd = ts_dir(project)
    manifest = _load_json(tsd / "manifest.json")
    content = cm.read_text(encoding="utf-8") if cm.is_file() else ""
    stripped = _strip_block(content)
    if add:
        if cm.is_file():
            bak = tsd / "backups" / f"CLAUDE.md.{_stamp()}.bak"
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cm, bak)
        new = stripped.rstrip("\n") + ("\n\n" if stripped.strip() else "") + f"{CLAUDE_MD_BEGIN}\n{text}{CLAUDE_MD_END}\n"
        cm.write_text(new, encoding="utf-8")
        entry = {"file": "CLAUDE.md", "block": "token-saver", "existed_before": bool(content)}
        manifest.setdefault("blocks", [])
        manifest["blocks"] = [b for b in manifest["blocks"] if b.get("file") != "CLAUDE.md"] + [entry]
        msg = "bloc TOKEN SAVER ajouté à CLAUDE.md"
    else:
        if stripped != content:
            raw_text, crlf = _read_nl(cm)
            restored = False
            for bak in sorted((tsd / "backups").glob("CLAUDE.md.*.bak")):     # la plus ancienne sauvegarde sans bloc : octet pour octet
                try:
                    bt = _read_nl(bak)[0]
                except (OSError, UnicodeDecodeError):
                    continue
                if CLAUDE_MD_BEGIN not in bt and bt.rstrip("\n") == _strip_block(raw_text).rstrip("\n"):
                    shutil.copy2(bak, cm)
                    restored = True
                    break
            if not restored:
                _write_nl(cm, _strip_block(raw_text), crlf)                    # fins de ligne d'origine conservées
        manifest["blocks"] = [b for b in manifest.get("blocks", []) if b.get("file") != "CLAUDE.md"]
        msg = "bloc TOKEN SAVER retiré de CLAUDE.md" if stripped != content else "aucun bloc TOKEN SAVER dans CLAUDE.md"
    if manifest:
        _dump_json(tsd / "manifest.json", manifest)
    return msg


def _strip_block(content: str) -> str:
    i = content.find(CLAUDE_MD_BEGIN)
    j = content.find(CLAUDE_MD_END)
    if i < 0 or j < 0:
        return content
    return (content[:i].rstrip("\n") + "\n" + content[j + len(CLAUDE_MD_END):].lstrip("\n")).strip("\n") + "\n" if content.strip() else content


# ---------------------------------------------------------------- backup / restore

def backups(project: Path) -> list[Path]:
    d = ts_dir(project) / "backups"
    return sorted(d.glob("*.bak")) if d.is_dir() else []


def restore(project: Path, stamp: str | None = None) -> Path | None:
    cands = backups(project)
    if stamp:
        cands = [c for c in cands if stamp in c.name]
    if not cands:
        return None
    b = cands[-1]
    target = project / ".claude" / b.name.rsplit(".", 2)[0]   # settings.local.json.<stamp>.bak -> settings.local.json
    shutil.copy2(b, target)
    return target


def recover_pending(project: Path) -> str | None:
    """Installation interrompue : restaure la sauvegarde notée dans manifest.pending.json."""
    tsd = ts_dir(project)
    pending = tsd / "manifest.pending.json"
    if not pending.is_file():
        return None
    info = _load_json(pending)
    msg = "installation interrompue détectée"
    if info.get("backup"):
        shutil.copy2(project / info["backup"], Path(info["file"]))
        msg += f" : {info['file']} restauré depuis {info['backup']}"
    pending.unlink()
    return msg
