"""Per-record hashing and diffing of a CRM JSON document (one file holding companies, their people, templates, signals).

Pure functions, no DB: brain_serve.py applies the result, tests/test_crm_diff.py checks it.

Document shape:
    {meta: {...}, templates: [...], companies: [{..., people: [...]}], signals: [...]}

Keys: a company's id, a person's id, a template's id, a signal's id. Real documents have
people without an id and duplicated ids, so keys are made unique deterministically (document order): a missing person id becomes
"<company_key>/<slug(name)>", a repeated key gets "#2", "#3", ... appended.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from typing import Any, Iterable

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_sha(obj: Any) -> str:
    return hashlib.sha1(canonical(obj).encode("utf-8")).hexdigest()


def slug(s: str) -> str:
    return _SLUG_RE.sub("-", (s or "").lower()).strip("-") or "unnamed"


class _Keys:
    """Hands out unique keys in document order."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}

    def take(self, base: str) -> str:
        n = self.seen.get(base, 0) + 1
        self.seen[base] = n
        return base if n == 1 else f"{base}#{n}"


def parse_date(v: Any) -> date | None:
    """ISO date or datetime string -> date; anything else -> None."""
    if not v or not isinstance(v, str):
        return None
    s = v.strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def split_doc(doc: dict) -> dict[str, dict[str, dict]]:
    """Flatten the document into {table: {key: record}} where every record carries its sha.

    companies: data = the company WITHOUT its people (people are their own table).
    people:    data = the person + company_id.
    """
    out: dict[str, dict[str, dict]] = {"companies": {}, "people": {}, "templates": {}, "signals": {}}
    ck, pk, tk, sk = _Keys(), _Keys(), _Keys(), _Keys()

    for c in doc.get("companies") or []:
        cid = ck.take(str(c.get("id") or slug(c.get("company") or c.get("name") or "")))
        cdata = {k: v for k, v in c.items() if k != "people"}
        out["companies"][cid] = {
            "id": cid,
            "name": c.get("company") or c.get("name"),
            "data": cdata,
            "sha": record_sha(cdata),
        }
        for p in c.get("people") or []:
            base = str(p.get("id") or f"{cid}/{slug(p.get('name') or '')}")
            pid = pk.take(base)
            ns = p.get("next_step") if isinstance(p.get("next_step"), dict) else {}
            out["people"][pid] = {
                "id": pid,
                "company_id": cid,
                "name": p.get("name"),
                "email": (p.get("email") or None),
                "linkedin": (p.get("linkedin") or None),
                "stage": (p.get("stage") or None),
                "next_step_date": parse_date(ns.get("date")),
                "next_step_text": ns.get("text") or None,
                "data": p,
                "sha": record_sha(p),
            }

    for t in doc.get("templates") or []:
        tid = tk.take(str(t.get("id") or record_sha(t)))
        out["templates"][tid] = {"id": tid, "data": t, "sha": record_sha(t)}

    for s in doc.get("signals") or []:
        sid = sk.take(str(s.get("id") or record_sha(s)))
        out["signals"][sid] = {"id": sid, "data": s, "sha": record_sha(s)}

    return out


def diff(current: dict[str, tuple[str, bool]], new: dict[str, dict]) -> tuple[list[str], list[str]]:
    """current: {key: (sha, is_deleted)} from the DB; new: {key: record} from split_doc.

    Returns (changed_keys, deleted_keys): a record is changed when it is new, its sha differs,
    or it was soft-deleted and is back; deleted when it is live in the DB and absent from the doc.
    """
    changed = [k for k, rec in new.items()
               if k not in current or current[k][0] != rec["sha"] or current[k][1]]
    deleted = [k for k, (_, is_del) in current.items() if not is_del and k not in new]
    return changed, deleted


def doc_sha(doc: dict) -> str:
    return record_sha(doc)


def iter_tables() -> Iterable[str]:
    return ("companies", "people", "templates", "signals")
