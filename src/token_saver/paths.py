"""Résolution des chemins : projet, dossier .token-saver, transcripts Claude Code."""
from __future__ import annotations

import os
import re
from pathlib import Path

TS_DIRNAME = ".token-saver"


def claude_config_dir() -> Path:
    """~/.claude (ou CLAUDE_CONFIG_DIR), là où Claude Code écrit projects/<projet>/*.jsonl."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def encode_project_dir(project: Path) -> str:
    """Claude Code nomme le dossier de transcripts en remplaçant tout caractère
    non alphanumérique du chemin absolu par '-' (D:\\dev\\mon-projet -> D--dev-mon-projet)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project))


_PY: str | None = None


def py_cmd() -> str:
    """Commande Python écrite dans les hooks, les raccourcis et les messages : `python` si le PATH l'a (Windows, venv…),
    sinon `python3` (macOS et Linux sans alias `python`)."""
    global _PY
    if _PY is None:
        import shutil
        _PY = "python" if shutil.which("python") else "python3" if shutil.which("python3") else "python"
    return _PY


def localize(text: str) -> str:
    """Les textes de l'outil disent `python .token-saver/bin/token-saver.pyz` ; sur une machine sans `python`, on écrit `python3`."""
    py = py_cmd()
    return text if py == "python" else text.replace("python .token-saver/bin/token-saver.pyz", f"{py} .token-saver/bin/token-saver.pyz")


def find_project_root(start: Path | None = None) -> Path | None:
    """Racine = premier ancêtre contenant .claude/, CLAUDE.md ou .git."""
    p = (start or Path.cwd()).resolve()
    for cand in (p, *p.parents):
        if (cand / ".claude").is_dir() or (cand / "CLAUDE.md").is_file() or (cand / ".git").exists():
            return cand
    return None


def transcript_dirs(project: Path, include_worktrees: bool = True) -> list[Path]:
    """Dossiers de transcripts du projet (et de ses worktrees Claude Code)."""
    base = claude_config_dir() / "projects"
    enc = encode_project_dir(project.resolve())
    dirs: list[Path] = []
    if (base / enc).is_dir():
        dirs.append(base / enc)
    if include_worktrees:
        dirs.extend(d for d in sorted(base.glob(enc + "--claude-worktrees-*")) if d.is_dir())
    return dirs


def transcript_files(project: Path) -> list[tuple[Path, str]]:
    """Liste (fichier, kind) avec kind = 'main' ou 'subagent'."""
    out: list[tuple[Path, str]] = []
    for d in transcript_dirs(project):
        out.extend((f, "main") for f in sorted(d.glob("*.jsonl")))
        out.extend((f, "subagent") for f in sorted(d.glob("*/subagents/*.jsonl")))
    return out


def ts_dir(project: Path) -> Path:
    return project / TS_DIRNAME


def default_db_path(project: Path) -> Path:
    return ts_dir(project) / "metrics" / "usage.sqlite"
