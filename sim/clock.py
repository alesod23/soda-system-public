"""clock.py: the sandbox clock. Simulated days and passes; the real clock is never consulted for simulated time."""
from __future__ import annotations

from datetime import date, datetime, timedelta


class SimClock:
    def __init__(self, start: str, passes: list[str]):
        self.start = date.fromisoformat(start)
        self.passes = sorted(passes)
        self.day_index = 0
        self.now = datetime.combine(self.start, datetime.min.time())

    def day(self, i: int) -> date:
        """Move to day i (0-based) at midnight; returns the simulated date."""
        self.day_index = i
        d = self.start + timedelta(days=i)
        self.now = datetime.combine(d, datetime.min.time())
        return d

    def at(self, hhmm: str) -> datetime:
        """Move to a time of the current day (never backwards)."""
        h, m = map(int, hhmm.split(":"))
        t = self.now.replace(hour=h, minute=m)
        if t > self.now:
            self.now = t
        return self.now

    def iso(self) -> str:
        return self.now.strftime("%Y-%m-%dT%H:%M")
