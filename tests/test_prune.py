"""Tests de l'élagage des outils inutilisés (prune) : plan, application, retrait exact, garde-fous."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import install as I  # noqa: E402
from token_saver import prune  # noqa: E402
from token_saver.db import connect  # noqa: E402


def fill(con, n_bash=400, extra=()):
    rows = [(f"t{i}", "f", "S", None, i, "2026-09-01T00:00:00", "Bash", "ls", 10, 100, None, None, None, None, 0, None, None) for i in range(n_bash)]
    for j, name in enumerate(extra):
        rows.append((f"x{j}", "f", "S", None, j, "2026-09-01T00:00:00", name, None, 10, 100, None, None, None, None, 0, None, None))
    con.executemany("INSERT INTO tool_uses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()


class PruneTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["CLAUDE_CONFIG_DIR"] = str(root / "cfg")
        (root / "cfg").mkdir()
        (root / ".claude.json").write_text(json.dumps({"pluginUsage": {"fiscal@inline": {}, "office@inline": {}}}), encoding="utf-8")
        self.p = root / "proj"
        (self.p / ".claude").mkdir(parents=True)
        (self.p / "CLAUDE.md").write_text("# p\n", encoding="utf-8")
        (self.p / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(git *)"], "deny": ["Read(.env)"]}}), encoding="utf-8")
        I.install(self.p, "light")

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.tmp.cleanup()

    def test_not_enough_history(self):
        con = connect(None); fill(con, n_bash=50)
        p = prune.plan(con)
        self.assertFalse(p["enough_history"]); self.assertEqual(p["deny"], [])
        self.assertIn("pas assez d'historique", prune.render_plan(p))

    def test_plan_apply_remove(self):
        con = connect(None); fill(con, n_bash=400, extra=("Artifact", "mcp__visualize__show_widget"))
        con.execute("INSERT INTO calls(file, session_id, agent_id, message_id, request_id, seq, ts, model, skill) VALUES('f','S',NULL,'m','r',1,'2026-09-01','x','fiscal:contexte')")
        con.commit()
        p = prune.plan(con)
        rules = {d["rule"] for d in p["deny"]}
        self.assertIn("mcp__Claude_Browser", rules); self.assertIn("Workflow", rules)
        self.assertNotIn("Artifact", rules); self.assertNotIn("mcp__visualize", rules)      # utilisés : conservés
        self.assertFalse(rules & prune.CORE)                                                  # jamais un outil de base
        self.assertFalse(rules & prune.DEFERRED_SERVERS)                                      # différés : pas de gain, pas touchés
        self.assertEqual([q["plugin"] for q in p["plugins"]], ["office@inline"])              # fiscal utilisé, office non
        r = prune.apply(self.p, p)
        s = json.loads((self.p / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertIn("mcp__Claude_Browser", s["permissions"]["deny"]); self.assertIn("Read(.env)", s["permissions"]["deny"])
        self.assertEqual(s["permissions"]["allow"], ["Bash(git *)"])
        self.assertEqual(s["enabledPlugins"], {"office@inline": False})
        self.assertTrue(all(ok for _, ok, _ in I.check(self.p) if _.startswith("aucun outil de base")))
        prune.apply(self.p, p)                                                                # idempotent
        s2 = json.loads((self.p / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(s2["permissions"]["deny"].count("mcp__Claude_Browser"), 1)
        n = prune.undo(self.p)                                                                # retour arrière sans désinstaller
        self.assertEqual(n, len(r["rules"]) + len(r["plugins"]))
        s_undo = json.loads((self.p / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(s_undo["permissions"]["deny"], ["Read(.env)"]); self.assertNotIn("enabledPlugins", s_undo)
        self.assertNotIn("prune", json.loads((self.p / ".token-saver" / "manifest.json").read_text(encoding="utf-8")))
        self.assertEqual(prune.undo(self.p), 0)                                               # deux fois : rien à remettre
        self.assertTrue(all(ok for _, ok, _ in I.check(self.p)), I.render_check(I.check(self.p)))   # l'installation est intacte
        r = prune.apply(self.p, p)
        rep = I.uninstall(self.p, purge=True)
        self.assertIn(("prune (%d règles)" % (len(r["rules"]) + len(r["plugins"])), "removed"), rep["settings"])
        s3 = json.loads((self.p / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(s3, {"permissions": {"allow": ["Bash(git *)"], "deny": ["Read(.env)"]}})   # état initial exact


if __name__ == "__main__":
    unittest.main()
