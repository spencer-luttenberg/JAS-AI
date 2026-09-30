CREATE TABLE IF NOT EXISTS github_repositories (
    full_name TEXT PRIMARY KEY,
    default_branch TEXT,
    head_sha TEXT,
    web_url TEXT,
    is_private BOOLEAN NOT NULL DEFAULT FALSE,
    last_synced_at TIMESTAMPTZ,
    file_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS github_files (
    repository TEXT NOT NULL
        REFERENCES github_repositories(full_name) ON DELETE CASCADE,
    path TEXT NOT NULL,
    blob_sha TEXT NOT NULL,
    size_bytes BIGINT,
    web_url TEXT,
    content_hash TEXT,
    indexed_at TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (repository, path)
);

CREATE TABLE IF NOT EXISTS github_chunks (
    repository TEXT NOT NULL,
    path TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    search_vector TSVECTOR
        GENERATED ALWAYS AS (
            to_tsvector(
                'english',
                coalesce(repository, '') || ' ' ||
                coalesce(path, '') || ' ' ||
                coalesce(content, '')
            )
        ) STORED,
    PRIMARY KEY (repository, path, chunk_index),
    FOREIGN KEY (repository, path)
        REFERENCES github_files(repository, path) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS github_chunks_search_idx
    ON github_chunks USING GIN (search_vector);

CREATE INDEX IF NOT EXISTS github_files_repository_idx
    ON github_files (repository, path);
