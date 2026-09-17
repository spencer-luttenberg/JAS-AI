CREATE TABLE IF NOT EXISTS discord_messages (
    message_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    channel_id BIGINT NOT NULL,
    author_id BIGINT NOT NULL,
    author_name TEXT NOT NULL,
    is_bot BOOLEAN NOT NULL DEFAULT FALSE,
    content TEXT NOT NULL DEFAULT '',
    reply_to_message_id BIGINT,
    attachments JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL,
    edited_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS discord_messages_channel_created_idx
    ON discord_messages (channel_id, created_at DESC);

CREATE INDEX IF NOT EXISTS discord_messages_author_idx
    ON discord_messages (author_id);
