CREATE TABLE IF NOT EXISTS jira_issues (
    issue_key TEXT PRIMARY KEY,
    issue_id TEXT NOT NULL UNIQUE,
    project_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    issue_type TEXT NOT NULL,
    status TEXT NOT NULL,
    priority TEXT,
    assignee TEXT,
    reporter TEXT,
    labels JSONB NOT NULL DEFAULT '[]'::jsonb,
    labels_text TEXT NOT NULL DEFAULT '',
    activity_text TEXT NOT NULL DEFAULT '',
    web_url TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    raw_data JSONB NOT NULL,
    synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    search_vector TSVECTOR
        GENERATED ALWAYS AS (
            to_tsvector(
                'english',
                coalesce(issue_key, '') || ' ' ||
                coalesce(summary, '') || ' ' ||
                coalesce(description, '') || ' ' ||
                coalesce(issue_type, '') || ' ' ||
                coalesce(status, '') || ' ' ||
                coalesce(priority, '') || ' ' ||
                coalesce(assignee, '') || ' ' ||
                coalesce(labels_text, '') || ' ' ||
                coalesce(activity_text, '')
            )
        ) STORED
);

CREATE INDEX IF NOT EXISTS jira_issues_project_updated_idx
    ON jira_issues (project_key, updated_at DESC);

CREATE INDEX IF NOT EXISTS jira_issues_search_idx
    ON jira_issues USING GIN (search_vector);

CREATE TABLE IF NOT EXISTS jira_comments (
    comment_id TEXT NOT NULL,
    issue_key TEXT NOT NULL REFERENCES jira_issues(issue_key) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (issue_key, comment_id)
);

CREATE INDEX IF NOT EXISTS jira_comments_issue_created_idx
    ON jira_comments (issue_key, created_at);

CREATE TABLE IF NOT EXISTS jira_changes (
    history_id TEXT NOT NULL,
    item_index INTEGER NOT NULL,
    issue_key TEXT NOT NULL REFERENCES jira_issues(issue_key) ON DELETE CASCADE,
    author_name TEXT NOT NULL,
    field TEXT NOT NULL,
    from_value TEXT,
    to_value TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (issue_key, history_id, item_index)
);

CREATE INDEX IF NOT EXISTS jira_changes_issue_created_idx
    ON jira_changes (issue_key, created_at);

CREATE TABLE IF NOT EXISTS jira_sync_state (
    project_key TEXT PRIMARY KEY,
    last_synced_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS jira_pending_actions (
    action_id UUID PRIMARY KEY,
    requester_id BIGINT NOT NULL,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    action_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    result_text TEXT,
    error TEXT,
    CONSTRAINT jira_pending_actions_status_check
        CHECK (status IN ('pending', 'executing', 'completed', 'cancelled', 'failed'))
);

CREATE INDEX IF NOT EXISTS jira_pending_actions_expiry_idx
    ON jira_pending_actions (status, expires_at);
