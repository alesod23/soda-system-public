#!/usr/bin/env python3
"""End-to-end test of the simulation harness, offline and deterministic (stub agents, stubbed classifier).

    python3 tests/test_sim.py

Two arms (A: no brain, B: brain on) run 2 simulated days each against the real hub/server.js and the real rules/
engine, in a temporary SODA_HOME. Checks: a rule the owner gave on day 1 is in the ledger AND in the compiled skill the
producers read on day 2; the system never sent without a yes; compare.py prints a verdict.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


class Simulation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="soda-sim-test-"))
        cls.env = dict(os.environ, SODA_HOME=str(cls.tmp))
        for k in ("SODA_LLM_STUB", "SODA_LLM_STUB_MAP", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
            cls.env.pop(k, None)
        cls.out = {}
        for arm in ("A", "B"):
            r = subprocess.run([sys.executable, str(REPO / "sim" / "run.py"), "--arm", arm, "--days", "2", "--run", f"t-{arm}",
                                "--stub", "--seed", "7"], env=cls.env, capture_output=True, text=True, timeout=300)
            cls.out[arm] = r

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def result(self, arm):
        return json.loads((self.tmp / "sim" / f"t-{arm}" / "result.json").read_text())

    def test_1_runs_finish(self):
        for arm, r in self.out.items():
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            res = self.result(arm)
            self.assertEqual(len(res["curve"]), 2)
            self.assertTrue(all(c["score"] is not None for c in res["curve"]))

    def test_2_day1_rule_is_read_on_day2(self):
        for arm in ("A", "B"):
            res = self.result(arm)
            day1 = [f for f in res["rules_filed"] if f["day"] == 1]
            self.assertTrue(day1, f"arm {arm}: no rule filed on day 1")
            d = self.tmp / "sim" / f"t-{arm}"
            for f in day1:
                ledger = (d / "ledgers" / ("HUB-CARD-CONTRACT.md" if f["ledger"] == "hub" else "EMAIL-REVIEW-CONTRACT.md")).read_text()
                self.assertIn(f"| {f['id']} | ", ledger)
                read = (d / "skills-read" / "day2" / ("hub.md" if f["ledger"] == "hub" else "drafting.md")).read_text()
                rule_sentence = f["text"].split(' *"')[0]
                self.assertIn(rule_sentence, read, "the day-2 producers did not read the rule filed on day 1")
                self.assertNotIn(rule_sentence, (d / "skills-read" / "day1" / ("hub.md" if f["ledger"] == "hub" else "drafting.md")).read_text())

    def test_3_never_sent_without_yes(self):
        for arm in ("A", "B"):
            d = self.tmp / "sim" / f"t-{arm}"
            state = json.loads((d / "hub" / "state.json").read_text())
            approved = {e for c in state["pending"].values() if c.get("verdict") == "yes" for e in (c.get("meta") or {}).get("events", [])}
            outbox = d / "outbox.jsonl"
            for line in (outbox.read_text().splitlines() if outbox.exists() else []):
                for e in json.loads(line)["event"].split(","):
                    self.assertIn(e, approved)

    def test_4_compare_prints_a_verdict(self):
        r = subprocess.run([sys.executable, str(REPO / "sim" / "compare.py"), "t-A", "t-B"], env=self.env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("VERDICT B vs A:", r.stdout)
        self.assertIn("permutation p=", r.stdout)
        print("\n" + r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
