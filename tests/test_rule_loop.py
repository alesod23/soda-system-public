#!/usr/bin/env python3
"""End-to-end test of the rule loop, offline (stdlib unittest; node 18+ for the hub).

    python3 tests/test_rule_loop.py

A temporary SODA_HOME is filled from examples/, the model is stubbed (SODA_LLM_STUB), the hub runs on a free port with
no Telegram token. Nothing outside the temp dir is written.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class RuleLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="soda-test-"))
        home = cls.tmp / "data"
        (home / "ledgers").mkdir(parents=True)
        (home / "skill-maps").mkdir()
        (home / "hub").mkdir()
        for f in (REPO / "examples" / "ledgers").glob("*.example.md"):
            shutil.copy(f, home / "ledgers" / f.name.replace(".example.md", ".md"))
        for f in (REPO / "examples" / "skill-maps").glob("*.json"):
            shutil.copy(f, home / "skill-maps" / f.name)
        shutil.copy(REPO / "hub" / "hub-rules.example.json", home / "hub" / "hub-rules.json")
        shutil.copy(REPO / "hub" / "executors.example.json", home / "hub" / "executors.json")
        cls.port = free_port()
        cls.env = dict(os.environ, SODA_HOME=str(home), SODA_SKILLS_DIR=str(home / "skills"), HUB_PORT=str(cls.port),
                       HUB_URL=f"http://127.0.0.1:{cls.port}", TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="")
        cls.home = home
        cls.hub = subprocess.Popen(["node", str(REPO / "hub" / "server.js")], env=cls.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for _ in range(50):
            try:
                cls.call("GET", "/pending"); break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.hub.terminate(); cls.hub.wait(5)
        if cls.hub.stdout:
            cls.hub.stdout.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def call(cls, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())

    def run_py(self, *args, env=None):
        r = subprocess.run([PY] + [str(a) for a in args], env=env or self.env, capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_1_compile_all(self):
        for key in ("hub", "notif", "crm", "email"):
            rc, out = self.run_py(REPO / "rules" / "compile_skill.py", "--contract", key, "--install")
            self.assertEqual(rc, 0, out)
        self.assertTrue((self.home / "skills" / "hub" / "SKILL.md").exists())

    def test_2_card_guard_idem_supersede(self):
        a = self.call("POST", "/pending", {"text": "Send the revised quote to the supplier?", "kind": "decision",
                                           "action": {"type": "echo", "text": "hello"}, "idem": "quote:supplier"})
        self.assertTrue(a["ok"])
        b = self.call("POST", "/pending", {"text": "Send the revised quote (v2) to the supplier?", "idem": "quote:supplier"})
        self.assertTrue(b.get("updated"))
        self.assertEqual(a["id"], b["id"])                     # one subject, one card
        g = self.call("POST", "/pending", {"text": "Confirm the calendar change as you asked earlier?"})
        self.assertEqual(g["guard"], "downgrade")              # filed pattern: told, not asked
        self.assertEqual(self.call("GET", f"/item/{g['id']}")["kind"], "update")
        r = self.call("POST", "/resolve", {"id": a["id"], "verdict": "yes", "device": "test"})
        self.assertEqual(r["verdict"], "yes")
        for _ in range(50):                                    # the executor runs asynchronously
            it = self.call("GET", f"/item/{a['id']}")
            if it.get("action_result"):
                break
            time.sleep(0.1)
        self.assertTrue(it["action_result"]["ok"], it.get("action_result"))
        decisions = (self.home / "decisions.jsonl").read_text().splitlines()
        self.assertTrue(any(json.loads(l)["item"] == a["id"] for l in decisions))

    def test_3_rejection_becomes_a_rule(self):
        c = self.call("POST", "/pending", {"text": "Reply to the vendor and the landlord?", "kind": "decision"})
        self.call("POST", "/resolve", {"id": c["id"], "verdict": "no", "device": "test",
                                       "feedback": "never mix two different people in one card"})
        q = self.call("GET", "/feedback?status=open")["entries"]
        self.assertEqual(len(q), 1)
        stub = self.tmp / "stub.json"
        stub.write_text(json.dumps({"class": "rule", "contract": "hub", "rule": "A card never mixes two people.",
                                    "soft": False, "answer": "a new hub rule"}))
        env = dict(self.env, SODA_LLM_STUB=str(stub))
        ledger = self.home / "ledgers" / "HUB-CARD-CONTRACT.md"
        before = ledger.read_text().count("| H")
        rc, out = self.run_py(REPO / "rules" / "feedback_session.py", env=env)
        self.assertEqual(rc, 0, out)
        text = ledger.read_text()
        self.assertEqual(text.count("| H"), before + 1)
        self.assertIn('*"never mix two different people in one card"*', text)
        self.assertIn(f"[item: {c['id']}]", text)
        skill = (self.home / "skills" / "hub" / "SKILL.md").read_text()
        self.assertIn("A card never mixes two people.", skill)  # unmapped HARD row: in front at once, verbatim
        self.assertEqual(self.call("GET", "/feedback?status=open")["entries"], [])
        told = [i for i in self.call("GET", "/pending")["items"] if (i.get("meta") or {}).get("origin") == "feedback-session"]
        self.assertEqual(len(told), 1)                          # ONE told card per run

    def test_4_like_and_confirmation(self):
        stub = self.tmp / "stub2.json"
        stub.write_text(json.dumps({"class": "confirmation", "contract": "notif", "duplicate_of": "H1", "answer": "H1 says it"}))
        env = dict(self.env, SODA_LLM_STUB=str(stub))
        rc, out = self.run_py(REPO / "rules" / "ledger_verdict.py", "file", "--surface", "notif",
                              "--quote", "say who wrote it, always", env=env)
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.loads(out)["class"], "confirmation")
        conf = (self.home / "rule-confirmations.jsonl").read_text()
        self.assertIn('"notif:H1"', conf)


if __name__ == "__main__":
    unittest.main(verbosity=2)
