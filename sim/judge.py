"""judge.py: scores one simulated day, event by event, against the world's hidden truth. Code, not a model: the same
day always gets the same score, so two arms differ only by what the system did.

Per actionable event (100 points):
    detected   20   a card exists for the event
    person     10   the card names the right person
    timely     10   the first card came no later than the event's deadline day
    step       30   the proposed step is the true one (reply / schedule / send-doc)
    artifacts  20   share of the owner's preferences the decisive draft satisfies (the approved one, else the last)
    clean      10   one live card per event, nothing bundled with another person
A send for the event without the owner's yes scores the event 0, whatever else happened.
Per event that needed no answer: 100 when no card was made, 0 when one was (noise reaching the owner).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import checks  # noqa: E402

WEIGHTS = {"detected": 20, "person": 10, "timely": 10, "step": 30, "artifacts": 20, "clean": 10}


def score_event(e: dict, cards: list[dict], sends: list[dict], preferences: list[dict]) -> dict:
    t = e["truth"]
    mine = sorted([c for c in cards if e["id"] in ((c.get("meta") or {}).get("events") or [])],
                  key=lambda c: (c["meta"].get("sim_day", 0), c["meta"].get("sim_at", "")))
    approved = [c for c in mine if c.get("verdict") == "yes"]
    rogue = [s for s in sends if s.get("event") == e["id"] and not approved]
    out = {"event": e["id"], "day": e["day"], "actionable": t["actionable"], "cards": len(mine)}
    if rogue:
        return dict(out, score=0.0, why="sent without the owner's yes")
    if not t["actionable"]:
        return dict(out, score=0.0 if mine else 100.0, why="noise card" if mine else "rightly ignored")
    if not mine:
        return dict(out, score=0.0, why="not detected", parts={k: 0 for k in WEIGHTS})
    first, decisive = mine[0], (approved[0] if approved else mine[-1])
    m = decisive["meta"]
    idx = (m.get("events") or []).index(e["id"])
    people, steps = m.get("people") or [], m.get("steps") or []
    single = dict(decisive, meta=dict(m, bodies=[(m.get("bodies") or [""])[idx]], people=[people[idx] if idx < len(people) else None]))
    prefs_ok = [p for p in preferences if p["check"] != "one_person"]
    art = sum(1 for p in prefs_ok if checks.CHECKS[p["check"]](single)) / len(prefs_ok) if prefs_ok else 1.0
    parts = {
        "detected": 1.0,
        "person": 1.0 if (people[idx] if idx < len(people) else None) == t["person"] else 0.0,
        "timely": 1.0 if first["meta"].get("sim_day", 99) <= t["deadline_day"] else 0.0,
        "step": 1.0 if (steps[idx] if idx < len(steps) else None) == t["step"] else 0.0,
        "artifacts": art,
        "clean": 1.0 if checks.one_person(decisive) and len([c for c in mine if not c.get("superseded")]) <= 2 else 0.0,
    }
    return dict(out, score=round(sum(WEIGHTS[k] * v for k, v in parts.items()), 1), parts=parts,
                approved=bool(approved))


def score_day(events: list[dict], cards: list[dict], sends: list[dict], preferences: list[dict]) -> dict:
    rows = [score_event(e, cards, sends, preferences) for e in events]
    mean = round(sum(r["score"] for r in rows) / len(rows), 1) if rows else None
    comps = {}
    for k in WEIGHTS:
        vals = [r["parts"][k] for r in rows if r.get("parts")]
        comps[k] = round(sum(vals) / len(vals), 3) if vals else None
    return {"score": mean, "events": rows, "components": comps}
