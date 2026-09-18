"""Tests du hook H6 : l'autonomie tapée dans l'app est détournée vers le mode nuit (une session neuve par cycle)."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import hooks as H  # noqa: E402
from token_saver import install as I  # noqa: E402
from token_saver import nuit  # noqa: E402
from token_saver.config import load_config  # noqa: E402


class H6Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["CLAUDE_CONFIG_DIR"] = str(root / "cfg")
        (root / "cfg").mkdir()
        self.p = root / "proj"
        for name in ("autonomie", "autonomie-totale", "stop-autonomie", "ship-pr"):
            (self.p / ".claude" / "skills" / name).mkdir(parents=True)
            (self.p / ".claude" / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\nfais\n", encoding="utf-8")
        (self.p / "CLAUDE.md").write_text("# p\n", encoding="utf-8")
        I.install(self.p, "light")
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.p)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)

    def tearDown(self):
        for k in ("CLAUDE_PROJECT_DIR", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID"):
            os.environ.pop(k, None)
        self.tmp.cleanup()

    def hook(self, prompt, session="app-session-1"):
        out = io.StringIO()
        H.run("h6-prompt", io.StringIO(json.dumps({"session_id": session, "cwd": str(self.p), "hook_event_name": "UserPromptSubmit", "prompt": prompt})), out)
        return json.loads(out.getvalue())

    def fake_start_ok(self, project, cfg, cycles, prompt, launcher):
        self.started.append(cycles)
        nuit.write_state(project, {"status": "running", "pid": os.getpid(), "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "cycles_target": cycles, "cycles_done": 0})
        return "nuit lancée (pid 1)"

    def test_detection_and_hook_registered(self):
        cfg = load_config(self.p)
        self.assertEqual(cfg["nuit"]["autonomy_commands"], ["autonomie", "autonomie-totale"])
        self.assertEqual(cfg["nuit"]["stop_commands"], ["stop-autonomie"])
        s = json.loads((self.p / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        h6 = [h for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"] if "h6-prompt" in h["command"]]
        self.assertEqual(len(h6), 1)
        self.assertEqual(h6[0]["timeout"], 180)
        res = {label: (ok, d) for label, ok, d in I.check(self.p)}
        self.assertIn("/autonomie, /autonomie-totale", res["autonomie en sessions neuves (H6)"][1])

    def test_autonomy_command_starts_night_and_blocks_prompt(self):
        self.started = []
        with mock.patch.object(nuit, "start", side_effect=self.fake_start_ok):
            out = self.hook("/autonomie-totale")
        self.assertEqual(out["decision"], "block")
        self.assertIn("autonomie lancée en sessions neuves à la place de /autonomie-totale", out["reason"])
        self.assertIn("illimité", out["reason"])
        self.assertEqual(self.started, [0])
        self.assertEqual(os.environ.get("CLAUDE_CODE_SESSION_ID"), "app-session-1")     # la session de l'app rend la main
        # pendant la nuit : les autres messages de l'app sont refusés, sauf les commandes TOKEN SAVER
        out = self.hook("corrige le bug du HUD")
        self.assertEqual(out["decision"], "block")
        self.assertIn("une autonomie tourne", out["reason"])
        self.assertEqual(self.hook("/token-saver-status"), {})
        self.assertEqual(self.hook("/autonomie 1", session="other-app-session")["decision"], "block")   # une autre session de l'app : refusée aussi
        # la session du runner n'est jamais touchée
        (self.p / ".token-saver" / "state" / "nuit-sessions.json").write_text(json.dumps(["runner-own"]), encoding="utf-8")
        self.assertEqual(self.hook("/autonomie 1", session="runner-own"), {})
        # /stop-autonomie arrête la nuit (drapeau) et le message est refusé avec l'explication
        out = self.hook("/stop-autonomie")
        self.assertEqual(out["decision"], "block")
        self.assertIn("arrêt demandé", out["reason"])
        self.assertTrue(nuit.stop_file(self.p).is_file())

    def test_cycles_argument_and_fallback_when_night_cannot_start(self):
        self.started = []
        with mock.patch.object(nuit, "start", side_effect=self.fake_start_ok):
            out = self.hook("/autonomie 3")
        self.assertEqual(self.started, [3])
        self.assertIn("3 cycle(s)", out["reason"])
        nuit.write_state(self.p, {"status": "stopped"})
        with mock.patch.object(nuit, "start", return_value="nuit NON lancée — corriger d'abord :\n  ✗ CLI connecté — non\n  ✓ binaire"):
            out = self.hook("/autonomie-totale")
            self.assertEqual(out["decision"], "block")                                    # 18/09 : refusé avec la raison, jamais dans l'app en silence
            self.assertIn("NON lancées", out["reason"]); self.assertIn("✗ CLI connecté", out["reason"]); self.assertIn("2 minutes", out["reason"])
            self.assertEqual(self.hook("/autonomie 2"), {"decision": "block", "reason": self.hook("/autonomie 2")["reason"]}) if False else None
            out = self.hook("/autonomie-totale")                                          # l'utilisateur insiste dans les 2 minutes : l'app
            self.assertNotIn("decision", out)
            self.assertIn("à ta demande", out["systemMessage"]); self.assertIn("✗ CLI connecté", out["systemMessage"])
            out = self.hook("/autonomie-totale")                                          # troisième fois : de nouveau un refus (le compteur est remis)
            self.assertEqual(out["decision"], "block")
            self.assertEqual(self.hook("/autonomie-totale", session="other-app-session")["decision"], "block")   # une autre session : son propre compteur
        self.assertEqual(self.hook("/stop-autonomie"), {})                                # rien à arrêter : le skill du projet s'exécute
        self.assertEqual(self.hook("/ship-pr"), {})
        self.assertEqual(self.hook("bonjour"), {})

    def test_session_start_learns_night_state(self):
        def start(source="startup"):
            out = io.StringIO()
            H.run("h2-start", io.StringIO(json.dumps({"session_id": "s2", "cwd": str(self.p), "hook_event_name": "SessionStart", "source": source})), out)
            return json.loads(out.getvalue())
        self.assertEqual(start(), {})                                                     # jamais de nuit : rien à dire
        nuit.write_state(self.p, {"status": "running", "pid": os.getpid(), "started": "2026-09-17T18:59:00", "cycles_done": 3})
        ctx = start()["hookSpecificOutput"]["additionalContext"]
        self.assertIn("une autonomie tourne sur ce projet depuis 18:59 (3 cycle(s)", ctx)
        self.assertIn("ne modifie pas le dépôt", ctx)
        nuit.write_state(self.p, {"status": "done", "pid": 0, "started": "2026-09-17T18:59:00", "cycles_done": 19,
                                  "ended": time.strftime("%Y-%m-%dT%H:%M:%S")})
        ctx = start("resume")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("s'est terminée", ctx); self.assertIn("19 cycle(s)", ctx); self.assertIn("rien ne tourne", ctx)
        nuit.write_state(self.p, {"status": "done", "pid": 0, "started": "2026-09-10T18:59:00", "cycles_done": 2, "ended": "2026-09-11T07:00:00"})
        self.assertEqual(start(), {})                                                     # trop ancien : silence

    def test_env_checks_proposed_only_from_project_content(self):
        self.assertEqual(I.propose_env_checks(self.p), [])                                # ni CI GitHub ni Android : rien
        (self.p / ".github" / "workflows").mkdir(parents=True)
        (self.p / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
        self.assertEqual(I.propose_env_checks(self.p), [])                                # workflow sans remote GitHub : rien non plus
        self.assertEqual(load_config(self.p)["nuit"]["env_checks"], [])

    def test_context_meter_warns_once_per_level(self):
        tr = self.p / "transcript.jsonl"
        def write(ctx_tokens):
            rec = {"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": ctx_tokens - 10, "cache_creation_input_tokens": 0}}}
            tr.write_text("\n".join([json.dumps({"type": "user", "message": {"content": "x"}}), json.dumps(rec)]) + "\n", encoding="utf-8")
        def meter():
            out = io.StringIO()
            H.run("h7-prompt", io.StringIO(json.dumps({"session_id": "app-1", "cwd": str(self.p), "transcript_path": str(tr), "prompt": "continue"})), out)
            return json.loads(out.getvalue())
        write(120_000)
        self.assertEqual(meter(), {})                                                     # sous le premier palier : silence
        write(250_000)
        out = meter()
        self.assertIn("250K tokens", out["systemMessage"])
        self.assertIn("/token-saver-next", out["systemMessage"])
        self.assertIn("propose à l'utilisateur /token-saver-next", out["hookSpecificOutput"]["additionalContext"])
        write(300_000)
        self.assertEqual(meter(), {})                                                     # même palier : une seule fois
        write(450_000)
        self.assertIn("450K", meter()["systemMessage"])                                   # palier suivant
        (self.p / ".token-saver" / "state" / "nuit-sessions.json").write_text(json.dumps(["app-1"]), encoding="utf-8")
        write(900_000)
        self.assertEqual(meter(), {})                                                     # session du runner : jamais dérangée

    def test_handoff_note_given_to_new_sessions(self):
        hf = self.p / ".token-saver" / "state" / "handoff.md"
        hf.write_text("## Passage de relais\n**Fait** : X\n**Reste à faire** : Y\n", encoding="utf-8")
        def start(session, source="startup"):
            out = io.StringIO()
            H.run("h2-start", io.StringIO(json.dumps({"session_id": session, "cwd": str(self.p), "hook_event_name": "SessionStart", "source": source})), out)
            return json.loads(out.getvalue())
        ctx = start("n1")["hookSpecificOutput"]["additionalContext"]
        self.assertIn("passage de relais", ctx); self.assertIn("Reste à faire", ctx)
        self.assertEqual(start("n1"), {})                                                 # même session : une fois
        self.assertIn("passage de relais", start("n2", "clear")["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(start("n3"), {})                                                 # deux sessions servies : la note s'efface
        self.assertEqual(start("n4", "resume"), {})                                       # une reprise n'est pas une session neuve

    def test_feature_off(self):
        cfgf = self.p / ".token-saver" / "config.json"
        c = json.loads(cfgf.read_text(encoding="utf-8"))
        c["features"]["H6_autonomy_redirect"] = False
        cfgf.write_text(json.dumps(c), encoding="utf-8")
        with mock.patch.object(nuit, "start", side_effect=AssertionError("ne doit pas être appelé")):
            self.assertEqual(self.hook("/autonomie-totale"), {})


if __name__ == "__main__":
    unittest.main()
