"""checks.py: the owner's hidden taste as code, shared by the stub owner (to decide) and the judge (to score).

The system under test never imports this file: it only learns the taste through the owner's feedback sentences,
filed by the real rules/ engine and read back as compiled skills. That separation is what makes a learning curve
meaningful.
"""
from __future__ import annotations

import re

FILLER = re.compile(r"hope (this|you) find|hope you are well|just checking in|hope all is well", re.I)


def first_sentence(body: str) -> str:
    m = re.search(r"^.*?[.?!](\s|$)", (body or "").strip(), re.S)
    return (m.group(0) if m else body or "").strip()


def ask_first(card: dict) -> bool:
    """The request is in the first sentence of every draft on the card."""
    return all("?" in first_sentence(b) for b in bodies(card))


def no_filler(card: dict) -> bool:
    return not any(FILLER.search(b) for b in bodies(card))


def one_person(card: dict) -> bool:
    return len(set((card.get("meta") or {}).get("people") or [])) <= 1


def bodies(card: dict) -> list[str]:
    m = card.get("meta") or {}
    return [b for b in (m.get("bodies") or [m.get("body") or ""]) if b is not None]


CHECKS = {"ask_first": ask_first, "no_filler": no_filler, "one_person": one_person}


def failing(card: dict, preferences: list[dict]) -> list[dict]:
    """The preferences this card fails, in the scenario's order."""
    return [p for p in preferences if not CHECKS[p["check"]](card)]
