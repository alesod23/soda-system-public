#!/usr/bin/env python3
"""feedback_session.py: the rule maker. Turns every queued sentence of feedback into the right kind of record.

The owner gives feedback wherever they happen to be looking: a reason on a rejected card, a comment on the review page,
`notif: ...` on Telegram, a line typed into a feedback box on any page. All of it lands in ONE queue (the hub is its
only writer: POST /feedback). This script is its ONE consumer. Every few minutes (cron or a systemd timer):

  1. read the open entries (GET <hub>/feedback?status=open)
  2. classify each one IN CODE with ledger_verdict.file_feedback():
        like / confirmation / rule / case  -> filed at once in the ledger the sentence is ABOUT; the skill is recompiled
        system / work                      -> appended to handoffs.jsonl for a working session (a model with tools) to
                                              act on; nothing here edits code or produces work
     an entry of kind "rule" (the owner said "rule: ..." explicitly) is filed as a rule even when it reads like a case
  3. mark the entries done on the hub, each with its result line ("filed as hub H9", "confirms notif:H2", ...)
  4. post ONE update card summarising what was filed and where, so a mis-filed rule is corrected with one more sentence
     on that card (which comes back here as a new entry). The card pings once per run at most.

    feedback_session.py [--dry]            one pass (the cron entry point)
    feedback_session.py add --surface hub --text "..." [--item <id>]     queue one sentence by hand (through the hub)

Nothing here sends a message to anyone other than the owner's own hub.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))
import soda_paths as P  # noqa: E402
import ledger_verdict as LV  # noqa: E402

LOG = P.HOME / "feedback" / "feedback-session.jsonl"


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hub(method: str, path: str, body: dict | None = None, timeout: int = 15) -> dict:
    req = urllib.request.Request(P.HUB_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def process(entry: dict, dry: bool = False, llm_fn=None, run=None) -> dict:
    """One queue entry -> {"result": one line, "class", ...}. Never raises."""
    surface = entry.get("surface") or "hub"
    quote, item, line = entry.get("text") or "", entry.get("item") or "", entry.get("line") or ""
    if entry.get("kind") == "change":
        # "N change: <what>" is a request to redo THIS artifact: work for a session, and maybe also a rule
        append(P.HANDOFFS, {"ts": now(), "class": "work", "surface": surface, "item": item, "text": quote, "line": line})
        return {"result": f"redo requested for {item}: handed to a session", "class": "work"}
    r = LV.file_feedback(surface, quote, item=item, line=line, llm_fn=llm_fn, dry=dry, run=run)
    if not r.get("ok"):
        return {"result": "not filed: " + str(r.get("error"))[:200], "class": None, "error": True}
    if entry.get("kind") == "rule" and r["class"] in ("case", "system", "work") and r.get("decision", {}).get("rule"):
        # declared rule: the owner said it IS a rule, so it is filed as one whatever the classifier thought
        rule = r["decision"]["rule"]
        args = [str(HERE / "addrule.py"), "--contract", r["contract"], "--rule", rule, "--quote", quote,
                "--source", f"owner declared rule on {surface} {now()[:10]}"]
        if not dry:
            rc, out = (run or LV.L.run_py)(args)
            if rc != 0:
                return {"result": "declared rule not filed: " + out[-200:], "class": "rule", "error": True}
        return {"result": f"declared rule filed in {r['contract']}", "class": "rule", "contract": r["contract"]}
    if r.get("handoff"):
        append(P.HANDOFFS, {"ts": now(), "class": r["class"], "surface": surface, "item": item, "text": quote,
                            "line": line, "answer": r.get("answer")})
    return {"result": f"{r['class']}: {r.get('filed')}" + (f" ({r['answer']})" if r.get("answer") else ""),
            "class": r["class"], "contract": r.get("contract")}


def run_once(dry: bool = False) -> int:
    try:
        entries = hub("GET", "/feedback?status=open").get("entries", [])
    except OSError as e:
        print(f"hub not reachable at {P.HUB_URL}: {e}", file=sys.stderr)
        return 1
    if not entries:
        print("feedback queue empty")
        return 0
    results, lines = {}, []
    for e in entries:
        r = process(e, dry=dry)
        results[e["id"]] = r["result"]
        lines.append(f"- \"{e['text'][:90]}\" ({e.get('surface')}) -> {r['result']}")
        append(LOG, {"ts": now(), "entry": e, "outcome": r, "dry": dry})
        print(lines[-1])
    if dry:
        return 0
    hub("POST", "/feedback/done", {"ids": list(results), "results": results})
    # ONE told card per run: kind update (nothing to approve), one idem key per day so a second run edits it in place
    hub("POST", "/pending", {"text": f"feedback filed: {len(results)} sentence(s)", "kind": "update",
                             "context": "\n".join(lines)[:3000] + "\n\nWrong ledger or wrong rule? Say so on this card.",
                             "meta": {"origin": "feedback-session"}, "idem": "feedback-session:" + now()[:10]})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    ap.add_argument("--dry", action="store_true", help="classify and print; file nothing, mark nothing")
    add = sub.add_parser("add", help="queue one sentence (through the hub)")
    add.add_argument("--surface", required=True)
    add.add_argument("--text", required=True)
    add.add_argument("--item", default="")
    add.add_argument("--line", default="")
    add.add_argument("--kind", default="feedback")
    a = ap.parse_args(argv)
    if a.cmd == "add":
        print(json.dumps(hub("POST", "/feedback", {"surface": a.surface, "text": a.text, "item": a.item or None,
                                                   "line": a.line or None, "kind": a.kind})))
        return 0
    return run_once(a.dry)


if __name__ == "__main__":
    sys.exit(main())
