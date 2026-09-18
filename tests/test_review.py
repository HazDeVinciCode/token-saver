"""Tests de la revue bornée : tours (complet, delta, vérification, plafond), verdict déduit du rapport, bloc de procédure
dans les commandes de revue du projet (quel que soit leur nom) et retrait à l'identique, mesure pour le conseiller."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import install as I  # noqa: E402
from token_saver import review  # noqa: E402
from token_saver.config import load_config  # noqa: E402
from token_saver.db import connect  # noqa: E402


def git(p: Path, *a: str) -> str:
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *a], cwd=str(p),
                          capture_output=True, text=True, encoding="utf-8", check=True).stdout


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["CLAUDE_CONFIG_DIR"] = str(root / "cfg")
        (root / "cfg").mkdir()
        self.p = root / "proj"
        (self.p / ".claude" / "skills" / "review-pr").mkdir(parents=True)
        (self.p / ".claude" / "commands").mkdir()
        (self.p / "CLAUDE.md").write_text("# p\n", encoding="utf-8")
        self.skill_orig = "---\nname: review-pr\ndescription: revue\n---\n# /review-pr\n\n## Grille\n- perfs\n"
        (self.p / ".claude" / "skills" / "review-pr" / "SKILL.md").write_text(self.skill_orig, encoding="utf-8")
        self.cmd_orig = b"Relis le code.\r\n- r\xc3\xa8gle 1\r\n"                       # CRLF + UTF-8 : restauré octet pour octet
        (self.p / ".claude" / "commands" / "code-review.md").write_bytes(self.cmd_orig)
        (self.p / ".claude" / "commands" / "deploy.md").write_text("déploie\n", encoding="utf-8")
        git(self.p, "init", "-q", "-b", "main")
        (self.p / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        git(self.p, "add", ".")
        git(self.p, "commit", "-q", "-m", "init")

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.tmp.cleanup()

    def _commit(self, name: str, text: str, msg: str) -> None:
        (self.p / name).write_text(text, encoding="utf-8")
        git(self.p, "add", name)
        git(self.p, "commit", "-q", "-m", msg)

    def hook(self, name, payload):
        import io
        from token_saver import hooks as H
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.p)
        try:
            out = io.StringIO()
            H.run(name, io.StringIO(json.dumps({"session_id": "s1", "cwd": str(self.p), **payload})), out)
            return json.loads(out.getvalue())
        finally:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)

    def test_review_commands_detected_never_modified_procedure_by_hook(self):
        """Depuis le 18/09/2026 : aucun fichier du projet n'est modifié ; la procédure arrive par hook quand la revue démarre."""
        p = I.install(self.p, "light", dry_run=True)
        self.assertEqual(p["review_commands"], [".claude/skills/review-pr/SKILL.md", ".claude/commands/code-review.md"])
        self.assertIn(I.RULES_FILE, "\n".join(p["created"]))
        I.install(self.p, "light")
        self.assertEqual((self.p / ".claude" / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8"), self.skill_orig)   # intact
        self.assertEqual((self.p / ".claude" / "commands" / "code-review.md").read_bytes(), self.cmd_orig)                    # octet pour octet
        self.assertEqual((self.p / ".claude" / "commands" / "deploy.md").read_text(encoding="utf-8"), "déploie\n")
        self.assertEqual((self.p / "CLAUDE.md").read_text(encoding="utf-8"), "# p\n")                                        # CLAUDE.md jamais touché
        self.assertFalse((self.p / ".claude" / "skills" / I.REVIEW_SKILL_NAME).exists())   # le projet a ses commandes : pas de générique
        cfg = load_config(self.p)
        self.assertEqual(cfg["review"]["commands"], [".claude/skills/review-pr/SKILL.md", ".claude/commands/code-review.md"])
        rules = (self.p / I.RULES_FILE).read_text(encoding="utf-8")                   # nos consignes : dans un fichier à nous
        self.assertIn("`/review-pr`, `/code-review`", rules); self.assertIn("Compact instructions", rules); self.assertTrue(rules.startswith("<!--"))
        m = json.loads((self.p / ".token-saver" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["blocks"], [])
        s = json.loads((self.p / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        cmds = [h["command"] for lst in s["hooks"].values() for e in lst for h in e["hooks"]]
        self.assertTrue(any("h3-prompt" in c for c in cmds) and any("h3-skill" in c for c in cmds))
        res = {label: ok for label, ok, _ in I.check(self.p)}
        self.assertTrue(res["revue bornée : procédure soufflée par hook au démarrage des commandes de revue du projet"])
        self.assertTrue(res["aucun fichier du projet modifié (CLAUDE.md, commandes de revue : aucun bloc TOKEN SAVER)"])
        self.assertTrue(all(res.values()), res)
        # la procédure arrive quand la revue démarre : commande tapée…
        out = self.hook("h3-prompt", {"prompt": "/review-pr 12"})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("review begin", ctx); self.assertIn("jamais de carte ni d'issue de suivi", ctx); self.assertIn("grille de la commande", ctx)
        self.assertEqual(self.hook("h3-prompt", {"prompt": "/deploy"}), {})
        self.assertEqual(self.hook("h3-prompt", {"prompt": "relis la PR 12"}), {})    # pas la commande : rien (les agents ont H3)
        # … ou skill lancé par Claude lui-même (cycle d'autonomie)
        out = self.hook("h3-skill", {"tool_name": "Skill", "tool_input": {"skill": "code-review"}, "tool_response": {"success": True}})
        self.assertIn("review begin", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("h3-skill", {"tool_name": "Skill", "tool_input": {"skill": "deploy"}}), {})
        self.assertEqual(self.hook("h3-skill", {"tool_name": "Bash", "tool_input": {"command": "/review-pr"}}), {})
        I.install(self.p, "light")                                                    # idempotent
        self.assertEqual((self.p / ".claude" / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8"), self.skill_orig)
        rep = I.uninstall(self.p)
        self.assertIn(I.RULES_FILE, rep["files_removed"])
        self.assertFalse((self.p / ".claude" / "rules").exists())
        self.assertEqual((self.p / ".claude" / "skills" / "review-pr" / "SKILL.md").read_text(encoding="utf-8"), self.skill_orig)
        self.assertEqual((self.p / ".claude" / "commands" / "code-review.md").read_bytes(), self.cmd_orig)
        self.assertEqual((self.p / "CLAUDE.md").read_text(encoding="utf-8"), "# p\n")

    def test_update_removes_blocks_of_previous_versions_and_keeps_project_rules(self):
        """Incident du 18/09 : les blocs écrits par les versions antérieures (CLAUDE.md, commandes de revue) sont retirés à la mise à jour,
        octet pour octet ; ce que le projet a écrit lui-même hors bloc reste intact ; la nouvelle consigne arrive par hook."""
        I.install(self.p, "light")
        sk = self.p / ".claude" / "skills" / "review-pr" / "SKILL.md"
        cmd = self.p / ".claude" / "commands" / "code-review.md"
        old_block = f"\n{I.REVIEW_BLOCK_BEGIN}\n{I.REVIEW_PROCEDURE.replace('jamais de carte ni', 'ils vont dans une carte')}{I.REVIEW_BLOCK_END}\n"
        rule = "\n**Un 🟠 ne devient jamais une carte.** Il se corrige dans la même PR.\n"
        sk.write_text(self.skill_orig.replace("- perfs\n", "- perfs\n" + rule) + old_block, encoding="utf-8")   # règle du projet + ancien bloc
        cmd.write_bytes(self.cmd_orig + old_block.replace("\n", "\r\n").encode("utf-8"))
        I.claude_md_block(self.p, add=True)                                             # ancien bloc CLAUDE.md
        m = json.loads((self.p / ".token-saver" / "manifest.json").read_text(encoding="utf-8"))
        m["blocks"] += [{"file": ".claude/skills/review-pr/SKILL.md", "block": "review", "backup": None},
                        {"file": ".claude/commands/code-review.md", "block": "review", "backup": None}]
        (self.p / ".token-saver" / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        res = {label: (ok, d) for label, ok, d in I.check(self.p)}
        self.assertFalse(res["aucun fichier du projet modifié (CLAUDE.md, commandes de revue : aucun bloc TOKEN SAVER)"][0])
        I.install(self.p, "light")                                                      # mise à jour de l'outil
        self.assertEqual(sk.read_text(encoding="utf-8"), self.skill_orig.replace("- perfs\n", "- perfs\n" + rule))   # bloc parti, règle gardée
        self.assertEqual(cmd.read_bytes(), self.cmd_orig)                               # CRLF d'origine
        self.assertEqual((self.p / "CLAUDE.md").read_text(encoding="utf-8"), "# p\n")
        m = json.loads((self.p / ".token-saver" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(m["blocks"], [])
        self.assertTrue(all(ok for _, ok, _ in I.check(self.p)), I.render_check(I.check(self.p)))
        ctx = self.hook("h3-prompt", {"prompt": "/review-pr"})["hookSpecificOutput"]["additionalContext"]
        self.assertIn("jamais de carte ni d'issue de suivi", ctx); self.assertNotIn("vont dans une carte", ctx)

    def test_block_removal_restores_originals_byte_for_byte(self):
        """Cas réel (18/09) : la pose du bloc avait perdu la ligne vide finale (194 → 193 octets) ; le retrait doit rendre
        l'original exact, depuis la plus ancienne sauvegarde ; idem pour CLAUDE.md (fins de ligne CRLF conservées)."""
        cm = self.p / "CLAUDE.md"
        cm.write_bytes(b"# p\r\n\r\n## R\xc3\xa8gles\r\n- une\r\n\r\n")
        cm_orig = cm.read_bytes()
        I.install(self.p, "light")
        I.claude_md_block(self.p, add=True)                                             # ancienne version : bloc + sauvegarde de l'original
        self.assertIn(I.CLAUDE_MD_BEGIN, cm.read_text(encoding="utf-8"))
        cmd = self.p / ".claude" / "commands" / "code-review.md"
        orig = b"Relis le code.\n- r\xc3\xa8gle 1\n\n"
        bak_dir = self.p / ".token-saver" / "backups"
        (bak_dir / "review-block..claude__commands__code-review.md.20260916T191546.bak").write_bytes(orig)                   # 1re pose
        (bak_dir / "review-block..claude__commands__code-review.md.20260916T191714.bak").write_bytes(orig.rstrip(b"\n") + b"\n")   # 2e pose
        cmd.write_bytes(orig.rstrip(b"\n") + b"\n\n" + f"{I.REVIEW_BLOCK_BEGIN}\n{I.REVIEW_PROCEDURE}{I.REVIEW_BLOCK_END}\n".encode("utf-8"))
        m = json.loads((self.p / ".token-saver" / "manifest.json").read_text(encoding="utf-8"))
        m["blocks"].append({"file": ".claude/commands/code-review.md", "block": "review",
                            "backup": ".token-saver/backups/review-block..claude__commands__code-review.md.20260916T191714.bak"})
        (self.p / ".token-saver" / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        I.install(self.p, "light")                                                      # mise à jour
        self.assertEqual(cmd.read_bytes(), orig)                                        # ligne vide finale retrouvée
        self.assertEqual(cm.read_bytes(), cm_orig)                                      # CLAUDE.md : octet pour octet, CRLF compris
        sk = self.p / ".claude" / "skills" / "review-pr" / "SKILL.md"                   # bloc orphelin (aucune sauvegarde, manifest muet)
        sk.write_text(self.skill_orig + f"\n{I.REVIEW_BLOCK_BEGIN}\n{I.REVIEW_PROCEDURE}{I.REVIEW_BLOCK_END}\n", encoding="utf-8")
        I.install(self.p, "light")
        self.assertEqual(sk.read_text(encoding="utf-8"), self.skill_orig)

    def test_rules_file_modified_by_hand_is_kept(self):
        I.install(self.p, "light")
        f = self.p / I.RULES_FILE
        mine = f.read_text(encoding="utf-8") + "\n## Ma règle\n- toujours en français\n"
        f.write_text(mine, encoding="utf-8")
        I.install(self.p, "light")                                                      # mise à jour : la version locale est conservée
        self.assertEqual(f.read_text(encoding="utf-8"), mine)
        res = {label: (ok, d) for label, ok, d in I.check(self.p)}
        item = next(v for k, v in res.items() if k.startswith("règles TOKEN SAVER"))
        self.assertTrue(item[0]); self.assertIn("modifié à la main", item[1])
        rep = I.uninstall(self.p)
        self.assertIn(I.RULES_FILE, rep["files_kept"])                                  # jamais effacé sans --purge
        self.assertEqual(f.read_text(encoding="utf-8"), mine)

    def test_generic_skill_when_project_has_no_review_command(self):
        import shutil
        shutil.rmtree(self.p / ".claude" / "skills" / "review-pr")
        (self.p / ".claude" / "commands" / "code-review.md").unlink()
        I.install(self.p, "light")
        g = self.p / ".claude" / "skills" / I.REVIEW_SKILL_NAME / "SKILL.md"
        self.assertTrue(g.is_file())
        self.assertIn(I.REVIEW_BLOCK_BEGIN, g.read_text(encoding="utf-8"))
        self.assertEqual(load_config(self.p)["review"]["commands"], [])
        res = {label: ok for label, ok, _ in I.check(self.p)}
        self.assertTrue(res["revue bornée : commande générique /token-saver-review fournie (aucune commande de revue dans le projet)"])
        (self.p / ".claude" / "skills" / "ux-review").mkdir()                         # revue d'écrans, pas de code : jamais touchée
        (self.p / ".claude" / "skills" / "ux-review" / "SKILL.md").write_text("Évalue l'ergonomie tactile des écrans.\n", encoding="utf-8")
        (self.p / ".claude" / "skills" / "revue").mkdir()                             # le projet se dote d'une commande : la générique s'efface
        (self.p / ".claude" / "skills" / "revue" / "SKILL.md").write_text("Relis le diff de la PR.\n", encoding="utf-8")
        I.install(self.p, "light")
        self.assertFalse(g.exists())
        self.assertEqual((self.p / ".claude" / "skills" / "revue" / "SKILL.md").read_text(encoding="utf-8"), "Relis le diff de la PR.\n")   # jamais modifiée
        self.assertEqual((self.p / ".claude" / "skills" / "ux-review" / "SKILL.md").read_text(encoding="utf-8"), "Évalue l'ergonomie tactile des écrans.\n")
        self.assertEqual(load_config(self.p)["review"]["commands"], [".claude/skills/revue/SKILL.md"])
        ux = self.p / ".claude" / "skills" / "ux-review" / "SKILL.md"                  # bloc posé à tort par une version antérieure : retiré à la mise à jour
        ux.write_text(ux.read_text(encoding="utf-8") + f"\n{I.REVIEW_BLOCK_BEGIN}\n{I.REVIEW_PROCEDURE}{I.REVIEW_BLOCK_END}\n", encoding="utf-8")
        m = json.loads((self.p / ".token-saver" / "manifest.json").read_text(encoding="utf-8"))
        m["blocks"].append({"file": ".claude/skills/ux-review/SKILL.md", "block": "review", "backup": None})
        (self.p / ".token-saver" / "manifest.json").write_text(json.dumps(m), encoding="utf-8")
        I.install(self.p, "light")
        self.assertEqual(ux.read_text(encoding="utf-8"), "Évalue l'ergonomie tactile des écrans.\n")
        self.assertIn("(`/revue`)", (self.p / I.RULES_FILE).read_text(encoding="utf-8"))
        self.assertIn("review begin", self.hook("h3-prompt", {"prompt": "/revue"})["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(self.hook("h3-prompt", {"prompt": "/ux-review"}), {})           # revue d'écrans : pas de procédure de code
        I.uninstall(self.p)
        self.assertEqual((self.p / ".claude" / "skills" / "revue" / "SKILL.md").read_text(encoding="utf-8"), "Relis le diff de la PR.\n")

    def test_rounds_full_then_delta_then_verification_then_cap(self):
        I.install(self.p, "light")
        cfg = load_config(self.p)
        rc, out = review.begin(self.p, cfg)
        self.assertEqual(rc, 2, out)                                                   # sur main : refusé
        git(self.p, "checkout", "-q", "-b", "feat")
        self._commit("a.py", "def f():\n    return 2\n", "change a")
        rc, out = review.begin(self.p, cfg)
        self.assertEqual(rc, 0, out)
        self.assertIn("tour 1/3 (COMPLET)", out)
        diff1 = (self.p / ".token-saver" / "state" / "review" / "feat-r1.diff").read_text(encoding="utf-8")
        self.assertIn("+    return 2", diff1)
        rep = self.p / ".token-saver" / "state" / "review" / "feat-r1.md"
        rep.write_text("🔴 a.py:2 — retourne 2 au lieu de 1\n🔴 a.py:1 — pas de docstring\n🟠 a.py:1 — nom trop court\n", encoding="utf-8")
        rc, out = review.end(self.p, cfg, verdict="FUSIONNABLE", report=str(rep))      # verdict déduit du rapport, pas de l'argument
        self.assertEqual(rc, 0)
        self.assertIn("BLOQUE", out)
        self.assertIn("2 🔴, 1 🟠", out)
        rc, out = review.begin(self.p, cfg)                                             # même commit : pas de nouveau tour
        self.assertEqual(rc, 0)
        self.assertIn("déjà relu", out)
        self._commit("b.py", "x = 1\n", "add b")
        rc, out = review.begin(self.p, cfg)
        self.assertIn("tour 2/3 (DELTA)", out)
        self.assertIn("🔴 du tour précédent à vérifier (2)", out)
        diff2 = (self.p / ".token-saver" / "state" / "review" / "feat-r2.diff").read_text(encoding="utf-8")
        self.assertIn("b.py", diff2)
        self.assertNotIn("a.py", diff2)                                                 # seulement les lignes changées depuis le tour 1
        rep2 = self.p / ".token-saver" / "state" / "review" / "feat-r2.md"
        rep2.write_text("🟢 b.py:1 — ok\n🟠 b.py:1 — nom\n", encoding="utf-8")
        rc, out = review.end(self.p, cfg, report=str(rep2))
        self.assertIn("FUSIONNABLE", out)
        self.assertIn("wait-ci", out)
        self.assertIn("jamais de carte ni d'issue de suivi", out)                       # règle du 18/09 : les 🟠 se corrigent dans la PR
        self.assertIn("se corrigent dans cette PR avant la fusion", out)
        self.assertNotIn("vont dans une carte", out)
        self._commit("c.py", "y = 2\n", "add c")
        rc, out = review.begin(self.p, cfg)
        self.assertIn("tour 3/3 (VÉRIFICATION)", out)
        rc, out = review.end(self.p, cfg, verdict="BLOQUE")
        self.assertIn("plafond de 3 tours atteint", out)
        self._commit("d.py", "z = 3\n", "add d")
        rc, out = review.begin(self.p, cfg)
        self.assertEqual(rc, 3)                                                         # jamais de 4ᵉ tour
        self.assertIn("plafond atteint", out)
        self.assertIn("feat", review.status(self.p))
        log = (self.p / ".token-saver" / "metrics" / "review-log.jsonl").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual([json.loads(l)["verdict"] for l in log], ["BLOQUE", "FUSIONNABLE", "BLOQUE"])
        rc, out = review.begin(self.p, cfg, fresh=True)                                 # sur décision humaine : on repart du tour 1
        self.assertIn("tour 1/3 (COMPLET)", out)
        self.assertIn("a.py", (self.p / ".token-saver" / "state" / "review" / "feat-r1.diff").read_text(encoding="utf-8"))

    def test_report_parsing_verdict_line_headings_and_amend(self):
        I.install(self.p, "light")
        cfg = load_config(self.p)
        git(self.p, "checkout", "-q", "-b", "feat")
        self._commit("a.py", "def f():\n    return 2\n", "change a")
        review.begin(self.p, cfg)
        rep = self.p / ".token-saver" / "state" / "review" / "feat-r1.md"
        rep.write_text("- 🟢 a.py:1 — ok\n\nVerdict : aucun 🔴 → FUSIONNABLE.\n", encoding="utf-8")   # cas réel du 16/09
        rc, out = review.end(self.p, cfg, report=str(rep))
        self.assertEqual(rc, 0, out)
        self.assertIn("FUSIONNABLE, 0 🔴", out)
        rep.write_text("- **🔴 a.py:2** — bug\n1. 🟠 a.py:1 — nom\n", encoding="utf-8")            # rapport corrigé après clôture, même commit
        rc, out = review.end(self.p, cfg, report=str(rep))
        self.assertEqual(rc, 0, out)
        self.assertIn("corrigé", out)
        self.assertIn("BLOQUE, 1 🔴, 1 🟠", out)
        st = review.read_state(self.p, "feat")
        self.assertEqual(len(st["rounds"]), 1)
        self.assertEqual(st["rounds"][0]["red"], ["**🔴 a.py:2** — bug"])
        self._commit("b.py", "x = 1\n", "add b")
        review.begin(self.p, cfg)
        rep2 = self.p / ".token-saver" / "state" / "review" / "feat-r2.md"
        rep2.write_text("## 🔴 Bloquants\n- b.py:1 — bug\n", encoding="utf-8")                      # titre : ambigu, jamais un faux FUSIONNABLE
        rc, out = review.end(self.p, cfg, report=str(rep2))
        self.assertEqual(rc, 2)
        self.assertIn("ambigu", out)
        rep2.write_text("- 🔴 b.py:1 — bug\n", encoding="utf-8")
        rc, out = review.end(self.p, cfg, report=str(rep2))
        self.assertEqual(rc, 0)
        self.assertIn("BLOQUE, 1 🔴", out)

    def test_wait_ci_without_gh(self):
        with mock.patch.object(review.shutil, "which", return_value=None):
            rc, out = review.wait_ci(self.p, timeout_min=1)
        self.assertEqual(rc, 2)
        self.assertIn("gh", out)

    def test_old_install_uninstalls_cleanly(self):
        """Un projet installé par une version antérieure (bloc CLAUDE.md suivi au manifest) : le retrait remet CLAUDE.md d'origine."""
        I.install(self.p, "light")
        I.claude_md_block(self.p, add=True)
        I.uninstall(self.p)
        self.assertEqual((self.p / "CLAUDE.md").read_text(encoding="utf-8"), "# p\n")
        self.assertFalse((self.p / I.RULES_FILE).exists())

    def test_measure_chains_and_polling(self):
        con = connect(None)
        ts = "2026-09-15T00:00:00"
        for i in range(1, 5):
            con.execute("INSERT INTO tool_uses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"t{i}", "f", "S", None, 100 + i, ts, "Agent", "godot", 10, 100, None, None, None, None, 0, None,
                         json.dumps({"description": f"Relecture {i} de la PR #12"})))
            con.execute("INSERT INTO agents VALUES(?,?,?,?,?,?)", (f"a{i}", "S", "godot", "opus", None, f"t{i}"))
            con.execute("INSERT INTO calls(file, session_id, agent_id, message_id, request_id, seq, ts, model, input, cache_read, cache_write, output, ctx) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"ag{i}", "S", f"a{i}", f"m{i}", f"r{i}", 1, ts, "opus", 1000, 5_000_000, 0, 10, 5_001_000))
        for k in range(25):
            con.execute("INSERT INTO tool_uses VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"p{k}", "f", "S", None, k, ts, "Bash", "gh pr checks 12", 10, 100, None, None, None, None, 0, None, None))
            con.execute("INSERT INTO calls(file, session_id, agent_id, message_id, request_id, seq, ts, model, input, cache_read, cache_write, output, ctx) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", ("f", "S", None, f"pm{k}", f"pr{k}", k, ts, "opus", 100, 400_000, 0, 10, 400_100))
        con.commit()
        m = review.measure(con, None)
        self.assertEqual(m["reviews"], 4)
        self.assertEqual(m["chains"][0][0], "12")
        self.assertEqual(m["chains"][0][1]["rounds"], 4)
        self.assertEqual(m["poll_calls"], 25)
        self.assertGreater(m["poll_tokens"], 9_000_000)


class ReviewHookTest(unittest.TestCase):
    """H3 : un agent lancé pour relire reçoit le brief (diff en fichier, tour) ; son rapport est enregistré au retour."""

    def setUp(self):
        import io
        self.io = io
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["CLAUDE_CONFIG_DIR"] = str(root / "cfg")
        (root / "cfg").mkdir()
        self.p = root / "proj"
        (self.p / ".claude").mkdir(parents=True)
        (self.p / "CLAUDE.md").write_text("# p\n", encoding="utf-8")
        git(self.p, "init", "-q", "-b", "main")
        (self.p / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        git(self.p, "add", ".")
        git(self.p, "commit", "-q", "-m", "init")
        git(self.p, "checkout", "-q", "-b", "feat")
        (self.p / "a.py").write_text("def f():\n    return 2\n", encoding="utf-8")
        git(self.p, "commit", "-q", "-am", "change")
        I.install(self.p, "light")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.p)

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.tmp.cleanup()

    def _commit(self, name: str, text: str, msg: str) -> None:
        (self.p / name).write_text(text, encoding="utf-8")
        git(self.p, "add", name)
        git(self.p, "commit", "-q", "-m", msg)

    def hook(self, name, payload):
        from token_saver import hooks as H
        out = self.io.StringIO()
        H.run(name, self.io.StringIO(json.dumps({"session_id": "s1", "cwd": str(self.p), **payload})), out)
        return json.loads(out.getvalue())

    def launch(self, tid, description, prompt="Relis les changements de la branche feat et donne ton avis."):
        return self.hook("h3-agent-pre", {"tool_name": "Agent", "tool_use_id": tid,
                                          "tool_input": {"description": description, "prompt": prompt, "subagent_type": "general-purpose"}})

    def test_review_agent_gets_brief_and_its_report_is_recorded(self):
        out = self.launch("tu1", "Relire la branche feat")
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["permissionDecision"], "allow")
        new = hso["updatedInput"]["prompt"]
        self.assertIn("tour 1/3 (COMPLET)", new)
        self.assertIn(".token-saver/state/review/feat-r1.diff", new)
        self.assertTrue(new.endswith("Relis les changements de la branche feat et donne ton avis."))
        self.assertEqual(hso["updatedInput"]["subagent_type"], "general-purpose")
        self.assertEqual(self.launch("tu9", "Explorer le module audio", "Liste les fichiers audio."), {})          # pas une relecture
        self.assertEqual(self.launch("tu8", "Relire la PR", "Relis .token-saver/state/review/feat-r1.diff"), {})   # procédure déjà suivie
        out = self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "tu1",
                                          "tool_response": [{"type": "text", "text": "🔴 a.py:2 — retourne 2\n🟠 a.py:1 — nom\n"}]})
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BLOQUE, 1 🔴, 1 🟠", ctx)
        self.assertIn("a.py:2", ctx)
        st = review.read_state(self.p, "feat")
        self.assertEqual(len(st["rounds"]), 1)
        self.assertEqual(st["hook_agents"], {})
        self.assertEqual(self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "zz", "tool_response": "x"}), {})   # inconnu
        out = self.launch("tu2", "Relire encore la branche feat")                          # même commit : pas de nouvelle relecture
        new = out["hookSpecificOutput"]["updatedInput"]["prompt"]
        self.assertIn("déjà relu", new)
        self.assertIn("Ne relis rien", new)
        self.assertEqual(self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "tu2", "tool_response": "🔴 x"}), {})
        self.assertEqual(len(review.read_state(self.p, "feat")["rounds"]), 1)

    def test_parallel_reviewers_add_up(self):
        self.launch("a1", "Revue sécurité du code")
        self.launch("a2", "Revue perfs du code")
        self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "a1", "tool_response": "🔴 a.py:2 — faille\n"})
        out = self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "a2", "tool_response": "🔴 a.py:1 — lent\n🟢 a.py:2 — ok\n"})
        self.assertIn("BLOQUE, 2 🔴", out["hookSpecificOutput"]["additionalContext"])
        st = review.read_state(self.p, "feat")
        self.assertEqual(len(st["rounds"]), 1)
        self.assertEqual(len(st["rounds"][0]["red"]), 2)

    def test_report_file_written_by_agent_is_used_when_result_has_no_text(self):
        out = self.launch("toolu_ABC123", "Relire la branche feat")
        new = out["hookSpecificOutput"]["updatedInput"]["prompt"]
        own = ".token-saver/state/review/feat-r1-ABC123.md"
        self.assertIn(own, new)                                                              # rapport propre à cet agent
        (self.p / own).write_text("🔴 a.py:2 — bug\n🟢 a.py:1 — ok\n", encoding="utf-8")
        meta = {"status": "completed", "agentId": "ab9577", "content": [{"type": "text", "text": "This agent's report was delivered as a message."}]}
        out = self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "toolu_ABC123", "tool_response": meta})
        self.assertIn("BLOQUE, 1 🔴", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(review.read_state(self.p, "feat")["rounds"][0]["report"], own)
        self.launch("toolu_DEF456", "Relire la branche feat à nouveau")                     # même commit : pas de tour, rien d'enregistré
        handback = {"status": "completed", "handback": {"message": "Rapport : aucun 🔴, rien n'empêche la fusion."}}
        self.assertEqual(self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "toolu_DEF456", "tool_response": handback}), {})
        self._commit("b.py", "x = 1\n", "add b")
        self.launch("toolu_GHI789", "Relire le correctif de la branche feat")
        out = self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "toolu_GHI789", "tool_response": handback})   # via handback
        self.assertIn("FUSIONNABLE, 0 🔴", out["hookSpecificOutput"]["additionalContext"])

    def test_unformatted_report_keeps_round_open(self):
        self.launch("b1", "Relire la branche")
        out = self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "b1", "tool_response": "J'ai regardé, ça me semble correct."})
        self.assertIn("review end", out["hookSpecificOutput"]["additionalContext"])
        self.assertIsNotNone(review.read_state(self.p, "feat")["pending"])
        out = self.hook("h3-agent-post", {"tool_name": "Agent", "tool_use_id": "b2", "tool_response": "🟢 ok"})            # non enregistré : ignoré
        self.assertEqual(out, {})

    def test_hook_registered_by_installer(self):
        s = json.loads((self.p / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        pre = [h for e in s["hooks"]["PreToolUse"] for h in e["hooks"] if "h3-agent-pre" in h["command"]]
        self.assertEqual(len(pre), 1)
        self.assertEqual([e["matcher"] for e in s["hooks"]["PreToolUse"] if any("h3" in h["command"] for h in e["hooks"])], ["Agent|Task"])
        self.assertTrue(all(ok for label, ok, _ in I.check(self.p) if label.startswith("hooks TOKEN SAVER")))


if __name__ == "__main__":
    unittest.main()
