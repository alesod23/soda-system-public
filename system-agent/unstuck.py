#!/usr/bin/env python3
"""unstuck.py: the owner's YES on a STUCK card performs the one action (docs/05-system-agent.md).

`unstuck.py tick` (cron, every minute) reads the registry for stuck lines that carry a hub card, asks the hub for each
card (GET /item/<id>) and, on a resolved card:
  verdict yes  runs the line's for_owner action ONCE: {"kind": "run", "argv": [...]} (no shell), records the exit code
               and the tail of the output on the registry line. The system agent's next tick re-runs the check and
               closes the line when it passes.
  verdict no   records the no on the line. Nothing runs, no second card.

    unstuck.py tick [--dry]
"""
import argparse
import json
import subprocess
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import registry as R  # noqa: E402
import system_agent as SA  # noqa: E402


def perform(fh, dry=False, runner=None):
    ok, why = R.validate_for_owner(fh)
    if not ok:
        return {"ran": False, "error": why}
    if dry:
        return {"ran": False, "dry": True, "argv": fh["argv"]}
    if runner:
        return runner(fh["argv"])
    try:
        r = subprocess.run(fh["argv"], capture_output=True, text=True, timeout=fh.get("timeout_s", 300))
        return {"ran": True, "exit": r.returncode, "tail": ((r.stdout or "") + (r.stderr or ""))[-600:]}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ran": True, "exit": 127, "tail": str(e)[:300]}


def tick(dry=False, hub=None, runner=None):
    hub = hub or SA.hub
    out = []
    for l in R.load().values():
        if l.get("status") != "stuck" or not l.get("card") or l.get("unstuck"):
            continue
        it = hub("GET", "/item/" + urllib.parse.quote(str(l["card"])))
        if not it or not it.get("resolved"):
            continue
        if it.get("verdict") == "yes" and l.get("for_owner"):
            res = perform(l["for_owner"], dry=dry, runner=runner)
            rec = dict(l, unstuck=dict(res, verdict="yes"), status="verifying", next="the owner's yes ran the action; the next check decides")
        else:
            rec = dict(l, unstuck={"verdict": it.get("verdict"), "ran": False}, next="the owner said no: stays open, no second card")
        if not dry:
            R.put(rec)
        out.append({"id": l["id"], "unstuck": rec["unstuck"]})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(tick(dry=a.dry), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
