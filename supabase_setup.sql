-- One-time schema setup for the Supabase migration. Run once via the
-- Supabase SQL Editor or `psql "$DATABASE_URL" -f supabase_setup.sql`.
--
-- Booleans are kept as INTEGER 0/1 (not native BOOLEAN) on the four
-- pre-existing app tables, JSON columns are kept as TEXT (not JSONB), and
-- timestamps are kept as TEXT ISO-8601 -- all three deliberately match the
-- original SQLite schema's conventions, because every read/write site in the
-- app does its own Python-side bool()/json.dumps()/json.loads()/isoformat()
-- conversion. Changing column types here would make psycopg2's automatic
-- type adaptation silently return different Python types than those sites
-- expect (e.g. JSONB columns come back as already-parsed dicts, breaking
-- every json.loads(row["tags"]) call).
--
-- No REFERENCES/FK constraints are declared. SQLite never enforced the ones
-- in the original schema either (PRAGMA foreign_keys was never turned on),
-- and app.py's delete_project() actively depends on that being unenforced
-- (it deletes a project without first clearing tasks.project_id).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS tokens (
    provider      TEXT PRIMARY KEY,
    access_token  TEXT NOT NULL,
    refresh_token TEXT,
    token_type    TEXT NOT NULL DEFAULT 'Bearer',
    expires_at    TEXT
);

CREATE TABLE IF NOT EXISTS calendars (
    id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#4a9eff',
    source     TEXT NOT NULL DEFAULT 'local',
    ics_url    TEXT,
    visible    INTEGER NOT NULL DEFAULT 1,
    import_as  TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id             TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    calendar_id    TEXT NOT NULL,
    title          TEXT NOT NULL,
    description    TEXT,
    start_datetime TEXT NOT NULL,
    end_datetime   TEXT,
    all_day        INTEGER NOT NULL DEFAULT 0,
    location       TEXT,
    source_uid     TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    title      TEXT NOT NULL DEFAULT 'New Chat',
    indexed    INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id         TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    chat_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    sources    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS note_connections (
    id                TEXT PRIMARY KEY,
    source_path       TEXT NOT NULL,
    target_path       TEXT NOT NULL,
    source_collection TEXT,
    target_collection TEXT,
    distance          REAL,
    summary           TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT DEFAULT 'active',
    progress    INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    parent_task_id     TEXT,
    title              TEXT NOT NULL,
    description        TEXT,
    status             TEXT DEFAULT 'inbox',
    priority           TEXT DEFAULT 'medium',
    due_date           TEXT,
    completed_at       TEXT,
    estimated_effort   INTEGER,
    energy_type        TEXT,
    fear_level         INTEGER,
    ambiguity_level    INTEGER,
    project_id         TEXT,
    source_type        TEXT DEFAULT 'manual',
    ai_generated       INTEGER DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    tags               TEXT DEFAULT '[]',
    dependencies       TEXT DEFAULT '[]',
    task_notes         TEXT DEFAULT '[]',
    psych_reasoning    TEXT,
    recurring          TEXT,
    source_uid         TEXT,
    source_calendar_id TEXT
);

-- pgvector: single table for all vault chunks across every collection
-- (collection = one vault top-level folder, or the fixed "chats" collection).
-- Collection names are dynamic/unbounded, so a table-per-collection design
-- would require runtime DDL every time a new vault folder is created -- a
-- single filtered table avoids that. Chunk ids (md5(source_path)[:8]_{i})
-- are only unique *within* a collection (mirroring Chroma's independent
-- per-collection id namespaces), hence the composite primary key.
CREATE TABLE IF NOT EXISTS vault_chunks (
    id           TEXT NOT NULL,
    collection   TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    heading      TEXT DEFAULT '',
    ai_generated BOOLEAN NOT NULL DEFAULT false,
    reviewed     BOOLEAN NOT NULL DEFAULT true,
    text         TEXT NOT NULL,
    embedding    VECTOR(3072) NOT NULL,  -- confirmed via embed_query() dimensionality check
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (collection, id)
);
CREATE INDEX IF NOT EXISTS idx_vault_chunks_source
    ON vault_chunks (collection, source_path);
-- No HNSW/IVFFlat index on embedding: pgvector caps ANN indexes at 2000
-- dimensions and gemini-embedding-001 produces 3072-dim vectors. At personal-
-- vault scale (a handful of collections, low thousands of chunks) an exact
-- brute-force `ORDER BY embedding <=> query` scan per collection is fast
-- enough and strictly more accurate than an approximate index would be.

-- Replaces the in-memory rolling-window deque in services/ai/budget.py, which
-- silently loses its shared state across multiple serverless instances.
CREATE TABLE IF NOT EXISTS ai_usage_log (
    id    BIGSERIAL PRIMARY KEY,
    ts    TIMESTAMPTZ NOT NULL DEFAULT now(),
    cost  NUMERIC(12, 8) NOT NULL,
    kind  TEXT NOT NULL,  -- 'generation' | 'embedding'
    model TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_usage_log_ts ON ai_usage_log (ts);

-- Private bucket backing the vault (replaces local data/vault/ markdown files).
INSERT INTO storage.buckets (id, name, public)
VALUES ('vault', 'vault', false)
ON CONFLICT (id) DO NOTHING;

-- Training Journal (Phase 1) -------------------------------------------------
-- Immutable raw log (training_entries) + append-only structured extractions
-- (training_extractions) pulled from it on demand. See README's "Training
-- Journal" section for the full design rationale.

CREATE TABLE IF NOT EXISTS training_entries (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    entry_date  TEXT NOT NULL,   -- local calendar date (YYYY-MM-DD), resolved via the client's tz cookie
    content     TEXT NOT NULL,   -- raw text, verbatim, never edited after insert
    processed   INTEGER NOT NULL DEFAULT 0,  -- 0 = pending extraction, flips to 1 once processed
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_training_entries_date ON training_entries (entry_date);
CREATE INDEX IF NOT EXISTS idx_training_entries_processed ON training_entries (processed) WHERE processed = 0;

CREATE TABLE IF NOT EXISTS training_attachments (
    id           TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    entry_id     TEXT NOT NULL,
    storage_key  TEXT NOT NULL,   -- key in the 'training-journal' Storage bucket
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_training_attachments_entry ON training_attachments (entry_id);

-- Append-only: reprocessing an entry never UPDATEs a row here, it INSERTs new
-- ones (see services/training/extraction.py) so results stay fully
-- reproducible/re-runnable against a better model later, per the spec.
-- metric_type + a flexible JSON `data` payload (same convention as this
-- codebase's existing tags/dependencies/task_notes JSON-TEXT columns) instead
-- of one rigid table per metric, so new metric types don't need schema churn.
CREATE TABLE IF NOT EXISTS training_extractions (
    id                TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    source_entry_id   TEXT NOT NULL,
    entry_date        TEXT NOT NULL,   -- denormalized from the source entry for cheap date-range queries
    metric_type       TEXT NOT NULL,   -- weight | body_measurement | nutrition | sleep | resting_hr |
                                        -- run | workout_set | soreness_injury | mood_energy | recovery | steps | note
    data              TEXT NOT NULL,   -- JSON object, shape depends on metric_type
    confidence        REAL NOT NULL,
    extraction_model  TEXT NOT NULL,
    extracted_at      TEXT NOT NULL,
    superseded        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_training_extractions_lookup
    ON training_extractions (metric_type, entry_date) WHERE superseded = 0;

-- Private bucket for training-journal attachments (photos, PDFs, screenshots).
-- Separate from the 'vault' bucket deliberately: vault uploads trigger RAG
-- indexing side effects that attachments must never go through.
INSERT INTO storage.buckets (id, name, public)
VALUES ('training-journal', 'training-journal', false)
ON CONFLICT (id) DO NOTHING;
