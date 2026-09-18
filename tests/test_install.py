"""Tests d'isolation T1-T5 (projets temporaires, CLAUDE_CONFIG_DIR temporaire)."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import time

from token_saver import install as I  # noqa: E402
from token_saver.paths import encode_project_dir  # noqa: E402


def tree(root: Path) -> dict[str, str]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root)).replace("\\", "/")] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["CLAUDE_CONFIG_DIR"] = str(root / "cfg")
        (root / "cfg").mkdir()
        self.A, self.B = root / "A", root / "B"
        for p in (self.A, self.B):
            (p / ".claude").mkdir(parents=True)
            (p / "CLAUDE.md").write_text("# projet\n", encoding="utf-8")
            (p / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["Bash(git *)"]},
                                                                            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}]}}, indent=2),
                                                                encoding="utf-8")
        self.before_A, self.before_B, self.before_cfg = tree(self.A), tree(self.B), tree(root / "cfg")

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def test_T1_install_isolated(self):
        I.install(self.A, "autonome")
        self.assertEqual(tree(self.B), self.before_B)                       # B intact
        self.assertEqual(tree(Path(self.tmp.name) / "cfg"), self.before_cfg)  # rien de global
        s = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(s["autoCompactWindow"], 600000)
        self.assertNotIn("env", s)                                            # aucun plafond de fenêtre
        self.assertEqual(s["permissions"]["allow"], ["Bash(git *)"])        # clés utilisateur intactes
        self.assertEqual(s["hooks"]["PreToolUse"][0]["hooks"][0]["command"], "rtk hook claude")  # hook tiers intact
        self.assertTrue((self.A / ".token-saver" / "manifest.json").is_file())
        self.assertTrue((self.A / ".token-saver" / "bin" / "token-saver.pyz").is_file())
        self.assertEqual((self.A / ".token-saver" / ".gitignore").read_text(), "*\n")

    def test_T2_uninstall_restores(self):
        I.install(self.A, "autonome")
        r = I.uninstall(self.A, purge=True)
        self.assertFalse(r["not_installed"])
        self.assertEqual(tree(self.A), self.before_A)                        # état initial exact (hashes)
        self.assertFalse((self.A / ".token-saver").exists())

    def test_T3_idempotent(self):
        def marked_count():
            s = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
            pre = s.get("hooks", {}).get("PreToolUse", [])
            m = [e for e in pre if any(str(h.get("statusMessage", "")).startswith("token-saver:") for h in e["hooks"])]
            return len(m), len(pre) - len(m)
        I.install(self.A, "light")
        m1, others1 = marked_count()
        for _ in range(2):
            I.install(self.A, "light")
        m3, others3 = marked_count()
        self.assertGreaterEqual(m1, 1)
        self.assertEqual((m1, others1), (m3, others3))                        # aucune duplication
        self.assertEqual(others3, 1)                                          # l'entrée rtk est intacte
        m = json.loads((self.A / ".token-saver" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len([k for k in m["modified"][0]["keys"] if k["path"] == "statusLine"]), 1)

    def test_T4_interrupted_install_recovers(self):
        with self.assertRaises(RuntimeError):
            I.install(self.A, "autonome", fail_after_backup=True)
        self.assertTrue((self.A / ".token-saver" / "manifest.pending.json").is_file())
        msg = I.recover_pending(self.A)
        self.assertIn("restauré", msg)
        s = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertNotIn("autoCompactWindow", s)

    def test_profile_change_removes_our_keys(self):
        I.install(self.A, "autonome")
        s = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(s["autoCompactWindow"], 600000)
        p = I.install(self.A, "light")                                          # profil sans réglage de contexte
        self.assertIn("remove_ours", [k["action"] for k in p["keys"]])
        s = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertNotIn("autoCompactWindow", s)
        self.assertNotIn("env", s)
        self.assertEqual(s["permissions"]["allow"], ["Bash(git *)"])
        I.install(self.A, "autonome")                                          # repart proprement
        self.assertFalse((self.A / ".token-saver" / "manifest.pending.json").exists())

    def test_T5_update_keeps_customization(self):
        I.install(self.A, "light")
        cfg = self.A / ".token-saver" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c["ledger"] = {"ttl_calls": 99}
        cfg.write_text(json.dumps(c), encoding="utf-8")
        I.install(self.A, "light")
        c2 = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertEqual(c2["ledger"]["ttl_calls"], 99)
        self.assertEqual(c2["profile"], "light")

    def test_user_value_kept_without_force(self):
        s = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        s["autoCompactWindow"] = 180000
        (self.A / ".claude" / "settings.local.json").write_text(json.dumps(s), encoding="utf-8")
        p = I.install(self.A, "autonome")
        self.assertEqual([k["action"] for k in p["keys"] if k["path"] == "autoCompactWindow"], ["keep_user"])
        s2 = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(s2["autoCompactWindow"], 180000)
        I.uninstall(self.A, purge=True)
        s3 = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(s3["autoCompactWindow"], 180000)                     # jamais touché

    def test_dry_run_writes_nothing(self):
        p = I.install(self.A, "light", dry_run=True)
        self.assertIn("keys", p)
        self.assertEqual(tree(self.A), self.before_A)

    def test_stale_h5_pending_is_cleaned(self):
        I.install(self.A, "autonome")
        st = self.A / ".token-saver" / "state" / "h5-pending.json"
        sf = self.A / ".claude" / "settings.local.json"
        s = json.loads(sf.read_text(encoding="utf-8")); s["autoCompactWindow"] = 100000
        sf.write_text(json.dumps(s), encoding="utf-8")
        st.write_text(json.dumps({"previous": 600000, "low": 100000}), encoding="utf-8")
        I.install(self.A, "autonome")
        self.assertEqual(json.loads(sf.read_text(encoding="utf-8"))["autoCompactWindow"], 600000)
        self.assertFalse(st.exists())

    def test_check_and_stale_feature_pruned(self):
        I.install(self.A, "autonome")
        cfg = self.A / ".token-saver" / "config.json"
        c = json.loads(cfg.read_text(encoding="utf-8"))
        c["features"]["H5_cycle_compact"] = True                                # résidu d'une ancienne version
        c["h5_cycle_compact"] = {"window_low": 100000}
        cfg.write_text(json.dumps(c), encoding="utf-8")
        bad = [r for r in I.check(self.A) if not r[1]]
        self.assertEqual([r[0] for r in bad], ["features connues uniquement (aucun résidu)"])
        I.install(self.A, "autonome")                                          # réinstallation = purge
        c2 = json.loads(cfg.read_text(encoding="utf-8"))
        self.assertNotIn("H5_cycle_compact", c2["features"])
        self.assertNotIn("h5_cycle_compact", c2)
        self.assertTrue(all(ok for _, ok, _ in I.check(self.A)), I.render_check(I.check(self.A)))

    def test_check_detects_unapplied_context_setting(self):
        I.install(self.A, "autonome")
        ev = self.A / ".token-saver" / "metrics" / "hook-events.jsonl"
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text(json.dumps({"ts": "2000-01-01T00:00:00", "hook": "H2", "action": "reset", "note": "resume"}) + "\n", encoding="utf-8")
        bad = [r for r in I.check(self.A) if not r[1]]
        self.assertEqual([r[0] for r in bad], ["réglage de contexte appliqué à la session en cours"])
        self.assertIn("/autocompact", bad[0][2])
        ev.write_text(json.dumps({"ts": "2999-01-01T00:00:00", "hook": "H2", "action": "reset", "note": "startup"}) + "\n", encoding="utf-8")
        self.assertTrue(all(ok for _, ok, _ in I.check(self.A)), I.render_check(I.check(self.A)))
        I.install(self.A, "autonome")                                          # réinstallation à l'identique : pas de nouvelle date
        self.assertTrue(all(ok for _, ok, _ in I.check(self.A)), I.render_check(I.check(self.A)))
        s = json.loads((self.A / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertTrue(any(e.get("matcher") == "startup|compact|clear|resume" for e in s["hooks"]["SessionStart"]))
        ev.write_text(json.dumps({"ts": "2000-01-01T00:00:00", "hook": "H2", "action": "reset", "note": "resume"}) + "\n", encoding="utf-8")
        I.install(self.A, "light")                                             # réglage retiré : la session ouverte garde l'ancien
        bad = [r for r in I.check(self.A) if not r[1]]
        self.assertEqual([r[0] for r in bad], ["réglage de contexte appliqué à la session en cours"])
        self.assertIn("/autocompact 1m", bad[0][2])
        tdir = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / encode_project_dir(self.A.resolve())   # l'utilisateur tape /autocompact 1m
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "S.jsonl").write_text(json.dumps({"type": "user", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 5)),
                                                  "message": {"role": "user", "content": "<command-message>autocompact</command-message>\n<command-name>/autocompact</command-name>\n<command-args>1m</command-args>"}}) + "\n",
                                      encoding="utf-8")
        res = I.check(self.A)
        self.assertTrue(all(ok for _, ok, _ in res), I.render_check(res))
        self.assertIn("via `/autocompact 1m`", next(d for l, _, d in res if l.startswith("réglage de contexte")))

    def test_small_window_refused(self):
        I.PROFILES["_bad"] = {"settings": {"autoCompactWindow": 150000}, "env": {}, "features": {}}
        try:
            with self.assertRaises(ValueError):
                I.plan(self.A, "_bad")
        finally:
            del I.PROFILES["_bad"]


if __name__ == "__main__":
    unittest.main()
