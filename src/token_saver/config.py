"""Configuration : défauts + fusion avec <projet>/.token-saver/config.json."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .paths import ts_dir

# Multiplicateurs de prix API (platform.claude.com/docs/en/about-claude/pricing) relatifs
# au prix input du modèle. Les facteurs par modèle sont relatifs à Opus 5 et restent des
# ESTIMATIONS tant qu'ils n'ont pas été vérifiés : ils ne servent qu'au registre ESTIMÉ.
DEFAULT_CONFIG: dict = {
    "version": 1,
    "cost_model": {
        "weights": {"input": 1.0, "cache_read": 0.1, "cache_write_5m": 1.25, "cache_write_1h": 2.0, "output": 5.0},
        "model_factors": {"opus": 1.0, "fable": 2.0, "sonnet": 0.6, "haiku": 0.2, "default": 1.0},
        "model_factors_verified": False,
    },
    "thresholds": {
        "cold_rebuild_tokens": 50000,
        "long_context_tokens": 150000,
        "calls_per_turn_alert": 25,
        "big_write_tokens": 5000,
    },
    "features": {"H1_read_ledger": False, "H2_compact_reset": False, "H2_brief": False,
                 "H3_review_guard": False, "H4_loop_guard": False, "H6_autonomy_redirect": False, "H7_context_meter": False,
                 "statusline": False, "find": False},
    # H7 : rappel de contexte lourd. Au-delà de chaque palier, l'utilisateur voit une ligne (coût par message, /token-saver-next)
    # et Claude reçoit la consigne de proposer une session neuve quand la tâche est finie. Une fois par palier et par conversation.
    "context_meter": {"levels": [200000, 400000, 600000, 800000]},
    # Un refus qui est contourné coûte un appel API de plus (relecture de tout le contexte) : on ne refuse que
    # les relectures qui en valent la peine (≥ min_tokens) et on cesse de refuser dans un transcript après
    # max_overrides contournements (leçon EXP-03 : 7 contournements sur 18 refus en une nuit).
    "ledger": {"ttl_calls": 60, "ttl_minutes": 30, "min_tokens": 2000, "max_overrides": 2},
    # Brief post-compaction : rappel FACTUEL (fichiers touchés, dernières commandes) + pointeurs vers les fichiers d'état
    # du projet à relire avant de continuer. Jamais la dernière consigne utilisateur (leçon EXP-02 : consigne périmée réinjectée).
    "h2_brief": {"state_files": [], "max_chars": 1400},
    # `nuit` : une session neuve par cycle via `claude -p` (voir nuit.py). prompt = la commande du projet qui fait UN cycle
    # (`/autonomie 1`, détectée à l'installation) ; vide = la recette TOKEN SAVER `.token-saver/cycle.md`, proposée d'après le projet
    # et validée par l'utilisateur au premier `/token-saver-autonomie` (voir recette.py).
    "nuit": {"claude_cmd": None, "prompt": None, "prompt_auto": True, "model": "opus[1m]", "permission_mode": "auto",
             "cycle_timeout_min": 240, "max_hours": 12, "pause_between_s": 15, "retry_wait_min": 30,
             # H6 : commandes du projet qui lancent/arrêtent le mode autonome dans l'app (détectées à l'installation) ;
             # tapées dans une session de l'app, elles sont détournées vers le mode nuit (une session neuve par cycle).
             "autonomy_commands": [], "stop_commands": [], "commands_auto": True,
             # heure de fin locale « HH:MM » (optionnelle, en plus de max_hours) ; vérifications d'environnement déclarées par le
             # projet ({name, command, expect: exit0|nonempty|regex:…, required}) — proposées à l'installation d'après le projet.
             "end_at": None, "env_checks": [], "env_checks_auto": True},
    # `review` : revue bornée (voir review.py). commands = commandes de revue du projet où la procédure est installée
    # (détectées à l'installation quand commands_auto est vrai) ; max_rounds tours puis la PR revient à l'humain.
    "review": {"max_rounds": 3, "max_findings": 12, "base": None, "wait_ci_timeout_min": 45, "commands": [], "commands_auto": True},
    "find": {"paths": ["docs/**/*.md", "*.md", ".claude/rules/**/*.md"],
             "exclude": ["node_modules", ".godot", ".git", ".token-saver", "bin", "obj"],
             "max_section_tokens": 1500, "guard": False},
    "statusline": {"ctx_warn_pct": 70, "ctx_crit_pct": 85, "cache_expiry_warn_min": 10,
                   "calls_per_turn_warn": 25, "quota_5h_warn_pct": 80, "quota_7d_warn_pct": 90},
}


def load_config(project: Path | None) -> dict:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if project is None:
        return cfg
    f = ts_dir(project) / "config.json"
    if f.is_file():
        try:
            user = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cfg
        _merge(cfg, user)
    return cfg


def _merge(base: dict, over: dict) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def model_factor(cfg: dict, model: str | None) -> float:
    m = (model or "").lower()
    factors = cfg["cost_model"]["model_factors"]
    for key in ("opus", "fable", "sonnet", "haiku"):
        if key in m:
            return float(factors.get(key, 1.0))
    return float(factors.get("default", 1.0))
