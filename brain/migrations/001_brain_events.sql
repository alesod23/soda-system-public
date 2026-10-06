-- 001_brain_events.sql: the monitor layer's tables (one event log for every channel). A skeleton: optional.
-- Idempotent like schema.sql: safe to run twice. Needs schema.sql first (extension vector, schema brain).
-- Not folded into schema.sql on purpose: schema.sql is applied on every install, and these tables belong to a layer
-- that is first run in shadow mode (subscribers judge, nothing acts) before it is switched live.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS brain;

-- One row per thing that happened between the owner and a person, every channel, both directions.
-- Dedupe key: (source, source_id). `source` names the adapter instance (gmail:work, gmail:personal, whatsapp,
-- linkedin, slack:work, calendar:work, notes:meetings, crm); `source_id` is the id that source gives the thing.
CREATE TABLE IF NOT EXISTS brain.events (
    id          bigserial   PRIMARY KEY,
    source      text        NOT NULL,
    source_id   text        NOT NULL,
    channel     text        NOT NULL CHECK (channel IN ('email','whatsapp','linkedin','slack','calendar','meeting','crm')),
    direction   text        NOT NULL CHECK (direction IN ('in','out','internal')),   -- out = the owner sent it
    ts          timestamptz NOT NULL,                  -- when it happened at the source (not when we saw it)
    ts_precision text       NOT NULL DEFAULT 'exact' CHECK (ts_precision IN ('exact','minute','day')),
    thread      text,                                  -- thread / chat / conversation id at the source
    person_id   text,                                  -- crm.people.id; NULL until resolved (no FK: events may precede the row)
    person_via  text,                                  -- how the link was found: route | email | phone | linkedin | slack | manual
    handles     jsonb       NOT NULL DEFAULT '[]'::jsonb,   -- [{kind: email|phone|linkedin|slack|name, value}] the raw counterpart
    text        text        NOT NULL DEFAULT '',
    data        jsonb       NOT NULL DEFAULT '{}'::jsonb,   -- adapter extras (subject, url, account, attachments ...)
    tsv         tsvector    GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED,
    embedding   vector(384),                           -- filled by the index pass, never by ingest (ingest stays cheap)
    ingested_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS events_person_ts_idx  ON brain.events (person_id, ts DESC);
CREATE INDEX IF NOT EXISTS events_ts_idx         ON brain.events (ts DESC);
CREATE INDEX IF NOT EXISTS events_unresolved_idx ON brain.events (ingested_at) WHERE person_id IS NULL;
CREATE INDEX IF NOT EXISTS events_thread_idx     ON brain.events (source, thread);
CREATE INDEX IF NOT EXISTS events_tsv_gin        ON brain.events USING gin (tsv);
CREATE INDEX IF NOT EXISTS events_embedding_hnsw ON brain.events USING hnsw (embedding vector_cosine_ops);

-- The remembered route per person (remember the path that reached a person once): which
-- handle at which channel is this person. Written on the first resolution, read first on every later one, and used
-- for on-demand backfill (read THAT person's thread).
CREATE TABLE IF NOT EXISTS brain.event_routes (
    kind        text        NOT NULL,                  -- email | phone | linkedin | slack | wa_chat | li_thread
    value       text        NOT NULL,                  -- normalised (lowercase email, digits-only phone ...)
    person_id   text        NOT NULL,
    channel     text        NOT NULL,
    learned_via text        NOT NULL,                  -- crm | manual | backfill
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    hits        int         NOT NULL DEFAULT 1,
    PRIMARY KEY (kind, value)
);
CREATE INDEX IF NOT EXISTS event_routes_person_idx ON brain.event_routes (person_id);

-- Incremental reading: one opaque cursor per adapter instance, written in the same transaction as the events.
CREATE TABLE IF NOT EXISTS brain.event_cursors (
    source     text        PRIMARY KEY,
    cursor     jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Every subscriber sees every event it wants exactly once: a delivery row per (subscriber, event).
-- status error = retried by the next dispatch; mode shadow = the subscriber judged but did not act.
CREATE TABLE IF NOT EXISTS brain.event_deliveries (
    subscriber   text        NOT NULL,
    event_id     bigint      NOT NULL REFERENCES brain.events(id) ON DELETE CASCADE,
    mode         text        NOT NULL CHECK (mode IN ('shadow','live')),
    status       text        NOT NULL CHECK (status IN ('ok','skipped','error')),
    result       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    delivered_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (subscriber, event_id)
);
CREATE INDEX IF NOT EXISTS event_deliveries_error_idx ON brain.event_deliveries (subscriber) WHERE status = 'error';

-- Attribution: every time a path finds that an item (hub card, to-do, CRM step) is settled.
-- path A = event push (a subscriber on arrival), B = periodic sweep reading this log. First finder gets the credit;
-- the other path's later finding of the same item is kept as "would also have found it".
CREATE TABLE IF NOT EXISTS brain.event_findings (
    id        bigserial   PRIMARY KEY,
    surface   text        NOT NULL CHECK (surface IN ('hub','todo','crm')),
    item_id   text        NOT NULL,
    path      text        NOT NULL CHECK (path IN ('A','B')),
    finder    text        NOT NULL,                    -- subscriber or sweep name
    mode      text        NOT NULL CHECK (mode IN ('shadow','live')),
    verdict   text        NOT NULL,                    -- done | moved | outdated | none
    event_id  bigint      REFERENCES brain.events(id) ON DELETE SET NULL,   -- the event that proves it
    evidence  text        NOT NULL DEFAULT '',
    found_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE NULLS NOT DISTINCT (surface, item_id, path, finder, event_id)   -- PG 15+; a status-only finding has no event
);
CREATE INDEX IF NOT EXISTS event_findings_item_idx ON brain.event_findings (surface, item_id, found_at);

-- First finder per item, the other path's overlap, and time-to-find (found_at - the proving event's ts).
CREATE OR REPLACE VIEW brain.event_attribution AS
WITH f AS (
    SELECT f.*, e.ts AS event_ts,
           row_number() OVER (PARTITION BY f.surface, f.item_id ORDER BY f.found_at, f.id) AS rk
    FROM brain.event_findings f
    LEFT JOIN brain.events e ON e.id = f.event_id
    WHERE f.verdict <> 'none'
)
SELECT first.surface, first.item_id, first.path AS first_path, first.finder AS first_finder, first.mode,
       first.found_at, EXTRACT(EPOCH FROM first.found_at - first.event_ts) AS time_to_find_s,
       EXISTS (SELECT 1 FROM f o WHERE o.surface = first.surface AND o.item_id = first.item_id
                                   AND o.path <> first.path) AS other_path_also
FROM f first
WHERE first.rk = 1;
