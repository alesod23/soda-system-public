#!/usr/bin/env python3
"""system_agent.py: the maintainer of the system (docs/05-system-agent.md).

Three stages, cheapest first:
  1. CHECKS, no model: declared in config.json (copy config.example.json), each with a stable `key`. Kinds:
       http        GET url, pass on 2xx                   {"url": "...", "timeout_s": 5}
       tcp         a port accepts a connection            {"host": "127.0.0.1", "port": 5432}
       file_fresh  a file changed in the last N minutes   {"path": "...", "max_age_min": 30}
       systemd     a unit is active                       {"unit": "my-hub.service", "user": false}
       command     argv exits 0 (no shell)                {"argv": ["pg_isready", "-q"]}
  2. WHITELIST FIXES: a check may declare `fix` = {"kind": "systemd_restart", "unit": ...} or {"kind": "command",
     "argv": [...]}. Capped per incident (config fix_caps[kind]) and spaced (fix_gap_min). A fix counts only when the
     check then passes `registry_close_after_passes` times in a row, in this agent's own later ticks.
  3. STUCK: after `escalate_after_runs` failing ticks with no fix left, the line goes `stuck` and ONE hub card
     "STUCK on <key>" is posted (idempotent key, so it pings once). If the check declares `for_owner` (one action only
     the owner may authorise), the owner's YES on the card runs it (unstuck.py). The card closes itself when the check passes.

It never sends to a person, never signs in, never deletes data: the only outbound call is to the local approval hub.

    system_agent.py check [--dry]          run every check (cron: every 10 min)
    system_agent.py check --only <key>     one check, prints OK/BAD, exits 0/1 (what a fix session re-runs as proof)
    system_agent.py status                 the last run, check by check
    system_agent.py registry [--md FILE]   the open registry lines (optionally rendered as markdown)
    system_agent.py update [--dry]         the daily system update: ONE hub card of kind `update`
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "lib"))
import registry as R  # noqa: E402
import soda_paths as P  # noqa: E402

CFG_PATH = Path(os.environ.get("SYSAGENT_CONFIG") or (HERE / "config.json"))
STATE_DIR = P.HOME / "system-agent"
STATE = STATE_DIR / "state.json"
RUNS = STATE_DIR / "runs.jsonl"
OFF = STATE_DIR / "OFF"


def cfg():
    p = CFG_PATH if CFG_PATH.exists() else HERE / "config.example.json"
    return json.loads(p.read_text(encoding="utf-8"))


def now():
    return datetime.now(timezone.utc)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def rd(p, default):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def wr(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)


def append(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def run(argv, timeout=60):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return r.returncode, ((r.stdout or "") + (r.stderr or ""))[-600:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)[:300]


def hub(method, path, payload=None, timeout=10):
    """The local approval hub, or None when it does not answer (the agent never crashes on it)."""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(P.HUB_URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except Exception:
        return None


# ------------------------------------------------------------------ the checks

def run_check(c):
    """-> (ok, evidence). Never raises."""
    kind = c.get("kind")
    try:
        if kind == "http":
            with urllib.request.urlopen(c["url"], timeout=c.get("timeout_s", 5)) as r:
                return 200 <= r.status < 300, f"HTTP {r.status}"
        if kind == "tcp":
            with socket.create_connection((c.get("host", "127.0.0.1"), int(c["port"])), timeout=c.get("timeout_s", 3)):
                return True, f"port {c['port']} open"
        if kind == "file_fresh":
            p = Path(os.path.expanduser(os.path.expandvars(c["path"])))
            if not p.exists():
                return False, f"{p.name} missing"
            age = (now().timestamp() - p.stat().st_mtime) / 60
            return age <= c["max_age_min"], f"{p.name} changed {age:.0f} min ago (max {c['max_age_min']})"
        if kind == "systemd":
            argv = ["systemctl"] + (["--user"] if c.get("user") else []) + ["is-active", c["unit"]]
            rc, out = run(argv, 15)
            return rc == 0, out.strip() or f"exit {rc}"
        if kind == "command":
            rc, out = run(c["argv"], c.get("timeout_s", 60))
            return rc == 0, f"exit {rc}" + (f": {out.strip()[-160:]}" if out.strip() else "")
        return False, f"unknown check kind {kind!r}"
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:200]}"


def apply_fix(fix, dry):
    if dry:
        return 0, "dry run: not executed"
    if fix["kind"] == "systemd_restart":
        return run(["systemctl"] + (["--user"] if fix.get("user") else []) + ["restart", fix["unit"]], 60)
    if fix["kind"] == "command":
        return run(fix["argv"], fix.get("timeout_s", 120))
    return 2, f"fix kind {fix['kind']!r} is not whitelisted"


def tick_one(c, conf, lines, dry=False, t=None):
    """One check through the three stages. Returns a result dict; writes registry lines unless dry."""
    t = t or now()
    key = c["key"]
    rid = f"SA-{key}"
    ok, ev = run_check(c)
    line = lines.get(rid)
    res = {"key": key, "ok": ok, "evidence": ev}
    put = (lambda l: l) if dry else R.put
    if ok:
        if line and R.is_open(line):
            passes = int(line.get("passes") or 0) + 1
            need = conf.get("registry_close_after_passes", 2)
            if passes >= need:
                res["closed"] = rid
                put(dict(line, status="fixed", passes=passes, evidence=ev, next="closed: the check passed "
                         f"{passes} times in a row"))
                if line.get("card"):
                    hub("POST", "/close", {"id": line["card"], "reason": f"check {key} passes again"}) if not dry else None
            else:
                res["verifying"] = rid
                put(dict(line, status="verifying", passes=passes, evidence=ev, next=f"one more pass closes it ({passes}/{need})"))
        return res
    # failing
    if not line or not R.is_open(line):
        line = {"id": rid, "key": key, "what": c.get("what") or f"check {key} fails", "since": iso(t),
                "class": "owner_command" if c.get("owner_only") else "maintenance", "owner": "agent",
                "attempts": 0, "fails": 0, "fixes": {}, "status": "open"}
    line = dict(line, passes=0, fails=int(line.get("fails") or 0) + 1, evidence=ev)
    fix = c.get("fix")
    if fix and line["class"] == "maintenance":
        cap = conf.get("fix_caps", {}).get(fix["kind"], 1)
        done = int((line.get("fixes") or {}).get(fix["kind"], 0))
        last = parse(line.get("last_fix_at"))
        gap_ok = not last or (t - last) >= timedelta(minutes=conf.get("fix_gap_min", 25))
        if done < cap and gap_ok:
            rc, out = apply_fix(fix, dry)
            line["fixes"] = dict(line.get("fixes") or {}, **{fix["kind"]: done + 1})
            line.update(last_fix_at=iso(t), last_action=f"{fix['kind']} -> exit {rc}", status="fixing",
                        attempts=int(line.get("attempts") or 0) + 1,
                        next=f"re-check next tick; it counts only after {conf.get('registry_close_after_passes', 2)} passes")
            res["fixed_with"] = fix["kind"]
            put(line)
            return res
    if line["fails"] >= conf.get("escalate_after_runs", 3) and not line.get("card"):
        tried = [(line.get("last_fix_at"), line.get("last_action"))] if line.get("last_action") else []
        fh = c.get("for_owner")
        if fh:
            okfh, why = R.validate_for_owner(fh)
            if not okfh:
                fh = None
                line["for_owner_refused"] = why
        line = dict(line, for_owner=fh, owner="owner", status="stuck", next="waiting for the owner's yes on the STUCK card" if fh
                    else "only the owner can look at this; the card says what was tried")
        text, ctx, meta = R.stuck_card(line, tried)
        if not dry:
            r = hub("POST", "/pending", {"text": text, "context": ctx, "kind": "decision", "type": "stuck",
                                         "meta": meta, "idem": f"stuck:{rid}"})
            if r and r.get("id"):
                line["card"] = r["id"]
        res["stuck"] = text
    elif R.is_open(line) and line.get("status") != "stuck":
        line["status"] = "open"
        line.setdefault("next", "next tick re-checks")
    put(line)
    return res


def cmd_check(a):
    conf = cfg()
    checks = conf.get("checks", [])
    if a.only:
        c = next((c for c in checks if c["key"] == a.only), None)
        if not c:
            print(f"unknown key {a.only!r}; keys: {', '.join(c['key'] for c in checks)}")
            return 2
        ok, ev = run_check(c)
        print(("OK " if ok else "BAD ") + f"{c['key']}: {ev}")
        return 0 if ok else 1
    if OFF.exists():
        print(f"OFF ({OFF} exists): nothing runs")
        return 0
    lines = R.load()
    t = now()
    results = [tick_one(c, conf, lines, dry=a.dry, t=t) for c in checks]
    rec = {"at": iso(t), "dry": a.dry, "results": results}
    if not a.dry:
        wr(STATE, rec)
        append(RUNS, {"at": rec["at"], "ok": sum(r["ok"] for r in results), "bad": sum(not r["ok"] for r in results)})
    for r in results:
        extra = " ".join(f"{k}={v}" for k, v in r.items() if k not in ("key", "ok", "evidence"))
        print(f"{'OK ' if r['ok'] else 'BAD'} {r['key']:<20} {r['evidence'][:90]} {extra}")
    return 0


def cmd_status(_a):
    s = rd(STATE, None)
    if not s:
        print("no run yet")
        return 0
    print(f"last run {s['at']}{' (dry)' if s.get('dry') else ''}")
    for r in s["results"]:
        print(f"  {'OK ' if r['ok'] else 'BAD'} {r['key']:<20} {r['evidence'][:100]}")
    return 0


def cmd_registry(a):
    lines = R.load()
    text = R.render_md(lines, a.md)
    print(text, end="")
    print(json.dumps(R.counts(lines)))
    return 0


def rule_hits_24h(t):
    cut = iso(t - timedelta(hours=24))
    c = Counter()
    try:
        for l in open(P.HITS, encoding="utf-8", errors="replace"):
            try:
                j = json.loads(l)
            except ValueError:
                continue
            if str(j.get("ts", "")) >= cut:
                c[(j.get("surface") or "?", "ok" if j.get("action") == "ok" else "caught")] += 1
    except OSError:
        pass
    return c


def cmd_update(a):
    """The daily system update: what ran, what is broken and who owns it, what was fixed, how the rules fired."""
    t = now()
    runs = []
    try:
        for l in open(RUNS, encoding="utf-8"):
            j = json.loads(l)
            if str(j.get("at", "")) >= iso(t - timedelta(hours=24)):
                runs.append(j)
    except (OSError, ValueError):
        pass
    lines = R.load()
    opened = [l for l in lines.values() if R.is_broken(l)]
    fixed = [l for l in lines.values() if l.get("status") == "fixed" and str(l.get("at", "")) >= iso(t - timedelta(hours=24))]
    hits = rule_hits_24h(t)
    surfaces = sorted({s for s, _ in hits})
    body = [f"Checks: {len(runs)} ticks in 24 h, {sum(r['bad'] for r in runs)} failing results.",
            f"Broken now: {len(opened)}" + "".join(f"\n- {l['key']}: {l.get('what')} (owner {l.get('owner')}, since "
                                                    f"{R.short_time(l.get('since'))}; next: {l.get('next')})" for l in opened),
            f"Fixed in 24 h: {len(fixed)}" + "".join(f"\n- {l['key']}: {l.get('last_action') or 'passed again'}" for l in fixed),
            "Rule hits in 24 h: " + (", ".join(f"{s} {hits[(s, 'ok')]} ok / {hits[(s, 'caught')]} caught" for s in surfaces) or "none")]
    text = f"System update {t.date().isoformat()}: {len(opened)} broken, {len(fixed)} fixed"
    ctx = "\n\n".join(body)
    if a.dry:
        print(text + "\n\n" + ctx)
        return 0
    r = hub("POST", "/pending", {"text": text, "context": ctx, "kind": "update", "type": "system-update",
                                 "meta": {"origin": "system-agent"}, "idem": f"system-update:{t.date().isoformat()}"})
    print(json.dumps(r) if r else "hub did not answer; update printed instead:\n" + text + "\n\n" + ctx)
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--dry", action="store_true")
    c.add_argument("--only")
    sub.add_parser("status")
    r = sub.add_parser("registry")
    r.add_argument("--md")
    u = sub.add_parser("update")
    u.add_argument("--dry", action="store_true")
    a = ap.parse_args(argv)
    return {"check": cmd_check, "status": cmd_status, "registry": cmd_registry, "update": cmd_update}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
