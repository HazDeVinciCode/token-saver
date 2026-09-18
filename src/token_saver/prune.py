"""`token-saver prune` — retire du contexte les outils et plugins que CE projet n'utilise jamais.

Chaque appel API relit la description de tous les outils disponibles (dans l'app desktop : ~40K tokens de
catalogue). Une règle `permissions.deny` avec le nom d'un outil ou d'un serveur MCP le retire du contexte
(documentation Claude Code), et `enabledPlugins: {"<plugin>": false}` désactive un plugin pour ce projet seulement.
On ne retire que ce qui n'a JAMAIS été utilisé dans l'historique du projet, jamais un outil de base, et tout
est inscrit dans le manifest pour être retiré à l'identique par `uninstall`.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .paths import claude_config_dir, ts_dir

# Outils de base : jamais retirés, même sans usage.
CORE = {"Bash", "PowerShell", "Read", "Edit", "Write", "Glob", "Grep", "Agent", "Skill", "ToolSearch", "AskUserQuestion",
        "WebFetch", "WebSearch", "Monitor", "TaskOutput", "TaskStop", "ListAgents", "SendMessage", "NotebookEdit"}
# Candidats connus (app desktop) avec une taille indicative en tokens (ESTIMÉ, mesuré sur `/context` le 15/09/2026).
CANDIDATES: dict[str, int] = {
    "Workflow": 1500, "ReportFindings": 600, "ScheduleWakeup": 1100, "SuggestSkills": 400, "Artifact": 5500, "SendUserFile": 500,
    "EnterPlanMode": 150, "ExitPlanMode": 150, "EnterWorktree": 150, "ExitWorktree": 150, "CronCreate": 200, "CronDelete": 100,
    "CronList": 100, "DesignSync": 200, "ListPlugins": 100, "ListSkills": 100, "PushNotification": 150, "RemoteTrigger": 300,
    "SearchPlugins": 150, "SearchSkills": 150, "SuggestPluginInstall": 150,
    "mcp__Claude_Browser": 7400, "mcp__terminal": 400, "mcp__visualize": 1500, "mcp__ccd_session": 1700, "mcp__ccd_session_mgmt": 5900,
    "mcp__ccd_connectors": 1200, "mcp__ccd_directory": 600, "mcp__ccd_host": 1600, "mcp__ccd_pr": 1600, "mcp__ccd_sidebar": 2100,
    "mcp__ccd_view": 850, "mcp__ccd_window": 1200, "mcp__claude-in-chrome": 9500, "mcp__computer-use": 12000,
    "mcp__mcp-registry": 1100, "mcp__scheduled-tasks": 3600,
}
DEFERRED_SERVERS = {"mcp__claude-in-chrome", "mcp__computer-use", "mcp__ccd_session_mgmt", "mcp__ccd_connectors", "mcp__ccd_directory",
                    "mcp__ccd_host", "mcp__ccd_pr", "mcp__ccd_sidebar", "mcp__ccd_view", "mcp__ccd_window", "mcp__mcp-registry",
                    "mcp__scheduled-tasks", "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree", "CronCreate", "CronDelete",
                    "CronList", "DesignSync", "ListPlugins", "ListSkills", "PushNotification", "RemoteTrigger", "SearchPlugins",
                    "SearchSkills", "SuggestPluginInstall"}   # différés : seuls leurs noms sont chargés → gain réel ≈ 0, on ne les touche pas
MIN_HISTORY_CALLS = 300


def usage(con: sqlite3.Connection) -> tuple[int, set[str], set[str]]:
    """(nombre d'appels d'outils dans l'historique, outils utilisés, préfixes de plugins utilisés)."""
    total = con.execute("SELECT COUNT(*) FROM tool_uses").fetchone()[0]
    tools = {r[0] for r in con.execute("SELECT DISTINCT name FROM tool_uses") if r[0]}
    plugins = set()
    for (s,) in con.execute("SELECT DISTINCT skill FROM calls WHERE skill LIKE '%:%'"):
        plugins.add(s.split(":")[0])
    for (t,) in con.execute("SELECT DISTINCT type FROM agents WHERE type LIKE '%:%'"):
        plugins.add(t.split(":")[0])
    return total, tools, plugins


def installed_plugins() -> list[str]:
    """Identifiants `nom@marketplace` connus de Claude Code sur cette machine (lecture seule de ~/.claude.json)."""
    try:
        cj = json.loads((claude_config_dir().parent / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return sorted((cj.get("pluginUsage") or {}).keys())


def plan(con: sqlite3.Connection) -> dict:
    total, used, used_plugins = usage(con)
    if total < MIN_HISTORY_CALLS:
        return {"enough_history": False, "total_calls": total, "deny": [], "plugins": [], "saving_tokens": 0}
    used_servers = {t.split("__")[1] for t in used if t.startswith("mcp__") and t.count("__") >= 2}
    deny = []
    for name, size in CANDIDATES.items():
        if name in CORE or name in DEFERRED_SERVERS:
            continue
        if name.startswith("mcp__"):
            if name[5:] in used_servers:
                continue
        elif name in used:
            continue
        deny.append({"rule": name, "tokens": size})
    plugins = []
    for pid in installed_plugins():
        short = pid.split("@")[0]
        if short not in used_plugins:
            plugins.append({"plugin": pid, "tokens": 1200})
    return {"enough_history": True, "total_calls": total, "deny": deny, "plugins": plugins,
            "saving_tokens": sum(d["tokens"] for d in deny) + sum(p["tokens"] for p in plugins)}


def render_plan(p: dict, applied: bool = False) -> str:
    if not p["enough_history"]:
        return (f"pas assez d'historique dans ce projet ({p['total_calls']} appels d'outils, minimum {MIN_HISTORY_CALLS}) : "
                f"on ne retire rien tant qu'on ne sait pas ce que le projet utilise")
    L = [f"{'Retiré' if applied else 'À retirer'} du contexte de ce projet (jamais utilisé en {p['total_calls']:,} appels d'outils) :".replace(",", " ")]
    for d in p["deny"]:
        L.append(f"  - outil {d['rule']:28} ≈ {d['tokens'] / 1e3:4.1f}K tokens par appel")
    for q in p["plugins"]:
        L.append(f"  - plugin {q['plugin']:27} ≈ {q['tokens'] / 1e3:4.1f}K tokens par appel (désactivé pour ce projet seulement)")
    if not p["deny"] and not p["plugins"]:
        L.append("  (rien : tout ce qui est chargé a déjà servi)")
    L.append(f"≈ {p['saving_tokens'] / 1e3:.0f}K tokens de moins relus à CHAQUE appel [ESTIMÉ, tailles mesurées sur /context] · "
             f"effet : à la prochaine ouverture de session (le cache est reconstruit une fois)")
    return "\n".join(L)


def apply(project: Path, p: dict) -> dict:
    """Écrit les règles dans .claude/settings.local.json et les inscrit dans le manifest (retrait exact par uninstall)."""
    from .install import _dump_json, _load_json
    sf = project / ".claude" / "settings.local.json"
    s = _load_json(sf)
    deny = s.setdefault("permissions", {}).setdefault("deny", [])
    added_rules = []
    for d in p["deny"]:
        if d["rule"] not in deny:
            deny.append(d["rule"])
            added_rules.append(d["rule"])
    ep = s.setdefault("enabledPlugins", {})
    added_plugins = []
    for q in p["plugins"]:
        if q["plugin"] not in ep:
            ep[q["plugin"]] = False
            added_plugins.append(q["plugin"])
    if not ep:
        s.pop("enabledPlugins", None)
    _dump_json(sf, s)
    mf = ts_dir(project) / "manifest.json"
    manifest = _load_json(mf)
    pr = manifest.setdefault("prune", {"deny": [], "plugins": []})
    pr["deny"] = sorted(set(pr["deny"]) | set(added_rules))
    pr["plugins"] = sorted(set(pr["plugins"]) | set(added_plugins))
    pr["saving_tokens"] = p["saving_tokens"]
    _dump_json(mf, manifest)
    return {"rules": added_rules, "plugins": added_plugins}


def undo(project: Path) -> int:
    """`prune --undo` : remet les outils et plugins retirés, sans toucher au reste de l'installation. Retourne le nombre d'entrées remises."""
    from .install import _dump_json, _load_json
    mf = ts_dir(project) / "manifest.json"
    manifest = _load_json(mf)
    n = remove(project, manifest)
    if manifest.get("prune"):
        manifest.pop("prune", None)
        _dump_json(mf, manifest)
    return n


def remove(project: Path, manifest: dict) -> int:
    """Retire exactement ce que `apply` a ajouté. Retourne le nombre d'entrées retirées."""
    from .install import _dump_json, _load_json
    pr = manifest.get("prune") or {}
    if not pr:
        return 0
    sf = project / ".claude" / "settings.local.json"
    s = _load_json(sf)
    n = 0
    deny = (s.get("permissions") or {}).get("deny")
    if isinstance(deny, list):
        for rule in pr.get("deny", []):
            if rule in deny:
                deny.remove(rule)
                n += 1
        if not deny:
            s["permissions"].pop("deny", None)
        if not s.get("permissions"):
            s.pop("permissions", None)
    ep = s.get("enabledPlugins")
    if isinstance(ep, dict):
        for pid in pr.get("plugins", []):
            if ep.get(pid) is False:
                ep.pop(pid)
                n += 1
        if not ep:
            s.pop("enabledPlugins", None)
    _dump_json(sf, s)
    return n
