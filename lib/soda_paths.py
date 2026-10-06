"""soda_paths.py: the one place that says where the SODA system keeps its files.

Every Python tool in this repo imports it, so a deployment changes paths through the environment (or a `.env` file at
the repo root), never by editing code.

    SODA_HOME         the data directory (default: <repo>/data). Holds everything that is state, never code:
                        ledgers/            the rule ledgers, <NAME>-CONTRACT.md
                        skill-maps/         skill-map-<surface>.json, the hand-kept mapping ledger rows -> compiled rules
                        skills/             compiled skills (unless SODA_SKILLS_DIR points elsewhere)
                        rule-hits.jsonl     one line per observer decision (the counts the compiler reads)
                        decisions.jsonl     one line per verdict on a hub card
                        rule-confirmations.jsonl, rule-cases.jsonl
                        feedback/queue.jsonl  sentences waiting for the feedback session
                        hub/state.json      the approval hub's cards
    SODA_SKILLS_DIR   where compiled skills are installed (default: $SODA_HOME/skills). Point it at
                      ~/.claude/skills to have Claude Code load them as skills.
    SODA_SURFACES     the surface registry (default: <repo>/config/surfaces.json)

Stdlib only, no side effects on import beyond reading `.env` (existing environment variables always win).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """KEY=VALUE lines from <repo>/.env into os.environ; existing variables win; never creates the file."""
    p = path or REPO / ".env"
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


load_dotenv()


def _p(env: str, default: Path) -> Path:
    v = os.environ.get(env)
    return Path(v).expanduser() if v else default


HOME = _p("SODA_HOME", REPO / "data")
LEDGERS = HOME / "ledgers"
SKILL_MAPS = HOME / "skill-maps"
SKILLS = _p("SODA_SKILLS_DIR", HOME / "skills")
HITS = _p("SODA_HITS", HOME / "rule-hits.jsonl")
DECISIONS = _p("SODA_DECISIONS", HOME / "decisions.jsonl")
CONFIRMATIONS = HOME / "rule-confirmations.jsonl"
CASES = HOME / "rule-cases.jsonl"
FEEDBACK_QUEUE = _p("SODA_FEEDBACK_QUEUE", HOME / "feedback" / "queue.jsonl")
HANDOFFS = HOME / "feedback" / "handoffs.jsonl"
LOOP_STATE = HOME / "rule-loop-state.json"
SURFACES_FILE = _p("SODA_SURFACES", REPO / "config" / "surfaces.json")
HUB_URL = os.environ.get("HUB_URL", "http://127.0.0.1:4180").rstrip("/")


def surfaces() -> dict:
    """The surface registry: {"ledgers": {key: {...}}, "surface_ledger": {surface: key}}."""
    return json.loads(SURFACES_FILE.read_text(encoding="utf-8"))


def ledger_path(key: str) -> Path:
    """The ledger file of one surface key (email, hub, crm, notif, ...)."""
    led = surfaces()["ledgers"].get(key)
    if not led:
        raise KeyError(f"unknown ledger {key!r}: add it to {SURFACES_FILE}")
    return LEDGERS / led["file"]


def ledger_keys() -> list[str]:
    return sorted(surfaces()["ledgers"])
