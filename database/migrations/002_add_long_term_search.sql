ALTER TABLE discord_messages
    ADD COLUMN IF NOT EXISTS search_vector TSVECTOR
    GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(author_name, '') || ' ' || coalesce(content, '')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS discord_messages_search_idx
    ON discord_messages USING GIN (search_vector);

CREATE TABLE IF NOT EXISTS discord_channel_sync_state (
    channel_id BIGINT PRIMARY KEY,
    guild_id BIGINT NOT NULL,
    last_message_id BIGINT,
    backfilled_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);
