#!/usr/bin/env python3
"""SODA BRAIN door: one process, FastAPI (HTTP) + MCP (streamable HTTP at /mcp, or --stdio).

    brain_serve.py                                      # BRAIN_HOST:BRAIN_PORT (default 127.0.0.1:4150)
    brain_serve.py --host 0.0.0.0 --port 4150           # reachable from other machines (set SODA_TOKEN first)
    brain_serve.py --stdio                              # MCP over stdio for a local Claude Code

Auth: header X-Soda-Token == env SODA_TOKEN on every route except GET /health; loopback is exempt.
Env (from the environment or the repo's .env): SODA_DB_DSN, SODA_TOKEN, SODA_TOKEN_RO, BRAIN_SOURCES, BRAIN_REPO,
BRAIN_MODEL, BRAIN_HOST, BRAIN_PORT.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date, datetime, timezone
from typing import Any

import brain_index
import crm_diff
import soda_paths  # brain_index put <repo>/lib on sys.path

log = logging.getLogger("brain")
LOOPBACK = {"127.0.0.1", "::1", "localhost"}

POOL = None            # psycopg_pool.ConnectionPool, opened in the lifespan / stdio runner
ARGS = None            # parsed CLI args
_REINDEX_LOCK = threading.Lock()
_REINDEX_RUNNING = False


# ----------------------------------------------------------------- db helpers

def open_pool(dsn: str | None = None):
    global POOL
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from pgvector.psycopg import register_vector
    dsn = dsn or os.environ.get("SODA_DB_DSN")
    if not dsn:
        raise SystemExit("SODA_DB_DSN is not set (put it in the repo's .env, see .env.example)")
    POOL = ConnectionPool(dsn, min_size=1, max_size=4, open=False, kwargs={"row_factory": dict_row},
                          configure=register_vector)
    POOL.open(wait=True, timeout=30)
    return POOL


def rows(sql: str, params: list | tuple = ()) -> list[dict]:
    with POOL.connection() as conn:
        return conn.execute(sql, params).fetchall()


def one(sql: str, params: list | tuple = ()) -> dict | None:
    with POOL.connection() as conn:
        return conn.execute(sql, params).fetchone()


def jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if hasattr(v, "tolist"):
        return v.tolist()
    return v


# ----------------------------------------------------------------- brain logic

def query_embedding(text: str) -> list[float] | None:
    """With --no-model the first call loads the model (get_embedder caches it)."""
    try:
        return brain_index.embed_query(text)
    except Exception as e:  # model missing: full-text leg only
        log.warning("no embedding for query: %s", e)
        return None


def recall(query: str, k: int = 8) -> list[dict]:
    k = max(1, min(int(k or 8), 50))
    emb = query_embedding(query)
    res = rows("SELECT * FROM brain.search(%s, %s::vector, %s)", [query, emb, k])
    return jsonable(res)


# ----------------------------------------------------------------- the owner's rules (docs/01-rule-loop.md, part 6)
# "What does the owner like and dislike about X", answered from the rule ledgers only. The ledgers are indexed one page
# per row (brain_index.rule_pages, kind "rule", path <file>#H<n>); this answers in the owner's own words, split into
# what was liked (LIKED rows) and what was ruled (HARD / SOFT rows), with how often each was re-confirmed
# (rule-confirmations.jsonl). No schema change: brain.search with a wide k, filtered to rule rows.
def _ledger_of() -> dict:
    """ledger file stem -> ledger key, from config/surfaces.json (e.g. HUB-CARD-CONTRACT -> hub)."""
    try:
        return {Path(v["file"]).stem: k for k, v in soda_paths.surfaces()["ledgers"].items()}
    except Exception:
        return {}


LEDGER_OF = _ledger_of()
CONFIRMATIONS = os.environ.get("BRAIN_CONFIRMATIONS", str(soda_paths.CONFIRMATIONS))


def confirmation_counts(path: str | None = None) -> dict:
    import json as _json
    out: dict = {}
    try:
        with open(path or CONFIRMATIONS, encoding="utf-8") as f:
            for line in f:
                try:
                    r = str(_json.loads(line).get("rule") or "")
                except ValueError:
                    continue
                if r:
                    out[r] = out.get(r, 0) + 1
    except OSError:
        pass
    return out


def shape_rules(hits: list[dict], confirmations: dict, ledger: str | None = None, k: int = 10) -> dict:
    """brain.search rows -> {"liked": [...], "rules": [...]} of ledger rows only, best first, one entry per row."""
    import re as _re
    seen, liked, rules = set(), [], []
    for h in hits:
        path = str(h.get("path") or "")
        if h.get("kind") != "rule" or "#H" not in path or path in seen:
            continue
        seen.add(path)
        stem = path.rsplit("/", 1)[-1].split("#")[0].replace(".md", "")
        led = LEDGER_OF.get(stem, stem.lower())
        if ledger and led != ledger:
            continue
        rid = path.split("#", 1)[1]
        text = str(h.get("text") or "")
        body = text.split(": ", 1)[1] if text.startswith(rid + ":") else text
        src = _re.search(r"\(source: (.+?)\)\s*$", body)
        body = _re.sub(r"\n?\(source: .+?\)\s*$", "", body).strip()
        quote = _re.search(r'\*"(.+?)"\*', body)
        row = {"ledger": led, "rule": rid, "text": _re.sub(r'\s*\*".+?"\*', "", body).replace("**", "").strip()[:600],
               "his_words": quote.group(1)[:400] if quote else None, "source": src.group(1) if src else None,
               "soft": "SOFT, flag only" in body, "confirmed": confirmations.get(f"{led}:{rid}", 0),
               "score": h.get("score"), "path": path}
        (liked if body.startswith("LIKED:") else rules).append(row)
    return {"liked": liked[:k], "rules": rules[:k]}


def his_rules(topic: str, k: int = 10, ledger: str | None = None) -> dict:
    k = max(1, min(int(k or 10), 50))
    hits = jsonable(rows("SELECT * FROM brain.search(%s, %s::vector, %s)", [topic, query_embedding(topic), 200]))
    out = shape_rules(hits, confirmation_counts(), ledger, k)
    out["topic"] = topic
    return out


def match_event(text: str, k: int = 8, include_done: bool = False) -> list[dict]:
    """The nearest open to-dos, CRM review items, CRM people and hub cards for an event text
    (brain.match_event, RRF over their chunks). include_done also returns done to-dos and dealt review items."""
    k = max(1, min(int(k or 8), 50))
    emb = query_embedding(text)
    res = rows("SELECT * FROM brain.match_event(%s, %s::vector, %s, %s)", [text, emb, k, bool(include_done)])
    return jsonable(res)


def page(path: str) -> dict | None:
    r = one("SELECT path, kind, title, description, type, since, superseded_by, checked_last, sha, body, "
            "updated_at, deleted_at FROM brain.pages WHERE path = %s", [path])
    return jsonable(r) if r else None


def what_is_true(topic: str) -> dict:
    """recall + the newest non-superseded page first, with since dates."""
    hits = recall(topic, 12)
    by_path: dict[str, dict] = {}
    for h in hits:
        p = by_path.setdefault(h["path"], {"path": h["path"], "title": h["title"], "kind": h["kind"],
                                           "since": h["since"], "superseded_by": h["superseded_by"],
                                           "score": 0.0, "snippets": []})
        p["score"] = max(p["score"], h["score"])
        if len(p["snippets"]) < 2:
            p["snippets"].append(h["text"][:600])
    pages = sorted(by_path.values(),
                   key=lambda p: (p["superseded_by"] is not None, p["since"] is None, p["since"] or "", -p["score"]))
    # newest first among dated ones: sort dated pages by since desc inside the non-superseded group
    dated = [p for p in pages if p["superseded_by"] is None and p["since"]]
    undated = [p for p in pages if p["superseded_by"] is None and not p["since"]]
    superseded = [p for p in pages if p["superseded_by"] is not None]
    dated.sort(key=lambda p: p["since"], reverse=True)
    ordered = dated + undated + superseded
    return {"topic": topic, "current": ordered[0] if ordered else None, "pages": ordered,
            "note": "pages ordered: dated non-superseded (newest first), undated, then superseded"}


def health() -> dict:
    out = {"ok": True, "db": False, "pages": None, "chunks": None, "people": None, "last_index_at": None}
    try:
        r = one("SELECT (SELECT count(*) FROM brain.pages WHERE deleted_at IS NULL) AS pages,"
                " (SELECT count(*) FROM brain.chunks c JOIN brain.pages p ON p.id = c.page_id"
                "   WHERE p.deleted_at IS NULL) AS chunks,"
                " (SELECT count(*) FROM crm.people WHERE deleted_at IS NULL) AS people,"
                " (SELECT v FROM brain.meta WHERE k = 'last_index_at') AS last_index_at")
        out.update(db=True, pages=r["pages"], chunks=r["chunks"], people=r["people"],
                   last_index_at=r["last_index_at"])
    except Exception as e:
        out.update(ok=False, error=str(e).strip().splitlines()[0])
    return out


def start_reindex() -> dict:
    global _REINDEX_RUNNING
    with _REINDEX_LOCK:
        if _REINDEX_RUNNING:
            return {"started": False, "running": True}
        _REINDEX_RUNNING = True

    def work():
        global _REINDEX_RUNNING
        try:
            brain_index.run_index(os.environ.get("SODA_DB_DSN"), embed=brain_index.get_embedder, log=log.info)
        except Exception:
            log.exception("reindex failed")
        finally:
            _REINDEX_RUNNING = False

    threading.Thread(target=work, name="reindex", daemon=True).start()
    return {"started": True}


# ----------------------------------------------------------------- crm logic

_PERSON_COLS = ("id", "company_id", "name", "email", "linkedin", "stage", "next_step_date", "next_step_text")


def crm_put(source: str, at: str | None, sha: str | None, doc: dict) -> dict:
    from psycopg.types.json import Jsonb
    sha = sha or crm_diff.doc_sha(doc)
    with POOL.connection() as conn:
        with conn.transaction():
            existing = conn.execute("SELECT id FROM crm.doc_versions WHERE sha = %s", [sha]).fetchone()
            if existing:
                return {"ok": True, "version": existing["id"], "unchanged": True}
            # Retention: the whole document is kept ONCE A DAY (the day's newest distinct save); every save still
            # updates the per-record tables below, which hold the real history. Without this, a CRM that saves every
            # few seconds piles up gigabytes of near-identical whole documents within days.
            today = conn.execute("SELECT id FROM crm.doc_versions WHERE received_at::date = (coalesce(%s::timestamptz, now()))::date "
                                 "ORDER BY received_at DESC LIMIT 1", [at]).fetchone()
            if today:
                version = today["id"]
                conn.execute("UPDATE crm.doc_versions SET sha = %s, source = %s, received_at = coalesce(%s::timestamptz, now()), doc = %s WHERE id = %s",
                             [sha, source or "unknown", at, Jsonb(doc), version])    # the day's snapshot = the newest save of the day
            else:
                version = conn.execute(
                    "INSERT INTO crm.doc_versions (sha, source, received_at, doc) VALUES (%s, %s, coalesce(%s, now()), %s) "
                    "RETURNING id", [sha, source or "unknown", at, Jsonb(doc)]).fetchone()["id"]

            split = crm_diff.split_doc(doc)
            changed_n: dict[str, int] = {}
            deleted_n: dict[str, int] = {}
            for table in crm_diff.iter_tables():
                cur = {r["id"]: (r["sha"], r["deleted_at"] is not None) for r in
                       conn.execute(f"SELECT id, sha, deleted_at FROM crm.{table}")}
                changed, deleted = crm_diff.diff(cur, split[table])
                changed_n[table] = len(changed)
                deleted_n[table] = len(deleted)
                recs = [split[table][k] for k in changed]
                with conn.cursor() as c:
                    if table == "companies":
                        c.executemany(
                            "INSERT INTO crm.companies (id, name, data, sha, updated_at, deleted_at) "
                            "VALUES (%s,%s,%s,%s,now(),NULL) ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name, "
                            "data=EXCLUDED.data, sha=EXCLUDED.sha, updated_at=now(), deleted_at=NULL",
                            [(r["id"], r["name"], Jsonb(r["data"]), r["sha"]) for r in recs])
                    elif table == "people":
                        c.executemany(
                            "INSERT INTO crm.people (id, company_id, name, email, linkedin, stage, next_step_date, "
                            "next_step_text, data, sha, updated_at, deleted_at) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),NULL) ON CONFLICT (id) DO UPDATE SET "
                            "company_id=EXCLUDED.company_id, name=EXCLUDED.name, email=EXCLUDED.email, "
                            "linkedin=EXCLUDED.linkedin, stage=EXCLUDED.stage, next_step_date=EXCLUDED.next_step_date, "
                            "next_step_text=EXCLUDED.next_step_text, data=EXCLUDED.data, sha=EXCLUDED.sha, "
                            "updated_at=now(), deleted_at=NULL",
                            [tuple(r[c_] for c_ in _PERSON_COLS) + (Jsonb(r["data"]), r["sha"]) for r in recs])
                    else:
                        c.executemany(
                            f"INSERT INTO crm.{table} (id, data, sha, updated_at, deleted_at) "
                            "VALUES (%s,%s,%s,now(),NULL) ON CONFLICT (id) DO UPDATE SET data=EXCLUDED.data, "
                            "sha=EXCLUDED.sha, updated_at=now(), deleted_at=NULL",
                            [(r["id"], Jsonb(r["data"]), r["sha"]) for r in recs])
                    if deleted:
                        c.execute(f"UPDATE crm.{table} SET deleted_at = now(), updated_at = now() "
                                  "WHERE id = ANY(%s) AND deleted_at IS NULL", [deleted])

            meta = dict(doc.get("meta") or {})
            meta.update(_version=version, _sha=sha, _source=source, _at=at,
                        _received_at=datetime.now(timezone.utc).isoformat())
            with conn.cursor() as c:
                c.executemany("INSERT INTO crm.meta (k, v) VALUES (%s, %s) ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v",
                              [(k, Jsonb(v)) for k, v in meta.items()])
    return {"ok": True, "version": version, "changed": changed_n, "deleted": deleted_n}


def crm_events(lines: list[dict]) -> dict:
    from psycopg.types.json import Jsonb
    params = []
    skipped = 0
    for ln in lines or []:
        if not isinstance(ln, dict) or not ln.get("source") or ln.get("id") in (None, ""):
            skipped += 1
            continue
        params.append((ln["source"], str(ln["id"]), ln.get("at"), ln.get("kind"), ln.get("channel"),
                       ln.get("direction"), ln.get("person_id"), Jsonb(ln)))
    inserted = 0
    if params:
        with POOL.connection() as conn, conn.cursor() as c:
            c.executemany(
                "INSERT INTO crm.events (source, id, at, kind, channel, direction, person_id, data) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (source, id) DO NOTHING", params)
            inserted = c.rowcount if c.rowcount is not None and c.rowcount >= 0 else 0
    return {"inserted": inserted, "skipped": skipped + (len(params) - inserted)}


def crm_doc() -> dict | None:
    r = one("SELECT id, sha, source, received_at, doc FROM crm.doc_versions ORDER BY id DESC LIMIT 1")
    return jsonable(r) if r else None


def crm_person(pid: str) -> dict | None:
    r = one("SELECT p.*, c.name AS company FROM crm.people p LEFT JOIN crm.companies c ON c.id = p.company_id "
            "WHERE p.id = %s", [pid])
    return jsonable(r) if r else None


def crm_company(cid: str) -> dict | None:
    r = one("SELECT * FROM crm.companies WHERE id = %s", [cid])
    if not r:
        return None
    r = jsonable(r)
    r["people"] = jsonable(rows(
        "SELECT id, name, email, linkedin, stage, next_step_date, next_step_text, data, updated_at, deleted_at "
        "FROM crm.people WHERE company_id = %s ORDER BY name", [cid]))
    return r


def crm_search(q: str | None = None, stage: str | None = None, due_before: str | None = None,
               limit: int = 50) -> list[dict]:
    where = ["p.deleted_at IS NULL"]
    params: list = []
    if q:
        like = f"%{q}%"
        where.append("(p.name ILIKE %s OR p.email ILIKE %s OR c.name ILIKE %s)")
        params += [like, like, like]
    if stage:
        where.append("p.stage = %s")
        params.append(stage)
    if due_before:
        where.append("p.next_step_date <= %s")
        params.append(crm_diff.parse_date(due_before) or due_before)
    limit = max(1, min(int(limit or 50), 500))
    params.append(limit)
    return jsonable(rows(
        "SELECT p.id, p.company_id, c.name AS company, p.name, p.email, p.linkedin, p.stage, p.next_step_date, "
        "p.next_step_text, p.updated_at FROM crm.people p LEFT JOIN crm.companies c ON c.id = p.company_id "
        f"WHERE {' AND '.join(where)} ORDER BY p.next_step_date NULLS LAST, p.name LIMIT %s", params))


def crm_changes(since: str) -> dict:
    out = {"since": since}
    for table in ("companies", "people", "templates", "signals"):
        out[table] = jsonable(rows(
            f"SELECT * FROM crm.{table} WHERE updated_at > %s ORDER BY updated_at LIMIT 2000", [since]))
    return out


# ----------------------------------------------------------------- MCP

def build_mcp():
    from mcp.server.mcpserver import MCPServer
    srv = MCPServer("soda-brain", instructions=(
        "The owner's brain: memory pages, system docs, handoffs, rule ledgers (recall / what_is_true / page / his_rules), "
        "the CRM mirror (crm_person / crm_search) and event matching (match_event)."))

    @srv.tool(name="recall", description="Hybrid (vector + full-text) search over the brain pages. Returns k chunks "
              "with path, title, score, text, since, superseded_by.")
    def _recall(query: str, k: int = 8) -> list[dict]:
        return recall(query, k)

    @srv.tool(name="what_is_true", description="What is currently true about a topic: recall, grouped by page, the "
              "newest non-superseded page first, with since dates.")
    def _what_is_true(topic: str) -> dict:
        return what_is_true(topic)

    @srv.tool(name="his_rules", description="What the owner likes and dislikes about a topic, in the owner's own words: "
              "the rows of the rule ledgers (config/surfaces.json) nearest to the topic, split into liked (LIKED rows) and "
              "rules (what was ruled), with the source and how often each was re-confirmed. Optional ledger filters to "
              "one. Read this before deciding anything on the owner's behalf.")
    def _his_rules(topic: str, k: int = 10, ledger: str | None = None) -> dict:
        return his_rules(topic, k, ledger)

    @srv.tool(name="match_event", description="An event text (a mail, a calendar change, a meeting note, a proposed "
              "step) -> the nearest OPEN to-dos (tasks and sub-items, kind 'todo'), CRM review items (kind "
              "'review': id = the person id, parent_id = due|owed|waiting|coming, due = the step date), CRM people (kind "
              "'person') and hub cards (kind 'card'), with scores. Use before proposing a step: it may already be on the owner's to-do "
              "or on the CRM review board.")
    def _match_event(text: str, k: int = 8, include_done: bool = False) -> list[dict]:
        return match_event(text, k, include_done)

    @srv.tool(name="page", description="A whole brain page by path (e.g. memory/pref_short_cards.md).")
    def _page(path: str) -> dict:
        return page(path) or {"error": "not found", "path": path}

    @srv.tool(name="crm_person", description="One CRM person by id, with the company name.")
    def _crm_person(id: str) -> dict:
        return crm_person(id) or {"error": "not found", "id": id}

    @srv.tool(name="crm_search", description="Search CRM people: q matches name/email/company (ilike), stage, "
              "due_before (ISO date, next step due on or before), limit (default 50).")
    def _crm_search(q: str | None = None, stage: str | None = None, due_before: str | None = None,
                    limit: int = 50) -> list[dict]:
        return crm_search(q, stage, due_before, limit)

    return srv


# ----------------------------------------------------------------- HTTP app

class TokenAuth:
    """Pure ASGI middleware: X-Soda-Token must equal SODA_TOKEN except for GET /health and loopback clients."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not self.allowed(scope):
            from starlette.responses import JSONResponse
            await JSONResponse({"error": "unauthorized: X-Soda-Token"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)

    # routes a READ-ONLY token (SODA_TOKEN_RO, e.g. for a door exposed through a reverse proxy) may use: every GET, recall,
    # and the MCP endpoint (its tools only read); never /crm/put, /crm/events, /brain/reindex
    RO_POST = {"/brain/recall", "/brain/match", "/brain/rules", "/mcp", "/mcp/"}

    @staticmethod
    def allowed(scope) -> bool:
        path, method = scope.get("path") or "", scope.get("method") or ""
        if path == "/health" and method == "GET":
            return True
        headers = {k: v for k, v in (scope.get("headers") or [])}
        client = scope.get("client")
        # loopback is exempt only when nothing was proxied to it (a local reverse proxy hands the request over on
        # loopback and carries the real client in X-Forwarded-For; such a request is remote and needs a token)
        if client and client[0] in LOOPBACK and b"x-forwarded-for" not in headers and b"x-forwarded-host" not in headers:
            return True
        given = headers.get(b"x-soda-token", b"").decode("latin-1")
        if not given:
            return False
        expected = os.environ.get("SODA_TOKEN")
        if expected and given == expected:
            return True
        ro = os.environ.get("SODA_TOKEN_RO")
        if ro and given == ro:
            return method in ("GET", "HEAD") or (method == "POST" and path in TokenAuth.RO_POST)
        return False


def build_app(host: str, no_model: bool):
    from fastapi import FastAPI, HTTPException, Query, Request
    from mcp.server.transport_security import TransportSecuritySettings

    mcp_srv = build_mcp()
    mcp_app = mcp_srv.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True, json_response=True, host=host,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @asynccontextmanager
    async def lifespan(app):
        open_pool()
        if not no_model:
            brain_index.get_embedder()
        async with mcp_srv.session_manager.run():
            yield
        POOL.close()

    app = FastAPI(title="soda-brain", lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.get("/health")
    def _health():
        return health()

    @app.post("/brain/recall")
    def _recall(body: dict):
        if not body.get("query"):
            raise HTTPException(400, "query required")
        return recall(body["query"], body.get("k", 8))

    @app.post("/brain/match")
    def _match(body: dict):
        if not body.get("text"):
            raise HTTPException(400, "text required")
        return match_event(body["text"], body.get("k", 8), body.get("include_done", False))

    @app.post("/brain/rules")
    def _rules(body: dict):
        if not body.get("topic"):
            raise HTTPException(400, "topic required")
        return his_rules(body["topic"], body.get("k", 10), body.get("ledger"))

    @app.get("/brain/page")
    def _page(path: str = Query(...)):
        r = page(path)
        if not r:
            raise HTTPException(404, "page not found")
        return r

    @app.post("/brain/reindex")
    def _reindex():
        return start_reindex()

    @app.post("/crm/put")
    def _crm_put(body: dict):
        doc = body.get("doc")
        if not isinstance(doc, dict) or "companies" not in doc:
            raise HTTPException(400, "doc with companies[] required")
        return crm_put(body.get("source") or "unknown", body.get("at"), body.get("sha"), doc)

    @app.post("/crm/events")
    def _crm_events(body: dict):
        return crm_events(body.get("lines") or [])

    @app.get("/crm/doc")
    def _crm_doc():
        r = crm_doc()
        if not r:
            raise HTTPException(404, "no doc stored yet")
        return r

    @app.get("/crm/person/{pid}")
    def _crm_person(pid: str):
        r = crm_person(pid)
        if not r:
            raise HTTPException(404, "person not found")
        return r

    @app.get("/crm/company/{cid}")
    def _crm_company(cid: str):
        r = crm_company(cid)
        if not r:
            raise HTTPException(404, "company not found")
        return r

    @app.get("/crm/people")
    def _crm_people(q: str | None = None, stage: str | None = None, due_before: str | None = None,
                    limit: int = 50):
        return crm_search(q, stage, due_before, limit)

    @app.get("/crm/changes")
    def _crm_changes(since: str = Query(...)):
        return crm_changes(since)

    app.mount("/", mcp_app)   # serves exactly /mcp; everything else unmatched -> 404 from it
    return TokenAuth(app)


# ----------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("BRAIN_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BRAIN_PORT", "4150")))
    ap.add_argument("--stdio", action="store_true", help="MCP over stdio (no HTTP)")
    ap.add_argument("--no-model", action="store_true", help="load the embedding model on first recall, not at start")
    ap.add_argument("--log-level", default="info")
    global ARGS
    ARGS = ap.parse_args(argv)
    brain_index.load_env()
    logging.basicConfig(level=ARGS.log_level.upper(), stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "huggingface_hub", "urllib3", "sentence_transformers", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)   # one line per HEAD request otherwise

    if ARGS.stdio:
        open_pool()
        if not ARGS.no_model:
            brain_index.get_embedder()
        build_mcp().run("stdio")
        POOL.close()
        return 0

    import uvicorn
    uvicorn.run(build_app(ARGS.host, ARGS.no_model), host=ARGS.host, port=ARGS.port,
                log_level=ARGS.log_level, access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
