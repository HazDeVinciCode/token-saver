"""Tests des hooks H1/H2 sur un projet temporaire (payloads Claude Code simulés)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import hooks as H  # noqa: E402


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "proj"
        (self.p / ".token-saver" / "state").mkdir(parents=True)
        (self.p / ".token-saver" / "config.json").write_text(json.dumps(
            {"features": {"H1_read_ledger": True, "H2_compact_reset": True, "H2_brief": True}}), encoding="utf-8")
        self.f = self.p / "src" / "a.cs"
        self.f.parent.mkdir()
        self.f.write_text("\n".join(f"line {i}" for i in range(1, 301)), encoding="utf-8")
        self.transcript = self.p / "transcript.jsonl"
        self.transcript.write_text("", encoding="utf-8")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.p)

    def tearDown(self):
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
        self.tmp.cleanup()

    def run_hook(self, name, payload):
        out = io.StringIO()
        base = {"session_id": "sess1234abcd", "transcript_path": str(self.transcript), "cwd": str(self.p)}
        H.run(name, io.StringIO(json.dumps({**base, **payload})), out)
        return json.loads(out.getvalue())

    def read_payload(self, offset=None, limit=None, response_lines=300):
        inp = {"file_path": str(self.f)}
        if offset:
            inp["offset"] = offset
        if limit:
            inp["limit"] = limit
        content = "\n".join(f"line {i:04d} " + "x" * 34 for i in range(1, response_lines + 1))   # ~3K tokens pour 300 lignes
        return {"tool_name": "Read", "tool_input": inp, "tool_use_id": "t1",
                "tool_response": {"type": "text", "file": {"filePath": str(self.f), "content": content, "numLines": response_lines,
                                                            "startLine": offset or 1, "totalLines": 300}}}

    def test_identical_reread_denied_then_override(self):
        self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})          # 1re lecture : rien
        self.run_hook("h1-post", self.read_payload())
        r = self.run_hook("h1-pre", self.read_payload())                             # relecture identique : refus
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("déjà dans ton contexte", r["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})          # Claude insiste : autorisé
        savings = (self.p / ".token-saver" / "metrics" / "savings.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(savings), 1)
        self.assertEqual(json.loads(savings[0])["registry"], "avoided")
        events = [json.loads(l) for l in (self.p / ".token-saver" / "metrics" / "hook-events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([e["action"] for e in events], ["allow", "log", "deny", "override"])
        self.assertTrue(all(e["latency_ms"] < 2000 for e in events))

    def test_changed_file_and_other_range_allowed(self):
        self.run_hook("h1-post", self.read_payload())
        self.assertEqual(self.run_hook("h1-pre", self.read_payload(offset=250, limit=100)), {})   # hors plage lue
        time.sleep(1.1)
        self.f.write_text("changed", encoding="utf-8")
        self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})                        # fichier modifié

    def test_partial_read_covered(self):
        self.run_hook("h1-post", self.read_payload())                                                # 1-300 lu
        r = self.run_hook("h1-pre", self.read_payload(offset=10, limit=20))                          # sous-plage : refus
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")

    def test_reset_on_session_start_and_precompact(self):
        self.run_hook("h1-post", self.read_payload())
        self.assertEqual(self.run_hook("h2-start", {"source": "clear"}), {})
        self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})                        # ledger vidé
        self.run_hook("h1-post", self.read_payload())
        self.assertEqual(self.run_hook("h2-precompact", {"trigger": "auto"}), {})
        self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})

    def test_brief_after_compaction(self):
        lines = [
            {"type": "user", "message": {"role": "user", "content": "Corrige le test de session"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x1", "name": "Edit",
                                                                                  "input": {"file_path": str(self.f)}}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "x2", "name": "Bash",
                                                                                  "input": {"command": "dotnet test"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x2", "is_error": True,
                                                                        "content": "FAIL"}]}},
        ]
        self.transcript.write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")
        self.run_hook("h1-post", self.read_payload())
        self.run_hook("h2-precompact", {"trigger": "auto"})
        r = self.run_hook("h2-start", {"source": "compact"})
        text = r["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("Corrige le test", text)                       # jamais de consigne périmée réinjectée (EXP-02)
        self.assertIn("PAS une consigne", text)
        self.assertIn("a.cs (×1)", text)
        self.assertIn("dotnet test → ÉCHEC", text)
        self.assertLessEqual(len(text), 1200)
        self.assertEqual(self.run_hook("h2-start", {"source": "resume"}), {})                    # pas de brief hors compaction

    def bash_payload(self, cmd, stdout="x" * 12000):
        return {"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": "b1",
                "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}}

    def test_parse_read_commands(self):
        P = H.parse_read_command
        self.assertEqual(P("cat -n src/a.cs", "D:/p"), (os.path.join("D:/p", "src/a.cs"), 1, None))
        self.assertEqual(P("rtk read D:/p/a.cs", None), ("D:/p/a.cs", 1, None))
        self.assertEqual(P("sed -n '120,220p' src/a.cs", "D:/p")[1:], (120, 220))
        self.assertEqual(P("head -n 40 a.cs", "D:/p")[1:], (1, 40))
        self.assertIsNone(P("cat a.cs | head -5", "D:/p"))          # pipe : pas une lecture pure
        self.assertIsNone(P("grep -n foo a.cs", "D:/p"))
        self.assertIsNone(P("cat a.cs && ls", "D:/p"))

    def test_bash_reread_denied(self):
        rel = os.path.relpath(self.f, self.p).replace("\\", "/")
        cmd = f"cat -n {rel}"
        self.assertEqual(self.run_hook("h1-pre", self.bash_payload(cmd)), {})
        self.run_hook("h1-post", self.bash_payload(cmd))
        r = self.run_hook("h1-pre", self.bash_payload(cmd))                                        # même cat : refus
        self.assertEqual(r["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("sed -n", r["hookSpecificOutput"]["permissionDecisionReason"])
        r2 = self.run_hook("h1-pre", self.bash_payload(f"sed -n '10,20p' {rel}"))                  # sous-plage couverte : refus
        self.assertEqual(r2["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(self.run_hook("h1-pre", self.bash_payload(f"grep -n line {rel}")), {})    # grep : jamais bloqué
        self.assertEqual(self.run_hook("h1-pre", self.read_payload(offset=10, limit=5))["hookSpecificOutput"]["permissionDecision"], "deny")  # Read après cat : couvert

    def test_small_files_and_override_budget(self):
        # fichier petit (< min_tokens) : jamais refusé
        self.run_hook("h1-post", self.read_payload(response_lines=3))
        self.assertEqual(self.run_hook("h1-pre", self.read_payload(response_lines=3)), {})
        # après max_overrides contournements, plus aucun refus dans ce transcript
        for _ in range(2):
            self.run_hook("h1-post", self.read_payload())
            self.assertEqual(self.run_hook("h1-pre", self.read_payload())["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})       # override
        self.run_hook("h1-post", self.read_payload())
        self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})           # budget épuisé : allow

    def test_fail_open(self):
        out = io.StringIO()
        H.run("h1-pre", io.StringIO("not json"), out)
        self.assertEqual(json.loads(out.getvalue()), {})
        out = io.StringIO()
        H.run("unknown", io.StringIO("{}"), out)
        self.assertEqual(json.loads(out.getvalue()), {})

    def test_feature_disabled(self):
        (self.p / ".token-saver" / "config.json").write_text(json.dumps({"features": {"H1_read_ledger": False}}), encoding="utf-8")
        self.run_hook("h1-post", self.read_payload())
        self.assertEqual(self.run_hook("h1-pre", self.read_payload()), {})


if __name__ == "__main__":
    unittest.main()
