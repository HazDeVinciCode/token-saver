"""Tests du runner `nuit` avec un faux claude (aucun appel réel)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from token_saver import nuit  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_claude.py")


class NuitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "proj"
        (self.p / ".token-saver" / "state").mkdir(parents=True)
        self.script = Path(self.tmp.name) / "scenario.txt"
        os.environ["FAKE_CLAUDE_SCRIPT"] = str(self.script)
        os.environ["CLAUDE_CODE_SESSION_ID"] = "app-session"     # variable de l'app : ne doit pas atteindre le CLI
        self.cfg = {"nuit": {"claude_cmd": [sys.executable, FAKE], "model": "opus[1m]", "pause_between_s": 0,
                             "cycle_timeout_min": 1, "max_hours": 1, "retry_wait_min": 1, "prompt": "/autonomie 1"}}

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_SCRIPT", None)
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        self.tmp.cleanup()

    def calls(self):
        f = Path(str(self.script) + ".calls")
        return [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []

    def test_three_cycles_fresh_sessions(self):
        self.script.write_text("ok\nok\nok\n", encoding="utf-8")
        rc = nuit.run(self.p, self.cfg, cycles=3)
        self.assertEqual(rc, 0)
        st = nuit.read_state(self.p)
        self.assertEqual((st["status"], st["cycles_done"]), ("done", 3))
        calls = self.calls()
        self.assertEqual(len(calls), 3)
        a = calls[0]["argv"]
        self.assertIn("-p", a); self.assertIn("--output-format", a); self.assertIn("json", a)
        self.assertIn("--permission-mode", a); self.assertIn("auto", a); self.assertIn("--permission-prompts", a); self.assertIn("none", a)
        self.assertIn("--model", a); self.assertIn("opus[1m]", a)
        self.assertTrue(a[a.index("-p") + 1].startswith("/autonomie 1"))
        self.assertIn("cycle 2", calls[1]["argv"][calls[1]["argv"].index("-p") + 1])
        self.assertFalse(calls[0]["env_has_app_vars"])                                  # env de l'app filtré
        self.assertEqual(Path(calls[0]["cwd"]).resolve(), self.p.resolve())
        rows = [json.loads(l) for l in nuit.cycles_file(self.p).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["kind"] for r in rows], ["ok", "ok", "ok"])
        self.assertEqual(rows[0]["cache_read"], 900000)
        self.assertIn("3 cycle(s)", nuit.status(self.p))

    def test_bilan_written_at_end_and_compared_next_time(self):
        self.script.write_text("ok\nok\nok\nok\nok\n", encoding="utf-8")
        nuit.run(self.p, self.cfg, cycles=3)
        rows = [json.loads(l) for l in nuit.bilans_file(self.p).read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual(len(rows), 1)
        b = rows[0]
        self.assertEqual((b["cycles_ok"], b["cycles_failed"], b["status"]), (3, 0, "done"))
        self.assertEqual(b["tokens_read"], 3 * (100 + 900000 + 50000))                    # repli : usages des cycles (pas de transcript)
        self.assertIn("résultats des cycles", b["source"])
        self.assertIsNone(b["previous"])
        md = nuit.bilan_md(self.p).read_text(encoding="utf-8")
        self.assertTrue(md.startswith("BILAN DE L'AUTONOMIE"))
        self.assertIn("3 cycle(s)", md)
        self.assertIn("BILAN DE L'AUTONOMIE", nuit.status(self.p))                            # la nuit finie montre son bilan
        time.sleep(1.1)                                                                    # une autre nuit, une autre seconde
        nuit.run(self.p, self.cfg, cycles=2)                                               # sessions fake-3 et fake-4 : distinctes de la première nuit
        rows = [json.loads(l) for l in nuit.bilans_file(self.p).read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertEqual([r["cycles_ok"] for r in rows], [3, 2])
        self.assertEqual(rows[1]["previous"]["cycles_ok"], 3)
        self.assertIn("vs la précédente", nuit.bilan_md(self.p).read_text(encoding="utf-8"))

    def test_end_time_and_until(self):
        t0 = time.mktime((2026, 9, 17, 19, 0, 0, 0, 0, -1))
        self.assertEqual(time.localtime(nuit.end_time("08:00", t0))[:5], (2026, 9, 18, 8, 0))          # le lendemain
        self.assertEqual(time.localtime(nuit.end_time("23h30", t0))[:5], (2026, 9, 17, 23, 30))        # le soir même
        self.assertIsNone(nuit.end_time("8h60", t0)); self.assertIsNone(nuit.end_time("bientôt", t0)); self.assertIsNone(nuit.end_time(None, t0))
        self.script.write_text("ok\nok\n", encoding="utf-8")
        real = nuit.end_time
        nuit.end_time = lambda end_at, t: t - 1                                            # heure de fin déjà passée
        try:
            rc = nuit.run(self.p, self.cfg, cycles=0, until="08:00")
        finally:
            nuit.end_time = real
        self.assertEqual(rc, 0)
        st = nuit.read_state(self.p)
        self.assertEqual((st["status"], st["cycles_done"], st["end_at"]), ("done", 0, "08:00"))
        self.assertIn("heure de fin atteinte (08:00)", nuit.log_file(self.p).read_text(encoding="utf-8"))
        self.assertEqual(self.calls(), [])

    def test_unfinished_cycle_is_told_to_the_next_one(self):
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(self.p), check=True)
        (self.p / "brouillon.txt").write_text("en cours\n", encoding="utf-8")            # fichier laissé non commité
        self.script.write_text("wait\nok\n", encoding="utf-8")
        nuit.run(self.p, self.cfg, cycles=2)
        calls = self.calls()
        p2 = calls[1]["argv"][calls[1]["argv"].index("-p") + 1]
        self.assertIn("ATTENTION : le cycle précédent (1) semble inachevé", p2)
        self.assertIn("attente", p2)
        self.assertIn("1 fichier(s) modifié(s) ou nouveaux non commités", p2)
        self.assertIn("Reprends ce travail", p2)
        self.assertIn("trace d'inachevé", nuit.log_file(self.p).read_text(encoding="utf-8"))
        p1 = calls[0]["argv"][calls[0]["argv"].index("-p") + 1]
        self.assertNotIn("ATTENTION", p1)

    def test_env_checks_warn_or_block(self):
        py = sys.executable
        self.cfg["nuit"]["env_checks"] = [
            {"name": "présent", "command": f'"{py}" -c "print(\'device ok\')"', "expect": "regex:device"},
            {"name": "absent", "command": f'"{py}" -c "import sys; print(\'rien\'); sys.exit(1)"'}]
        m = nuit.auth_marker(self.p); m.parent.mkdir(parents=True, exist_ok=True); m.write_text("ok", encoding="utf-8")   # pas de ping
        (self.p / ".claude" / "skills" / "autonomie").mkdir(parents=True)
        (self.p / ".claude" / "skills" / "autonomie" / "SKILL.md").write_text("un cycle\n", encoding="utf-8")            # commande de cycle présente
        res = {label: (ok, d) for label, ok, d in nuit.check(self.p, self.cfg, do_ping=False)}
        self.assertEqual(res["environnement : présent"], (True, "device ok"))
        self.assertTrue(res["environnement : absent"][0])                                 # avertissement : ne bloque pas
        self.assertIn("⚠", res["environnement : absent"][1])
        msg = nuit.start(self.p, self.cfg, 1, None, [py, "-c", "pass"])
        self.assertIn("autonomie lancée", msg)
        self.assertIn("⚠ absent : rien", msg)
        self.assertNotIn("⚠ présent", msg)
        nuit.write_state(self.p, {"status": "done"})
        self.cfg["nuit"]["env_checks"][1]["required"] = True
        msg = nuit.start(self.p, self.cfg, 1, None, [py, "-c", "pass"])
        self.assertIn("NON lancée", msg)
        self.assertIn("requis", msg)

    def test_auth_error_stops_with_help(self):
        self.script.write_text("auth\n", encoding="utf-8")
        rc = nuit.run(self.p, self.cfg, cycles=2)
        self.assertEqual(rc, 1)
        st = nuit.read_state(self.p)
        self.assertEqual((st["status"], st["error"]), ("error", "auth"))
        self.assertIn("/login", nuit.log_file(self.p).read_text(encoding="utf-8"))
        self.assertEqual(len(self.calls()), 1)

    def test_limit_waits_then_continues(self):
        self.script.write_text("limit\nok\n", encoding="utf-8")
        nuit.time.sleep = lambda s: None                                                 # pas d'attente réelle
        try:
            rc = nuit.run(self.p, self.cfg, cycles=1)
        finally:
            nuit.time.sleep = time.sleep
        self.assertEqual(rc, 0)
        self.assertEqual(nuit.read_state(self.p)["cycles_done"], 1)
        self.assertIn("limite de quota", nuit.log_file(self.p).read_text(encoding="utf-8"))
        self.assertEqual(len(self.calls()), 2)

    def test_model_variant_fallback(self):
        self.script.write_text("model\nmodel\n", encoding="utf-8")
        rc = nuit.run(self.p, self.cfg, cycles=1)
        self.assertEqual(rc, 0)
        calls = self.calls()
        self.assertIn("--model", calls[0]["argv"]); self.assertNotIn("--model", calls[1]["argv"])

    def test_error_twice_stops(self):
        self.script.write_text("error\nerror\n", encoding="utf-8")
        nuit.time.sleep = lambda s: None
        try:
            rc = nuit.run(self.p, self.cfg, cycles=3)
        finally:
            nuit.time.sleep = time.sleep
        self.assertEqual(rc, 1)
        self.assertEqual(nuit.read_state(self.p)["status"], "error")

    def test_stop_flag_ends_cleanly(self):
        import threading
        self.script.write_text("ok\nok\nok\n", encoding="utf-8")
        threading.Timer(0.3, lambda: nuit.stop(self.p)).start()                        # drapeau posé pendant la boucle
        rc = nuit.run(self.p, self.cfg, cycles=0)
        self.assertEqual(rc, 0)
        st = nuit.read_state(self.p)
        self.assertEqual(st["status"], "stopped")
        self.assertGreaterEqual(st["cycles_done"], 1)

    def test_max_hours_ends(self):
        self.script.write_text("ok\n", encoding="utf-8")
        cfg = {"nuit": {**self.cfg["nuit"], "max_hours": 0.0001}}                       # 0,36 s
        rc = nuit.run(self.p, cfg, cycles=0)
        self.assertEqual(rc, 0)
        self.assertEqual(nuit.read_state(self.p)["status"], "done")

    def test_parse_reset(self):
        t = nuit.parse_reset("You've hit your session limit · resets 12:30am (Europe/Paris)")
        self.assertIsNotNone(t); self.assertGreater(t, time.time())
        self.assertIsNone(nuit.parse_reset("rien à voir"))

    def test_find_claude_from_config(self):
        self.assertEqual(nuit.find_claude({"claude_cmd": ["x", "y"]}), ["x", "y"])

    def test_check_and_start_refuse_when_not_logged_in(self):
        (self.p / ".claude" / "skills" / "autonomie").mkdir(parents=True)
        (self.p / ".claude" / "skills" / "autonomie" / "SKILL.md").write_text("---\nname: autonomie\n---\n", encoding="utf-8")
        self.script.write_text("auth\nauth\n", encoding="utf-8")                        # un ping pour check, un pour start
        res = nuit.check(self.p, self.cfg, do_ping=True)
        labels = {l: ok for l, ok, _ in res}
        self.assertTrue(labels["binaire claude trouvé"])
        self.assertTrue(labels["recette de cycle disponible"])
        self.assertFalse(labels["CLI connecté (appel de test, 1 tour)"])
        msg = nuit.start(self.p, self.cfg, 1, None, [sys.executable, "-c", "pass"])
        self.assertIn("NON lancée", msg)
        self.assertIn("/login", msg)
        self.assertFalse(nuit.auth_marker(self.p).exists())

    def test_check_refuses_when_app_session_is_active(self):
        from token_saver.paths import encode_project_dir
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self.tmp.name) / "cfg")
        try:
            tdir = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / encode_project_dir(self.p.resolve())
            tdir.mkdir(parents=True)
            (tdir / "other-session.jsonl").write_text("{}\n", encoding="utf-8")        # écrit à l'instant = session active
            res = nuit.check(self.p, self.cfg, do_ping=False)
            item = next(r for r in res if r[0].startswith("aucune autre session"))
            self.assertFalse(item[1]); self.assertIn("/stop-autonomie", item[2])
            old = time.time() - 3600
            os.utime(tdir / "other-session.jsonl", (old, old))                           # silence d'une heure = libre
            res = nuit.check(self.p, self.cfg, do_ping=False)
            self.assertTrue(next(r for r in res if r[0].startswith("aucune autre session"))[1])
            (tdir / "launcher.jsonl").write_text("{}\n", encoding="utf-8")             # la session qui lance la nuit n'est pas « une autre »
            os.environ["CLAUDE_CODE_SESSION_ID"] = "launcher"
            res = nuit.check(self.p, self.cfg, do_ping=False)
            self.assertTrue(next(r for r in res if r[0].startswith("aucune autre session"))[1])
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            os.environ["CLAUDE_CODE_SESSION_ID"] = "app-session"

    def test_check_ok_sets_auth_marker(self):
        (self.p / ".claude" / "skills" / "autonomie").mkdir(parents=True)
        (self.p / ".claude" / "skills" / "autonomie" / "SKILL.md").write_text("---\nname: autonomie\n---\n", encoding="utf-8")
        self.script.write_text("ok\n", encoding="utf-8")
        res = nuit.check(self.p, self.cfg, do_ping=True)
        self.assertTrue(all(ok for _, ok, _ in res), res)
        self.assertTrue(nuit.auth_marker(self.p).exists())


if __name__ == "__main__":
    unittest.main()
