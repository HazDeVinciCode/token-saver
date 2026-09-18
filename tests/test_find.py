"""Tests de la recherche locale par sections (FTS5)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import find as F  # noqa: E402

DOC = """# Guide du projet

Intro générale.

## Génération des assets

Le script `generate-assets.py` produit les sprites à partir des PNG sources.
Lancer `python tools/generate-assets.py --all`.

## Architecture des scènes

Chaque scène Godot hérite de GameRoot. Les signaux passent par GameSession.

### Sous-section réseau

Détails réseau ici.

```
# pas un titre
```
"""


class FindTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name)
        (self.p / ".token-saver").mkdir()
        (self.p / "docs").mkdir()
        (self.p / "docs" / "GUIDE.md").write_text(DOC, encoding="utf-8")
        (self.p / "node_modules" / "x").mkdir(parents=True)
        (self.p / "node_modules" / "x" / "README.md").write_text("# ignoré\n\nassets assets assets", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_sections_and_search(self):
        st = F.refresh(self.p)
        self.assertEqual(st["indexed"], 1)                       # node_modules exclu
        self.assertEqual(st["total_sections"], 4)                # début, assets, architecture, réseau (bloc de code ignoré)
        r = F.search(self.p, "génération des assets", top=1)
        self.assertIn("docs/GUIDE.md:", r)
        self.assertIn("Guide du projet > Génération des assets", r)
        self.assertIn("generate-assets.py", r)
        self.assertNotIn("GameRoot", r)
        r2 = F.search(self.p, "generation asset", top=1)         # sans accents, singulier (préfixe)
        self.assertIn("Génération des assets", r2)
        r3 = F.search(self.p, "scene godot signaux", top=1)
        self.assertIn("Architecture des scènes", r3)

    def test_lazy_refresh_and_budget(self):
        F.refresh(self.p)
        (self.p / "docs" / "NEW.md").write_text("# Réseau\n\n" + "paquet UDP ".join([""] * 400), encoding="utf-8")
        r = F.search(self.p, "paquet UDP", top=1, max_tokens=100)
        self.assertIn("docs/NEW.md", r)
        self.assertIn("suite : Read", r)                          # coupé proprement avec pointeur
        self.assertLess(len(r), 100 * 4 + 300)
        (self.p / "docs" / "NEW.md").unlink()
        st = F.refresh(self.p)
        self.assertEqual(st["removed"], 1)

    def test_no_hit(self):
        F.refresh(self.p)
        self.assertIn("aucune section", F.search(self.p, "zzzz qqqq"))


if __name__ == "__main__":
    unittest.main()
