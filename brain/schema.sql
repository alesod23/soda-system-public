-- SODA BRAIN schema. Postgres 16 + pgvector. Idempotent: safe to apply on every install.
-- Apply as the `soda` role after a superuser ran CREATE EXTENSION vector in this database
-- (the line below is then a no-op NOTICE). See docs/03-brain.md.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS brain;
CREATE SCHEMA IF NOT EXISTS crm;

-- ---------------------------------------------------------------- brain

CREATE TABLE IF NOT EXISTS brain.pages (
    id            bigserial PRIMARY KEY,
    path          text        NOT NULL UNIQUE,
    kind          text        NOT NULL CHECK (kind IN ('memory','system','handoff','rule','hint','other')),
    title         text,
    description   text,
    type          text,                     -- frontmatter metadata.type (feedback | reference | project | user ...)
    since         date,
    superseded_by text,
    checked_last  date,
    sha           text        NOT NULL,
    body          text        NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    deleted_at    timestamptz
);

CREATE TABLE IF NOT EXISTS brain.chunks (
    id        bigserial PRIMARY KEY,
    page_id   bigint   NOT NULL REFERENCES brain.pages(id) ON DELETE CASCADE,
    ord       int      NOT NULL,
    text      text     NOT NULL,
    tsv       tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384),
    UNIQUE (page_id, ord)
);

-- the UNIQUE on pages.path already is a btree; this named one keeps the index explicit
CREATE INDEX IF NOT EXISTS pages_path_idx       ON brain.pages (path);
CREATE INDEX IF NOT EXISTS pages_kind_idx       ON brain.pages (kind) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS chunks_page_idx      ON brain.chunks (page_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_gin       ON brain.chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_embedding_hnsw ON brain.chunks USING hnsw (embedding vector_cosine_ops);

-- small key/value store: last_index_at and friends
CREATE TABLE IF NOT EXISTS brain.meta (
    k text PRIMARY KEY,
    v jsonb NOT NULL
);

-- Hybrid retrieval: vector top-k and full-text top-k fused by reciprocal rank fusion (RRF, k=60).
-- q_emb may be NULL (no model loaded): then only the full-text leg contributes.
-- q may be empty: then only the vector leg contributes.
CREATE OR REPLACE FUNCTION brain.search(q text, q_emb vector(384), k int DEFAULT 8)
RETURNS TABLE (
    path text, title text, ord int, text text, score double precision,
    kind text, since date, superseded_by text
)
LANGUAGE sql STABLE AS $$
WITH params AS (
    SELECT GREATEST(k, 1) AS k,
           CASE WHEN coalesce(q, '') = '' THEN NULL ELSE websearch_to_tsquery('english', q) END AS tsq
),
vec AS (
    SELECT c.id, row_number() OVER (ORDER BY c.embedding <=> q_emb) AS r
    FROM brain.chunks c
    JOIN brain.pages p ON p.id = c.page_id
    WHERE q_emb IS NOT NULL AND p.deleted_at IS NULL AND c.embedding IS NOT NULL
    ORDER BY c.embedding <=> q_emb
    LIMIT (SELECT k * 4 FROM params)
),
ts AS (
    SELECT c.id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, (SELECT tsq FROM params)) DESC) AS r
    FROM brain.chunks c
    JOIN brain.pages p ON p.id = c.page_id
    WHERE (SELECT tsq FROM params) IS NOT NULL
      AND p.deleted_at IS NULL
      AND c.tsv @@ (SELECT tsq FROM params)
    ORDER BY ts_rank_cd(c.tsv, (SELECT tsq FROM params)) DESC
    LIMIT (SELECT k * 4 FROM params)
),
fused AS (
    SELECT id, SUM(1.0 / (60 + r)) AS score
    FROM (SELECT id, r FROM vec UNION ALL SELECT id, r FROM ts) u
    GROUP BY id
)
SELECT p.path, p.title, c.ord, c.text, f.score::double precision, p.kind, p.since, p.superseded_by
FROM fused f
JOIN brain.chunks c ON c.id = f.id
JOIN brain.pages  p ON p.id = c.page_id
ORDER BY f.score DESC, p.path, c.ord
LIMIT (SELECT k FROM params);
$$;

-- ---------------------------------------------------------------- crm

CREATE TABLE IF NOT EXISTS crm.doc_versions (
    id          bigserial   PRIMARY KEY,
    sha         text        NOT NULL UNIQUE,
    source      text        NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    doc         jsonb       NOT NULL
);

CREATE TABLE IF NOT EXISTS crm.companies (
    id         text        PRIMARY KEY,
    name       text,
    data       jsonb       NOT NULL,
    sha        text        NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);

CREATE TABLE IF NOT EXISTS crm.people (
    id             text        PRIMARY KEY,
    company_id     text        REFERENCES crm.companies(id),
    name           text,
    email          text,
    linkedin       text,
    stage          text,
    next_step_date date,
    next_step_text text,
    data           jsonb       NOT NULL,
    sha            text        NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    deleted_at     timestamptz
);

CREATE TABLE IF NOT EXISTS crm.templates (
    id   text  PRIMARY KEY,
    data jsonb NOT NULL,
    sha  text  NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);

CREATE TABLE IF NOT EXISTS crm.signals (
    id   text  PRIMARY KEY,
    data jsonb NOT NULL,
    sha  text  NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz
);

CREATE TABLE IF NOT EXISTS crm.meta (
    k text  PRIMARY KEY,
    v jsonb
);

CREATE TABLE IF NOT EXISTS crm.events (
    source    text        NOT NULL,
    id        text        NOT NULL,
    at        timestamptz,
    kind      text,
    channel   text,
    direction text,
    person_id text,
    data      jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (source, id)
);

CREATE INDEX IF NOT EXISTS people_email_idx      ON crm.people (email);
CREATE INDEX IF NOT EXISTS people_linkedin_idx   ON crm.people (linkedin);
CREATE INDEX IF NOT EXISTS people_stage_idx      ON crm.people (stage);
CREATE INDEX IF NOT EXISTS people_next_step_idx  ON crm.people (next_step_date);
CREATE INDEX IF NOT EXISTS people_company_idx    ON crm.people (company_id);
CREATE INDEX IF NOT EXISTS people_updated_idx    ON crm.people (updated_at);
CREATE INDEX IF NOT EXISTS companies_updated_idx ON crm.companies (updated_at);
CREATE INDEX IF NOT EXISTS events_at_idx         ON crm.events (at);
CREATE INDEX IF NOT EXISTS events_person_idx     ON crm.events (person_id);

-- ---------------------------------------------------------------- todo
-- The to-do is a first-class row of the brain; markdown task files are the view (mirrored by
-- todo_ingest.py on the index timer). Done is a state with evidence and a date, never a delete; deleted_at
-- only marks a file that vanished from the task folder, and the row stays.

CREATE SCHEMA IF NOT EXISTS todo;

CREATE TABLE IF NOT EXISTS todo.items (
    id            text        PRIMARY KEY,                 -- the task slug; a sub-item is <slug>#<n>
    parent_id     text        REFERENCES todo.items(id) ON DELETE CASCADE,
    title         text        NOT NULL,
    body          text,                                    -- the notes (never rendered on the page)
    project       text,
    bucket        text,
    status        text        NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled','parked')),
    stage         text,                                    -- created | in_progress | ready_for_review | finished
    due           date,
    surface_on    date,
    contact_id    text,                                    -- crm.people.id when the task names a person
    hub_card      text,
    source        text,                                    -- capture | intake | crm | notes | janitor | brain | manual
    origin_path   text,                                    -- Tasks/<dir>/<slug>.md
    sha           text,
    created_at    timestamptz,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    done_at       timestamptz,
    done_evidence jsonb,                                   -- {by, source, quote, at}
    data          jsonb       NOT NULL DEFAULT '{}'::jsonb, -- the rest of the frontmatter
    deleted_at    timestamptz
);

CREATE TABLE IF NOT EXISTS todo.links (
    id      bigserial   PRIMARY KEY,
    item_id text        NOT NULL REFERENCES todo.items(id) ON DELETE CASCADE,
    kind    text        NOT NULL,                          -- person | meeting | thread | calendar_event | notion_page | hub_card | parent
    ref     text        NOT NULL,
    data    jsonb       NOT NULL DEFAULT '{}'::jsonb,
    at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (item_id, kind, ref)
);

CREATE TABLE IF NOT EXISTS todo.history (                 -- append-only: every change, with its evidence
    id       bigserial   PRIMARY KEY,
    item_id  text        NOT NULL REFERENCES todo.items(id) ON DELETE CASCADE,
    at       timestamptz NOT NULL DEFAULT now(),
    by       text        NOT NULL,                         -- ingest | brain | janitor | owner | pipeline
    change   jsonb       NOT NULL,
    evidence jsonb
);

CREATE TABLE IF NOT EXISTS todo.chunks (
    id        bigserial PRIMARY KEY,
    item_id   text      NOT NULL REFERENCES todo.items(id) ON DELETE CASCADE,
    ord       int       NOT NULL,
    text      text      NOT NULL,
    tsv       tsvector  GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384),
    UNIQUE (item_id, ord)
);

-- Never-delete: a cascade would wipe an item's history with the item. RESTRICT on
-- every reference to todo.items, and todo.history is append-only: UPDATE, DELETE and TRUNCATE are refused.
ALTER TABLE todo.history DROP CONSTRAINT IF EXISTS history_item_id_fkey;
ALTER TABLE todo.history ADD CONSTRAINT history_item_id_fkey FOREIGN KEY (item_id) REFERENCES todo.items(id) ON DELETE RESTRICT;
ALTER TABLE todo.links DROP CONSTRAINT IF EXISTS links_item_id_fkey;
ALTER TABLE todo.links ADD CONSTRAINT links_item_id_fkey FOREIGN KEY (item_id) REFERENCES todo.items(id) ON DELETE RESTRICT;
ALTER TABLE todo.items DROP CONSTRAINT IF EXISTS items_parent_id_fkey;
ALTER TABLE todo.items ADD CONSTRAINT items_parent_id_fkey FOREIGN KEY (parent_id) REFERENCES todo.items(id) ON DELETE RESTRICT;
CREATE OR REPLACE FUNCTION todo.history_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'todo.history is append-only (never-delete rule): % refused', TG_OP;
END $$;
DROP TRIGGER IF EXISTS history_no_update_delete ON todo.history;
CREATE TRIGGER history_no_update_delete BEFORE UPDATE OR DELETE ON todo.history FOR EACH ROW EXECUTE FUNCTION todo.history_immutable();
DROP TRIGGER IF EXISTS history_no_truncate ON todo.history;
CREATE TRIGGER history_no_truncate BEFORE TRUNCATE ON todo.history FOR EACH STATEMENT EXECUTE FUNCTION todo.history_immutable();

-- the embedding model is stored next to every vector (a model change = re-embed what differs, never a silent mix)
ALTER TABLE brain.chunks      ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT 'intfloat/multilingual-e5-small';
ALTER TABLE todo.chunks       ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT 'intfloat/multilingual-e5-small';
ALTER TABLE IF EXISTS crm.people_chunks ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT 'intfloat/multilingual-e5-small';
ALTER TABLE IF EXISTS hub.card_chunks   ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT 'intfloat/multilingual-e5-small';

CREATE INDEX IF NOT EXISTS todo_items_status_idx  ON todo.items (status) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS todo_items_due_idx     ON todo.items (due);
CREATE INDEX IF NOT EXISTS todo_items_contact_idx ON todo.items (contact_id);
CREATE INDEX IF NOT EXISTS todo_items_parent_idx  ON todo.items (parent_id);
CREATE INDEX IF NOT EXISTS todo_history_item_idx  ON todo.history (item_id, at);
CREATE INDEX IF NOT EXISTS todo_links_item_idx    ON todo.links (item_id);
CREATE INDEX IF NOT EXISTS todo_chunks_tsv_gin    ON todo.chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS todo_chunks_embedding_hnsw ON todo.chunks USING hnsw (embedding vector_cosine_ops);

-- CRM people get a vector too (one chunk per person: name, company, role, step, loops, facts), so an
-- event can find a person by meaning, not only by ilike.
CREATE TABLE IF NOT EXISTS crm.people_chunks (
    id        bigserial PRIMARY KEY,
    person_id text      NOT NULL REFERENCES crm.people(id) ON DELETE CASCADE,
    ord       int       NOT NULL,
    sha       text      NOT NULL,
    text      text      NOT NULL,
    tsv       tsvector  GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384),
    UNIQUE (person_id, ord)
);
CREATE INDEX IF NOT EXISTS people_chunks_tsv_gin ON crm.people_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS people_chunks_embedding_hnsw ON crm.people_chunks USING hnsw (embedding vector_cosine_ops);

-- Hub cards are rows too: open and resolved, embedded, so an event can find the
-- card it settles and a card can look for its own outcome. Filled by todo_ingest.upsert_cards from the hub's state.
CREATE SCHEMA IF NOT EXISTS hub;
CREATE TABLE IF NOT EXISTS hub.cards (
    id          text        PRIMARY KEY,
    seq         int,
    day         date,
    text        text        NOT NULL,
    context     text,
    kind        text,
    type        text,
    action      jsonb,
    meta        jsonb,
    origin      text,
    person      text,
    status      text        NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')),
    verdict     text,
    resolved_by text,
    created_at  timestamptz,
    resolved_at timestamptz,
    sha         text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hub.card_chunks (
    id        bigserial PRIMARY KEY,
    card_id   text      NOT NULL REFERENCES hub.cards(id) ON DELETE CASCADE,
    ord       int       NOT NULL,
    text      text      NOT NULL,
    tsv       tsvector  GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384),
    UNIQUE (card_id, ord)
);
CREATE INDEX IF NOT EXISTS hub_cards_status_idx ON hub.cards (status);
CREATE INDEX IF NOT EXISTS hub_cards_person_idx ON hub.cards (person);
CREATE INDEX IF NOT EXISTS hub_card_chunks_tsv_gin ON hub.card_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS hub_card_chunks_embedding_hnsw ON hub.card_chunks USING hnsw (embedding vector_cosine_ops);

-- on a fresh database the two ALTERs above run before these tables exist (IF EXISTS skips them): add the column here
ALTER TABLE crm.people_chunks ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT 'intfloat/multilingual-e5-small';
ALTER TABLE hub.card_chunks   ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT 'intfloat/multilingual-e5-small';

-- CRM review items are rows too: one row per person on a CRM review board (any HTTP endpoint that lists them,
-- read-only, env REVIEW_ITEMS_URL), filled by todo_ingest.upsert_reviews on the index timer. kind = due | owed (a reply the owner owes) | waiting | coming | done; status
-- open | dealt (sent, skipped, dealt with today) | gone (no longer on the board: the row stays, never a delete).
CREATE TABLE IF NOT EXISTS crm.review_items (
    pid          text        PRIMARY KEY,
    name         text,
    org          text,
    kind         text        NOT NULL,
    status       text        NOT NULL DEFAULT 'open' CHECK (status IN ('open','dealt','gone')),
    step_text    text,
    step_date    date,
    channel      text,
    version_v    int,
    version_at   timestamptz,
    version_head text,                                     -- subject + the first 400 characters of the current draft
    dealt        jsonb,
    data         jsonb       NOT NULL DEFAULT '{}'::jsonb, -- overdue, who_owes, waiting, commented, work_job, crm_kind
    sha          text        NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS crm.review_chunks (
    id        bigserial PRIMARY KEY,
    pid       text      NOT NULL REFERENCES crm.review_items(pid) ON DELETE RESTRICT,
    ord       int       NOT NULL,
    text      text      NOT NULL,
    tsv       tsvector  GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    embedding vector(384),
    model     text      NOT NULL DEFAULT 'intfloat/multilingual-e5-small',
    UNIQUE (pid, ord)
);
CREATE INDEX IF NOT EXISTS review_items_status_idx ON crm.review_items (status);
CREATE INDEX IF NOT EXISTS review_chunks_tsv_gin ON crm.review_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS review_chunks_embedding_hnsw ON crm.review_chunks USING hnsw (embedding vector_cosine_ops);

-- An event (a mail, a calendar change, a Notion note, a verdict) finds the nearest open to-dos and CRM people:
-- the same RRF as brain.search over todo.chunks and crm.people_chunks (plus hub cards and CRM review items). include_done = true also returns done
-- items (ranked by the same score, status says so): "did I already do this?".
DROP FUNCTION IF EXISTS brain.match_event(text, vector, int, boolean);   -- the row type grew (sim column): a plain REPLACE refuses
CREATE OR REPLACE FUNCTION brain.match_event(q text, q_emb vector(384), k int DEFAULT 8, include_done boolean DEFAULT false)
RETURNS TABLE (
    kind text, id text, parent_id text, title text, text text, status text, due date, score double precision,
    sim double precision   -- cosine similarity of the vector leg (NULL without a query vector): the "strong hit" gauge
)
LANGUAGE sql STABLE AS $$
WITH params AS (
    SELECT GREATEST(k, 1) AS k,
           CASE WHEN coalesce(q, '') = '' THEN NULL ELSE websearch_to_tsquery('english', q) END AS tsq
),
cand AS (
    -- `w` ranks old and done things down at query time: done/resolved 0.6, older than 90 days 0.85
    SELECT 'todo'::text AS kind, c.id AS cid, i.id, i.parent_id, i.title, c.text, i.status, i.due, c.embedding, c.tsv,
           (CASE WHEN i.status = 'open' THEN 1.0 ELSE 0.6 END) * (CASE WHEN coalesce(i.updated_at, now()) < now() - interval '90 days' THEN 0.85 ELSE 1.0 END) AS w
    FROM todo.chunks c JOIN todo.items i ON i.id = c.item_id
    WHERE i.deleted_at IS NULL AND (include_done OR i.status = 'open')
    UNION ALL
    SELECT 'person'::text, c.id, p.id, NULL::text, p.name, c.text, p.stage, p.next_step_date, c.embedding, c.tsv,
           (CASE WHEN coalesce(p.updated_at, now()) < now() - interval '90 days' THEN 0.85 ELSE 1.0 END)
    FROM crm.people_chunks c JOIN crm.people p ON p.id = c.person_id
    WHERE p.deleted_at IS NULL
    UNION ALL
    SELECT 'card'::text, c.id, h.id, h.origin, left(h.text, 200), c.text, h.status, h.day, c.embedding, c.tsv,
           (CASE WHEN h.status = 'open' THEN 1.0 ELSE 0.6 END) * (CASE WHEN coalesce(h.created_at, now()) < now() - interval '90 days' THEN 0.85 ELSE 1.0 END)
    FROM hub.card_chunks c JOIN hub.cards h ON h.id = c.card_id
    WHERE include_done OR h.status = 'open'
    UNION ALL
    -- a CRM review item (id = the pid, parent_id = its kind, title = the person's name, due = the step date)
    SELECT 'review'::text, c.id, r.pid, r.kind, r.name, c.text, r.status, r.step_date, c.embedding, c.tsv,
           (CASE WHEN r.status = 'open' THEN 1.0 ELSE 0.6 END)
    FROM crm.review_chunks c JOIN crm.review_items r ON r.pid = c.pid
    WHERE r.status = 'open' OR (include_done AND r.status = 'dealt')
),
vec AS (
    SELECT kind, cid, row_number() OVER (ORDER BY embedding <=> q_emb) AS r
    FROM cand WHERE q_emb IS NOT NULL AND embedding IS NOT NULL
    ORDER BY embedding <=> q_emb
    LIMIT (SELECT k * 4 FROM params)
),
ts AS (
    SELECT kind, cid, row_number() OVER (ORDER BY ts_rank_cd(tsv, (SELECT tsq FROM params)) DESC) AS r
    FROM cand WHERE (SELECT tsq FROM params) IS NOT NULL AND tsv @@ (SELECT tsq FROM params)
    ORDER BY ts_rank_cd(tsv, (SELECT tsq FROM params)) DESC
    LIMIT (SELECT k * 4 FROM params)
),
fused AS (
    SELECT kind, cid, SUM(1.0 / (60 + r)) AS score
    FROM (SELECT kind, cid, r FROM vec UNION ALL SELECT kind, cid, r FROM ts) u
    GROUP BY kind, cid
),
ranked AS (                    -- k per kind: hundreds of people never crowd the to-dos out of the answer
    SELECT c.kind, c.id, c.parent_id, c.title, c.text, c.status, c.due, (f.score * c.w)::double precision AS score,
           CASE WHEN q_emb IS NULL OR c.embedding IS NULL THEN NULL ELSE (1 - (c.embedding <=> q_emb))::double precision END AS sim,
           row_number() OVER (PARTITION BY c.kind ORDER BY f.score * c.w DESC, c.id) AS rk
    FROM fused f JOIN cand c ON c.kind = f.kind AND c.cid = f.cid
)
SELECT kind, id, parent_id, title, text, status, due, score, sim
FROM ranked WHERE rk <= (SELECT k FROM params)
ORDER BY kind DESC, score DESC;   -- 'todo' rows first, then 'review', 'person', 'card'
$$;
