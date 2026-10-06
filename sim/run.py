#!/usr/bin/env python3
"""run.py: run one ARM of the simulation for N simulated days, in a sandbox.

    python3 sim/run.py --arm A --days 5 --run trial-A [--scenario sim/scenario/example] [--stub] [--seed 7]

The sandbox is $SODA_HOME/sim/<run>/: its own ledgers, skill maps, compiled skills, CRM, hub state, feedback queue,
outbox. Nothing real is read after the copy and nothing real is written: the hub runs from hub/server.js on a free
port with no Telegram token, its only executor (sim/outbox.py) appends to the sandbox outbox, and the rules/ engine
runs as subprocesses with SODA_HOME pointing at the sandbox.

One simulated day (sandbox clock, sim/clock.py):
    world: the day's events (each with a hidden truth)
    at every pass (scenario "passes"): the producers handle what arrived, reading the compiled skills FROM DISK
        (a copy of what they read is kept in skills-read/day<N>/), and post one card per event to the sandbox hub
        (one card for two people when the hub skill does not forbid it: the stub producer's bad habit)
    at the owner's passes: the owner answers every open card; a reason goes through the hub to the feedback queue
    end of day, when the arm learns: rules/feedback_session.py files the sentences (addrule -> compile_skill), so
        the NEXT day's producers read the new rules
After the last day one settle morning handles late events (no new events, no learning), then the judge scores every
day (sim/judge.py) and result.json is written. compare.py reads it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "lib"))
sys.path.insert(0, str(HERE))
import soda_paths as P  # noqa: E402
from agents import World, Producer, Owner  # noqa: E402
from clock import SimClock  # noqa: E402
from judge import score_day  # noqa: E402
from simtrace import Tracer  # noqa: E402


def free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def jl(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    except OSError:
        return []


def ledger_rows(path: Path) -> list[str]:
    return re.findall(r"^\| (H\d+) \| (.+?) \| [^|]+\|\s*$", path.read_text(encoding="utf-8"), re.M) if path.exists() else []


class Sim:
    def __init__(self, a):
        self.a = a
        self.arm = json.loads(Path(a.arm if a.arm.endswith(".json") else HERE / "arms" / f"{a.arm}.json").read_text())
        self.sc_dir = Path(a.scenario)
        self.sc = json.loads((self.sc_dir / "scenario.json").read_text())
        self.cast = json.loads((self.sc_dir / "cast.json").read_text())
        self.dir = P.HOME / "sim" / a.run
        if self.dir.exists():
            if not a.fresh:
                sys.exit(f"{self.dir} exists: pick another --run name or pass --fresh")
            shutil.rmtree(self.dir)
        self.dir.mkdir(parents=True)
        self.port = free_port()
        self.env = dict(os.environ, SODA_HOME=str(self.dir), SODA_SKILLS_DIR=str(self.dir / "skills"),
                        HUB_PORT=str(self.port), HUB_HOST="127.0.0.1", HUB_URL=f"http://127.0.0.1:{self.port}",
                        TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="", SODA_TZ="UTC")
        for k in ("SODA_HITS", "SODA_DECISIONS", "SODA_FEEDBACK_QUEUE", "HUB_RULES", "HUB_EXECUTORS"):
            self.env.pop(k, None)     # every path follows the sandbox's SODA_HOME
        self.tracer = Tracer(self.dir, a.run, self.arm["name"])
        self.hub = None

    # ---------------------------------------------------------------- sandbox
    def build(self):
        for sub in ("ledgers", "skill-maps"):
            shutil.copytree(self.sc_dir / sub, self.dir / sub)
        (self.dir / "hub").mkdir()
        (self.dir / "hub" / "hub-rules.json").write_text('{"downgrade": [], "drop": []}\n')
        (self.dir / "hub" / "executors.json").write_text(json.dumps(
            {"sim-send": {"argv": [sys.executable, str(HERE / "outbox.py"), "{event}", "{to}", "{card}"], "timeout_s": 30}}))
        crm = [{k: v for k, v in p.items() if k != "alt_email"} for p in self.cast["people"]]   # the CRM never knows alt addresses
        (self.dir / "crm.json").write_text(json.dumps(crm, indent=1))
        self.crm = crm
        for key in ("email", "hub"):
            self.py(REPO / "rules" / "compile_skill.py", "--contract", key, "--install", check=True)
        self.hub = subprocess.Popen(["node", str(REPO / "hub" / "server.js")], env=self.env, cwd=str(REPO),
                                    stdout=open(self.dir / "hub.log", "w"), stderr=subprocess.STDOUT)
        for _ in range(60):
            try:
                self.call("GET", "/pending"); return
            except OSError:
                time.sleep(0.1)
        raise SystemExit("the sandbox hub did not start (see hub.log)")

    def py(self, *args, env=None, check=False):
        r = subprocess.run([sys.executable] + [str(x) for x in args], env=env or self.env, capture_output=True,
                           text=True, timeout=600, cwd=str(REPO))
        if check and r.returncode != 0:
            raise SystemExit(f"{args[0]} failed: {r.stdout[-400:]}{r.stderr[-400:]}")
        return r.returncode, r.stdout + r.stderr

    def call(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def state(self) -> dict:
        try:
            return json.loads((self.dir / "hub" / "state.json").read_text())
        except (OSError, ValueError):
            return {"pending": {}}

    def skills(self) -> dict:
        def rd(n):
            try:
                return (self.dir / "skills" / n / "SKILL.md").read_text(encoding="utf-8")
            except OSError:
                return ""
        return {"drafting": rd("drafting"), "hub": rd("hub")}

    # ---------------------------------------------------------------- one day
    def producers(self, clock, day, events, handled, redo):
        sk = self.skills()
        snap = self.dir / "skills-read" / f"day{day + 1}"
        if not snap.exists():
            snap.mkdir(parents=True)
            for n, t in sk.items():
                (snap / f"{n}.md").write_text(t, encoding="utf-8")
        now = clock.now.strftime("%H:%M")
        due = [e for e in events if e["id"] not in handled and (e["day"] < day or e["at"] <= now)]
        due += [e for e in redo]
        redo.clear()
        props = []
        for e in due:
            with self.tracer.span("event", event=e["id"], pass_at=now) as sp:
                person = self.producer.match(e)
                pr = self.producer.propose(e, person, sk)
                sp.update(person=person and person["id"], step=pr["step"], actionable=pr["actionable"])
            handled.add(e["id"])
            if pr["actionable"]:
                props.append((e, person, pr))
        # the stub producer's bad habit: two people in the same pass share one card, unless the hub skill forbids it
        groups, i = [], 0
        while i < len(props):
            e, person, pr = props[i]
            nxt = props[i + 1] if i + 1 < len(props) else None
            if pr.get("bundle_ok") and nxt and nxt[1] and person and nxt[1]["id"] != person["id"]:
                groups.append([props[i], nxt]); i += 2
            else:
                groups.append([props[i]]); i += 1
        for g in groups:
            names = [(p["name"] if p else e["from_name"]) for e, p, _ in g]
            meta = {"origin": "sim-producer", "events": [e["id"] for e, _, _ in g], "people": [p and p["id"] for _, p, _ in g],
                    "steps": [pr["step"] for _, _, pr in g], "bodies": [pr["body"] for _, _, pr in g],
                    "to": ", ".join(e["from_email"] for e, _, _ in g), "subject": g[0][2]["subject"],
                    "body": "\n\n---\n\n".join(pr["body"] for _, _, pr in g), "sim_day": day, "sim_at": clock.iso()}
            self.call("POST", "/pending", {"text": f"Send the reply to {' and '.join(names)}?", "kind": "decision",
                                           "type": "email-draft", "meta": meta,
                                           "action": {"type": "sim-send", "event": ",".join(meta["events"]), "to": meta["to"], "card": ""}})

    def owner_pass(self, clock, redo, events_by_id):
        for c in [c for c in self.state()["pending"].values()
                  if not c["resolved"] and (c.get("meta") or {}).get("origin") == "sim-producer"]:
            with self.tracer.span("owner", card=c["id"]) as sp:
                verdict, fb = self.owner.decide(c)
                sp.update(verdict=verdict, feedback=fb)
            self.call("POST", "/resolve", {"id": c["id"], "verdict": verdict, "feedback": fb or None, "device": "sim-owner"})
            if verdict == "no":
                for eid in c["meta"]["events"]:
                    e = events_by_id[eid]
                    if e["truth"]["actionable"] and not e.get("_redone"):
                        e["_redone"] = True
                        redo.append(e)          # a session redoes a rejected draft on the next pass, once
        # executors run asynchronously: wait until every yes has its result, so the outbox is complete
        for _ in range(100):
            if all(c.get("action_result") for c in self.state()["pending"].values() if c.get("verdict") == "yes" and c.get("action")):
                break
            time.sleep(0.1)

    def learn(self, day):
        before = {k: ledger_rows(self.dir / "ledgers" / f) for k, f in
                  (("email", "EMAIL-REVIEW-CONTRACT.md"), ("hub", "HUB-CARD-CONTRACT.md"))}
        env = dict(self.env)
        if self.a.stub:
            smap = self.dir / "stub-map.json"
            # keyed on the classifier's sentence slot ("""<sentence>"""), never on the bare quote: the prompt also lists the
            # ledger's existing rows, which carry earlier quotes
            smap.write_text(json.dumps({'"""' + q + '"""': json.dumps(v) for q, v in self.owner.stub_map.items()}))
            env["SODA_LLM_STUB_MAP"] = str(smap)
        with self.tracer.span("feedback-session", day=day + 1) as sp:
            rc, out = self.py(REPO / "rules" / "feedback_session.py", env=env)
            sp.update(rc=rc, out=out[-300:])
        filed = []
        for k, f in (("email", "EMAIL-REVIEW-CONTRACT.md"), ("hub", "HUB-CARD-CONTRACT.md")):
            for rid, text in ledger_rows(self.dir / "ledgers" / f):
                if (rid, text) not in before[k]:
                    filed.append({"day": day + 1, "ledger": k, "id": rid, "text": text})
        return filed

    # ---------------------------------------------------------------- the run
    def run(self):
        self.build()
        clock = SimClock(self.sc["start"], self.sc["passes"])
        world = World(self.cast, self.sc, self.a.seed, self.a.stub, self.tracer)
        self.producer = Producer(self.crm, self.arm, self.a.stub, self.tracer, os.environ.get("BRAIN_URL", ""))
        persona = (self.sc_dir / "persona.md").read_text(encoding="utf-8")
        all_events, truth = [], {}
        self.owner = Owner(persona, self.sc["preferences"], self.a.stub, self.tracer, truth)
        handled, redo, filed = set(), [], []
        for d in range(self.a.days + 1):                      # the last iteration is the settle morning
            settle = d == self.a.days
            date = clock.day(d).isoformat()
            self.tracer.day(f"day {d + 1} {date}" + (" (settle)" if settle else ""), day=d + 1)
            if not settle:
                with self.tracer.span("world", day=d + 1) as sp:
                    evs = world.events(d, date)
                    sp.update(events=len(evs))
                for e in evs:
                    truth[e["id"]] = e["truth"]
                    jl(self.dir / "events.jsonl", e)
                all_events += evs
            by_id = {e["id"]: e for e in all_events}
            for p in self.sc["passes"][:1] + (self.sc["owner_passes"][:1] if settle else self.sc["passes"][1:]):
                clock.at(p)
                with self.tracer.span("pass", at=p, day=d + 1):
                    self.producers(clock, d, all_events, handled, redo)
                    if p in self.sc["owner_passes"]:
                        self.owner_pass(clock, redo, by_id)
            if not settle and self.arm.get("learning", True):
                filed += self.learn(d)
            self.tracer.flush()
        return self.finish(all_events, filed)

    def finish(self, events, filed):
        cards = list(self.state()["pending"].values())
        sends = []
        for s in read_jsonl(self.dir / "outbox.jsonl"):
            for eid in str(s.get("event") or "").split(","):
                sends.append(dict(s, event=eid))
        curve, rows = [], []
        for d in range(self.a.days):
            day_events = [e for e in events if e["day"] == d]
            r = score_day(day_events, cards, sends, self.sc["preferences"])
            curve.append({"day": d + 1, "date": SimClock(self.sc["start"], []).day(d).isoformat(), "score": r["score"],
                          "components": r["components"]})
            for row in r["events"]:
                jl(self.dir / "scores.jsonl", row)
                rows.append(row)
        result = {"run": self.a.run, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "arm": self.arm["name"],
                  "arm_config": self.arm, "scenario": self.sc_dir.name, "stub": self.a.stub, "seed": self.a.seed,
                  "days": self.a.days, "curve": curve, "rules_filed": filed,
                  "counts": {"events": len(events), "cards": len([c for c in cards if (c.get("meta") or {}).get("origin") == "sim-producer"]),
                             "approved": len([c for c in cards if c.get("verdict") == "yes"]), "sends": len(sends),
                             "rules_filed": len(filed)},
                  "langfuse": self.tracer.enabled}
        (self.dir / "result.json").write_text(json.dumps(result, indent=1, ensure_ascii=False))
        return result

    def stop(self):
        if self.hub:
            self.hub.terminate()
            try:
                self.hub.wait(5)
            except subprocess.TimeoutExpired:
                self.hub.kill()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arm", required=True, help="A | B | C (sim/arms/<name>.json) or a path to an arm json")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--run", required=True, help="run name: the sandbox is $SODA_HOME/sim/<run>")
    ap.add_argument("--scenario", default=str(HERE / "scenario" / "example"))
    ap.add_argument("--stub", action="store_true", help="no model calls: deterministic agents, stubbed classifier")
    ap.add_argument("--seed", type=int, default=7, help="the world's seed (same seed = same events for every arm)")
    ap.add_argument("--fresh", action="store_true", help="replace an existing sandbox of the same name")
    a = ap.parse_args(argv)
    sim = Sim(a)
    try:
        r = sim.run()
    finally:
        sim.stop()
    print(f"run {r['run']} arm {r['arm']}: " + "  ".join(f"d{c['day']} {c['score']}" for c in r["curve"])
          + f"  | rules filed {len(r['rules_filed'])}, sends {r['counts']['sends']}  -> {sim.dir / 'result.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
