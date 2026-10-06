#!/usr/bin/env python3
"""Tests of the registry and of one check going through fix -> two-pass proof -> stuck (stdlib unittest, temp dirs):
python3 system-agent/test_registry.py"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="soda-sysagent-test-")
os.environ["SODA_HOME"] = TMP
os.environ["SYSAGENT_REGISTRY"] = str(Path(TMP) / "broken.jsonl")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import registry as R  # noqa: E402
import system_agent as SA  # noqa: E402
import unstuck as U  # noqa: E402

PY = sys.executable
CONF = {"registry_close_after_passes": 2, "escalate_after_runs": 3, "fix_gap_min": 25, "fix_caps": {"command": 1}}


class Registry(unittest.TestCase):
    def setUp(self):
        Path(os.environ["SYSAGENT_REGISTRY"]).unlink(missing_ok=True)
        self.posted = []
        SA.hub = lambda m, p, b=None, timeout=10: (self.posted.append((m, p, b)) or {"ok": True, "id": "card1"})

    def test_newest_line_wins(self):
        R.put({"id": "a", "status": "open", "what": "x"})
        R.put({"id": "a", "status": "fixed"})
        l = R.load()["a"]
        self.assertEqual((l["status"], l["what"]), ("fixed", "x"))
        self.assertIn("nothing is broken", R.render_md())

    def test_for_owner_validation(self):
        self.assertTrue(R.validate_for_owner({"kind": "run", "what": "restart", "argv": ["true"]})[0])
        self.assertFalse(R.validate_for_owner({"kind": "run", "what": "x", "argv": "rm -rf /"})[0])
        self.assertFalse(R.validate_for_owner({"kind": "run", "what": "x", "argv": ["curl", "token=abc123"]})[0])

    def test_fix_then_two_passes_close(self):
        flag = Path(TMP) / "flag"
        flag.unlink(missing_ok=True)
        c = {"key": "demo", "kind": "command", "argv": [PY, "-c", f"import os,sys; sys.exit(0 if os.path.exists({str(flag)!r}) else 1)"],
             "fix": {"kind": "command", "argv": [PY, "-c", f"open({str(flag)!r}, 'w').close()"]}}
        t = datetime.now(timezone.utc)
        r = SA.tick_one(c, CONF, R.load(), t=t)
        self.assertEqual(r.get("fixed_with"), "command")
        r = SA.tick_one(c, CONF, R.load(), t=t + timedelta(minutes=10))
        self.assertEqual(R.load()["SA-demo"]["status"], "verifying")
        r = SA.tick_one(c, CONF, R.load(), t=t + timedelta(minutes=20))
        self.assertEqual(r.get("closed"), "SA-demo")
        self.assertEqual(R.load()["SA-demo"]["status"], "fixed")

    def test_stuck_card_once_and_unstuck_yes(self):
        c = {"key": "down", "kind": "command", "argv": [PY, "-c", "import sys; sys.exit(1)"], "owner_only": True,
             "for_owner": {"kind": "run", "what": "restart it", "argv": [PY, "-c", "print('restarted')"]}}
        t = datetime.now(timezone.utc)
        for i in range(5):
            SA.tick_one(c, CONF, R.load(), t=t + timedelta(minutes=10 * i))
        cards = [p for p in self.posted if p[1] == "/pending"]
        self.assertEqual(len(cards), 1)
        self.assertTrue(cards[0][2]["text"].startswith("STUCK on down"))
        self.assertEqual(R.load()["SA-down"]["status"], "stuck")
        out = U.tick(hub=lambda m, p, b=None, timeout=10: {"resolved": True, "verdict": "yes"})
        self.assertEqual(out[0]["unstuck"]["exit"], 0)
        self.assertIn("restarted", out[0]["unstuck"]["tail"])
        self.assertEqual(U.tick(hub=lambda m, p, b=None, timeout=10: {"resolved": True, "verdict": "yes"}), [])


if __name__ == "__main__":
    unittest.main()
