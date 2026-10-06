"""hits.py: write one observer hit line (the rule loop's measurement, docs/01-rule-loop.md "Hit line").

    {"ts","surface","rule","action":"ok|flag|regen|downgrade|block","item","model","skill_v","path","by","note"}

`surface` is the LEDGER the rule belongs to (the compiler counts by it); `by` names the observer. action "ok" means
the rule was checked and held; anything else counts as a hit. An observer never raises and never blocks the work.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import soda_paths as P  # noqa: E402


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def skill_version(name: str) -> str | None:
    """The `skill_v:` line of an installed compiled skill, so every hit says which compile it was judged against."""
    try:
        head = (P.SKILLS / name / "SKILL.md").read_text(encoding="utf-8")[:1500]
    except OSError:
        return None
    m = re.search(r"^skill_v:\s*(\S+)", head, re.M)
    return m.group(1) if m else None


def hit(surface, rule, action, item, model=None, skill_v=None, path=None, by=None, note="", hits_file=None):
    """Append one hit line. Never raises: a logging failure must not touch the work."""
    row = {"ts": now_iso(), "surface": surface, "rule": rule, "action": action, "item": item,
           "model": model, "skill_v": skill_v, "path": path, "by": by, "note": (note or "")[:300]}
    f = Path(hits_file or P.HITS)
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as e:
        sys.stderr.write(f"hit log failed: {e}\n")
    return row
