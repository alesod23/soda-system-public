#!/usr/bin/env python3
"""todo_ingest.py: the to-dos, CRM people, hub cards and CRM review items become rows of the brain (the mirror).

  $TASKLAND/Tasks/{active,inbox,waiting,archive}/*.md -> todo.items (+ a row per sub-checkbox as <slug>#<n>),
      todo.history on every change, todo.links (person, hub card, parent), todo.chunks embedded with the index model.
      Skipped when TASKLAND is not set.
  crm.people -> crm.people_chunks (one embedded chunk per person).
  the approval hub's state.json (HUB_STATE, default $SODA_HOME/hub/state.json) -> hub.cards + hub.card_chunks.
  a CRM review board (REVIEW_ITEMS_URL, read-only; skipped when unset) -> crm.review_items + crm.review_chunks.
Runs at the end of brain_index.run_index (same process, the model already loaded), or by hand:

    todo_ingest.py [--dry-run] [--stats] [--tasks DIR] [--reviews-dry]

Idempotent: a file whose sha is unchanged touches nothing. Done is a state with evidence, never a delete: a file that
vanished from the task folder gets deleted_at (history says so) and comes back when the file does. Nothing here writes a
task file: the markdown files stay the writer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import brain_index as bi  # noqa: E402
import soda_paths  # noqa: E402  (brain_index put <repo>/lib on sys.path)

DIRS = ("active", "inbox", "waiting", "archive")
SUB_RE = re.compile(r"^\s*- \[( |x|X)\]\s*(.*?)\s*$")
STATUSES = ("open", "done", "cancelled", "parked")
FIELDS = ("parent_id", "title", "body", "project", "bucket", "status", "stage", "due", "surface_on", "contact_id",
          "hub_card", "source", "origin_path", "sha", "created_at", "done_at", "done_evidence", "data")
DATA_KEYS = ("tags", "hub_cards", "stage_note", "picked_at", "picked_by", "picked_auto", "ready_at", "migrated_to_crm",
             "crm_url", "parent", "call_step", "flush_done", "completed", "dir")


def tasks_root() -> Path | None:
    """The to-do folder (env TASKLAND); None = no to-do mirror."""
    env = os.environ.get("TASKLAND")
    return Path(env).expanduser() if env else None


def _sha(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _dt(v):
    """A frontmatter date or datetime -> aware datetime (midnight UTC for a bare date), else None."""
    if not v:
        return None
    s = str(v).strip().strip('"').strip("'")
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        d = bi._as_date(s)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def rows_of(file: Path, root: Path) -> list[dict]:
    """One task file -> the item row followed by its sub-item rows."""
    text = file.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
    meta, body = bi.parse_frontmatter(text)
    slug = file.stem
    status = str(meta.get("status") or "open").strip().lower()
    if status not in STATUSES:
        status = "open"
    notes, subs = [], []
    for ln in body.split("\n"):
        m = SUB_RE.match(ln)
        if m:
            subs.append((m.group(1).lower() == "x", m.group(2)))
        elif not subs:            # notes sit before the sub-checklist
            notes.append(ln)
    notes_txt = "\n".join(notes).strip()[:4000]
    data = {k: meta[k] for k in DATA_KEYS if meta.get(k) not in (None, "", {})}
    data["dir"] = file.parent.name
    done_at = _dt(meta.get("completed")) if status in ("done", "cancelled") else None
    item = {
        "id": slug, "parent_id": None, "title": str(meta.get("title") or slug).strip(), "body": notes_txt,
        "project": meta.get("project") or None, "bucket": meta.get("bucket") or None, "status": status,
        "stage": meta.get("stage") or None, "due": bi._as_date(meta.get("due")), "surface_on": bi._as_date(meta.get("surface_on")),
        "contact_id": (meta.get("contact") or None), "hub_card": (str(meta.get("hub_card")) if meta.get("hub_card") else None),
        "source": meta.get("source") or None, "origin_path": str(file.relative_to(root)).replace("\\", "/"), "sha": _sha(text),
        "created_at": _dt(meta.get("created")), "done_at": done_at,
        "done_evidence": ({"by": "task-file", "source": "frontmatter status", "at": done_at.isoformat()} if done_at else None),
        "data": data,
        "chunks": [item_chunk(str(meta.get("title") or slug), notes_txt, [t for _, t in subs])],
    }
    out = [item]
    for n, (done, t) in enumerate(subs, 1):
        sub_status = "done" if done else ("cancelled" if status == "cancelled" else "open")
        out.append({
            "id": f"{slug}#{n}", "parent_id": slug, "title": t[:500], "body": None, "project": item["project"], "bucket": item["bucket"],
            "status": sub_status, "stage": None, "due": item["due"], "surface_on": None, "contact_id": None, "hub_card": None,
            "source": item["source"], "origin_path": item["origin_path"], "sha": _sha(("x" if done else " ") + t),
            "created_at": item["created_at"], "done_at": None, "done_evidence": None, "data": {"n": n},
            "chunks": [f"{item['title']}: {t}"],
        })
    return out


def item_chunk(title: str, notes: str, subs: list[str]) -> str:
    parts = [title]
    if notes:
        parts.append(notes[:1500])
    if subs:
        parts.append("\n".join("- " + s for s in subs))
    return "\n".join(parts)


def collect(root: Path) -> list[dict]:
    out = []
    for d in DIRS:
        for f in sorted((root / "Tasks" / d).glob("*.md")):
            try:
                out.extend(rows_of(f, root))
            except (OSError, ValueError) as e:
                print(f"todo_ingest: skipped {f.name}: {e}", file=sys.stderr)
    return out


def _jsonable(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _diff(old: dict | None, new: dict) -> dict:
    """The fields that changed (old -> new), for the history row; a new row lists every set field."""
    ch = {}
    for k in FIELDS:
        if k in ("sha",):
            continue
        a = old.get(k) if old else None
        b = new.get(k)
        if _jsonable(a) != _jsonable(b):
            ch[k] = {"from": _jsonable(a), "to": _jsonable(b)} if old else _jsonable(b)
    return ch


def upsert_items(conn, rows: list[dict], embed: Callable[[list[str]], list[list[float]]], log=print) -> dict:
    from psycopg.types.json import Jsonb
    now = datetime.now(timezone.utc)
    existing = {r[0]: dict(zip(("sha", "status", "due", "stage", "title", "parent_id", "body", "project", "bucket",
                                "surface_on", "contact_id", "hub_card", "source", "origin_path", "created_at", "done_at",
                                "done_evidence", "data", "deleted_at"), r[1:]))
                for r in conn.execute("SELECT id, sha, status, due, stage, title, parent_id, body, project, bucket, surface_on, "
                                      "contact_id, hub_card, source, origin_path, created_at, done_at, done_evidence, data, deleted_at "
                                      "FROM todo.items").fetchall()}
    changed, inserted, revived = [], 0, 0
    seen = set()
    for r in rows:                      # parents come first in rows_of, so the FK holds
        seen.add(r["id"])
        old = existing.get(r["id"])
        if old and old["sha"] == r["sha"] and old["deleted_at"] is None:
            continue
        ch = _diff(old, r)
        if old and old["deleted_at"] is not None:
            ch["deleted"] = {"from": True, "to": False}; revived += 1
        done_at = r["done_at"]
        if r["status"] in ("done", "cancelled") and not done_at:
            done_at = old["done_at"] if old and old.get("done_at") else now
        if r["status"] == "open":
            done_at = None
        conn.execute(
            "INSERT INTO todo.items (id, parent_id, title, body, project, bucket, status, stage, due, surface_on, contact_id, hub_card, "
            "source, origin_path, sha, created_at, updated_at, done_at, done_evidence, data, deleted_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL) "
            "ON CONFLICT (id) DO UPDATE SET parent_id=EXCLUDED.parent_id, title=EXCLUDED.title, body=EXCLUDED.body, project=EXCLUDED.project, "
            "bucket=EXCLUDED.bucket, status=EXCLUDED.status, stage=EXCLUDED.stage, due=EXCLUDED.due, surface_on=EXCLUDED.surface_on, "
            "contact_id=EXCLUDED.contact_id, hub_card=EXCLUDED.hub_card, source=EXCLUDED.source, origin_path=EXCLUDED.origin_path, "
            "sha=EXCLUDED.sha, created_at=COALESCE(todo.items.created_at, EXCLUDED.created_at), updated_at=EXCLUDED.updated_at, "
            "done_at=EXCLUDED.done_at, done_evidence=COALESCE(EXCLUDED.done_evidence, todo.items.done_evidence), data=EXCLUDED.data, deleted_at=NULL",
            [r["id"], r["parent_id"], r["title"], r["body"], r["project"], r["bucket"], r["status"], r["stage"], r["due"], r["surface_on"],
             r["contact_id"], r["hub_card"], r["source"], r["origin_path"], r["sha"], r["created_at"], now, done_at,
             Jsonb(r["done_evidence"] or (({"by": "task-file", "source": "frontmatter status", "at": now.isoformat()}) if done_at and not (old and old.get("done_evidence")) else None)),
             Jsonb(r["data"])])
        if ch:
            conn.execute("INSERT INTO todo.history (item_id, at, by, change, evidence) VALUES (%s,%s,%s,%s,%s)",
                         [r["id"], now, "ingest", Jsonb(ch), Jsonb({"path": r["origin_path"], "sha": r["sha"]})])
        for kind, ref in (("person", r["contact_id"]), ("hub_card", r["hub_card"]), ("parent", r["parent_id"])):
            if ref:
                conn.execute("INSERT INTO todo.links (item_id, kind, ref, at) VALUES (%s,%s,%s,%s) ON CONFLICT (item_id, kind, ref) DO NOTHING",
                             [r["id"], kind, str(ref), now])
        changed.append(r)
        if not old:
            inserted += 1
    # files that vanished: the row stays, deleted_at says when, history says so
    gone = [i for i, o in existing.items() if i not in seen and o["deleted_at"] is None]
    for i in gone:
        conn.execute("UPDATE todo.items SET deleted_at = %s, updated_at = %s WHERE id = %s", [now, now, i])
        conn.execute("INSERT INTO todo.history (item_id, at, by, change, evidence) VALUES (%s,%s,%s,%s,%s)",
                     [i, now, "ingest", Jsonb({"deleted": {"from": False, "to": True}}), Jsonb({"reason": "file not in the task folder"})])
    # chunks of what changed
    todo_texts = [(r["id"], ci, t) for r in changed for ci, t in enumerate(r["chunks"])]
    if todo_texts:
        vecs = []
        for start in range(0, len(todo_texts), bi.EMBED_BATCH):
            batch = todo_texts[start:start + bi.EMBED_BATCH]
            vecs.extend(embed([bi.passage_text("to-do", t) for _, _, t in batch]))
        for r in changed:
            conn.execute("DELETE FROM todo.chunks WHERE item_id = %s", [r["id"]])
        for (iid, ci, t), v in zip(todo_texts, vecs):
            conn.execute("INSERT INTO todo.chunks (item_id, ord, text, embedding) VALUES (%s,%s,%s,%s)", [iid, ci, t, v])
    conn.commit()
    return {"files": len({r["origin_path"] for r in rows}), "items": len(rows), "changed": len(changed), "new": inserted,
            "revived": revived, "deleted": len(gone), "embedded": len(todo_texts)}


def person_text(p: dict, company: str | None) -> str:
    d = p.get("data") or {}
    rs = d.get("relationship_state") or {}
    loops = [l.get("what") for l in (rs.get("open_loops") or []) if isinstance(l, dict) and l.get("what")]
    facts = [str(x) for x in (d.get("context_facts") or rs.get("context_facts") or []) if x][:8]
    ns = d.get("next_step") or {}
    parts = [p.get("name") or p.get("id"), company or (d.get("company") or ""), d.get("role") or d.get("title") or "",
             f"stage {p.get('stage')}" if p.get("stage") else "",
             ("next step: " + (ns.get("text") if isinstance(ns, dict) else str(ns))) if ns else (("next step: " + p["next_step_text"]) if p.get("next_step_text") else ""),
             ("open loops: " + "; ".join(loops[:6])) if loops else "", ("facts: " + "; ".join(facts)) if facts else "",
             (rs.get("summary") or "")[:400]]
    return "\n".join(x for x in parts if x)


def upsert_people_chunks(conn, embed: Callable[[list[str]], list[list[float]]]) -> dict:
    companies = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM crm.companies").fetchall()}
    people = conn.execute("SELECT id, name, company_id, stage, next_step_text, data FROM crm.people WHERE deleted_at IS NULL").fetchall()
    have = {r[0]: r[1] for r in conn.execute("SELECT person_id, sha FROM crm.people_chunks WHERE ord = 0").fetchall()}
    todo = []
    for pid, name, cid, stage, nst, data in people:
        t = person_text({"id": pid, "name": name, "stage": stage, "next_step_text": nst, "data": data}, companies.get(cid))
        sha = _sha(t)
        if have.get(pid) != sha:
            todo.append((pid, sha, t))
    for start in range(0, len(todo), bi.EMBED_BATCH):
        batch = todo[start:start + bi.EMBED_BATCH]
        for (pid, sha, t), v in zip(batch, embed([bi.passage_text("person", t) for _, _, t in batch])):
            conn.execute("DELETE FROM crm.people_chunks WHERE person_id = %s", [pid])
            conn.execute("INSERT INTO crm.people_chunks (person_id, ord, sha, text, embedding) VALUES (%s,0,%s,%s,%s)", [pid, sha, t, v])
    gone = [pid for pid in have if pid not in {p[0] for p in people}]
    for pid in gone:
        conn.execute("DELETE FROM crm.people_chunks WHERE person_id = %s", [pid])
    conn.commit()
    return {"people": len(people), "embedded": len(todo), "removed": len(gone)}


HUB_STATE = Path(os.environ.get("HUB_STATE") or (soda_paths.HOME / "hub" / "state.json"))


def card_person(c: dict) -> str | None:
    m = c.get("meta") or {}
    a = c.get("action") or {}
    for k in ("pid", "crm_pid", "person", "jid", "to"):
        v = m.get(k) or (a.get(k) if isinstance(a, dict) else None)
        if v:
            return str(v)[:200]
    return None


def upsert_cards(conn, embed: Callable[[list[str]], list[list[float]]]) -> dict:
    """Hub cards -> hub.cards (+ one embedded chunk each). A card gone from the hub's pending map keeps its row,
    status resolved (the hub prunes resolved cards after 24 h; decisions.jsonl is the durable record of the verdict)."""
    from psycopg.types.json import Jsonb
    if not HUB_STATE.exists():
        return {"skipped": "no hub state here"}
    try:
        st = json.loads(HUB_STATE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)[:120]}
    pending = st.get("pending") or {}
    have = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, sha, status FROM hub.cards").fetchall()}
    now = datetime.now(timezone.utc)
    changed = []
    for cid, c in pending.items():
        meta = c.get("meta") or {}
        status = "resolved" if c.get("resolved") else "open"
        text = str(c.get("text") or "")
        sha = _sha(json.dumps([text, c.get("context"), status, c.get("verdict"), c.get("resolved_by")], ensure_ascii=False))
        if have.get(cid) == (sha, status):
            continue
        chunk = (text + ("\n" + str(c.get("context") or "")[:1500] if c.get("context") else ""))[:2000]
        conn.execute(
            "INSERT INTO hub.cards (id, seq, day, text, context, kind, type, action, meta, origin, person, status, verdict, resolved_by, created_at, resolved_at, sha, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET text=EXCLUDED.text, context=EXCLUDED.context, kind=EXCLUDED.kind, "
            "type=EXCLUDED.type, action=EXCLUDED.action, meta=EXCLUDED.meta, origin=EXCLUDED.origin, person=EXCLUDED.person, status=EXCLUDED.status, verdict=EXCLUDED.verdict, "
            "resolved_by=EXCLUDED.resolved_by, resolved_at=EXCLUDED.resolved_at, sha=EXCLUDED.sha, updated_at=EXCLUDED.updated_at",
            [cid, c.get("seq"), _dt(str(c.get("created_at") or "")[:10]), text, c.get("context"), c.get("kind"), c.get("type"), Jsonb(c.get("action")) if c.get("action") else None,
             Jsonb(meta) if meta else None, (meta.get("origin") or meta.get("source") or None), card_person(c), status, c.get("verdict"), c.get("resolved_by"),
             _dt(c.get("created_at")), _dt(c.get("resolved_at")), sha, now])
        changed.append((cid, chunk))
    gone = [cid for cid, (sha, status) in have.items() if cid not in pending and status == "open"]
    for cid in gone:
        conn.execute("UPDATE hub.cards SET status = 'resolved', resolved_at = COALESCE(resolved_at, %s), resolved_by = COALESCE(resolved_by, 'pruned from the hub'), updated_at = %s WHERE id = %s", [now, now, cid])
    if changed:
        vecs = []
        for start in range(0, len(changed), bi.EMBED_BATCH):
            batch = changed[start:start + bi.EMBED_BATCH]
            vecs.extend(embed([bi.passage_text("hub card", t) for _, t in batch]))
        for (cid, t), v in zip(changed, vecs):
            conn.execute("DELETE FROM hub.card_chunks WHERE card_id = %s", [cid])
            conn.execute("INSERT INTO hub.card_chunks (card_id, ord, text, embedding) VALUES (%s,0,%s,%s)", [cid, t, v])
    conn.commit()
    return {"cards": len(pending), "changed": len(changed), "resolved_gone": len(gone)}


REVIEW_URL = os.environ.get("REVIEW_ITEMS_URL") or ""   # e.g. http://127.0.0.1:<crm port>/review/items; unset = skipped
REVIEW_KIND = {"reply": "owed"}     # the board's "reply" = a reply the owner owes; due, waiting, coming, done keep their name


def fetch_review_items(url: str = REVIEW_URL, timeout: int = 90) -> list[dict]:
    """The CRM review board, read-only: GET <url> -> {ok, items: [{pid, name, org, kind, step, review, dealt, ...}]}."""
    if not url:
        raise ValueError("REVIEW_ITEMS_URL is not set")
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8"))
    if not d.get("ok", True) or not isinstance(d.get("items"), list):
        raise ValueError("review/items answered without items")
    return d["items"]


def review_row(x: dict) -> dict:
    """One board item -> the crm.review_items row and its one chunk (who, the step, the head of the current draft)."""
    step = x.get("step") or {}
    rv = x.get("review") or {}
    cur = rv.get("current") or None
    dealt = x.get("dealt") or None
    kind = REVIEW_KIND.get(x.get("kind"), x.get("kind") or "due")
    status = "dealt" if (dealt or kind == "done") else "open"
    head = ""
    if cur:
        head = ((("subject: " + cur["subject"] + "\n") if cur.get("subject") else "") + str(cur.get("text") or ""))[:400]
    data = {"crm_kind": x.get("kind"), "overdue": step.get("overdue") or 0, "who_owes": step.get("who_owes"),
            "waiting": x.get("waiting"), "commented": bool(x.get("commented")), "work_job": x.get("work_job"),
            "versions": rv.get("versions") or 0}
    data = {k: v for k, v in data.items() if v not in (None, "", False, 0)}
    when = f", due {step['date']}" if step.get("date") else ""
    chunk = "\n".join(t for t in (
        f"{x.get('name') or x.get('pid')}" + (f", {x['org']}" if x.get("org") else "") + f": CRM review item, {kind}, {status}",
        f"next step{when}: {step.get('text') or ''}" + (f" [{x.get('channel') or step.get('channel')}]" if (x.get("channel") or step.get("channel")) else ""),
        ("current draft: " + head) if head else "") if t)
    row = {"pid": x["pid"], "name": x.get("name") or None, "org": x.get("org") or None, "kind": kind, "status": status,
           "step_text": step.get("text") or None, "step_date": bi._as_date(step.get("date")), "channel": x.get("channel") or step.get("channel"),
           "version_v": (cur or {}).get("v"), "version_at": _dt((cur or {}).get("at")), "version_head": head or None,
           "dealt": dealt, "data": data, "chunk": chunk}
    row["sha"] = _sha(json.dumps([chunk, status, kind, dealt, data], ensure_ascii=False, sort_keys=True, default=str))
    return row


def upsert_reviews(conn, embed: Callable[[list[str]], list[list[float]]], items: list[dict] | None = None, url: str = REVIEW_URL) -> dict:
    """CRM review items -> crm.review_items (+ one embedded chunk each). Unset or unreachable board = nothing changes (a
    row is never marked gone on a failed read). An item no longer on the board keeps its row, status gone."""
    from psycopg.types.json import Jsonb
    if items is None:
        if not url:
            return {"skipped": "REVIEW_ITEMS_URL not set"}
        try:
            items = fetch_review_items(url)
        except Exception as e:
            return {"skipped": f"review/items unreachable: {type(e).__name__} {str(e)[:120]}"}
    rows = [review_row(x) for x in items if x.get("pid")]
    have = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT pid, sha, status FROM crm.review_items").fetchall()}
    now = datetime.now(timezone.utc)
    changed = []
    for r in rows:
        if have.get(r["pid"], (None, None))[0] == r["sha"]:
            continue
        conn.execute(
            "INSERT INTO crm.review_items (pid, name, org, kind, status, step_text, step_date, channel, version_v, version_at, version_head, dealt, data, sha, updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (pid) DO UPDATE SET name=EXCLUDED.name, org=EXCLUDED.org, kind=EXCLUDED.kind, "
            "status=EXCLUDED.status, step_text=EXCLUDED.step_text, step_date=EXCLUDED.step_date, channel=EXCLUDED.channel, version_v=EXCLUDED.version_v, "
            "version_at=EXCLUDED.version_at, version_head=EXCLUDED.version_head, dealt=EXCLUDED.dealt, data=EXCLUDED.data, sha=EXCLUDED.sha, updated_at=EXCLUDED.updated_at",
            [r["pid"], r["name"], r["org"], r["kind"], r["status"], r["step_text"], r["step_date"], r["channel"], r["version_v"], r["version_at"],
             r["version_head"], Jsonb(r["dealt"]) if r["dealt"] else None, Jsonb(r["data"]), r["sha"], now])
        changed.append(r)
    listed = {r["pid"] for r in rows}
    gone = [pid for pid, (_, st) in have.items() if pid not in listed and st != "gone"]
    for pid in gone:
        conn.execute("UPDATE crm.review_items SET status = 'gone', sha = 'gone', updated_at = %s WHERE pid = %s", [now, pid])
    if changed:
        vecs = []
        for start in range(0, len(changed), bi.EMBED_BATCH):
            batch = changed[start:start + bi.EMBED_BATCH]
            vecs.extend(embed([bi.passage_text("CRM review item", r["chunk"]) for r in batch]))
        for r, v in zip(changed, vecs):
            conn.execute("DELETE FROM crm.review_chunks WHERE pid = %s", [r["pid"]])
            conn.execute("INSERT INTO crm.review_chunks (pid, ord, text, embedding) VALUES (%s,0,%s,%s)", [r["pid"], r["chunk"], v])
    conn.commit()
    return {"items": len(rows), "open": sum(1 for r in rows if r["status"] == "open"), "changed": len(changed), "gone": len(gone)}


def run(conn=None, embed: Callable | None = None, root: Path | None = None, log=print) -> dict:
    t0 = time.time()
    root = root or tasks_root()
    rows = collect(root) if root else []
    own = conn is None
    conn = conn or bi.connect()
    try:
        enc = embed or (lambda texts: bi.get_embedder()(texts))
        s = upsert_items(conn, rows, enc, log) if root else {"skipped": "TASKLAND not set"}
        s["people_chunks"] = upsert_people_chunks(conn, enc)
        s["cards"] = upsert_cards(conn, enc)
        s["reviews"] = upsert_reviews(conn, enc)
    finally:
        if own:
            conn.close()
    s["seconds"] = round(time.time() - t0, 1)
    log("todo_ingest " + json.dumps(s, default=str))
    return s


def stats(conn) -> dict:
    return {
        "items": conn.execute("SELECT count(*) FROM todo.items WHERE deleted_at IS NULL").fetchone()[0],
        "open": conn.execute("SELECT count(*) FROM todo.items WHERE deleted_at IS NULL AND status = 'open'").fetchone()[0],
        "sub_items": conn.execute("SELECT count(*) FROM todo.items WHERE parent_id IS NOT NULL AND deleted_at IS NULL").fetchone()[0],
        "history": conn.execute("SELECT count(*) FROM todo.history").fetchone()[0],
        "chunks": conn.execute("SELECT count(*) FROM todo.chunks").fetchone()[0],
        "people_chunks": conn.execute("SELECT count(*) FROM crm.people_chunks").fetchone()[0],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="parse the files, print counts, touch nothing")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--tasks", help="to-do root holding Tasks/<dir>/*.md (default env TASKLAND)")
    ap.add_argument("--reviews-dry", action="store_true", help="read the CRM review board (REVIEW_ITEMS_URL), print the rows it would write, touch nothing")
    a = ap.parse_args(argv)
    bi.load_env()
    if a.reviews_dry:
        rows = [review_row(x) for x in fetch_review_items() if x.get("pid")]
        by = {}
        for r in rows:
            by[r["kind"] + " " + r["status"]] = by.get(r["kind"] + " " + r["status"], 0) + 1
        print(json.dumps({"url": REVIEW_URL, "rows": len(rows), "by_kind_status": by,
                          "sample": [{k: r[k] for k in ("pid", "kind", "status", "step_date", "chunk")} for r in rows[:3]]}, indent=1, default=str, ensure_ascii=False))
        return 0
    root = Path(a.tasks) if a.tasks else tasks_root()
    if a.dry_run:
        if not root:
            print(json.dumps({"root": None, "note": "TASKLAND not set: no to-do mirror"}))
            return 0
        rows = collect(root)
        by = {}
        for r in rows:
            k = ("sub " if r["parent_id"] else "") + r["status"]
            by[k] = by.get(k, 0) + 1
        print(json.dumps({"root": str(root), "files": len({r["origin_path"] for r in rows}), "rows": len(rows), "by_status": by,
                          "sample": [r["id"] for r in rows[:5]]}, indent=1))
        return 0
    conn = bi.connect()
    try:
        if a.stats:
            print(json.dumps(stats(conn), indent=1)); return 0
    finally:
        if a.stats:
            conn.close()
    run(conn, root=root)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
