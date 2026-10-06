#!/usr/bin/env python3
"""todo_match.py: THE CONNECTION. An event (a mail, a calendar change, a meeting note) finds the nearest open to-dos,
CRM people, CRM review items and hub cards in the brain, and a model judges whether the event settles any of them.

Two uses:

  1. lookup (no model): `match(text, k)` -> hits from POST /brain/match on the door. Producers (a meeting loop, any
     card maker) call it BEFORE proposing a step, so the card can say "already on your to-do" or not be made at all.
  2. judge (one model call, headless): `judge(event, hits)` -> for each candidate: done / moved / none, with the
     evidence quote. A hub card proven done is closed through the hub's /close with the evidence (no verdict, its
     action never executed). A to-do verdict is PROPOSED (logged) unless TODO_TICK_CMD names the command that owns
     task state. Nothing is ever deleted. Every outcome is one line in $SODA_HOME/todo-match.jsonl.

    todo_match.py lookup "text" [--k 8] [--done]
    todo_match.py judge --text "event text" --source "email:<thread>" [--k 8] [--apply] [--dry]
    todo_match.py event --text "event text" --source "email:<thread>" [--dry]    (the hook: lookup, judge on a strong hit)

Env: SODA_DOOR (default http://127.0.0.1:4150), SODA_TOKEN_RO / SODA_TOKEN, HUB_URL, SODA_LLM_BIN (default claude),
SODA_LLM_MODEL (default opus), SODA_LLM_STUB (a file whose text answers every model call: tests and offline demos),
MATCH_STRONG (cosine similarity a hit needs before the judge runs, default 0.84),
TODO_TICK_CMD (optional argv template, JSON list, placeholders {id} {verdict} {due} {evidence}).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import soda_paths  # noqa: E402

DOOR = os.environ.get("SODA_DOOR", "http://127.0.0.1:4150").rstrip("/")
LOG = soda_paths.HOME / "todo-match.jsonl"
LLM_BIN = os.environ.get("SODA_LLM_BIN") or shutil.which("claude") or "claude"
LLM_MODEL = os.environ.get("SODA_LLM_MODEL", "opus")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
STRONG = float(os.environ.get("MATCH_STRONG", "0.84"))


def token(ro: bool = True) -> str:
    for name in (("SODA_TOKEN_RO", "SODA_TOKEN") if ro else ("SODA_TOKEN",)):
        if os.environ.get(name):
            return os.environ[name]
    return ""


def match(text: str, k: int = 8, include_done: bool = False, timeout: int = 30) -> list[dict]:
    """The nearest to-dos, review items, people and cards for an event text. [] when the door is unreachable."""
    body = json.dumps({"text": text[:6000], "k": k, "include_done": include_done}).encode("utf-8")
    req = urllib.request.Request(DOOR + "/brain/match", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-Soda-Token": token()})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8") or "[]")
    except Exception as e:
        log_line(event="lookup-failed", error=type(e).__name__ + " " + str(e)[:160])
        return []


def open_todos(hits: list[dict]) -> list[dict]:
    return [h for h in hits if h.get("kind") == "todo" and h.get("status") == "open"]


def open_reviews(hits: list[dict]) -> list[dict]:
    """The person's open CRM review item (kind 'review', id = pid, parent_id = due|owed|waiting|coming)."""
    return [h for h in hits if h.get("kind") == "review" and h.get("status") == "open"]


def already_open_text(hits: list[dict], max_items: int = 6) -> str:
    """One block for a producer's prompt: what is already open for this person or action."""
    todos = open_todos(hits)[:max_items]
    reviews = open_reviews(hits)[:3]
    people = [h for h in hits if h.get("kind") == "person"][:3]
    if not todos and not people and not reviews:
        return ""
    L = ["Already in the system (the brain lookup; judge whether any of it IS the step you are about to propose):"]
    for h in todos:
        L.append(f"- to-do {h['id']}" + (f" (due {h['due']})" if h.get("due") else "") + f": {str(h.get('title') or '')[:160]}")
    for h in reviews:
        txt = re.sub(r"\s+", " ", str(h.get("text") or ""))[:220]
        L.append(f"- CRM review item {h['id']} ({h.get('parent_id')}" + (f", step {h['due']}" if h.get("due") else "") + f"): {txt}")
    for h in people:
        txt = re.sub(r"\s+", " ", str(h.get("text") or ""))[:220]
        L.append(f"- CRM {h['id']}: {txt}")
    return "\n".join(L)


JUDGE_PROMPT = """An event just happened in the owner's world. Decide, for each candidate item below, whether the event
shows that item is DONE (the thing it asks for happened), MOVED (its date changed), or UNCHANGED. Only what the event text
proves; never a guess. A reply that merely mentions a topic does not complete a to-do about it.

EVENT (__SOURCE__):
__EVENT__

CANDIDATES:
__CANDS__

Answer with ONE JSON array and nothing else: [{"id": "<candidate id>", "verdict": "done" | "moved" | "none",
"due": "YYYY-MM-DD when moved, else \\"\\"", "quote": "the words of the event that prove it, \\"\\" when none", "why": "one line"}]
Candidates you leave out count as "none"."""


def llm(prompt: str, model: str | None = None, timeout: int = 240) -> str:
    """One model answer as text. The prompt travels on stdin, never as an argument (command-line length limits)."""
    stub = os.environ.get("SODA_LLM_STUB")
    if stub:
        return Path(stub).read_text(encoding="utf-8")
    r = subprocess.run([LLM_BIN, "-p", "--model", model or LLM_MODEL, "--output-format", "text", "--max-turns", "1"],
                       input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NOWIN)
    if r.returncode != 0:
        raise RuntimeError(f"{LLM_BIN} exit {r.returncode}: {(r.stderr or r.stdout)[-200:]}")
    return r.stdout or ""


def judge(event: str, source: str, hits: list[dict], model: str | None = None) -> list[dict]:
    cands = open_todos(hits) + [h for h in hits if h.get("kind") == "card" and h.get("status") == "open"]
    if not cands:
        return []
    cl = "\n".join(f"- {h['id']}" + (" (hub card: done = the thing it asks about already happened; its action is never executed)" if h.get("kind") == "card" else "")
                   + (f" (due {h['due']})" if h.get("due") and h.get("kind") != "card" else "") + f": {str(h.get('title') or '')[:200]}" for h in cands)
    prompt = JUDGE_PROMPT.replace("__SOURCE__", source).replace("__EVENT__", event[:5000]).replace("__CANDS__", cl)
    txt = llm(prompt, model)
    i, j = txt.find("["), txt.rfind("]")
    if i < 0 or j < i:
        raise RuntimeError("judge gave no JSON: " + txt[-200:])
    out = json.loads(txt[i:j + 1])
    byid = {h["id"]: h for h in cands}
    return [dict(v, title=byid[v["id"]].get("title"), kind=byid[v["id"]].get("kind"))
            for v in out if isinstance(v, dict) and v.get("id") in byid]


def close_card(cid: str, source: str, quote: str, why: str, dry: bool) -> dict:
    """A card whose outcome an event proves: closed with the evidence, NO verdict (never a yes), nothing executed."""
    reason = f"done, seen in {source}: \"{quote[:160]}\" {why[:120]}".strip()
    if dry:
        return {"applied": False, "dry": "close " + cid + ": " + reason}
    req = urllib.request.Request(soda_paths.HUB_URL + "/close",
                                 data=json.dumps({"id": cid, "reason": reason, "notify": False}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return dict(json.loads(urllib.request.urlopen(req, timeout=20).read().decode("utf-8") or "{}"), applied=True)
    except Exception as e:
        return {"applied": False, "error": str(e)[:160]}


def apply(verdict: dict, source: str, dry: bool) -> dict:
    """A hub card proven done -> /close with the evidence. A to-do done/moved -> TODO_TICK_CMD when set (the one writer
    of task state), else proposed only. Nothing else writes anything."""
    if verdict.get("kind") == "card":
        if verdict.get("verdict") != "done":
            return {"applied": False}
        return close_card(verdict["id"], source, verdict.get("quote") or "", verdict.get("why") or "", dry)
    if verdict.get("verdict") not in ("done", "moved"):
        return {"applied": False}
    tmpl = os.environ.get("TODO_TICK_CMD")
    if not tmpl:
        return {"applied": False, "proposed": f"{verdict['verdict']} {verdict['id']}"}
    ev = json.dumps({"by": "brain", "source": source, "quote": verdict.get("quote") or "", "why": verdict.get("why") or ""},
                    ensure_ascii=False)
    fill = {"id": verdict["id"], "verdict": verdict["verdict"], "due": verdict.get("due") or "", "evidence": ev}
    cmd = [str(a).format(**fill) for a in json.loads(tmpl)]
    if dry:
        return {"applied": False, "dry": " ".join(cmd)}
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=NOWIN)
    return {"applied": r.returncode == 0, "out": (r.stdout or r.stderr or "")[-200:]}


def on_event(text: str, source: str, do_apply: bool = True, k: int = 6, dry: bool = False, model: str | None = None) -> dict:
    """The one hook every loop calls on an incoming event: lookup first (no model); the judge only on a strong hit.
    Returns what happened; every outcome is a line in todo-match.jsonl."""
    text = (text or "").strip()
    if len(text) < 20:
        return {"skipped": "too short"}
    hits = match(text, k)
    strong = [h for h in hits if h.get("kind") in ("todo", "card") and h.get("status") == "open" and (h.get("sim") or 0) >= STRONG]
    top = max((h.get("sim") or 0) for h in hits) if hits else 0
    if not strong:
        log_line(event="lookup", source=source, hits=len(hits), top_sim=round(top, 3), judged=False,
                 reviews=[h["id"] for h in open_reviews(hits)][:5])
        return {"hits": len(hits), "top_sim": top, "judged": False}
    try:
        verdicts = judge(text, source, strong, model)
    except Exception as e:
        log_line(event="judge-failed", source=source, error=str(e)[:200])
        return {"error": str(e)[:200]}
    out = []
    for v in verdicts:
        res = apply(v, source, dry or not do_apply) if v.get("verdict") != "none" else {"applied": False}
        log_line(event="judged", source=source, id=v["id"], kind=v.get("kind"), verdict=v.get("verdict"), due=v.get("due"),
                 quote=v.get("quote"), why=v.get("why"), **{kk: res[kk] for kk in res if kk != "ok"})
        out.append(dict(v, **res))
    return {"hits": len(hits), "top_sim": top, "judged": True, "verdicts": out}


def log_line(**e):
    e = dict(at=datetime.now(timezone.utc).isoformat(timespec="seconds"), **e)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    l = sub.add_parser("lookup"); l.add_argument("text"); l.add_argument("--k", type=int, default=8); l.add_argument("--done", action="store_true")
    j = sub.add_parser("judge"); j.add_argument("--text", required=True); j.add_argument("--source", default="manual")
    j.add_argument("--k", type=int, default=8); j.add_argument("--apply", action="store_true"); j.add_argument("--dry", action="store_true")
    j.add_argument("--model", default=None)
    e = sub.add_parser("event", help="the loop hook: lookup, judge on a strong hit, apply")
    e.add_argument("--text", default=""); e.add_argument("--source", default="manual")
    e.add_argument("--text-file", help="read the event text from a file instead"); e.add_argument("--dry", action="store_true")
    e.add_argument("--model", default=None)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if a.cmd == "event":
        txt = Path(a.text_file).read_text(encoding="utf-8", errors="replace") if a.text_file else a.text
        print(json.dumps(on_event(txt, a.source, do_apply=True, dry=a.dry, model=a.model), ensure_ascii=False))
        return 0
    if a.cmd == "lookup":
        for h in match(a.text, a.k, a.done):
            print(f"{h.get('score', 0):.4f} {h.get('kind')} {h.get('id')} [{h.get('status')}] {str(h.get('title') or '')[:90]}")
        return 0
    hits = match(a.text, a.k)
    verdicts = judge(a.text, a.source, hits, a.model)
    for v in verdicts:
        res = apply(v, a.source, a.dry or not a.apply) if v.get("verdict") != "none" else {"applied": False}
        log_line(event="judged", source=a.source, id=v["id"], verdict=v.get("verdict"), due=v.get("due"),
                 quote=v.get("quote"), why=v.get("why"), **{k: res[k] for k in res if k != "ok"})
        print(json.dumps(dict(v, **res), ensure_ascii=False))
    if not verdicts:
        log_line(event="no-candidates", source=a.source, hits=len(hits))
        print("no open item near this event")
    return 0


if __name__ == "__main__":
    sys.exit(main())
