"""Tests du collecteur sur un transcript synthétique (aucune donnée réelle)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver.collect import collect  # noqa: E402
from token_saver.db import connect  # noqa: E402
from token_saver.metrics import attribution, reads, snapshot, totals  # noqa: E402
from token_saver.paths import encode_project_dir  # noqa: E402
from token_saver.config import load_config  # noqa: E402


def _usage(i, cr, cw, o, th=0):
    return {"input_tokens": i, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw, "output_tokens": o,
            "cache_creation": {"ephemeral_1h_input_tokens": cw, "ephemeral_5m_input_tokens": 0},
            "output_tokens_details": {"thinking_tokens": th}}


def _assistant(mid, rid, ts, block, usage, model="claude-opus-5"):
    return {"type": "assistant", "sessionId": "S1", "requestId": rid, "timestamp": ts, "cwd": "X", "version": "2.1.260",
            "message": {"id": mid, "model": model, "role": "assistant", "content": [block], "usage": usage}}


def _user(ts, content, **extra):
    d = {"type": "user", "sessionId": "S1", "timestamp": ts, "message": {"role": "user", "content": content}}
    d.update(extra)
    return d


def _read_result(tid, path, start, num, total, text="x" * 4000):
    return _user("2026-09-01T10:00:03", [{"type": "tool_result", "tool_use_id": tid, "content": text}],
                 toolUseResult={"type": "text", "file": {"filePath": path, "startLine": start, "numLines": num, "totalLines": total}})


def build_transcript() -> list[dict]:
    L = []
    L.append(_user("2026-09-01T10:00:00", "Corrige le bug"))
    # appel 1 : texte + tool_use Read (deux lignes, même message id -> un seul appel)
    L.append(_assistant("m1", "r1", "2026-09-01T10:00:01", {"type": "text", "text": "Je lis le fichier."}, _usage(100, 20000, 5000, 50)))
    L.append(_assistant("m1", "r1", "2026-09-01T10:00:01", {"type": "tool_use", "id": "t1", "name": "Read",
                                                              "input": {"file_path": "D:\\proj\\a.py"}}, _usage(100, 20000, 5000, 50)))
    L.append(_read_result("t1", "D:\\proj\\a.py", 1, 100, 100))
    # appel 2 : relecture identique du même fichier -> redondante
    L.append(_assistant("m2", "r2", "2026-09-01T10:00:04", {"type": "tool_use", "id": "t2", "name": "Read",
                                                              "input": {"file_path": "D:\\proj\\a.py"}}, _usage(0, 26000, 1200, 40, th=10)))
    L.append(_read_result("t2", "D:\\proj\\a.py", 1, 100, 100))
    # compaction (system) puis appel 3 avec petit contexte
    L.append({"type": "system", "subtype": "compact_boundary", "sessionId": "S1", "timestamp": "2026-09-01T10:00:05",
              "compactMetadata": {"trigger": "auto", "preTokens": 27000, "postTokens": 3000, "durationMs": 1000}})
    L.append(_user("2026-09-01T10:00:06", "This session is being continued from a previous conversation...", isCompactSummary=True))
    L.append(_assistant("m3", "r3", "2026-09-01T10:00:07", {"type": "tool_use", "id": "t3", "name": "Read",
                                                              "input": {"file_path": "D:\\proj\\a.py"}}, _usage(0, 0, 3500, 30)))
    L.append(_read_result("t3", "D:\\proj\\a.py", 1, 100, 100))   # pas redondante : compaction entre
    # appel 4 : changement de modèle + rejet de quota + nouveau tour utilisateur
    L.append(_user("2026-09-01T10:00:08", "Continue"))
    L.append(_assistant("m4", "r4", "2026-09-01T10:00:09", {"type": "text", "text": "ok"}, _usage(0, 4500, 200, 10),
                        model="claude-sonnet-5"))
    L[-1]["quotaLimits"] = {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1}
    return L


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.project = root / "proj"
        self.project.mkdir()
        (self.project / ".claude").mkdir()
        cfg_dir = root / "claude-config"
        self.tdir = cfg_dir / "projects" / encode_project_dir(self.project.resolve())
        (self.tdir / "S1" / "subagents").mkdir(parents=True)
        os.environ["CLAUDE_CONFIG_DIR"] = str(cfg_dir)
        self.main = self.tdir / "S1.jsonl"
        with open(self.main, "w", encoding="utf-8") as f:
            for line in build_transcript():
                f.write(json.dumps(line) + "\n")
        sub = self.tdir / "S1" / "subagents" / "agent-abc.jsonl"
        with open(sub, "w", encoding="utf-8") as f:
            f.write(json.dumps(_user("2026-09-01T10:01:00", "tâche", agentId="abc", isSidechain=True)) + "\n")
            a = _assistant("s1", "sr1", "2026-09-01T10:01:01", {"type": "text", "text": "fait"}, _usage(0, 15000, 6000, 20))
            a["agentId"] = "abc"
            f.write(json.dumps(a) + "\n")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_calls_and_metrics(self):
        con = connect(None)
        stats = collect(con, self.project)
        self.assertEqual(stats["calls"], 5)                       # 4 principaux + 1 sous-agent (dédup m1)
        t = totals(con, None)
        self.assertEqual(t["main"]["calls"], 4)
        self.assertEqual(t["subagent"]["calls"], 1)
        self.assertEqual(t["all"]["ctx"], 25100 + 27200 + 3500 + 4700 + 21000)
        self.assertEqual(t["all"]["thinking"], 10)
        r = reads(con, None)
        self.assertEqual(r["total"], 3)
        self.assertEqual(r["redundant"], 1)                       # t2 seulement (t3 après compaction)
        ev = snapshot(con, "all", load_config(None))["events"]
        self.assertEqual(ev["compact"], 1)
        self.assertEqual(ev["limit_hit"], 1)
        self.assertEqual(ev["model_switch"], 1)
        self.assertEqual(ev["compact_detail"]["pre_avg"], 27000)
        turns = con.execute("SELECT COUNT(*) FROM user_turns WHERE agent_id IS NULL").fetchone()[0]
        self.assertEqual(turns, 2)                                # 'Corrige le bug' et 'Continue' (pas le résumé)
        att = attribution(con, None)
        self.assertGreater(att["shares"].get("startup(main)", 0), 0)
        self.assertIn("post-compaction", att["shares"])

    def test_incremental_and_truncation(self):
        con = connect(None)
        collect(con, self.project)
        with open(self.main, "a", encoding="utf-8") as f:
            f.write(json.dumps(_assistant("m5", "r5", "2026-09-01T10:00:10", {"type": "text", "text": "suite"},
                                          _usage(0, 4800, 100, 5), model="claude-sonnet-5")) + "\n")
        stats = collect(con, self.project)
        self.assertEqual(stats["calls"], 1)
        self.assertEqual(totals(con, None)["main"]["calls"], 5)
        # fichier tronqué -> ré-analyse complète sans doublons
        lines = self.main.read_text(encoding="utf-8").splitlines()[:3]
        self.main.write_text("\n".join(lines) + "\n", encoding="utf-8")
        stats = collect(con, self.project)
        self.assertEqual(stats["reset"], 1)
        self.assertEqual(totals(con, None)["main"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
