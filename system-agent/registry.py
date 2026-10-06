#!/usr/bin/env python3
"""registry.py: the known-broken registry of the system (docs/05-system-agent.md). Imported by system_agent.py and
unstuck.py.

THE REGISTRY is `$SODA_HOME/system-agent/broken.jsonl` (env SYSAGENT_REGISTRY), append-only, one JSON line per change
of an item; the newest line of an `id` wins (merged field by field):
  {id, key (the check key: `system_agent.py check --only <key>`), what, since,
   class maintenance|owner_command|owner_decision, owner agent|fix_session|owner, attempts, last_action, next,
   status open|fixing|verifying|stuck|fixed|gone, evidence, passes, card, for_owner, at}
Only `maintenance` is the agent's to-do. `owner_command` = the fix is known and only the owner's hand (a sign-in, a
hardware switch) runs it: a STUCK card with one action. `owner_decision` = waiting for the owner's choice: never "broken",
never a card from the agent.

`render_md()` writes the open lines as a markdown table (a "Known broken" view any page can include).
"""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import soda_paths as P  # noqa: E402

REG = Path(os.environ.get("SYSAGENT_REGISTRY") or (P.HOME / "system-agent" / "broken.jsonl"))
TZ = ZoneInfo(os.environ.get("SODA_TZ") or "UTC")
OPEN = ("open", "fixing", "verifying", "stuck")
CLASSES = ("maintenance", "owner_command", "owner_decision")
OWNERS = ("agent", "fix_session", "owner")
KINDS = ("run",)   # the one for_owner kind this public version executes (see unstuck.py)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path=None):
    """-> {id: merged newest state}, in first-seen order."""
    out = {}
    try:
        for l in open(path or REG, encoding="utf-8"):
            l = l.strip()
            if not l:
                continue
            try:
                j = json.loads(l)
            except ValueError:
                continue
            if j.get("id"):
                out[j["id"]] = dict(out.get(j["id"], {}), **j)
    except OSError:
        pass
    return out


def put(line, path=None):
    """Append a new state of one item (the newest line wins)."""
    line = dict(line, at=now_iso())
    p = Path(path or REG)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    return line


def is_open(l):
    return l.get("status") in OPEN


def is_broken(l):
    return is_open(l) and l.get("class") in ("maintenance", "owner_command")


def counts(lines):
    o = [l for l in lines.values() if is_broken(l)]
    return {"broken": len(o),
            "agent": sum(1 for l in o if l.get("owner") == "agent"),
            "fix_session": sum(1 for l in o if l.get("owner") == "fix_session"),
            "owner": sum(1 for l in o if l.get("owner") == "owner"),
            "owner_decision": sum(1 for l in lines.values() if l.get("class") == "owner_decision" and is_open(l))}


def short_time(s):
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(TZ)
        return d.strftime("%d %b %H:%M")
    except (ValueError, TypeError):
        return str(s or "?")


# ------------------------------------------------------------------ for_owner: the one action of a STUCK card
SECRETISH = re.compile(r"(token|secret|password|passwd|api[_-]?key|bearer)\s*[=:]\s*\S+|sk-[A-Za-z0-9]{12,}|xox[bp]-|ghp_[A-Za-z0-9]{10,}", re.I)


def validate_for_owner(o):
    """-> (ok, why). One action, a kind this version runs, an argv with no secret in it."""
    if not isinstance(o, dict):
        return False, "for_owner is not an object"
    if o.get("kind") not in KINDS:
        return False, f"kind {o.get('kind')!r} is not one of {KINDS}"
    if not str(o.get("what") or "").strip():
        return False, "no 'what' sentence"
    argv = o.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(x, str) for x in argv):
        return False, "a run action needs argv: a non-empty list of strings (no shell)"
    if SECRETISH.search(" ".join(argv)):
        return False, "the command carries something that looks like a secret"
    return True, ""


def stuck_card(l, tried):
    """The STUCK card of one line: the decision first, then what broke, what it means, what was tried, what YES and NO
    do. -> (text, context, meta). tried = [(when, what)]."""
    fh = l.get("for_owner") or {}
    what = str(l.get("what"))[:160]
    text = f"STUCK on {l.get('key')}: {what}"
    did = "; ".join(f"{w} ({t})" if t else w for t, w in tried[-4:]) or "nothing that is mine to do: only your hand runs this"
    ctx = "\n".join([
        f"What broke: {str(l.get('what'))[:240]}",
        f"What it means for you: {fh.get('what_it_means') or 'it stays broken until this is done'}",
        f"I did: {did}. Registry id {l['id']}" + (f", evidence: {l['evidence']}" if l.get("evidence") else ""),
        "What happens next: on your YES I run the step below once, re-run the check and close this card when it passes.",
        f"I need: {fh.get('what')}",
        f"YES = I run `{' '.join(fh.get('argv') or [])}` now and record what came back",
        "NO = it stays broken; the line stays in the registry with its age, no second card.",
        f"Open since {short_time(l.get('since'))}.",
    ])
    meta = {"origin": "system-agent", "registry_id": l["id"], "action": "unstuck", "person": l["id"]}
    return text, ctx, meta


def render_md(lines=None, out=None):
    """The open lines as a markdown table; written to `out` when given. -> the text."""
    lines = load() if lines is None else lines
    rows = sorted((l for l in lines.values() if is_broken(l)), key=lambda l: (str(l.get("owner")), str(l.get("since"))))
    md = ["| id | check | what | owner | since | next |", "|---|---|---|---|---|---|"]
    for l in rows:
        cell = lambda v: str(v or "").replace("|", "/").replace("\n", " ")[:140]
        md.append(f"| {cell(l['id'])} | {cell(l.get('key'))} | {cell(l.get('what'))} | {cell(l.get('owner'))} | "
                  f"{cell(short_time(l.get('since')))} | {cell(l.get('next'))} |")
    if not rows:
        md.append("| - | - | nothing is broken | - | - | - |")
    text = "\n".join(md) + "\n"
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
    return text
