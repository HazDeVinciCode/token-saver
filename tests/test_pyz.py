"""Test d'intégration : le parcours réel d'un utilisateur, depuis l'archive .pyz construite (pas depuis les sources)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PyzTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.pyz = root / "token-saver.pyz"
        zipapp.create_archive(str(ROOT / "src"), str(self.pyz), main="token_saver.cli:entry", compressed=True,
                              filter=lambda p: "__pycache__" not in p.parts)
        self.p = root / "proj"
        (self.p / ".claude").mkdir(parents=True)
        (self.p / "CLAUDE.md").write_text("# projet\n", encoding="utf-8")
        (self.p / "TODO.md").write_text("- [ ] tâche\n", encoding="utf-8")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "PYTHONPATH"))}
        self.env["CLAUDE_CONFIG_DIR"] = str(root / "cfg")
        (root / "cfg").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_ts(self, *args, cwd=None):
        p = subprocess.run([sys.executable, str(self.pyz), *args], cwd=str(cwd or self.p), env=self.env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=180)
        return p.returncode, p.stdout + p.stderr

    def test_install_check_uninstall_from_pyz(self):
        rc, out = self.run_ts("install", "--profile", "light", "--project", str(self.p), "-y")
        self.assertEqual(rc, 0, out)
        self.assertIn("installé", out)
        for f in ("manifest.json", "config.json", "bin/token-saver.pyz", "bin/statusline.py"):
            self.assertTrue((self.p / ".token-saver" / f).is_file(), f)
        self.assertEqual((self.p / "CLAUDE.md").read_text(encoding="utf-8"), "# projet\n")           # jamais modifié
        self.assertTrue((self.p / ".claude" / "rules" / "token-saver.md").is_file())                     # nos consignes, fichier à nous
        for s in ("token-saver-status", "token-saver-autonomie", "token-saver-next", "token-saver-off"):
            self.assertTrue((self.p / ".claude" / "skills" / s / "SKILL.md").is_file(), s)
        self.assertFalse((self.p / ".claude" / "skills" / "token-saver-cycle").exists())   # plus de cycle générique : une recette validée
        cfg = json.loads((self.p / ".token-saver" / "config.json").read_text(encoding="utf-8"))
        self.assertIsNone(cfg["nuit"]["prompt"])                                       # pas de commande de cycle dans le projet : recette TOKEN SAVER
        rc, out = self.run_ts("nuit", "start", "--cycles", "1", "--project", str(self.p))
        self.assertEqual(rc, 0, out)
        self.assertTrue(out.startswith("RECETTE À VALIDER"), out[:120])               # rien ne part : l'accompagnement d'abord
        self.assertIn("tâches    : TODO.md (1 tâche(s) à faire)", out)
        self.assertIn("? tests", out)
        rc, out = self.run_ts("nuit", "recette", "save", "--tests", "aucun", "--project", str(self.p))
        self.assertEqual(rc, 0, out)
        self.assertIn("recette enregistrée : .token-saver/cycle.md", out)
        rc, out = self.run_ts("nuit", "recette", "show", "--project", str(self.p))
        self.assertIn("# Recette de cycle TOKEN SAVER", out)
        rc, out = self.run_ts("doctor", "--fix", "find", "--project", str(self.p))
        self.assertEqual(rc, 0, out)
        rc, out = self.run_ts("check", "--project", str(self.p))
        self.assertEqual(rc, 0, out)
        self.assertIn("installation saine", out)
        self.assertIn("recette de cycle — recette TOKEN SAVER validée", out)
        rc, out = self.run_ts("status", "--since", "7d", "--project", str(self.p))
        self.assertEqual(rc, 3, out)                                                   # pas encore de transcript : code 3, pas de plantage
        rc, out = self.run_ts("uninstall", "--purge", "--project", str(self.p))
        self.assertEqual(rc, 0, out)
        self.assertFalse((self.p / ".token-saver").exists())
        self.assertFalse((self.p / ".claude" / "skills" / "token-saver-cycle").exists())
        self.assertFalse((self.p / ".claude" / "rules" / "token-saver.md").exists())
        self.assertNotIn("token-saver", (self.p / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_project_skill_is_preferred_as_cycle(self):
        (self.p / ".claude" / "skills" / "autonomie").mkdir(parents=True)
        (self.p / ".claude" / "skills" / "autonomie" / "SKILL.md").write_text("---\nname: autonomie\n---\n", encoding="utf-8")
        rc, out = self.run_ts("install", "--profile", "light", "--project", str(self.p), "-y")
        self.assertEqual(rc, 0, out)
        cfg = json.loads((self.p / ".token-saver" / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["nuit"]["prompt"], "/autonomie 1")


if __name__ == "__main__":
    unittest.main()
