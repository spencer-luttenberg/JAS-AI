CREATE TABLE IF NOT EXISTS google_drive_files (
    file_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    web_view_link TEXT,
    modified_at TIMESTAMPTZ,
    version TEXT,
    content_hash TEXT,
    indexed_at TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS google_drive_file_roots (
    root_id TEXT NOT NULL,
    file_id TEXT NOT NULL REFERENCES google_drive_files(file_id) ON DELETE CASCADE,
    PRIMARY KEY (root_id, file_id)
);

CREATE INDEX IF NOT EXISTS google_drive_file_roots_file_idx
    ON google_drive_file_roots (file_id);

CREATE TABLE IF NOT EXISTS google_drive_chunks (
    file_id TEXT NOT NULL REFERENCES google_drive_files(file_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    content TEXT NOT NULL,
    search_vector TSVECTOR
        GENERATED ALWAYS AS (
            to_tsvector(
                'english',
                coalesce(file_name, '') || ' ' || coalesce(content, '')
            )
        ) STORED,
    PRIMARY KEY (file_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS google_drive_chunks_search_idx
    ON google_drive_chunks USING GIN (search_vector);

CREATE TABLE IF NOT EXISTS google_drive_sync_state (
    root_id TEXT PRIMARY KEY,
    last_synced_at TIMESTAMPTZ NOT NULL,
    file_count INTEGER NOT NULL DEFAULT 0
);
