"""Point d'entrée : python -m token_saver <commande>."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import load_config
from .db import connect
from .paths import default_db_path, find_project_root, localize, py_cmd, transcript_dirs, ts_dir


def _project(args) -> Path:
    if args.project:
        p = Path(args.project).resolve()
        if not p.is_dir():
            sys.exit(f"projet introuvable : {p}")
        return p
    root = find_project_root()
    if root is None:
        sys.exit("aucun projet détecté (.claude/, CLAUDE.md ou .git) : utilise --project")
    return root


def _open_db(project: Path, args):
    """Base du projet si .token-saver existe (ou --db), sinon base en mémoire."""
    if getattr(args, "db", None):
        return connect(Path(args.db)), True
    if ts_dir(project).is_dir():
        return connect(default_db_path(project)), True
    return connect(None), False


def cmd_collect(args) -> int:
    from .collect import collect
    project = _project(args)
    con, persistent = _open_db(project, args)
    dirs = transcript_dirs(project)
    if not dirs:
        print(f"aucun transcript Claude Code pour {project} (dossier ~/.claude/projects/{project.name} absent)")
        return 3
    stats = collect(con, project, progress=(print if args.verbose else None))
    print(f"collecte : {stats['files_updated']}/{stats['files']} fichiers lus, {stats['lines']} lignes, "
          f"{stats['calls']} nouveaux appels, {stats['reset']} fichiers ré-analysés, {stats['seconds']} s"
          f"{'' if persistent else ' (base en mémoire : projet non instrumenté)'}")
    return 0


def cmd_status(args) -> int:
    from .collect import collect
    from .metrics import snapshot
    from .status import render
    project = _project(args)
    cfg = load_config(project)
    con, persistent = _open_db(project, args)
    if not transcript_dirs(project):
        print(f"aucun transcript Claude Code pour {project}")
        return 3
    if not args.no_collect:
        collect(con, project)
    snap = snapshot(con, args.since, cfg)
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2, default=str))
        return 0
    label = {"today": "aujourd'hui", "7d": "7 derniers jours", "30d": "30 derniers jours", "all": "tout l'historique"}[args.since]
    print(render(snap, str(project), persistent, cfg["features"], label))
    if not persistent:
        print("\n(projet non instrumenté : base en mémoire recalculée à chaque appel — `token-saver install` pour la conserver)")
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows : console cp1252 -> UTF-8 tolérant
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if py_cmd() != "python":                  # machine sans `python` (macOS/Linux) : les commandes citées disent `python3`
        class _Localized:
            def __init__(self, s):
                self._s = s

            def write(self, t):
                return self._s.write(localize(t))

            def __getattr__(self, n):
                return getattr(self._s, n)
        sys.stdout = _Localized(sys.stdout)
    ap = argparse.ArgumentParser(prog="token-saver", description="TOKEN SAVER — mesure, réglage et garde-fous pour Claude Code")
    ap.add_argument("--version", action="version", version=f"token-saver {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("collect", cmd_collect), ("status", cmd_status)):
        p = sub.add_parser(name)
        p.add_argument("--project", help="racine du projet (défaut : détection depuis le dossier courant)")
        p.add_argument("--db", help="chemin de la base SQLite (défaut : <projet>/.token-saver/metrics/usage.sqlite)")
        p.add_argument("-v", "--verbose", action="store_true")
        p.set_defaults(fn=fn)
    sp = sub.choices["status"]
    sp.add_argument("--since", choices=["today", "7d", "30d", "all"], default="7d")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--no-collect", action="store_true", help="ne pas relire les transcripts avant l'affichage")
    dp = sub.add_parser("dashboard", help="génère un dashboard HTML statique (aucun serveur)")
    dp.add_argument("--project")
    dp.add_argument("--all", action="store_true", help="vue multi-projets depuis ~/.claude/projects")
    dp.add_argument("--out", help="fichier HTML de sortie")
    dp.add_argument("--open", action="store_true", help="ouvre le fichier dans le navigateur")
    dp.set_defaults(fn=cmd_dashboard)
    ip = sub.add_parser("install", help="installe TOKEN SAVER dans le projet (project-local, réversible)")
    ip.add_argument("--project")
    ip.add_argument("--profile", choices=["observe", "light", "autonome"], default="light",
                    help="observe: statusline · light (défaut): + find, anti-relecture, rappel post-compaction, sans toucher au contexte · "
                         "autonome: light + compaction naturelle vers 600K (nuits d'autonomie)")
    ip.add_argument("--shared", action="store_true", help="écrire dans .claude/settings.json (partagé) au lieu de settings.local.json")
    ip.add_argument("--force", action="store_true", help="remplacer les valeurs utilisateur différentes (sauvegardées et restaurables)")
    ip.add_argument("--dry-run", action="store_true")
    ip.add_argument("-y", "--yes", action="store_true", help="ne pas demander de confirmation")
    ip.set_defaults(fn=cmd_install)
    up = sub.add_parser("uninstall", help="retire TOKEN SAVER du projet en rejouant le manifest")
    up.add_argument("--project")
    up.add_argument("--purge", action="store_true", help="supprime aussi métriques, rapports et sauvegardes")
    up.add_argument("--dry-run", action="store_true")
    up.set_defaults(fn=cmd_uninstall)
    for name, fn in (("backup", cmd_backup), ("restore", cmd_restore)):
        bp = sub.add_parser(name)
        bp.add_argument("--project")
        bp.add_argument("--stamp", help="restore : horodatage de la sauvegarde à utiliser (défaut : la plus récente)")
        bp.set_defaults(fn=fn)
    pp = sub.add_parser("profile", help="interrupteur : applique un profil sans question (ex. `profile autonome`, `profile light`)")
    pp.add_argument("name", choices=["observe", "light", "autonome"])
    pp.add_argument("--project")
    pp.set_defaults(fn=cmd_profile)
    op = sub.add_parser("off", help="désactive TOKEN SAVER dans le projet (retire réglages, hooks, bloc CLAUDE.md ; garde les métriques)")
    op.add_argument("--project")
    op.set_defaults(fn=lambda a: cmd_uninstall(argparse.Namespace(project=a.project, purge=False, dry_run=False)))
    onp = sub.add_parser("on", help="réactive TOKEN SAVER avec le dernier profil utilisé (défaut : light)")
    onp.add_argument("--project")
    onp.set_defaults(fn=cmd_on)
    np_ = sub.add_parser("nuit", help="une session neuve par cycle, pilotée en headless : start | stop | status | run | recette")
    np_.add_argument("action", choices=["check", "start", "stop", "status", "run", "bilan", "recette"])
    np_.add_argument("sub", nargs="?", choices=["propose", "save", "show"], help="recette : propose (inventaire + proposition) | save | show")
    np_.add_argument("--project")
    np_.add_argument("--cycles", type=int, default=0, help="nombre de cycles (0 = jusqu'à `nuit stop`, max_hours ou l'heure de fin)")
    np_.add_argument("--prompt", help="commande du projet qui fait UN cycle (défaut : config nuit.prompt, ex. /autonomie 1 ; vide = recette .token-saver/cycle.md)")
    np_.add_argument("--tasks", help="recette save : source des tâches (todo | issues | description libre)")
    np_.add_argument("--tests", help="recette save : commande de test (ou « aucun »)")
    np_.add_argument("--replace", action="store_true", help="recette save : utiliser la recette à la place de la commande de cycle du projet")
    np_.add_argument("--until", help="heure de fin locale HH:MM (défaut : config nuit.end_at) : la nuit finit son cycle en cours puis s'arrête")
    np_.add_argument("--now", action="store_true", help="stop : couper immédiatement le cycle en cours")
    np_.set_defaults(fn=cmd_nuit)
    rv = sub.add_parser("review", help="revue de code bornée : begin (diff en fichier, numéro de tour) | end (verdict) | wait-ci | status | reset")
    rv.add_argument("action", choices=["begin", "end", "wait-ci", "status", "reset"])
    rv.add_argument("--project")
    rv.add_argument("--pr", type=int, help="numéro de PR GitHub (sinon : la branche courante)")
    rv.add_argument("--base", help="branche de base (défaut : origin/HEAD, main ou master)")
    rv.add_argument("--fresh", action="store_true", help="begin : repartir du tour 1 (sur décision humaine)")
    rv.add_argument("--verdict", choices=["FUSIONNABLE", "BLOQUE"], help="end : verdict, si aucun rapport n'est donné")
    rv.add_argument("--report", help="end : fichier du rapport (les 🔴 qu'il contient déterminent le verdict)")
    rv.add_argument("--branch", help="end : branche du tour à clore (défaut : la branche courante, ou celle de --pr)")
    rv.add_argument("--timeout-min", type=int, default=None, help="wait-ci : délai maximal (défaut : config review.wait_ci_timeout_min)")
    rv.set_defaults(fn=cmd_review)
    pr = sub.add_parser("prune", help="retire du contexte les outils/plugins jamais utilisés par ce projet (aperçu ; --apply pour écrire ; --undo pour les remettre)")
    pr.add_argument("--project")
    pr.add_argument("--apply", action="store_true")
    pr.add_argument("--undo", action="store_true", help="remet les outils et plugins retirés par --apply (le reste de l'installation ne bouge pas)")
    pr.set_defaults(fn=cmd_prune)
    ov = sub.add_parser("overview", help="tout l'état sur un écran : installation, ce qui tourne, bilan de la dernière autonomie, dépense (/token-saver-status)")
    ov.add_argument("--project")
    ov.set_defaults(fn=cmd_overview)
    ck = sub.add_parser("check", help="contrôle pré-vol de l'installation (lecture seule) ; code de sortie 1 si une anomalie est trouvée")
    ck.add_argument("--project")
    ck.set_defaults(fn=cmd_check)
    bp2 = sub.add_parser("bootstrap", help="crée le raccourci global /token-saver-install (un seul fichier dans ~/.claude/skills) ; --remove pour le retirer")
    bp2.add_argument("--remove", action="store_true")
    bp2.set_defaults(fn=lambda a: print(__import__("token_saver.install", fromlist=["bootstrap"]).bootstrap(a.remove)) or 0)
    hp = sub.add_parser("hook", help="point d'entrée des hooks Claude Code (stdin JSON -> stdout JSON)")
    hp.add_argument("name")
    hp.set_defaults(fn=lambda a: __import__("token_saver.hooks", fromlist=["run"]).run(a.name))
    fp = sub.add_parser("find", help="recherche par sections dans les docs du projet (index local FTS5)")
    fp.add_argument("query", nargs="+")
    fp.add_argument("--project")
    fp.add_argument("--top", type=int, default=3)
    fp.add_argument("--max-tokens", type=int, default=2000)
    fp.add_argument("--in", dest="within", help="restreindre à un chemin (sous-chaîne)")
    fp.set_defaults(fn=cmd_find)
    xp = sub.add_parser("index", help="(ré)indexe les docs du projet pour `find`")
    xp.add_argument("--project")
    xp.add_argument("--rebuild", action="store_true")
    xp.set_defaults(fn=cmd_index)
    dr = sub.add_parser("doctor", help="audit du projet : opportunités classées par impact")
    dr.add_argument("--project")
    dr.add_argument("--since", choices=["today", "7d", "30d", "all"], default="30d")
    dr.add_argument("--json", action="store_true")
    dr.add_argument("--fix", choices=["find"], help="applique un correctif réversible : 'find' écrit le fichier de règles .claude/rules/token-saver.md")
    dr.add_argument("--unfix", choices=["find"], help="retire le correctif")
    dr.set_defaults(fn=cmd_doctor)
    ag = sub.add_parser("agents", help="conseiller d'agents : une ligne par agent (lancements, tokens relus par lancement, contexte moyen, éditions, modèle, dernier usage)")
    ag.add_argument("--project")
    ag.add_argument("--db")
    ag.add_argument("--since", choices=["today", "7d", "30d", "all"], default="30d")
    ag.set_defaults(fn=cmd_agents)
    args = ap.parse_args(argv)
    return args.fn(args)


def cmd_doctor(args) -> int:
    from .collect import collect
    from .doctor import render, run
    from .install import rules_block
    project = _project(args)
    if args.fix == "find":
        print(rules_block(project, add=True))
        return 0
    if args.unfix == "find":
        print(rules_block(project, add=False))
        return 0
    cfg = load_config(project)
    con, persistent = _open_db(project, args)
    collect(con, project)
    rep = run(con, project, cfg, args.since)
    if persistent:
        out = ts_dir(project) / "reports" / f"doctor-{rep['generated'][:10]}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str) if args.json else render(rep))
    return 0


def cmd_agents(args) -> int:
    from .agents import render_table
    from .collect import collect
    from .metrics import since_ts
    project = _project(args)
    con, _ = _open_db(project, args)
    collect(con, project)
    print(f"TOKEN SAVER — agents — {project.name} — période : {args.since}\n" + render_table(con, project, since_ts(args.since)))
    return 0


def cmd_find(args) -> int:
    from .find import search
    project = _project(args)
    print(search(project, " ".join(args.query), top=args.top, max_tokens=args.max_tokens, within=args.within))
    return 0


def cmd_index(args) -> int:
    from .find import refresh
    project = _project(args)
    st = refresh(project, rebuild=args.rebuild)
    print(f"index : {st['files']} fichiers, {st['indexed']} (ré)indexés, {st['removed']} retirés, {st['total_sections']} sections, {st['seconds']} s")
    return 0


def cmd_install(args) -> int:
    from .install import install, recover_pending, render_plan
    project = _project(args)
    msg = recover_pending(project)
    if msg:
        print(msg)
    p = install(project, args.profile, args.shared, args.force, dry_run=True)
    print(render_plan(p, args.dry_run))
    if args.dry_run:
        return 0
    if not args.yes:
        try:
            ans = input("\nContinue? [y/N] ").strip().lower()
        except EOFError:
            ans = ""
        if ans not in ("y", "yes", "o", "oui"):
            print("annulé")
            return 1
    r = install(project, args.profile, args.shared, args.force)
    print(f"\ninstallé : {r['project']}/.token-saver (profil {r['profile']}) — prend effet aux nouvelles sessions Claude Code")
    return 0


def cmd_profile(args) -> int:
    from .install import install, recover_pending
    project = _project(args)
    recover_pending(project)
    r = install(project, args.name)                     # écrit aussi le fichier de règles TOKEN SAVER si le profil a `find`
    changed = [k for k in r["keys"] if k["action"] in ("add", "override", "remove_ours")]
    print(f"profil {args.name} appliqué à {project.name} : " + (", ".join(f"{k['path']} {'retiré' if k['action'] == 'remove_ours' else '= ' + str(k['value'])}" for k in changed) or "aucun réglage de contexte modifié"))
    win = next((k["value"] for k in r["keys"] if k["path"] == "autoCompactWindow" and k["action"] in ("add", "override", "keep_ours")), None)
    print("hooks et raccourcis : effet immédiat · réglage de compaction : lu à la prochaine (re)prise de session — pour l'appliquer tout de suite à la "
          f"session ouverte, tape `/autocompact {win // 1000}k` dedans" if win else
          "hooks et raccourcis : effet immédiat · réglage de compaction : lu à la prochaine (re)prise de session")
    print("`token-saver check` pour vérifier l'installation · `token-saver profile light` pour revenir au réglage neutre")
    return 0


def cmd_on(args) -> int:
    import json as _json
    from .paths import ts_dir
    project = _project(args)
    cfg = ts_dir(project) / "config.json"
    last = "light"
    if cfg.is_file():
        try:
            last = _json.loads(cfg.read_text(encoding="utf-8")).get("profile") or "light"
        except ValueError:
            pass
    return cmd_profile(argparse.Namespace(project=str(project), name=last))


def cmd_nuit(args) -> int:
    from . import nuit
    project = _project(args)
    cfg = load_config(project)
    if not ts_dir(project).is_dir():
        print("TOKEN SAVER n'est pas installé dans ce projet (`install` d'abord)")
        return 2
    if args.action == "run":
        return nuit.run(project, cfg, args.cycles, args.prompt, until=args.until)
    if args.action == "check":
        from .install import render_check
        results = nuit.check(project, cfg, do_ping=True)
        print(render_check(results).replace("contrôle pré-vol (lecture seule)", "contrôle avant la nuit"))
        return 0 if all(ok for _, ok, _ in results) else 1
    if args.action == "start":
        pyz = ts_dir(project) / "bin" / "token-saver.pyz"
        launcher = [py_cmd(), str(pyz)] if pyz.is_file() else [sys.executable, "-m", "token_saver"]
        print(nuit.start(project, cfg, args.cycles, args.prompt, launcher, until=args.until))
        return 0
    if args.action == "bilan":
        bm = nuit.bilan_md(project)
        st = nuit.read_state(project)
        if st and st.get("status") != "running" and (not bm.is_file() or st.get("started", "")[:10] not in bm.read_text(encoding="utf-8").splitlines()[0]):
            nuit.bilan(project, cfg, st)                                # bilan manquant pour la dernière nuit : calculé maintenant
        print(bm.read_text(encoding="utf-8").strip() if bm.is_file() else "aucune nuit terminée dans ce projet : pas de bilan")
        return 0
    if args.action == "stop":
        print(nuit.stop(project, now=args.now))
        return 0
    if args.action == "recette":
        from . import recette
        if args.sub == "save":
            print(recette.save(project, cfg, tasks=args.tasks, tests=args.tests, replace=args.replace))
        elif args.sub == "show":
            print(recette.show(project, cfg))
        else:
            print(recette.propose(project, cfg))
        return 0
    print(nuit.status(project))
    return 0


def cmd_overview(args) -> int:
    """`/token-saver-status` : tout l'état sur un écran — installation, ce qui tourne, bilan de la dernière autonomie, dépense."""
    from . import nuit
    from .collect import collect
    from .install import check
    from .metrics import snapshot
    from .status import render
    project = _project(args)
    cfg = load_config(project)
    L = [f"TOKEN SAVER — {project.name}"]
    if ts_dir(project).is_dir():
        res = check(project)
        bad = [label for label, ok, _ in res if not ok]
        L.append("Installation : " + (f"en ordre ({len(res)} vérifications)" if not bad else "PROBLÈME — " + " ; ".join(bad)))
    else:
        L.append("Installation : TOKEN SAVER n'est pas installé dans ce projet")
    L += ["", nuit.status(project)]
    con, persistent = _open_db(project, args)
    if transcript_dirs(project):
        collect(con, project)
        for period, label in (("today", "aujourd'hui"), ("7d", "7 derniers jours")):
            lines = render(snapshot(con, period, cfg), str(project), persistent, cfg["features"], label).splitlines()
            try:
                i = lines.index("── EN CLAIR ──")
                j = next(k for k in range(i + 1, len(lines)) if lines[k].startswith("── "))
            except (ValueError, StopIteration):
                i, j = 0, len(lines)
            L += ["", f"── Dépense {label} ──"] + lines[i + 1:j]
    print("\n".join(L))
    return 0


def cmd_review(args) -> int:
    from . import review
    project = _project(args)
    cfg = load_config(project)
    if args.action == "begin":
        rc, text = review.begin(project, cfg, pr=args.pr, base=args.base, fresh=args.fresh)
    elif args.action == "end":
        rc, text = review.end(project, cfg, verdict=args.verdict, report=args.report, branch=args.branch, pr=args.pr)
    elif args.action == "wait-ci":
        rc, text = review.wait_ci(project, pr=args.pr, timeout_min=args.timeout_min or int(cfg["review"].get("wait_ci_timeout_min", 45)))
    elif args.action == "reset":
        rc, text = 0, review.reset(project)
    else:
        rc, text = 0, review.status(project)
    print(text)
    return rc


def cmd_prune(args) -> int:
    from . import prune
    from .collect import collect
    project = _project(args)
    if getattr(args, "undo", False):
        n = prune.undo(project)
        print(f"{n} outil(s)/plugin(s) remis dans le contexte de ce projet — effet à la prochaine ouverture de session" if n
              else "rien à remettre : aucun élagage appliqué dans ce projet")
        return 0
    if args.apply and not ts_dir(project).is_dir():
        print("TOKEN SAVER n'est pas installé dans ce projet (`install` d'abord) — l'aperçu sans --apply reste possible")
        return 2
    con, _ = _open_db(project, args)                      # aperçu possible sans installation (base en mémoire, lecture seule)
    if transcript_dirs(project):
        collect(con, project)
    p = prune.plan(con)
    if args.apply and p["enough_history"] and (p["deny"] or p["plugins"]):
        r = prune.apply(project, p)
        print(prune.render_plan(p, applied=True))
        print(f"écrit dans .claude/settings.local.json ({len(r['rules'])} règles, {len(r['plugins'])} plugins) · pour revenir : `prune --undo` (ou `uninstall`)")
    else:
        print(prune.render_plan(p))
        if p["enough_history"] and (p["deny"] or p["plugins"]) and not args.apply:
            print("(aperçu — `prune --apply` pour l'appliquer)")
    return 0


def cmd_check(args) -> int:
    from .install import check, render_check
    project = _project(args)
    results = check(project)
    print(render_check(results))
    return 0 if all(ok for _, ok, _ in results) else 1


def cmd_uninstall(args) -> int:
    from .install import render_uninstall, uninstall
    project = _project(args)
    r = uninstall(project, purge=args.purge, dry_run=args.dry_run)
    print(render_uninstall(r, args.dry_run))
    return 0


def cmd_backup(args) -> int:
    import shutil
    import time
    project = _project(args)
    dst = ts_dir(project) / "backups"
    dst.mkdir(parents=True, exist_ok=True)
    n = 0
    for name in ("settings.local.json", "settings.json"):
        f = project / ".claude" / name
        if f.is_file():
            shutil.copy2(f, dst / f"{name}.{time.strftime('%Y%m%dT%H%M%S')}.bak")
            n += 1
    print(f"{n} fichier(s) sauvegardé(s) dans {dst}")
    return 0


def cmd_restore(args) -> int:
    from .install import restore
    project = _project(args)
    t = restore(project, args.stamp)
    print(f"restauré : {t}" if t else "aucune sauvegarde trouvée")
    return 0 if t else 3


def cmd_dashboard(args) -> int:
    from .dashboard import generate
    project = None
    if not args.all or args.project:
        project = _project(args)
    cfg = load_config(project)
    target = generate(project, Path(args.out) if args.out else None, cfg, args.all, args.open)
    print(f"dashboard : {target}")
    return 0


def entry() -> None:
    """Point d'entrée de l'archive .pyz : propage le code de sortie (le lanceur généré par zipapp ne le fait pas)."""
    sys.exit(main())


if __name__ == "__main__":
    entry()
