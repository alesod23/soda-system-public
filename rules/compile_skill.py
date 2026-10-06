#!/usr/bin/env python3
"""
compile_skill.py - compile a surface's ledger into its one-screen skill (docs/01-rule-loop.md).

The ledger ($SODA_HOME/ledgers/<NAME>-CONTRACT.md) is every sentence of feedback the owner ever gave on that surface,
dated, verbatim, append-only. The skill is that ledger compiled: at most `skill_cap` rules in front, each with an
example, everything else in an "Assumed" list that the observer still checks and that comes back in front the moment
it fires. Every decider (a drafting session, the notification classifier, the hub's posting sessions) reads the
compiled skill, never the raw ledger.

Inputs
  --contract <key>             which ledger (keys in config/surfaces.json)
  $SODA_HOME/skill-maps/skill-map-<key>.json
                               hand-maintained: which ledger rows merge into which compiled rule, the group, the rule
                               sentence, one example, and `front` as the initial choice when no hits exist yet;
                               `usage` / `procedure` are the surface's own "How to use" and "Procedure" lines;
                               optional `glyphs` is a string of the surface's own markers that the no-emoji check
                               lets through
  $SODA_HOME/rule-hits.jsonl   optional, one line per observer decision:
                               {"ts","surface","rule","action","item","model","skill_v","path","by","note"}
                               action != "ok" counts as a hit; "ok" means checked and passed
  $SODA_HOME/rule-loop-state.json  optional config: skill_cap (15), retire_after_applicable (50)

Demotion: a compiled rule with 0 hits in the last `retire_after_applicable` applicable observed artifacts (items where
any of its ledger rules was checked) goes to Assumed. With fewer observed artifacts than that, a rule that fired at
all stays or comes in front; an untouched rule keeps the mapping's `front` flag. The cap is enforced by hit count,
then mapping order. A ledger row that no mapping entry covers yet compiles on its own, verbatim: HARD in front at
once, SOFT assumed; the summary says to map it. The loop never stalls on the JSON.

LIKED rows in the ledger (addrule.py --like) become the example of the compiled rule they map to (`[maps-to: H<n>]`),
otherwise they are listed under "Reference examples".

Usage
  compile_skill.py --contract hub [--out PATH | --install] [--hits PATH] [--rev a]
  --install writes $SODA_SKILLS_DIR/<name>/SKILL.md (backing up the current one once per day).
Without --out/--install the skill goes to stdout and the summary to stderr. Every run prints the diff summary
(merged / in front / assumed, promoted / demoted against the previous output).
Exit 0 = compiled and self-checked; 1 = a check failed (nothing written).
"""
import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))
import soda_paths as P  # noqa: E402
from hit_filter import is_test_hit  # noqa: E402  fixture hit lines never count

STATE = P.LOOP_STATE
HITS = P.HITS
DEFAULTS = {"skill_cap": 15, "retire_after_applicable": 50}
NEW_GROUP = "new, not yet mapped"

ROW_RE = re.compile(r"^\| H(\d+) \| (.+) \| ([^|]+?) \|\s*$")
TAG_RE = re.compile(r"\[ledger ([^\]]+)\]")
QUOTE_RE = re.compile(r'\*"(.+?)"\*')
ITEM_RE = re.compile(r"\[item: ([^\]]+)\]")
MAPS_RE = re.compile(r"\[maps-to: (H\d+)\]")
BAD_DASH = "—–"
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿]")


def die(msg):
    sys.stderr.write("compile_skill: " + msg + "\n")
    sys.exit(1)


def load_config():
    cfg = dict(DEFAULTS)
    if STATE.exists():
        try:
            j = (lambda d: d.get('config', d))(json.loads(STATE.read_text(encoding="utf-8")))
            for k in DEFAULTS:
                if isinstance(j.get(k), int):
                    cfg[k] = j[k]
        except ValueError:
            sys.stderr.write(f"compile_skill: {STATE.name} is not JSON, defaults used\n")
    return cfg


def parse_ledger(path):
    """-> {n: {"text","source","liked","quote","item","maps_to"}} for every `| H<n> | ... |` row."""
    rules = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = ROW_RE.match(line)
        if not m:
            continue
        n, text, source = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
        if n in rules:
            die(f"H{n} appears twice in {path.name}")
        r = {"text": text, "source": source, "liked": text.startswith("LIKED:")}
        if r["liked"]:
            q, it, mt = QUOTE_RE.search(text), ITEM_RE.search(text), MAPS_RE.search(text)
            r["quote"] = q.group(1) if q else ""
            r["item"] = it.group(1).strip() if it else ""
            r["maps_to"] = mt.group(1) if mt else ""
        rules[n] = r
    if not rules:
        die(f"no `| H<n> |` rows in {path.name}")
    return rules


def load_hits(path, surface):
    if not path.exists():
        return []
    hits = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            j = json.loads(line)
        except ValueError:
            sys.stderr.write(f"compile_skill: {path.name}:{i} is not JSON, skipped\n")
            continue
        if j.get("surface") != surface or not j.get("rule") or not j.get("item") or is_test_hit(j):
            continue
        hits.append(j)
    hits.sort(key=lambda j: str(j.get("ts", "")))
    return hits


def decide(rules, hits, cfg):
    """Sets rule["front"], rule["hits"], rule["applicable"], rule["why"] in place; returns the front list."""
    n_retire, cap = cfg["retire_after_applicable"], cfg["skill_cap"]
    by_rule = {}
    for h in hits:
        by_rule.setdefault(h["rule"], []).append(h)
    for r in rules:
        keys = [k for k in r["ledger"] if k.startswith("H")]
        lines = [h for k in keys for h in by_rule.get(k, [])]
        lines.sort(key=lambda j: str(j.get("ts", "")))
        items = []
        for h in lines:
            if h["item"] not in items:
                items.append(h["item"])
        window = set(items[-n_retire:])
        r["applicable"] = len(items)
        r["hits"] = sum(1 for h in lines if h["item"] in window and h.get("action") != "ok")
        r["hits_ever"] = sum(1 for h in lines if h.get("action") != "ok")
        if not hits:
            r["why"] = "no hits file, mapping flag"
        elif r["applicable"] >= n_retire:
            r["front"] = r["hits"] > 0
            r["why"] = f"{r['hits']} hits in the last {n_retire} applicable"
        elif r["hits_ever"] > 0:
            r["front"] = True
            r["why"] = f"fired {r['hits_ever']}x, {r['applicable']} applicable observed"
        else:
            r["why"] = f"{r['applicable']} applicable observed, no hit, mapping flag"
    front = [r for r in rules if r["front"]]
    # Cap order: a fresh unmapped HARD row is the newest feedback and outranks everything.
    front.sort(key=lambda r: (0 if r.get("unmapped") else 1, -r["hits"], rules.index(r)))
    for r in front[cap:]:
        r["front"] = False
        r["why"] += f"; over the cap of {cap}"
    return [r for r in rules if r["front"]]


def tag(r, likes):
    parts = ",".join(r["ledger"]) if r["ledger"] else ""
    if likes:
        parts += (" " if parts else "") + "+like " + ",".join(f"H{n}" for n in likes)
    bits = []
    if parts:
        bits.append("ledger " + parts)
    if r.get("memory"):
        bits.append("memory " + ", ".join(r["memory"]))
    return "[" + "; ".join(bits or ["ledger none"]) + "]"


def refs(text, rules):
    """`{R:id}` -> `R7` when that rule is in front, else `the <id> rule (Assumed)`."""
    def one(m):
        rid = m.group(1)
        for r in rules:
            if r["id"] == rid:
                return f"R{r['num']}" if r.get("num") else f"the {rid} rule (Assumed)"
        die(f"procedure/usage references unknown rule id {rid!r}")
    return re.sub(r"\{R:([\w-]+)\}", one, text)


def render(m, ledger, rules, front, today, rev, hits_used, ledger_name=""):
    n_ledger = sum(1 for r in ledger.values() if not r["liked"])
    liked = {n: r for n, r in ledger.items() if r["liked"]}
    like_for = {}
    for n, r in liked.items():
        for cr in rules:
            if r["maps_to"] and r["maps_to"] in cr["ledger"]:
                like_for.setdefault(cr["id"], []).append(n)
                break
    mapped_likes = {n for ns in like_for.values() for n in ns}
    orphan_likes = [n for n in sorted(liked) if n not in mapped_likes]

    L = []
    L += ["---", f"name: {m['skill_name']}", "description: >"]
    L += ["  " + w for w in wrap(m["description"], 84)]
    L += [f"compiled: {today}", f"skill_v: {today}{rev}", f"ledger: {ledger_name}",
          f"rules_in_ledger: {n_ledger}", f"rules_in_front: {len(front)}", "---", ""]
    title = {'crm': 'CRM'}.get(m['skill_name'], m['skill_name'].capitalize())
    L += [f"# {title} (compiled from the ledger, {today})", ""]
    num = 0
    for g in m["groups"]:
        for r in [r for r in front if r["group"] == g]:
            num += 1
            r["num"] = num
    L += ["## How to use this", ""]
    L += [f"{i}. {refs(u, rules)}" for i, u in enumerate(m["usage"], 1)]
    L += ["", "## The rules in front", ""]
    for g in m["groups"]:
        gr = [r for r in front if r["group"] == g]
        if not gr:
            continue
        L += [f"### {g.capitalize()}", ""]
        for r in gr:
            line = f"**R{r['num']}.** {r['rule']}"
            if r.get("example"):
                line += f" *e.g.* {r['example']}"
            for n in like_for.get(r["id"], []):
                line += f" *liked (H{n}):* \"{liked[n]['quote']}\""
            line += " " + tag(r, like_for.get(r["id"], []))
            L += [line, ""]
    L += ["## Assumed (still checked by the observer, not in front)", ""]
    for g in m["groups"]:
        for r in rules:
            if r["front"] or r["group"] != g:
                continue
            L += [f"- {r['rule']} {tag(r, like_for.get(r['id'], []))}"]
    L += [""]
    if orphan_likes:
        L += ["## Reference examples (liked by the owner, not yet mapped to a rule)", ""]
        for n in orphan_likes:
            r = liked[n]
            item = f" ({r['item']})" if r["item"] else ""
            L += [f"- \"{r['quote']}\"{item} [ledger H{n}]"]
        L += [""]
    L += ["## Procedure", ""]
    L += [f"{i}. {refs(p, rules)}" for i, p in enumerate(m["procedure"], 1)]
    return "\n".join(L) + "\n", n_ledger, orphan_likes


def wrap(text, width):
    out, cur = [], ""
    for w in text.split():
        if cur and len(cur) + 1 + len(w) > width:
            out.append(cur); cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        out.append(cur)
    return out


def check(text, ledger, m, cap):
    """Every H once, no dashes, no emoji, cap respected, size within a screen."""
    errs = []
    seen = {}
    for t in TAG_RE.findall(text):
        for h in re.findall(r"H\d+", t):
            seen[h] = seen.get(h, 0) + 1
    for n in ledger:
        c = seen.get(f"H{n}", 0)
        if c != 1:
            errs.append(f"H{n} appears {c}x in [ledger ...] tags (must be exactly once)")
    for h in seen:
        if int(h[1:]) not in ledger:
            errs.append(f"{h} is in the mapping but not in the ledger")
    glyphs = set(m.get("glyphs", ""))   # the surface's own markers (hub cards), the only emoji allowed
    for i, line in enumerate(text.splitlines(), 1):
        if any(ch in line for ch in BAD_DASH):
            errs.append(f"line {i}: em/en dash")
        if EMOJI_RE.search("".join(ch for ch in line if ch not in glyphs)):
            errs.append(f"line {i}: emoji")
    n_front = len(re.findall(r"^\*\*R\d+\.\*\*", text, re.M))
    if n_front > cap:
        errs.append(f"{n_front} rules in front, cap is {cap}")
    n_lines = text.count("\n")
    if n_lines > 110:
        errs.append(f"{n_lines} lines: no longer one screen (limit 110)")
    return errs


def previous_state(path):
    """front / assumed ledger-tag sets of an earlier compiled output, for the promoted/demoted summary."""
    if not path or not path.exists():
        return None
    t = path.read_text(encoding="utf-8")
    if "## Assumed" not in t:
        return None
    head, _, tail = t.partition("## Assumed")
    key = lambda tg: frozenset(re.findall(r"H\d+", tg))
    return {"front": {key(tg) for tg in TAG_RE.findall(head) if key(tg)},
            "assumed": {key(tg) for tg in TAG_RE.findall(tail) if key(tg)}}


def main():
    p = argparse.ArgumentParser(description="compile a surface's ledger into its skill")
    p.add_argument("--contract", required=True, choices=P.ledger_keys())
    p.add_argument("--map", help="mapping file (default $SODA_HOME/skill-maps/skill-map-<surface>.json)")
    p.add_argument("--hits", help=f"hit lines (default {HITS})")
    p.add_argument("--out", help="write the skill here")
    p.add_argument("--install", action="store_true", help="write $SODA_SKILLS_DIR/<name>/SKILL.md")
    p.add_argument("--rev", default="a", help="skill_v suffix letter for a second compile the same day")
    p.add_argument("--ledger", help=argparse.SUPPRESS)   # tests only: read another copy of the ledger
    a = p.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="\n"); sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

    contract = Path(a.ledger) if a.ledger else P.ledger_path(a.contract)
    if not contract.exists():
        die(f"ledger not found: {contract}")
    map_path = Path(a.map) if a.map else P.SKILL_MAPS / f"skill-map-{a.contract}.json"
    if not map_path.exists():
        die(f"mapping not found: {map_path} (write it by hand first: rules, groups, examples)")
    m = json.loads(map_path.read_text(encoding="utf-8"))
    if m.get("surface") != a.contract:
        die(f"{map_path.name} is for surface {m.get('surface')!r}, not {a.contract!r}")
    cfg = load_config()
    ledger = parse_ledger(contract)
    hits_path = Path(a.hits) if a.hits else HITS
    hits = load_hits(hits_path, a.contract)
    rules = [dict(r) for r in m["rules"]]
    for r in rules:
        r.setdefault("ledger", []); r.setdefault("memory", []); r.setdefault("front", False)
    # A row given after the mapping was last touched compiles on its own, verbatim: HARD in
    # front at once, SOFT assumed. The summary says to map it; the loop never stalls on the JSON.
    mapped = {h for r in rules for h in r["ledger"]}
    unmapped = []
    for n in sorted(ledger):
        if ledger[n]["liked"] or f"H{n}" in mapped:
            continue
        soft = ledger[n]["text"].startswith("**SOFT")
        text = re.sub(r"^\*\*SOFT, flag only\.\*\*\s*", "", ledger[n]["text"])
        rules.append({"id": f"H{n}", "group": NEW_GROUP, "front": not soft, "ledger": [f"H{n}"], "memory": [],
                      "rule": text, "example": "", "unmapped": True})
        unmapped.append(f"H{n}")
    if unmapped and NEW_GROUP not in m["groups"]:
        m["groups"] = list(m["groups"]) + [NEW_GROUP]
    front = decide(rules, hits, cfg)

    today = dt.date.today().isoformat()
    text, n_ledger, orphan_likes = render(m, ledger, rules, front, today, a.rev, bool(hits), contract.name)
    errs = check(text, ledger, m, cfg["skill_cap"])
    if errs:
        for e in errs:
            sys.stderr.write("compile_skill: CHECK FAILED: " + e + "\n")
        return 1

    out = Path(a.out) if a.out else None
    if a.install:
        out = P.SKILLS / m["skill_name"] / "SKILL.md"
    prev = previous_state(out)

    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        if out.exists() and out.read_text(encoding="utf-8") != text:
            bak = out.with_name(out.name + f".bak-{today.replace('-', '')}")
            if not bak.exists():
                bak.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
        out.write_text(text, encoding="utf-8", newline="\n")
        log = sys.stdout
    else:
        sys.stdout.write(text)
        log = sys.stderr

    n_liked = sum(1 for r in ledger.values() if r["liked"])
    n_mem = len({mm for r in rules for mm in r["memory"]})
    log.write(f"compiled {a.contract}: {n_ledger} ledger rules + {n_mem} memory files -> {len(rules)} compiled rules; "
              f"in front {len(front)} (cap {cfg['skill_cap']}), assumed {len(rules) - len(front)}; "
              f"liked {n_liked} ({len(orphan_likes)} unmapped); hits {'none for this surface in ' + hits_path.name + ', mapping flags used' if not hits else str(len(hits)) + ' lines from ' + hits_path.name}\n")
    for r in rules:
        log.write(f"  {'FRONT  ' if r['front'] else 'assumed'} {r['id']:<22} {tag(r, [])[:60]:<60} {r['why']}\n")
    if prev:
        key = lambda r: frozenset(h for h in r["ledger"] if h.startswith("H"))
        promoted = [r["id"] for r in rules if r["front"] and key(r) and key(r) in prev["assumed"]]
        demoted = [r["id"] for r in rules if not r["front"] and key(r) and key(r) in prev["front"]]
        log.write(f"  vs previous: promoted {promoted or 'none'}, demoted {demoted or 'none'}\n")
    if unmapped:
        log.write(f"  UNMAPPED ledger rules compiled verbatim: {', '.join(unmapped)} -> merge them into {map_path.name}\n")
    for c in m.get("contradictions", []):
        log.write("  contradiction (not resolved here): " + c + "\n")
    if out:
        log.write(f"wrote {out}; {text.count(chr(10))} lines\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
