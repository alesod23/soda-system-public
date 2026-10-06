#!/usr/bin/env python3
"""ledger_verdict.py: ONE classifier for a sentence of feedback given on ANY surface (docs/01-rule-loop.md, part 4).

    classify(surface, quote, item, line) -> {"class": like|confirmation|rule|case|system|work, "contract", ...}
    file_feedback(surface, quote, item, line) -> {"ok", "class", "filed", "handoff", ...}

THE SIX CLASSES (one per sentence; a sentence about two things is two entries upstream):
    like          praise: a LIKED row (addrule.py --like [--maps-to H<n>]), the compile turns it into an example
    confirmation  an existing rule already says it: one line in rule-confirmations.jsonl (no duplicate row)
    rule          a general rule that decides the NEXT artifact of the same kind: a row (addrule.py --contract <x>)
    case          a judgement on THIS one artifact only, nothing general in it ("this one was fine to skip"): one line
                  in rule-cases.jsonl, the observer's example set; never a row
    system        a request that the SOFTWARE behave differently: handed off to a working session
    work          a request for a thing to be produced: handed off to a working session
like / confirmation / rule / case are filed HERE, in code; system / work come back with handoff True and the caller
(feedback_session.py) records them for a session that can change code or produce the work.

WHICH LEDGER: the surface gives the default (config/surfaces.json "surface_ledger"); the model may move the sentence to
the ledger it is ABOUT (a sentence on a notification about how a draft reply is worded goes to email). The rows of the
default ledger are in the prompt, so a confirmation is found instead of a duplicate. The owner never picks a ledger.

After a row is filed the touched skill is recompiled (compile_skill.py --contract <x> --install) when a skill map
exists for it.

    ledger_verdict.py file --surface notif --quote "the owner's words" [--item <id>] [--line "what it showed"] [--dry]
    ledger_verdict.py classify --surface notif --quote "..."            (classification only, nothing filed)
Env: SODA_LLM_STUB=<file> answers the model call with that file's text (tests). Nothing here sends anything, ever.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))
import soda_paths as P  # noqa: E402
import llm as L  # noqa: E402

CONFIRMATIONS = P.CONFIRMATIONS
CASES = P.CASES
CLASSES = ("like", "confirmation", "rule", "case", "system", "work")
_CFG = P.surfaces()
LEDGERS = {k: v["file"] for k, v in _CFG["ledgers"].items()}
ABOUT = {k: v.get("about", "") for k, v in _CFG["ledgers"].items()}
# surface (where the sentence was said) -> default ledger. ONE line per new surface: this is the replication point.
SURFACE_LEDGER = dict(_CFG.get("surface_ledger", {}))
DEFAULT_LEDGER = _CFG.get("default_ledger") or sorted(LEDGERS)[0]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def ledger_rows(contract, width=420):
    if contract not in LEDGERS:
        return []
    f = P.ledger_path(contract)
    if not f.exists():
        return []
    return [f"{m.group(1)}: {m.group(2)[:width]}" for m in
            re.finditer(r"^\| (H\d+) \| (.+?) \| [^|]+\|\s*$", f.read_text(encoding="utf-8"), flags=re.M)]


def ids_of(contract):
    return {r.split(":", 1)[0] for r in ledger_rows(contract)}


PROMPT = """The owner of an assistant system gave a sentence of feedback on one artifact the system produced. Decide what the sentence IS, so it
is kept in the right place and the system learns from it. You only classify; code files the answer.

WHERE IT WAS SAID: __SURFACE__ (default ledger: __DEFAULT__)
THE ARTIFACT BEING JUDGED (what it showed):
__LINE__

THE SENTENCE (verbatim): \"\"\"__QUOTE__\"\"\"

THE LEDGERS (pick the one the sentence is ABOUT, not where it was said; the default when in doubt):
__ABOUT__

THE ROWS OF THE DEFAULT LEDGER (id: rule):
__RULES__

The class, exactly one:
- "like": praise for it as it is ("good one", "this is right", "perfect"). maps_to = the rule it satisfies, when one does.
- "confirmation": an existing row above already says the same, even in other words. duplicate_of = that id.
- "rule": a GENERAL rule that decides the next artifact of the same kind (e.g. "A group message that only announces an
  event never becomes a notification"), imperative, one sentence, in English, the owner's words kept in the quote.
- "case": a judgement on this one artifact only, with nothing general in it ("this one I had already seen").
- "system": a request that the software behave differently (a feature, a layout, a schedule, a new field on the card).
- "work": a request for something to be produced (a reply, a document, a search).
A sentence that both states a general rule and asks for a change is "rule" (the change follows from the rule).
"soft": true only when the sentence hedges ("maybe", "usually", "in general").

Answer with ONE JSON object and nothing else:
{"class": "like|confirmation|rule|case|system|work", "contract": "<ledger key>", "maps_to": null, "duplicate_of": null,
 "rule": "the general rule, only for class rule", "soft": false, "answer": "one short line: what it is and where it is kept"}"""


def classify(surface, quote, item="", line="", llm_fn=None):
    surface = str(surface or "").strip()
    default = SURFACE_LEDGER.get(surface, surface if surface in LEDGERS else DEFAULT_LEDGER)
    prompt = (PROMPT.replace("__SURFACE__", surface or "(unknown)").replace("__DEFAULT__", default)
              .replace("__LINE__", str(line or "(not passed)").strip()[:1500])
              .replace("__QUOTE__", str(quote or "").strip())
              .replace("__ABOUT__", "\n".join(f"- {k}: {v}" for k, v in ABOUT.items()))
              .replace("__RULES__", "\n".join(ledger_rows(default)) or "(empty)"))
    raw = (llm_fn or L.llm)(prompt)
    ans = L.parse_json(raw)
    cls = str(ans.get("class") or "").strip().lower()
    if cls not in CLASSES:
        raise ValueError(f"class {cls!r} is not one of {CLASSES}")
    contract = str(ans.get("contract") or default).strip().lower()
    ans["class"], ans["contract"], ans["default"] = cls, (contract if contract in LEDGERS else default), default
    return ans


def _append(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def file_feedback(surface, quote, item="", line="", llm_fn=None, dry=False, run=None):
    """-> {"ok", "class", "contract", "filed", "handoff", "answer"}. run(args) -> (rc, out) is injectable for tests."""
    quote = str(quote or "").strip()
    if not quote:
        return {"ok": False, "error": "empty sentence"}
    run = run or L.run_py
    try:
        ans = classify(surface, quote, item, line, llm_fn)
    except Exception as e:
        return {"ok": False, "error": f"the classifier did not answer: {str(e)[:200]}"}
    cls, c = ans["class"], ans["contract"]
    out = {"ok": True, "class": cls, "contract": c, "answer": str(ans.get("answer") or "")[:240], "handoff": False, "decision": ans}
    src = f"owner feedback on {surface} {dt.date.today().isoformat()}"
    ids = ids_of(c)
    dup = str(ans.get("duplicate_of") or "").split(":")[-1].strip()
    maps = str(ans.get("maps_to") or "").split(":")[-1].strip()
    if cls in ("system", "work"):
        out.update(handoff=True, filed="handed to the feedback session (" + cls + ")")
        return out
    if cls == "confirmation" and dup not in ids:
        cls = out["class"] = "rule" if str(ans.get("rule") or "").strip() else "case"   # a confirmation of nothing
    if cls == "confirmation":
        if not dry:
            _append(CONFIRMATIONS, {"ts": now(), "rule": f"{c}:{dup}", "quote": quote, "item": item, "surface": surface})
        out["filed"] = f"confirms {c}:{dup}" + (" (dry)" if dry else "")
        return out
    if cls == "case" or (cls == "rule" and not str(ans.get("rule") or "").strip()):
        out["class"] = "case"
        if not dry:
            _append(CASES, {"ts": now(), "surface": surface, "contract": c, "item": item, "quote": quote,
                            "line": str(line or "")[:600], "note": out["answer"]})
        out["filed"] = "case (rule-cases.jsonl)" + (" (dry)" if dry else "")
        return out
    args = [str(HERE / "addrule.py"), "--contract", c, "--quote", quote, "--source", src]
    if item:
        args += ["--item", str(item)]
    if cls == "like":
        args.append("--like")
        if maps in ids:
            args += ["--maps-to", maps]
        what = f"liked {c}" + (f":{maps}" if maps in ids else "")
    else:
        args += ["--rule", str(ans["rule"]).strip()]
        if ans.get("soft"):
            args.append("--soft")
        what = f"new rule {c}"
    if dry:
        out.update(filed=what + " (dry)", args=args)
        return out
    rc, o = run(args)
    if rc != 0:
        return {"ok": False, "error": "addrule failed: " + o[-300:], "decision": ans}
    m = re.search(r"\b(H\d+) added", o)
    out["filed"] = what + (f":{m.group(1)}" if (m and cls == "rule") else "")
    if (P.SKILL_MAPS / f"skill-map-{c}.json").exists():
        rc2, o2 = run([str(HERE / "compile_skill.py"), "--contract", c, "--install"])
        out["compiled"] = rc2 == 0
        if rc2 != 0:
            out["compile_error"] = o2[-300:]
    return out


def main(argv=None):
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("file", "classify"):
        s = sub.add_parser(name)
        s.add_argument("--surface", required=True)
        s.add_argument("--quote", required=True)
        s.add_argument("--item", default="")
        s.add_argument("--line", default="")
        s.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "classify":
        print(json.dumps(classify(a.surface, a.quote, a.item, a.line), ensure_ascii=False))
    else:
        print(json.dumps(file_feedback(a.surface, a.quote, a.item, a.line, dry=a.dry), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
