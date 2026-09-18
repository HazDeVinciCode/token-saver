# INSTALL.md — instructions pour Claude Code / instructions for Claude Code

> **English-speaking user?** Claude: if the user writes in English, ask the question below and answer in English, following exactly the same steps (the tool's own messages are in French for now). The four choices are **Autonomous** (long unattended runs), **Light** (recommended), **Measure only**, **Cancel**, mapped to `autonome`, `light`, `observe`, cancel.

Si un utilisateur te demande d'installer TOKEN SAVER depuis ce dépôt (par exemple « installe TOKEN SAVER depuis ~/dev/token-saver »), suis exactement ces étapes. Ne modifie rien de global : tout se passe dans le projet courant.

## 1. Pose UNE question à choix (outil AskUserQuestion), avec ces mots simples

**« Dans quel mode veux-tu activer TOKEN SAVER pour ce projet ? »**

- **Autonome (recommandé si ce projet tourne des nuits entières en mode automatique)** — Claude cherche dans tes docs au lieu de tout relire, évite les relectures inutiles, et la session est résumée automatiquement vers 600K de contexte au lieu de ~1M, avec un résumé en français qui garde la checklist du cycle. Bénéfice : −20 à −30 % du coût des nuits (estimation). Risque : faible — environ deux résumés de plus par nuit ; retour arrière en une commande si tu vois une baisse de qualité. Le réglage est lu à la prochaine ouverture de session.
- **Léger** — recherche dans les docs, anti-relecture, résumés de compaction en français avec la checklist ; aucun réglage de la fenêtre de contexte. Bénéfice : petites économies mesurées et compactions plus sûres. Risque : quasi nul, rien ne peut bloquer Claude.
- **Mesurer seulement** — rien ne change dans la façon de travailler ; tu obtiens le bilan, le tableau de bord et les conseils. Bénéfice : voir où partent tes tokens. Risque : aucun. Économie : aucune.
- **Annuler** — n'installe rien, ne modifie rien, et rends la main.

Correspondance : Autonome → `autonome`, Léger → `light`, Mesurer seulement → `observe`. **Si la réponse est Annuler (ou autre chose que ces trois modes) : n'exécute aucune commande, réponds simplement « Installation annulée, rien n'a été modifié » et arrête-toi.**

## 2. Installe, depuis le dossier du projet courant (Bash)

```
python <chemin-du-dépôt>/dist/token-saver.pyz install --profile <profil> -y
python <chemin-du-dépôt>/dist/token-saver.pyz doctor --fix find        # sauf pour observe
python <chemin-du-dépôt>/dist/token-saver.pyz prune                    # aperçu seulement : outils/plugins jamais utilisés par ce projet
```

Si l'aperçu de `prune` propose de retirer des outils, reproduis ses lignes telles quelles dans ta réponse et indique que `/token-saver-prune --apply` (dans une nouvelle session) l'applique ; n'applique pas toi-même.

`<chemin-du-dépôt>` est le dossier donné par l'utilisateur (celui qui contient ce fichier). Utilise des slashes dans le chemin. Si `python` échoue, essaie `python3` (macOS, Linux) ou `py -3` (Windows).

## 3. Réponds en 5 lignes maximum, en français, sans jargon

- le mode installé et ce qu'il fait en une phrase ;
- que sept raccourcis apparaissent après `/clear` ou une nouvelle session : `/token-saver-status` (tout l'état sur un écran), `/token-saver-next` (tâche finie : note l'état, on continue dans une session neuve qui reçoit la note toute seule), `/token-saver-autonomie` (lance l'autonomie en sessions neuves), `/token-saver-autonomie-stop`, `/token-saver-doctor`, `/token-saver-dashboard`, `/token-saver-off` ; plus `/token-saver-prune` pour les experts ;
- que l'**autonomie en sessions neuves** fait tourner chaque cycle de travail dans une conversation neuve, sans compaction ; elle utilise la commande de cycle du projet si elle existe (`/autonomie 1`) ; sinon, au premier `/token-saver-autonomie`, TOKEN SAVER regarde ce que le projet contient, propose une recette de cycle (tâches, tests, revue, commit, journal), pose au plus deux questions, et l'enregistre après accord (`/token-saver-autonomie config` pour la revoir). Prérequis une seule fois par machine : le CLI Claude doit être connecté hors de l'app (`/token-saver-status` le dit et explique quoi faire) ;
- que **toute revue de code** du projet suit maintenant la **procédure bornée**, qu'elle passe par sa commande (par exemple `/review-pr` ou `/code-review` ; sinon `/token-saver-review`) ou par un agent lancé pour relire : un hook souffle la procédure au démarrage de la revue, la commande du projet n'est pas modifiée. Le diff est écrit dans un fichier, le tour suivant ne relit que les lignes changées, 3 tours maximum, seuls les 🔴 empêchent la fusion, les 🟠 se corrigent dans la même PR ou sont écartés par écrit (jamais de carte de suivi), la CI est attendue en un seul appel ; la grille du projet n'a pas changé ;
- si le projet a une commande d'autonomie (par exemple `/autonomie-totale`) : qu'elle lance désormais les **sessions neuves** à sa place, une par cycle, et que sa commande d'arrêt (par exemple `/stop-autonomie`) les arrête ; si elles ne peuvent pas démarrer, le message est refusé avec la raison (retaper la commande dans les 2 minutes force le départ dans l'app) ;
- que `/token-saver-off` retire tout de ce projet (les mesures sont conservées) ;
- qu'aucun fichier du projet n'a été modifié, ni rien hors du projet : TOKEN SAVER a seulement créé `.token-saver/`, ses raccourcis `.claude/skills/token-saver*`, son fichier de règles `.claude/rules/token-saver.md` (lu par Claude Code comme CLAUDE.md), et ajouté ses hooks dans `.claude/settings.local.json` (personnel, sauvegardé avant).

Ne relance pas les commandes, ne modifie pas d'autres fichiers du projet, ne propose pas d'autres réglages.
