# TOKEN SAVER

[English version](README.md) · Python ≥ 3.10, aucune dépendance · 78 tests automatiques (Windows, macOS, Linux) + validations de bout en bout avec le vrai Claude Code · Licence MIT.

> Les messages de l'outil sont en français pour l'instant ; une version anglaise des messages est prévue.

## En une phrase

Un outil **par projet** pour Claude Code qui **mesure** où partent tes tokens, **évite** les gaspillages sans toucher à la qualité du travail, et fait tourner ton travail autonome **une session neuve par cycle** — installable et désinstallable en une commande, sans rien de global sur la machine.

## Installer (n'importe quel projet)

Une fois par machine : cloner le dépôt (par exemple dans `~/dev/token-saver`, ou `C:\dev\token-saver` sous Windows).

Puis, dans le projet, dans Claude Code :

> installe TOKEN SAVER depuis `~/dev/token-saver`

Claude lit `INSTALL.md`, pose une seule question (mode **Autonome** / **Léger** / **Mesurer seulement** / **Annuler**), installe, et te dit ce qui est actif. Équivalent en terminal, depuis le projet :

```bash
python <dépôt>/dist/token-saver.pyz install --profile light      # --dry-run : voir le plan sans rien écrire
```

Ce que ça crée : un dossier `.token-saver/` (ignoré par git, avec sa propre copie de l'outil), quelques lignes dans `.claude/settings.local.json` (personnel, non versionné, sauvegardé avant), un fichier de règles à lui, `.claude/rules/token-saver.md`, lu par Claude Code comme CLAUDE.md, et les raccourcis `/token-saver…`. **Aucun fichier du projet n'est modifié** : ni CLAUDE.md, ni les commandes, ni les agents (jusqu'au 18/09/2026 un bloc marqué était écrit dans CLAUDE.md et dans la commande de revue ; il a écrasé une correction locale, la mise à jour le retire). Tout est inscrit dans un manifest : `/token-saver-off` remet le projet **exactement** dans son état d'avant.

## Ce que ça fait, automatiquement

| | Ce que ça fait | Effet sur la qualité |
|---|---|---|
| **Compteur** (`/token-saver-status`, `/token-saver-dashboard`) | Lit les journaux que Claude Code écrit déjà ; bilan en clair : dépensé, économisé, continuité du travail ; tableau de bord HTML ; chaque chiffre est étiqueté **mesuré** ou **estimé** | aucun |
| **Conseiller** (`/token-saver-doctor`) | Constats classés par impact, avec le pourquoi et le chiffre ; dont le **conseiller d'agents** : agents du projet qui ne servent plus (leur description est relue à chaque appel), coût mesuré par lancement et agents lourds, candidats à un modèle moins cher (sur Opus sans écrire de code) ; tableau complet avec `token-saver agents` | aucun (il conseille, il ne change rien) |
| **Anti-relecture** | Refuse qu'un gros fichier inchangé soit relu à l'identique ; jamais bloquant (une relance passe toujours) | aucun |
| **`find`** | Donne à Claude les 2-3 sections utiles d'un gros document au lieu du document entier | aucun (lecture complète toujours possible) |
| **Résumés protégés** | Quand Claude Code résume la session, il le fait en français en gardant la procédure en cours et les vérifications restantes ; un rappel factuel suit (jamais une consigne) | protège la continuité |
| **Élagage des outils** (`/token-saver-prune`) | Retire du contexte les outils et plugins que **ce projet n'a jamais utilisés** dans son historique (≈ 10-20K tokens relus en moins à chaque appel dans l'app desktop) ; jamais un outil de base ; aperçu d'abord, `--apply` pour écrire | aucun (rien de ce qui a déjà servi n'est retiré) |
| **Autonomie en sessions neuves** (`/token-saver-autonomie`, ou la commande d'autonomie du projet) | Chaque cycle de travail dans une **session neuve** pilotée de l'extérieur (`claude -p`), sans compaction, avec un socle de démarrage de 22K au lieu de 63K | meilleure : chaque cycle repart des fichiers d'état, aucun résumé ne perd d'étape |
| **Passage de relais** (`/token-saver-next`) | La tâche finie, Claude note l'état en quelques lignes ; la session neuve suivante reçoit la note toute seule au démarrage : une session par tâche, sans rien perdre | meilleure : plus de conversation qui grossit des jours |
| **Revue bornée** (dans la commande de revue du projet, sinon `/token-saver-review`) | Le diff est écrit dans un fichier et relu une fois ; le tour suivant ne relit que les lignes changées, avec les 🔴 à vérifier ; 3 tours maximum puis la PR revient à l'humain ; seuls les 🔴 empêchent la fusion ; la CI est attendue en un seul appel | même profondeur de lecture, sans répétition |
| **Contrôle** (dans `/token-saver-status`) | 19 vérifications en lecture seule : réglages, hooks, intégrité, résidus, réglage appliqué à la session ouverte, aucun outil de base retiré, procédure de revue en place | — |

Ce qu'il ne fait **jamais** : modifier un réglage pendant qu'une session tourne, forcer des compactions fréquentes (mesuré : ça détruit la continuité), toucher à ta configuration globale, lancer un programme en arrière-plan sans ta commande.

## L'autonomie en sessions neuves

Le coût d'un long travail autonome est structurel : une seule session grossit pendant des heures (290-340K tokens relus à **chaque** appel, mesuré) et ses résumés perdent des étapes. L'autonomie en sessions neuves fait tourner chaque cycle dans une conversation neuve, pilotée de l'extérieur (`claude -p`) :

- Recette de cycle : la commande du projet si elle existe (par exemple `/autonomie 1`, détectée à l'installation). Sinon, au premier `/token-saver-autonomie`, l'outil **regarde ce que le projet contient** (commandes, agents, commande de test, `TODO.md` ou issues GitHub, dépôt, CI, commande de revue), propose une recette assemblée avec ça, pose au plus deux questions (où sont les tâches, comment lancer les tests) et l'enregistre après ton accord dans `.token-saver/cycle.md`, lisible et modifiable ; `/token-saver-autonomie config` la revoit. Sans rien dans le projet, la recette de base suffit : une tâche, réalisation avec tests, revue bornée, commit sur une branche `autonomie/<tâche>`, journal et état dans `.token-saver/nuit/`, marqueur `[nuit:fin]` quand il n'y a plus rien à faire. Rien n'est inventé : ni agent, ni commande, ni source de tâches.
- `/token-saver-autonomie 6` (6 cycles ; rien = jusqu'à l'arrêt) · `/token-saver-status` (à l'instant, ou le bilan) · `/token-saver-autonomie-stop` (fin du cycle en cours) ou `--now`.
- Avant de partir, le contrôle vérifie : binaire trouvé, recette de cycle disponible, CLI connecté (appel de test d'un tour), **aucune autre session active sur le projet**. Pendant l'autonomie : attente et reprise automatiques après une limite de quota, arrêt après deux échecs consécutifs, journal `state/nuit.log`, mesures par cycle `metrics/nuit-cycles.jsonl`.
- Après `/token-saver-autonomie`, ne travaille plus dans l'app sur ce projet tant qu'elle tourne (deux équipes sur le même code se gêneraient).
- **Automatique (hook H6)** : la commande d'autonomie du projet, détectée à l'installation (`/autonomie-totale`, `/autonomie N`…), tapée dans une session de l'app, lance les sessions neuves à sa place et le message est refusé avec l'explication ; `/stop-autonomie` (ou `/token-saver-autonomie-stop`) l'arrête ; pendant ce temps, les autres messages de l'app sur ce projet sont refusés. Si les sessions neuves ne peuvent pas démarrer (CLI non connecté, autre session active), l'autonomie démarre dans l'app comme avant et un message le dit.
- **Suivi et fin** : `/token-saver-status` montre ce que fait le cycle **à l'instant** (dernière action, contexte, derniers outils, dernier texte de Claude — lu dans son journal, zéro token). À la fin, l'outil écrit lui-même le **bilan de l'autonomie** : cycles, minutes, tokens relus par les transcripts sous-agents compris, contexte moyen, compactions, limites, tours de revue, PR fusionnées si le projet est sur GitHub sinon commits, comparés à la précédente. La session de l'app apprend à son ouverture qu'une autonomie tourne (ne pas toucher au dépôt) ou vient de finir (rien ne tourne).
- **Heure de fin** : `nuit.end_at` dans la config (ou `--until 08:00` en ligne de commande), en plus de `max_hours` ; l'autonomie finit son cycle en cours puis s'arrête.
- **Cycle inachevé** : si un cycle se termine en attente, en échec, avec des commits non poussés ou des fichiers non commités, le cycle suivant le reçoit noir sur blanc et reprend ce travail avant d'en commencer un autre.
- **Vérifications d'environnement** : déclarées par le projet dans `nuit.env_checks` (`name`, `command`, `expect` = `exit0` | `nonempty` | `regex:…`, `required`) et proposées à l'installation **d'après ce que le projet contient** — un workflow GitHub, un remote GitHub et `gh` → état de la CI ; des scripts qui parlent d'`adb`/Android et `adb` présent → appareil branché. Rien n'est supposé : un projet sans CI ni appareil n'a aucune vérification. Elles avertissent au lancement (`⚠`), et ne bloquent que si `required`.

Prérequis, une fois par machine : le CLI `claude` doit être connecté hors de l'app. Le runner trouve seul le binaire (PATH, ou celui embarqué par l'app desktop). Pour se connecter : un PowerShell **normal** (pas « en administrateur »), `& "$env:APPDATA\Claude\claude-code\<version>\claude.exe"`, `/login`, compte avec abonnement, autoriser dans le navigateur, `/exit`. `/token-saver-status` le dit et explique.

Validation : projet d'essai, 2 cycles réels → 2 sessions neuves, **0 compaction**, contexte maximal 32K, 2 tâches livrées avec tests, branches, commits, journal. Sur un projet réel, deux nuits complètes : 16 puis 27 PR fusionnées, 3 à 4,5 fois moins de tokens que le même travail dans une seule session de l'app qui grossit, qualité égale ou meilleure à la relecture.

## La revue de code

Mesuré sur deux projets réels (16/09/2026) : jusqu'à 8 relectures d'une même PR, 7 à 8 M tokens et 15 à 85 min par relecture, chaque tour relisant toute la PR ; un tour à six spécialistes en parallèle = 116 M tokens ; 710 appels qui ne faisaient qu'interroger l'état de la CI. Les revues pesaient 14 % et 22 % du volume de ces projets.

À l'installation, TOKEN SAVER trouve la commande de revue du projet, quel que soit son nom (`/review-pr`, `/code-review`, `/revue`…), sans la modifier : quand la revue démarre, que tu tapes la commande ou que Claude la lance lui-même dans un cycle, un hook lui souffle la procédure (même texte, ≈ 450 tokens, seulement à ce moment-là ; vérifié avec le vrai Claude Code dans les deux cas). La grille du projet ne change pas ; la procédure fixe ce qui est lu, le nombre de tours et ce qui bloque :

- `review begin` écrit le diff dans un fichier : **tour 1 = diff complet**, **tour 2 = les lignes changées depuis le tour 1** avec les 🔴 à vérifier, **tour 3 = vérification des 🔴 restants**. Jamais de 4ᵉ tour : la PR revient à l'humain.
- Les spécialistes prévus par la commande sont convoqués au tour 1, puis seulement ceux qui ont émis un 🔴 ; chacun reçoit le fichier de diff, jamais la PR entière.
- Rapport de 12 constats au plus, `fichier:ligne`, 🔴 / 🟠 / 🟢 ; `review end` déduit le verdict du rapport : **seuls les 🔴 empêchent la fusion** ; les 🟠 se corrigent dans la même PR avant la fusion ou sont écartés par écrit dans sa description, **jamais de carte ni d'issue de suivi** (mesuré sur un projet réel : 36 cartes « Suites de… » sur 63 ouvertes, nourries par l'ancienne consigne) ; un 🟠 qui décrit un défaut visible par l'utilisateur est un 🔴.
- `review wait-ci` attend la CI en un seul appel, au lieu de sondages répétés à contexte plein.
- Sans commande de revue dans le projet, `/token-saver-review` fournit une grille générique avec la même procédure.
- **Toute relecture confiée à un agent passe aussi par là** (hook H3) : quand un processus lance un agent pour relire du code, hors de la commande de revue, le hook prépare le diff, place le brief en tête de sa consigne (tour, fichier de diff, 🔴 à vérifier, format du rapport) et enregistre son rapport au retour ; le verdict est ajouté au contexte de l'orchestrateur. Validé avec le vrai Claude : relecture par sous-agent en 35 s, verdict enregistré sans intervention.

Le conseiller signale les relectures en chaîne (R17) et le sondage de la CI (R18) avec leur coût mesuré.

## Ce qu'on peut attendre, honnêtement

- Autonomie en sessions neuves : contexte moyen relu divisé par ~2 sur de longs processus (ESTIMÉ ≈ −40 % de tokens sur un long processus autonome, à mesurer par le compteur sur chaque projet), et surtout plus de résumés destructeurs.
- Revue bornée : tokens de revue divisés par 3 à 4 (ESTIMÉ : tours 2 et 3 sur le delta seulement, plus de 4ᵉ tour), même profondeur de lecture ; à vérifier avec `/token-saver-doctor` après quelques PR.
- Élagage des outils : ≈ 10-20K tokens de moins à chaque appel dans l'app desktop (ESTIMÉ à partir de `/context`).
- Anti-relecture et `find` : petits, mesurés.
- Ce que l'outil n'économisera pas : le travail que le processus lui-même demande (relectures en chaîne, tâches en parallèle). Le compteur te montre où ça part ; la décision reste la tienne.

Leçons mesurées : forcer des compactions fréquentes (150K-200K) a multiplié les compactions par 34 et fait perdre des étapes ; un hook qui modifiait un réglage en cours de session a provoqué une boucle. Le contexte juste après un résumé vaut déjà ~110K sur un gros projet ; toute fenêtre trop proche de ce plancher est refusée. Aucun hook ne touche plus à un réglage de contexte.

## Désinstaller

```bash
python .token-saver/bin/token-saver.pyz uninstall            # ou /token-saver-off — retire réglages, hooks, raccourcis, fichier de règles, élagage ; garde les métriques
python .token-saver/bin/token-saver.pyz uninstall --purge    # supprime aussi métriques, rapports, sauvegardes
```

Une autonomie en cours est arrêtée d'abord. Une clé que tu as modifiée toi-même entre-temps est conservée et signalée.

## Les quatre registres

| Registre | Contenu | Source |
|---|---|---|
| **MESURÉ** | tokens rapportés par l'API (input, cache read/write, output, thinking), contexte par appel, appels par tour, reconstructions à froid, relectures, compactions, rejets de quota | transcripts JSONL |
| **ÉVITÉ-MESURÉ** | tokens qu'une action de l'outil a empêché d'entrer dans le contexte, taille connue | journaux des hooks |
| **ESTIMÉ** | contre-factuels et tailles indicatives, hypothèse toujours affichée | calcul |
| **OVERHEAD** | tokens injectés par l'outil, latence des hooks, processus (0) | journaux des hooks |

Gain net = ÉVITÉ-MESURÉ − OVERHEAD. Les estimations ne sont jamais additionnées aux mesures.

## Fichiers

```
.token-saver/
├── config.json            features, seuils, profil, sources de `find`, réglages `nuit`
├── manifest.json          inventaire exact de l'installation (base de l'uninstall)
├── bin/                   token-saver.pyz, lanceurs, statusline.py
├── state/                 ledgers, instantanés pré-compaction, état et journal de l'autonomie, tours de revue (diff + rapport par tour)
├── metrics/               usage.sqlite, docs.sqlite, hook-events.jsonl, savings.jsonl, nuit-cycles.jsonl, review-log.jsonl
├── cycle.md               recette de cycle validée (projet sans commande de cycle) ; recette.json = tes réponses
├── nuit/                  ETAT.md, JOURNAL.md (recette TOKEN SAVER)
├── reports/               dashboard.html
└── backups/               settings.local.json.<horodatage>.bak
```

## Dépannage

- **Un hook refuse une lecture à tort** : relancer exactement la même lecture passe toujours ; ou `features.H1_read_ledger: false` dans `.token-saver/config.json`.
- **`python` introuvable** : les hooks, la statusline et le runner appellent `python` du PATH ; l'installation le vérifie.
- **L'autonomie refuse de démarrer** : `/token-saver-status` dit pourquoi (connexion du CLI, autre session active, recette de cycle absente).
- **Chiffres différents de `/usage`** : `/usage` couvre la conversation principale ; l'outil lit tous les transcripts du projet (sous-agents inclus). La pondération exacte du quota d'abonnement n'est pas publique : les tokens pondérés sont étiquetés ESTIMÉ.

## Développement

```
src/token_saver/   paths, config, db, collect, metrics, status, dashboard(+html), install, hooks, find, doctor, prune, nuit, recette, review, statusline, cli
tests/             unittest, sans dépendance (fake_claude.py simule `claude -p`)
```

Construire l'archive : `python -c "import zipapp; zipapp.create_archive('src','dist/token-saver.pyz',main='token_saver.cli:entry',compressed=True)"`. Licence MIT.
