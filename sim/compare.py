#!/usr/bin/env python3
"""compare.py: does a setup improve the system? Per-arm score curves over the simulated days, and a verdict.

    python3 sim/compare.py <run> <run> [<run> ...] [--base <run>] [--from-day 2]

A <run> is a run name under $SODA_HOME/sim/ or a path to a run directory. The first run is the base unless --base
says otherwise; every other run is compared with it.

The verdict compares the per-event scores (sim/judge.py) of each arm with the base's, pooled over the days from
--from-day on (default 1). Significance is a two-sided permutation test on the difference of means (stdlib, seeded,
5000 shuffles). With a few dozen events per arm, read it as indicative: a simulated world with a stub cast can show
that a setup helps HERE; only live numbers show it helps in real life.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import soda_paths as P  # noqa: E402


def load(run: str) -> dict:
    d = Path(run) if Path(run).is_dir() else P.HOME / "sim" / run
    r = json.loads((d / "result.json").read_text(encoding="utf-8"))
    r["_events"] = [json.loads(l) for l in (d / "scores.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    return r


def permutation_p(a: list[float], b: list[float], n: int = 5000, seed: int = 1) -> float:
    if not a or not b:
        return 1.0
    obs = abs(sum(b) / len(b) - sum(a) / len(a))
    pool, k = a + b, len(a)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        rng.shuffle(pool)
        if abs(sum(pool[k:]) / len(b) - sum(pool[:k]) / k) >= obs - 1e-12:
            hits += 1
    return (hits + 1) / (n + 1)


def mean(xs):
    return sum(xs) / len(xs) if xs else float("nan")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--base", help="the run every other run is compared with (default: the first)")
    ap.add_argument("--from-day", type=int, default=1, help="pool events from this simulated day on")
    a = ap.parse_args(argv)
    runs = [load(r) for r in a.runs]
    base = load(a.base) if a.base else runs[0]
    if a.base:
        runs = [base] + [r for r in runs if r["run"] != base["run"]]

    days = max(r["days"] for r in runs)
    label = lambda r: f"{r['arm']} ({r['run']})"
    w = max(len(label(r)) for r in runs) + 2
    print("score per simulated day (0-100, mean over that day's events)\n")
    print(" " * w + "".join(f"  d{d:<4}" for d in range(1, days + 1)) + "   mean")
    for r in runs:
        by = {c["day"]: c["score"] for c in r["curve"]}
        vals = [by.get(d) for d in range(1, days + 1)]
        print(label(r).ljust(w) + "".join(f"  {v:5.1f}" if v is not None else "      -" for v in vals)
              + f"  {mean([v for v in vals if v is not None]):5.1f}")
    print("\ncomponents (share of actionable events, all days)\n")
    comps = ["detected", "person", "timely", "step", "artifacts", "clean"]
    print(" " * w + "".join(f"{c:>10}" for c in comps) + "   rules filed   sends")
    for r in runs:
        parts = [e["parts"] for e in r["_events"] if e.get("parts")]
        print(label(r).ljust(w) + "".join(f"{mean([p[c] for p in parts]):10.2f}" for c in comps)
              + f"   {r['counts']['rules_filed']:>11}   {r['counts']['sends']:>5}")

    print()
    pick = lambda r: [e["score"] for e in r["_events"] if e["day"] + 1 >= a.from_day]
    b_scores = pick(base)
    for r in runs:
        if r is base:
            continue
        s = pick(r)
        delta = mean(s) - mean(b_scores)
        p = permutation_p(b_scores, s)
        if p < 0.05:
            note = "a real difference in this simulated world" + (" (better)" if delta > 0 else " (worse)")
        elif p < 0.2:
            note = "a hint, not established: run more days or seeds"
        else:
            note = "no clear difference"
        first = [c["score"] for c in r["curve"] if c["day"] == 1]
        last = [c["score"] for c in r["curve"] if c["day"] == r["days"]]
        trend = f"; its own curve d1 {first[0]:.1f} -> d{r['days']} {last[0]:.1f}" if first and last else ""
        print(f"VERDICT {r['arm']} vs {base['arm']}: {delta:+.1f} points per event "
              f"({mean(s):.1f} vs {mean(b_scores):.1f}, n={len(s)} vs {len(b_scores)}, days >= {a.from_day}), "
              f"permutation p={p:.3f}: {note}{trend}")
    if any(not r.get("stub") for r in runs) and any(r.get("stub") for r in runs):
        print("note: stub and model runs are mixed; compare like with like.")
    if len({r.get('seed') for r in runs}) > 1:
        print("note: the runs used different world seeds, so they did not see the same events.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
