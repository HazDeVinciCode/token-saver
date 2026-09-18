"""Tests de la recette de cycle : inventaire du projet, proposition, questions, enregistrement, usage par le runner."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import install as I  # noqa: E402
from token_saver import nuit, recette  # noqa: E402
from token_saver.config import load_config  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_claude.py")


class RecetteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["CLAUDE_CONFIG_DIR"] = str(root / "cfg")
        (root / "cfg").mkdir()
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.p = root / "proj"
        (self.p / ".claude").mkdir(parents=True)
        (self.p / "CLAUDE.md").write_text("# p\n", encoding="utf-8")
        self.script = root / "scenario.txt"
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(self.script)

    def tearDown(self):
        for k in ("CLAUDE_CONFIG_DIR", "FAKE_CLAUDE_SCRIPT", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def cfg(self):
        c = load_config(self.p)
        c["nuit"].update({"claude_cmd": [sys.executable, FAKE], "pause_between_s": 0, "cycle_timeout_min": 1, "max_hours": 1, "retry_wait_min": 1})
        return c

    def write(self, rel: str, text: str):
        f = self.p / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")

    def test_recipe_assembled_from_what_the_project_contains(self):
        self.write("package.json", json.dumps({"scripts": {"test": "vitest run"}}))
        self.write("TODO.md", "- [x] faite\n- [ ] première\n- [ ] deuxième\n")
        self.write(".claude/commands/review-pr.md", "Relis le diff de la PR avec la grille.\n")
        self.write(".claude/skills/ship-pr/SKILL.md", "---\nname: ship-pr\ndescription: pousse la branche et ouvre la PR\n---\n")
        self.write(".claude/skills/ux-review/SKILL.md", "Évalue l'ergonomie des écrans.\n")
        self.write(".claude/agents/testeur.md", "---\nname: testeur\ndescription: lance les tests et lit les résultats\n---\n")
        self.write(".claude/agents/relecteur.md", "---\nname: relecteur\ndescription: review du code, sécurité\n---\n")
        self.write(".github/workflows/ci.yml", "on: push\n")
        I.install(self.p, "light")
        cfg = load_config(self.p)
        self.assertIsNone(cfg["nuit"]["prompt"])                                          # pas de commande de cycle : recette TOKEN SAVER
        inv = recette.inventory(self.p, cfg)
        self.assertEqual(inv["tests"], {"command": "npm test", "source": "package.json"})
        self.assertEqual((inv["tasks"]["kind"], inv["tasks"]["open"]), ("todo", 2))
        kinds = {c["name"]: c["kind"] for c in inv["commands"]}
        self.assertEqual(kinds, {"review-pr": "revue", "ship-pr": "livraison", "ux-review": "autre"})
        self.assertEqual({a["name"]: a["kind"] for a in inv["agents"]}, {"relecteur": "revue", "testeur": "tests"})
        self.assertEqual(inv["review"]["command"], "/review-pr")
        self.assertEqual(recette.questions(inv, {}), [])                                  # tout est trouvé : aucune question
        text = recette.build(self.p, cfg, {}, inv)
        for needle in ("`TODO.md`", "`npm test`", "`/review-pr`", "`testeur`", "`relecteur`", "`/ship-pr`", "`/ux-review`", "[nuit:fin]"):
            self.assertIn(needle, text)
        self.assertNotIn("gh pr create", text)                                            # pas de remote GitHub : pas de PR
        self.assertNotIn("nuit/<slug", text)                                              # pas de git : pas de branche
        out = recette.propose(self.p, cfg)
        self.assertTrue(out.startswith(recette.HEADER_ASK), out[:80])
        self.assertIn("Trouvé dans le projet", out)
        self.assertIn("tâches    : TODO.md (2 tâche(s) à faire)", out)
        self.assertIn("/review-pr [revue]", out)
        self.assertNotIn("Questions", out)

    def test_empty_project_asks_two_questions_then_base_recipe(self):
        I.install(self.p, "light")
        cfg = load_config(self.p)
        inv = recette.inventory(self.p, cfg)
        qs = recette.questions(inv, {})
        self.assertEqual(len(qs), 2)
        self.assertIn("tâches", qs[0]); self.assertIn("todo", qs[0]); self.assertIn("issues", qs[0])
        self.assertIn("tests", qs[1]); self.assertIn("aucun", qs[1])
        base = recette.build(self.p, cfg, {}, inv)
        self.assertIn("`/token-saver-review`", base)                                     # revue bornée générique : existe sans commande du projet
        self.assertIn("`TODO.md` à la racine (première case non cochée", base)
        self.assertIn("n'a pas de commande de tests automatisés", base)
        self.assertNotIn("Aussi disponibles", base)
        self.assertIn("Questions", recette.propose(self.p, cfg))
        msg = recette.save(self.p, cfg, tasks="todo", tests="aucun")
        self.assertIn("recette enregistrée : .token-saver/cycle.md", msg)
        text = recette.recipe_path(self.p).read_text(encoding="utf-8")
        self.assertIn("première case non cochée", text); self.assertIn("pas de commande de tests", text)
        self.assertEqual(recette.load_answers(self.p)["tasks"], "todo")
        recette.save(self.p, cfg, tests="python -m pytest -q")                             # une réponse à la fois : l'autre est gardée
        text = recette.recipe_path(self.p).read_text(encoding="utf-8")
        self.assertIn("`python -m pytest -q`", text); self.assertIn("première case non cochée", text)
        recette.save(self.p, cfg, tasks="le board docs/BOARD.md, colonne Prêt")
        self.assertIn("indiquée par l'utilisateur : le board docs/BOARD.md, colonne Prêt", recette.recipe_path(self.p).read_text(encoding="utf-8"))
        self.assertEqual(recette.questions(inv, recette.load_answers(self.p)), [])        # répondu : plus de question
        self.assertTrue(recette.propose(self.p, cfg).startswith(recette.HEADER_HAVE))

    def test_start_without_recipe_proposes_and_run_uses_it(self):
        self.write("TODO.md", "- [ ] tâche\n")
        I.install(self.p, "light")
        cfg = self.cfg()
        py = sys.executable
        msg = nuit.start(self.p, cfg, 0, None, [py, "-c", "pass"])
        self.assertTrue(msg.startswith(recette.HEADER_ASK), msg[:80])                     # rien lancé : la proposition d'abord
        self.assertNotEqual(nuit.read_state(self.p).get("status"), "running")
        item = next(r for r in nuit.check(self.p, cfg, do_ping=False) if r[0] == "recette de cycle disponible")
        self.assertFalse(item[1]); self.assertIn("aucune", item[2])
        self.assertEqual(nuit.run(self.p, cfg, cycles=1), 2)                               # le runner sans recette s'arrête proprement
        self.assertEqual(nuit.read_state(self.p)["error"], "aucune recette de cycle")
        recette.save(self.p, cfg, tests="aucun")
        item = next(r for r in nuit.check(self.p, cfg, do_ping=False) if r[0] == "recette de cycle disponible")
        self.assertEqual(item, ("recette de cycle disponible", True, "recette TOKEN SAVER (cycle.md)"))
        self.script.write_text("ok\n", encoding="utf-8")
        self.assertEqual(nuit.run(self.p, cfg, cycles=1), 0)
        calls = [json.loads(l) for l in Path(str(self.script) + ".calls").read_text(encoding="utf-8").splitlines()]
        sent = calls[-1]["argv"][calls[-1]["argv"].index("-p") + 1]
        self.assertTrue(sent.startswith("# Recette de cycle TOKEN SAVER"), sent[:60])     # la recette est le prompt de la session neuve
        self.assertIn("Note du runner TOKEN SAVER : cycle 1", sent)
        self.assertEqual(nuit.read_state(self.p)["prompt"], "recette TOKEN SAVER (cycle.md)")
        self.assertIn("recette TOKEN SAVER", nuit.log_file(self.p).read_text(encoding="utf-8"))

    def test_project_command_is_kept_unless_replaced(self):
        self.write(".claude/skills/autonomie/SKILL.md", "---\nname: autonomie\n---\nun cycle\n")
        I.install(self.p, "light")
        cfg = load_config(self.p)
        self.assertEqual(cfg["nuit"]["prompt"], "/autonomie 1")
        out = recette.propose(self.p, cfg)
        self.assertTrue(out.startswith(recette.HEADER_HAVE)); self.assertIn("`/autonomie 1`", out)
        self.assertEqual(recette.show(self.p, cfg), "recette de cycle : la commande du projet `/autonomie 1`")
        msg = recette.save(self.p, cfg, tasks="todo", tests="aucun")
        self.assertIn("garde sa commande de cycle `/autonomie 1`", msg)
        self.assertEqual(load_config(self.p)["nuit"]["prompt"], "/autonomie 1")
        self.assertEqual(nuit.cycle_prompt(self.p, nuit.cfg_nuit(load_config(self.p)))[1], "/autonomie 1")
        msg = recette.save(self.p, cfg, replace=True)
        self.assertIn("remplace la commande du projet", msg)
        c2 = load_config(self.p)
        self.assertIsNone(c2["nuit"]["prompt"]); self.assertFalse(c2["nuit"]["prompt_auto"])
        self.assertEqual(nuit.cycle_prompt(self.p, nuit.cfg_nuit(c2))[1], "recette TOKEN SAVER (cycle.md)")
        I.install(self.p, "light")                                                         # une mise à jour respecte le choix
        self.assertIsNone(load_config(self.p)["nuit"]["prompt"])
        old = self.p / ".claude" / "skills" / "token-saver-cycle" / "SKILL.md"             # ancien cycle générique : migré vers la recette
        old.parent.mkdir(parents=True); old.write_text("python .token-saver/bin/token-saver.pyz\n", encoding="utf-8")
        cfgf = self.p / ".token-saver" / "config.json"
        c = json.loads(cfgf.read_text(encoding="utf-8")); c["nuit"].update({"prompt": "/token-saver-cycle", "prompt_auto": False})
        cfgf.write_text(json.dumps(c), encoding="utf-8")
        I.install(self.p, "light")
        self.assertEqual(load_config(self.p)["nuit"]["prompt"], "/autonomie 1")
        self.assertFalse(old.exists())

    def test_tests_detected_from_claude_md_and_git_branch_lines(self):
        self.write("CLAUDE.md", "# p\n\nTests : `dotnet test tests/Unit` avant chaque commit.\n")
        subprocess.run(["git", "init", "-q"], cwd=str(self.p), check=True)
        I.install(self.p, "light")
        inv = recette.inventory(self.p, load_config(self.p))
        self.assertEqual(inv["tests"], {"command": "dotnet test tests/Unit", "source": "CLAUDE.md"})
        text = recette.build(self.p, load_config(self.p), {}, inv)
        self.assertIn("`dotnet test tests/Unit`", text)
        self.assertIn("branche `autonomie/<slug-de-la-tache>`", text)
        self.assertIn("Laisse la branche commitée", text)                                 # git sans remote : pas de push, pas de PR


if __name__ == "__main__":
    unittest.main()
