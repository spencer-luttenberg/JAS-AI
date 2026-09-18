CREATE TABLE IF NOT EXISTS project_update_state (
    channel_id BIGINT PRIMARY KEY,
    current_run_id UUID,
    last_started_at TIMESTAMPTZ,
    last_completed_at TIMESTAMPTZ,
    last_message_id BIGINT,
    last_error TEXT
);
