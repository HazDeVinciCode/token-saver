"""Tests du conseiller d'agents (R5, R19, R20) sur une base synthétique : inutilisés, lourds, candidats à un modèle moins cher."""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import agents as A  # noqa: E402
from token_saver.config import load_config  # noqa: E402
from token_saver.db import connect  # noqa: E402
from token_saver.doctor import run as doctor_run  # noqa: E402

NOW = time.strftime("%Y-%m-%dT%H:%M:%S")
OLD = "2026-07-01T10:00:00"


class AgentsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "proj"
        d = self.p / ".claude" / "agents"
        d.mkdir(parents=True)
        (self.p / "CLAUDE.md").write_text("# p\n", encoding="utf-8")
        (d / "coder.md").write_text("---\nname: coder\ndescription: >\n  Écrit le code du projet,\n  avec ses tests.\nmodel: opus\n---\nTu codes.\n", encoding="utf-8")
        (d / "director.md").write_text("---\nname: director\ndescription: Tranche les questions de design, ne code pas.\nmodel: opus\n---\n", encoding="utf-8")
        (d / "vieil-agent.md").write_text("---\nname: vieil-agent\ndescription: Servait au début.\nmodel: sonnet\n---\n", encoding="utf-8")
        (d / "jamais.md").write_text("---\nname: jamais\ndescription: Jamais lancé, description longue " + "x" * 400 + "\n---\n", encoding="utf-8")
        self.con = connect(None)
        self.seq = 0

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def launch(self, agent_type: str, agent_id: str, ts: str, calls: int, ctx_each: int, model: str, edits: int = 0, reads: int = 3):
        self.seq += 1
        tid = f"tu{self.seq}"
        self.con.execute("INSERT INTO tool_uses(tool_use_id, file, session_id, agent_id, call_seq, ts, name, arg, input_chars, result_chars, is_error, extra) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (tid, "main.jsonl", "S", None, 1, ts, "Agent", agent_type, 3000, 100, 0, "{}"))
        self.con.execute("INSERT OR REPLACE INTO agents VALUES(?,?,?,?,?,?)", (agent_id, "S", agent_type, model, "desc", tid))
        for i in range(calls):
            self.seq += 1
            self.con.execute("INSERT INTO calls(file, session_id, agent_id, message_id, request_id, seq, ts, model, input, cache_read, cache_write, output, thinking, ctx) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (f"{agent_id}.jsonl", "S", agent_id, f"m{self.seq}", f"r{self.seq}", i + 1, ts, model,
                                                                   100, ctx_each - 100, 0, 500, 50, ctx_each))
        for i in range(edits):
            self.seq += 1
            self.con.execute("INSERT INTO tool_uses(tool_use_id, file, session_id, agent_id, call_seq, ts, name, is_error) VALUES(?,?,?,?,?,?,?,?)",
                             (f"tu{self.seq}", f"{agent_id}.jsonl", "S", agent_id, 1, ts, "Edit", 0))
        for i in range(reads):
            self.seq += 1
            self.con.execute("INSERT INTO tool_uses(tool_use_id, file, session_id, agent_id, call_seq, ts, name, is_error) VALUES(?,?,?,?,?,?,?,?)",
                             (f"tu{self.seq}", f"{agent_id}.jsonl", "S", agent_id, 1, ts, "Read", 0))

    def main_calls(self, n: int):
        for i in range(n):
            self.seq += 1
            self.con.execute("INSERT INTO calls(file, session_id, agent_id, message_id, request_id, seq, ts, model, input, cache_read, cache_write, output, thinking, ctx) "
                             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("main.jsonl", "S", None, f"m{self.seq}", f"r{self.seq}", i + 1, NOW, "claude-opus-5",
                                                                   100, 50000, 0, 300, 20, 50100))

    def test_frontmatter_block_scalars(self):
        fm = A.frontmatter("---\nname: qa\ndescription: >\n  Ligne un,\n  ligne deux.\ntools: Read, Grep\nmodel: sonnet\n---\ncorps\n")
        self.assertEqual(fm, {"name": "qa", "description": "Ligne un, ligne deux.", "tools": "Read, Grep", "model": "sonnet"})
        self.assertEqual(A.frontmatter("pas de frontmatter"), {})
        names = {a["name"]: a for a in A.project_agents(self.p)}
        self.assertEqual(names["coder"]["description"], "Écrit le code du projet, avec ses tests.")
        self.assertEqual(names["coder"]["model"], "opus")

    def test_unused_heavy_and_cheaper_model_candidates(self):
        self.main_calls(200)
        for i in range(4):                                                             # coder : Opus, écrit du code (2 éditions / lancement), lourd
            self.launch("coder", f"c{i}", NOW, calls=10, ctx_each=220_000, model="claude-opus-5", edits=2, reads=5)
        for i in range(5):                                                             # director : Opus, ne code pas (0,2 édition / lancement), léger
            self.launch("director", f"d{i}", NOW, calls=4, ctx_each=40_000, model="claude-opus-5", edits=1 if i == 0 else 0, reads=6)
        self.launch("vieil-agent", "v0", OLD, calls=3, ctx_each=30_000, model="claude-sonnet-5")    # plus lancé depuis longtemps
        self.launch("general-purpose", "g0", NOW, calls=5, ctx_each=90_000, model="claude-opus-5")   # agent intégré : dans le tableau, jamais candidat
        cfg = load_config(self.p)
        F = {f["id"]: f for f in A.advise(self.con, self.p, cfg, since=None)}
        self.assertEqual(F["R19"]["level"], "LOW")
        self.assertEqual(F["R19"]["data"]["unused"], ["jamais", "vieil-agent"])
        self.assertIn("jamais (jamais lancé)", F["R19"]["why"]); self.assertIn("vieil-agent (dernier lancement il y a", F["R19"]["why"])
        self.assertIn("× 200 appels", F["R19"]["impact"]); self.assertEqual(F["R19"]["registry"], "ESTIMÉ")
        self.assertEqual(F["R20"]["level"], "MEDIUM")
        self.assertEqual(F["R20"]["data"]["heavy"], ["coder"])
        self.assertIn("coder (contexte moyen 220K, 4 lancements, 8.8M relus)", F["R20"]["why"])
        self.assertIn("director : 5 lancement(s), 160K tokens relus par lancement (4.0 appels, contexte moyen 40K), opus", F["R20"]["why"])
        self.assertEqual(F["R20"]["registry"], "MESURÉ")
        self.assertEqual(F["R5"]["data"]["agents"], ["director"])                       # coder écrit du code : pas candidat ; general-purpose : intégré
        self.assertIn("0.2 édition(s) de fichier par lancement", F["R5"]["why"])
        self.assertEqual(F["R5"]["registry"], "ESTIMÉ")
        table = A.render_table(self.con, self.p, None)
        self.assertIn("coder", table); self.assertIn("(agent intégré, pas dans .claude/agents)", table)
        rep = doctor_run(self.con, self.p, cfg, "all")                                  # intégré au doctor
        self.assertEqual({f["id"] for f in rep["findings"]} & {"R5", "R19", "R20"}, {"R5", "R19", "R20"})

    def test_quiet_when_everything_is_fine(self):
        self.main_calls(10)
        for n in ("coder", "director", "vieil-agent", "jamais"):
            self.launch(n, f"{n}-0", NOW, calls=3, ctx_each=30_000, model="claude-sonnet-5", edits=2)
        F = {f["id"]: f for f in A.advise(self.con, self.p, load_config(self.p), since=None)}
        self.assertEqual(F["R19"]["level"], "INFO"); self.assertEqual(F["R20"]["level"], "INFO"); self.assertNotIn("R5", F)
        self.assertEqual(A.advise(connect(None), self.p, load_config(self.p), since=None)[0]["id"], "R19")   # base vide : les 4 agents jamais lancés


if __name__ == "__main__":
    unittest.main()
