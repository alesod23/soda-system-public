"""agents.py: the three parties of a simulated day.

  World     plays the contacts: generates the day's events, each with a hidden TRUTH the judge scores against
            (needs an answer? which person? which step?). The system never sees the truth.
  Producer  the system under test: matches each event to a person (brain off / local / door), decides the step,
            drafts the message, reading the COMPILED skills from the sandbox on every pass, and posts a card to the
            sandbox hub. It never sends: the hub does, on the owner's yes.
  Owner     plays the owner with a persona: answers cards at set passes and, the first time something is wrong, says
            why in one sentence. That sentence goes through the real hub -> feedback queue -> rules/ engine.

Every agent has a model mode (one `claude -p` call through rules/llm.py, traced) and a stub mode (deterministic code,
no model, seeded): the stub is what makes a run reproducible and offline. The stub producer understands only a few
phrasings of the scenario's rules when it reads its skill: a stand-in for a model reading the skill, said plainly.
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "rules"))
sys.path.insert(0, str(HERE))
import checks  # noqa: E402
import llm as L  # noqa: E402


def ask_model(tracer, role: str, prompt: str) -> dict:
    t0 = time.time()
    raw = L.llm(prompt)
    tracer.generation(role, prompt, raw, L.os.environ.get("SODA_LLM_MODEL", "opus"), int((time.time() - t0) * 1000))
    return L.parse_json(raw)


# ------------------------------------------------------------------ the world

TEMPLATES = {
    "reply": ["Quick question: does the {thing} also work with older {other} setups?",
              "Is the {thing} price per seat or per team?"],
    "schedule": ["Can we find a time for a short call this week about the {thing}?",
                 "Would you have 30 minutes to walk me through the {thing}?"],
    "followup": ["Any news on what we discussed last time?", "Just wondering where we are on the open point from our call?"],
    "noise": ["Thanks, got it!", "Great, thank you.", "Perfect, talk soon."],
}
THINGS = ["dashboard", "export feature", "pilot plan", "API", "onboarding guide"]


class World:
    def __init__(self, cast: dict, scenario: dict, seed: int, stub: bool, tracer):
        self.cast, self.sc, self.stub, self.tracer = cast, scenario, stub, tracer
        self.rng = random.Random(seed)

    def events(self, day_index: int, date: str) -> list[dict]:
        if not self.stub:
            try:
                return self._model(day_index, date)
            except Exception as e:  # noqa: BLE001  a world that fails falls back to the seeded stub, and says so
                sys.stderr.write(f"world model failed, stub used for day {day_index + 1}: {e}\n")
        return self._stub(day_index)

    def _stub(self, d: int) -> list[dict]:
        out = []
        for k in range(self.sc["events_per_day"]):
            p = self.rng.choice(self.cast["people"])
            r = self.rng.random()
            kind = "noise" if r < self.sc["noise_share"] else self.rng.choice(["reply", "schedule", "followup"])
            if kind == "followup" and not p.get("open_loop"):
                kind = "reply"
            text = self.rng.choice(TEMPLATES[kind]).format(thing=self.rng.choice(THINGS), other=self.rng.choice(THINGS))
            alt = self.rng.random() < self.sc["alt_share"]
            step = {"noise": "none", "reply": "reply", "schedule": "schedule"}.get(kind) or \
                ("schedule" if "book" in p["open_loop"] else "send-doc")
            hh = self.rng.choice([8, 9, 10, 11, 12, 14, 15, 16, 17, 19])
            out.append({
                "id": f"e{d + 1:02d}{k + 1:02d}", "day": d, "at": f"{hh:02d}:{self.rng.choice([0, 15, 30, 45]):02d}",
                "from_email": p["alt_email"] if alt else p["email"], "from_name": p["name"].split()[0] if alt else p["name"],
                "subject": "Re: " + p["company"], "text": text + f"\n\nBest, {p['name']} ({p['company']})" if alt else text,
                "truth": {"actionable": kind != "noise", "person": p["id"], "step": step, "deadline_day": d + 1},
            })
        return sorted(out, key=lambda e: e["at"])

    def _model(self, d: int, date: str) -> list[dict]:
        prompt = ("You play the contacts of a fictional person (the owner) for one simulated day, " + date + ". Write "
                  f"{self.sc['events_per_day']} incoming emails from the cast below. About {int(self.sc['noise_share'] * 100)}% "
                  "need no answer (a thank-you). About " + str(int(self.sc['alt_share'] * 100)) + "% come from the person's "
                  "alt_email and sign with name and company. A follow-up about the person's open_loop must not name it.\n"
                  "CAST: " + json.dumps(self.cast["people"]) + "\nAnswer ONE JSON object: {\"events\": [{\"at\": \"HH:MM\", "
                  "\"from_email\", \"from_name\", \"subject\", \"text\", \"truth\": {\"actionable\": bool, \"person\": <id>, "
                  "\"step\": \"reply|schedule|send-doc|none\"}}]}")
        ev = ask_model(self.tracer, "world", prompt)["events"]
        out = []
        for k, e in enumerate(ev):
            t = e.get("truth") or {}
            out.append({"id": f"e{d + 1:02d}{k + 1:02d}", "day": d, "at": str(e.get("at") or "09:00")[:5],
                        "from_email": e.get("from_email", ""), "from_name": e.get("from_name", ""),
                        "subject": e.get("subject", ""), "text": e.get("text", ""),
                        "truth": {"actionable": bool(t.get("actionable")), "person": t.get("person"),
                                  "step": t.get("step") or "none", "deadline_day": d + 1}})
        return sorted(out, key=lambda e: e["at"])


# ------------------------------------------------------------------ the system under test

def skill_flags(drafting: str, hub: str) -> dict:
    """What the STUB producer understands of its compiled skills (a model reads them whole)."""
    return {"ask_first": bool(re.search(r"ask (goes|comes|is) first|first sentence[^.]*(request|ask)", drafting, re.I)),
            "no_filler": "filler" in drafting.lower(),
            "one_person": bool(re.search(r"one person|two people", hub, re.I))}


class Producer:
    def __init__(self, crm: list[dict], arm: dict, stub: bool, tracer, brain_url: str = ""):
        self.crm, self.arm, self.stub, self.tracer, self.brain_url = crm, arm, stub, tracer, brain_url

    # --- person matching: the arm's brain setting is the variable under test
    def match(self, e: dict) -> dict | None:
        by_email = {p["email"].lower(): p for p in self.crm}
        if e["from_email"].lower() in by_email:
            return by_email[e["from_email"].lower()]
        mode = self.arm.get("brain", "off")
        if mode == "off":
            return None
        if mode == "door" and self.brain_url:
            try:
                req = urllib.request.Request(self.brain_url.rstrip("/") + "/brain/match", method="POST",
                                             data=json.dumps({"text": e["from_name"] + "\n" + e["text"], "k": 3}).encode(),
                                             headers={"Content-Type": "application/json"})
                hits = json.loads(urllib.request.urlopen(req, timeout=10).read())
                ids = [h["id"] for h in hits if h.get("kind") == "person"]
                return next((p for p in self.crm if p["id"] in ids[:1]), None)
            except OSError:
                pass   # the door is down: fall back to the local stand-in
        # local stand-in for /brain/match: token overlap of names and companies with the sender and the text
        hay = set(re.findall(r"[a-z]+", (e["from_name"] + " " + e["text"] + " " + e["from_email"]).lower()))
        best, score = None, 0
        for p in self.crm:
            toks = set(re.findall(r"[a-z]+", (p["name"] + " " + p["company"]).lower()))
            s = len(toks & hay)
            if s > score:
                best, score = p, s
        return best if score >= 2 else None

    # --- one event -> a proposal {actionable, step, person, subject, body}
    def propose(self, e: dict, person: dict | None, skills: dict) -> dict:
        if not self.stub:
            prompt = ("You draft replies for the owner. Follow the skills exactly.\n=== DRAFTING SKILL ===\n" + skills["drafting"]
                      + "\n=== HUB SKILL ===\n" + skills["hub"] + "\nINCOMING: " + json.dumps({k: e[k] for k in ("from_email", "from_name", "subject", "text")})
                      + "\nPERSON IN THE CRM: " + json.dumps(person) + "\nAnswer ONE JSON object: {\"actionable\": bool, "
                      "\"step\": \"reply|schedule|send-doc|none\", \"subject\": \"...\", \"body\": \"...\"}")
            try:
                r = ask_model(self.tracer, "producer", prompt)
                return {"actionable": bool(r.get("actionable")), "step": r.get("step") or "none",
                        "subject": r.get("subject") or e["subject"], "body": r.get("body") or ""}
            except Exception as ex:  # noqa: BLE001
                sys.stderr.write(f"producer model failed on {e['id']}: {ex}\n")
                return {"actionable": False, "step": "none", "subject": "", "body": ""}
        f = skill_flags(skills["drafting"], skills["hub"])
        text = e["text"].split("\n\nBest,")[0]
        actionable = "?" in text
        if not actionable:
            return {"actionable": False, "step": "none", "subject": "", "body": ""}
        if person and person.get("open_loop") and re.search(r"what we discussed|open point", text, re.I):
            step = "schedule" if "book" in person["open_loop"] else "send-doc"
        elif re.search(r"\b(call|time|minutes|meet)\b", text, re.I):
            step = "schedule"
        else:
            step = "reply"
        ask = {"reply": "could you confirm this answers it: yes, it does?",
               "schedule": "could we talk on Thursday at 15:00?",
               "send-doc": "could you confirm you received the document, attached here?"}[step]
        first = (person["name"] if person else e["from_name"]).split()[0]
        filler = "" if f["no_filler"] else "I hope this finds you well. "
        context = f"Thanks for your note about {e['subject'].replace('Re: ', '')}."
        body = (f"Hi {first}, {ask} {filler}{context}" if f["ask_first"]
                else f"Hi {first}, {filler}{context} {ask[0].upper() + ask[1:]}")
        return {"actionable": True, "step": step, "subject": e["subject"], "body": body.strip(), "bundle_ok": not f["one_person"]}


# ------------------------------------------------------------------ the owner

class Owner:
    def __init__(self, persona: str, preferences: list[dict], stub: bool, tracer, truth: dict):
        self.persona, self.prefs, self.stub, self.tracer = persona, preferences, stub, tracer
        self.truth = truth          # event id -> truth (the stub owner "lived" the day; a model owner reads the card)
        self.said: set[str] = set()
        self.stub_map: dict[str, dict] = {}   # what the stub classifier answers for each sentence said

    def decide(self, card: dict) -> tuple[str, str]:
        if not self.stub:
            m = card.get("meta") or {}
            view = card["text"] + "\n" + "\n---\n".join(checks.bodies(card)) + ("\nTo: " + str(m.get("to")) if m.get("to") else "")
            try:
                r = ask_model(self.tracer, "owner", self.persona + "\n\nTHE CARD:\n" + view)
                return ("yes" if r.get("verdict") == "yes" else "no"), str(r.get("feedback") or "").strip()
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(f"owner model failed on card {card['id']}: {e}\n")
                return "no", ""
        events = (card.get("meta") or {}).get("events") or []
        if any(not self.truth.get(x, {}).get("actionable", True) for x in events):
            return "no", ""                                        # noise: rejected, no explanation
        for p in checks.failing(card, self.prefs):
            if p["id"] in self.said:
                return "no", ""                                    # already said once: expects it learned
            self.said.add(p["id"])
            self.stub_map[p["quote"]] = {"class": "rule", "contract": p["ledger"], "rule": p["rule"], "soft": False,
                                         "answer": f"a new {p['ledger']} rule"}
            return "no", p["quote"]
        return "yes", ""
