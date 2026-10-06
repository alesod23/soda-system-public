#!/usr/bin/env python3
"""Which rules actually fire: the measurement half of the rule loop.

    python rules/rule_stats.py [--days 30] [--surface <ledger key>] [--json]

Per surface and rule: rules in the ledger, hits in rule-hits.jsonl by action (ok = checked and respected, flag /
downgrade / rewrite = violated and caught), first and last hit, days since; and the two lists that decide pruning:
rules that NEVER appear in the hits (nobody checks them) and rules checked often that were never violated (dead
weight in every prompt). `all` and `SOFT` hits are counted apart.
"""
import json, re, sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))
import soda_paths as P  # noqa: E402
from hit_filter import is_test_hit  # noqa: E402  fixture hit lines never count
HITS = P.HITS
LEDGERS = P.ledger_keys()


def rules_of(surface):
    p = P.ledger_path(surface)
    try:
        s = p.read_text(encoding="utf-8")
    except OSError:
        return []
    return re.findall(r"^\| (H\d+) \|", s, flags=re.M)


def main():
    a = sys.argv[1:]
    days = int(a[a.index("--days") + 1]) if "--days" in a else 30
    only = a[a.index("--surface") + 1] if "--surface" in a else None
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hits = defaultdict(lambda: defaultdict(int)); first, last = {}, {}
    n = 0
    lines = open(HITS, encoding="utf-8", errors="replace") if HITS.exists() else []
    for l in lines:
        if not l.startswith("{"):
            continue
        try:
            r = json.loads(l)
        except ValueError:
            continue
        ts = str(r.get("ts") or "")
        if ts < since or is_test_hit(r):
            continue
        n += 1
        k = (str(r.get("surface") or "?"), str(r.get("rule") or "none"))
        hits[k][str(r.get("action") or "?")] += 1
        first.setdefault(k, ts); last[k] = ts
    out = {}
    now = datetime.now(timezone.utc)
    for surface in LEDGERS:
        if only and surface != only:
            continue
        rows = []
        for rid in rules_of(surface):
            h = hits.get((surface, rid), {})
            checked = sum(h.values()); violated = sum(v for k, v in h.items() if k not in ("ok", "?"))
            ls = last.get((surface, rid))
            rows.append({"rule": rid, "checked": checked, "violated": violated, "by_action": dict(h), "first": first.get((surface, rid)), "last": ls,
                         "days_since": (now - datetime.fromisoformat(ls.replace("Z", "+00:00"))).days if ls else None})
        extra = {k[1]: dict(v) for k, v in hits.items() if k[0] == surface and k[1] not in {r["rule"] for r in rows}}
        never = [r["rule"] for r in rows if r["checked"] == 0]
        never_violated = [r["rule"] for r in rows if r["checked"] >= 20 and r["violated"] == 0]
        out[surface] = {"rules": len(rows), "never_checked": never, "checked_20plus_never_violated": never_violated, "extra_keys": extra, "rows": rows}
    if "--json" in a:
        print(json.dumps(out, indent=1)); return 0
    print(f"{HITS.name}: {n} lines in the last {days} days")
    for surface, o in out.items():
        rows = o["rows"]
        live = [r for r in rows if r["checked"]]
        print(f"\n== {surface}: {o['rules']} rules, {len(live)} ever checked, {len(o['never_checked'])} never checked, {len(o['checked_20plus_never_violated'])} checked 20+ times and never violated")
        for r in sorted(live, key=lambda r: -r["checked"])[:12]:
            print(f"   {r['rule']:5s} checked {r['checked']:5d} violated {r['violated']:4d}  last {str(r['last'])[:10]} ({r['days_since']} d)  {r['by_action']}")
        if o["extra_keys"]:
            print("   other keys:", {k: sum(v.values()) for k, v in o["extra_keys"].items()})
        print("   never checked:", " ".join(o["never_checked"]) or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
