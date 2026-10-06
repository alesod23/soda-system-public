# The brain: Postgres + pgvector

The brain answers one question for every other loop: **what is already open, and what is already true?**
It is a single Postgres 16 database with the `vector` extension (pgvector), filled from plain files by an indexer
and served by one small Python process (FastAPI over HTTP, plus an MCP endpoint for Claude Code).

Code: `brain/`. Schema: `brain/schema.sql` (idempotent, apply it on every install) and the optional
`brain/migrations/001_brain_events.sql` (the event log of a monitor layer).

## What is in it

| Schema.table | One row per | Filled by |
|---|---|---|
| `brain.pages` / `brain.chunks` | a markdown page (memory note, system doc, handoff) and its ~400-token chunks | `brain_index.py` |
| `brain.pages` (kind `rule`) | **one row of a rule ledger** (`<SODA_HOME>/ledgers/*-CONTRACT.md`, path `<file>#H<n>`) | `brain_index.py` |
| `crm.companies` / `crm.people` / `crm.templates` / `crm.signals` | a CRM record, upserted only when its sha changed; soft-deleted when gone | `POST /crm/put` |
| `crm.doc_versions` | the whole CRM document, at most once a day | `POST /crm/put` |
| `crm.events` | a CRM event line (message in/out, step due) | `POST /crm/events` |
| `crm.people_chunks` | one embedded summary per person (name, company, step, open loops, facts) | `todo_ingest.py` |
| `crm.review_items` / `crm.review_chunks` | a person on a CRM review board (optional, `REVIEW_ITEMS_URL`) | `todo_ingest.py` |
| `todo.items` / `todo.links` / `todo.chunks` | a to-do (and each sub-checkbox) from a folder of markdown task files (optional, `TASKLAND`) | `todo_ingest.py` |
| `todo.history` | every change of a to-do, with its evidence. **Append-only**: a trigger refuses UPDATE, DELETE, TRUNCATE | `todo_ingest.py` |
| `hub.cards` / `hub.card_chunks` | an approval-hub card, open or resolved | `todo_ingest.py` (reads the hub's `state.json`) |
| `brain.events` (+ routes, cursors, deliveries, findings) | anything that happened with a person on any channel (migration 001) | your adapters |

Every vector is 384-dimensional (`intfloat/multilingual-e5-small`, CPU, `passage:` / `query:` prefixes), and the
model name is stored next to it, so a model change re-embeds instead of silently mixing spaces. A page or chunk whose
content is unchanged keeps its embedding: a re-index of an unchanged corpus costs nothing.

Nothing is deleted: a page that vanishes gets `deleted_at`, a to-do that is done keeps its row with `done_evidence`,
a card pruned from the hub stays as `resolved`.

## How it searches

- `brain.search(q, q_emb, k)`: **hybrid retrieval**. Vector top-k (cosine, HNSW index) and full-text top-k
  (`tsvector`, GIN index) fused by reciprocal rank fusion (RRF, k=60). With no embedding only the full-text leg
  runs; with an empty query only the vector leg.
- `brain.match_event(q, q_emb, k, include_done)`: the same RRF over to-dos, CRM people, hub cards and CRM review
  items, **k per kind** (hundreds of people never crowd the to-dos out), down-weighting done and old things at query
  time, and returning `sim` (the raw cosine similarity) so a caller can tell a strong hit from a weak one.

`brain/todo_match.py` is the consumer of `match_event` that closes the loop ("THE CONNECTION"): an incoming event
looks up what it may settle; only on a strong hit (`MATCH_STRONG`, default 0.84) does one model call judge
done / moved / none with the quote that proves it. A hub card proven done is closed through the hub's `/close`
with the evidence (never a yes, its action never runs); a to-do verdict is proposed, or applied through the command
you name in `TODO_TICK_CMD`.

## The owner's rules, answered from the ledgers

`POST /brain/rules {topic}` (MCP tool `his_rules`) searches only the ledger rows and answers "what does the owner
like and dislike about X" in the owner's own words: `liked` (LIKED rows) and `rules` (HARD / SOFT rows), each with
its ledger, id, verbatim quote, source and how many times it was re-confirmed (`rule-confirmations.jsonl`). Agents
call it before deciding anything on the owner's behalf. A new ledger is covered at the next index: the glob picks up
every `*-CONTRACT.md`.

## Setup

```bash
# 1. Postgres 16 + pgvector (Debian / Ubuntu)
sudo apt install postgresql-16 postgresql-16-pgvector

# 2. a role and a database (pick your own password; keep it in .env, never in the repo)
sudo -u postgres psql -c "CREATE ROLE soda LOGIN PASSWORD 'change-me'"
sudo -u postgres psql -c "CREATE DATABASE soda OWNER soda"
# the extension needs a superuser once; after that the schema's own CREATE EXTENSION is a no-op
sudo -u postgres psql -d soda -c "CREATE EXTENSION IF NOT EXISTS vector"

# 3. the schema (idempotent: safe to re-run on every deploy)
psql "postgresql://soda:change-me@127.0.0.1/soda" -v ON_ERROR_STOP=1 -f brain/schema.sql
psql "postgresql://soda:change-me@127.0.0.1/soda" -v ON_ERROR_STOP=1 -f brain/migrations/001_brain_events.sql   # optional

# 4. a venv OUTSIDE the repo (CPU torch wheels; ~1 GB with the model)
python3.12 -m venv ~/.venvs/soda-brain
~/.venvs/soda-brain/bin/pip install -r brain/requirements.txt

# 5. .env (see .env.example): SODA_DB_DSN, SODA_TOKEN (openssl rand -hex 32), optional BRAIN_REPO / TASKLAND

# 6. index, then serve
~/.venvs/soda-brain/bin/python brain/brain_index.py --dry-run   # the plan, touches nothing
~/.venvs/soda-brain/bin/python brain/brain_index.py             # first run downloads the model once
~/.venvs/soda-brain/bin/python brain/brain_serve.py             # 127.0.0.1:4150
curl -s 127.0.0.1:4150/health
```

Run the indexer every few minutes (a systemd timer or cron); it only touches what changed.
To let Claude Code use the brain as tools, add the MCP endpoint (`http://127.0.0.1:4150/mcp`, header
`X-Soda-Token`) or run `brain_serve.py --stdio` as a stdio MCP server.

What it indexes by default: `memory/`, `system/` and `handoffs/` under `BRAIN_REPO` (default
`examples/brain`, a few invented pages) plus the ledgers in `$SODA_HOME/ledgers`. Override with
`BRAIN_SOURCES="kind=glob,..."` (kinds `memory|system|handoff|rule|hint|other`).

## Routes (HTTP)

Auth: header `X-Soda-Token: <SODA_TOKEN>` on every route except `GET /health`. Requests from loopback are exempt,
unless they carry `X-Forwarded-For` (a local reverse proxy forwarding a remote caller). Without `SODA_TOKEN` set,
every remote request is refused. An optional `SODA_TOKEN_RO` may use every GET plus recall, match, rules and MCP.

| Route | What |
|---|---|
| `GET /health` | `{ok, db, pages, chunks, people, last_index_at}` |
| `POST /brain/recall {query, k=8}` | hybrid search: `[{path, title, ord, text, score, kind, since, superseded_by}]` |
| `POST /brain/match {text, k=8, include_done=false}` | nearest to-dos, review items, people, cards: `[{kind, id, parent_id, title, text, status, due, score, sim}]` |
| `POST /brain/rules {topic, k=10, ledger?}` | the owner's rules about a topic: `{liked: [...], rules: [...]}` |
| `GET /brain/page?path=memory/x.md` | one page, body and fields |
| `POST /brain/reindex` | runs the indexer in the background: `{started}` |
| `POST /crm/put {source, at, sha, doc}` | store a CRM document: per-record diff, upsert changed, soft-delete missing |
| `POST /crm/events {lines:[...]}` | append event lines, deduplicated on `(source, id)` |
| `GET /crm/doc` | the latest stored document |
| `GET /crm/person/{id}` / `GET /crm/company/{id}` | one record (a company with its people) |
| `GET /crm/people?q=&stage=&due_before=&limit=50` | search people (name / email / company, stage, due date) |
| `GET /crm/changes?since=<iso>` | records updated after a timestamp |
| `POST /mcp` | MCP (streamable HTTP): `recall`, `what_is_true`, `his_rules`, `match_event`, `page`, `crm_person`, `crm_search` |

The CRM document shape `POST /crm/put` expects: `{meta, templates: [...], companies: [{id, company, ..., people:
[{id, name, email, linkedin, stage, next_step: {date, text}, ...}]}], signals: [...]}` (see `brain/crm_diff.py`).
