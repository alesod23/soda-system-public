#!/usr/bin/env python3
"""outbox.py: the simulation's only "sender". The sandbox hub runs it as the executor of action type `sim-send`, i.e.
ONLY after the owner's yes. Nothing leaves the machine: a send is one line in $SODA_HOME/outbox.jsonl.

    outbox.py <event id> <to> <card id>
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

home = Path(os.environ.get("SODA_HOME", "."))
event, to, card = (sys.argv[1:4] + ["", "", ""])[:3]
with open(home / "outbox.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "event": event, "to": to, "card": card}) + "\n")
print(f"sim-send event={event} to={to}")
