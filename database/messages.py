import json
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import discord

from database.db import get_pool


UPSERT_MESSAGE_SQL = """
    INSERT INTO discord_messages (
        message_id,
        guild_id,
        channel_id,
        author_id,
        author_name,
        is_bot,
        content,
        reply_to_message_id,
        attachments,
        created_at,
        edited_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
    ON CONFLICT (message_id) DO UPDATE SET
        guild_id = EXCLUDED.guild_id,
        channel_id = EXCLUDED.channel_id,
        author_id = EXCLUDED.author_id,
        author_name = EXCLUDED.author_name,
        is_bot = EXCLUDED.is_bot,
        content = EXCLUDED.content,
        reply_to_message_id = EXCLUDED.reply_to_message_id,
        attachments = EXCLUDED.attachments,
        edited_at = EXCLUDED.edited_at
"""


@dataclass(frozen=True)
class StoredMessage:
    message_id: int
    channel_id: int
    author_name: str
    is_bot: bool
    content: str
    attachments: tuple[dict[str, object], ...]
    created_at: datetime


@dataclass(frozen=True)
class ChannelHistorySyncState:
    backfilled: bool
    last_message_id: int | None


async def save_message(message: discord.Message) -> None:
    if message.guild is None:
        return

    await get_pool().execute(UPSERT_MESSAGE_SQL, *_message_values(message))


async def save_messages(messages: Iterable[discord.Message]) -> None:
    values = [
        _message_values(message)
        for message in messages
        if message.guild is not None
    ]
    if not values:
        return

    async with get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.executemany(UPSERT_MESSAGE_SQL, values)


async def delete_message(message_id: int) -> None:
    await get_pool().execute(
        "DELETE FROM discord_messages WHERE message_id = $1",
        message_id,
    )


async def delete_messages(message_ids: Iterable[int]) -> None:
    ids = list(message_ids)
    if not ids:
        return

    await get_pool().execute(
        "DELETE FROM discord_messages WHERE message_id = ANY($1::bigint[])",
        ids,
    )


async def get_recent_messages(
    channel_id: int,
    *,
    exclude_message_id: int | None = None,
    limit: int = 30,
) -> list[StoredMessage]:
    records = await get_pool().fetch(
        """
        SELECT message_id, channel_id, author_name, is_bot, content, attachments, created_at
        FROM (
            SELECT message_id, channel_id, author_name, is_bot, content, attachments, created_at
            FROM discord_messages
            WHERE channel_id = $1
              AND ($2::bigint IS NULL OR message_id <> $2)
            ORDER BY created_at DESC
            LIMIT $3
        ) AS recent_messages
        ORDER BY created_at ASC
        """,
        channel_id,
        exclude_message_id,
        max(1, min(limit, 100)),
    )

    return [
        StoredMessage(
            message_id=record["message_id"],
            channel_id=record["channel_id"],
            author_name=record["author_name"],
            is_bot=record["is_bot"],
            content=record["content"],
            attachments=tuple(_decode_attachments(record["attachments"])),
            created_at=record["created_at"],
        )
        for record in records
    ]


async def search_messages(
    guild_id: int,
    search_query: str,
    *,
    exclude_message_id: int | None = None,
    limit: int = 20,
) -> list[StoredMessage]:
    if not search_query:
        return []

    records = await get_pool().fetch(
        """
        WITH parsed_query AS (
            SELECT websearch_to_tsquery('english', $2) AS value
        )
        SELECT
            message_id,
            channel_id,
            author_name,
            is_bot,
            content,
            attachments,
            created_at
        FROM discord_messages, parsed_query
        WHERE guild_id = $1
          AND ($3::bigint IS NULL OR message_id <> $3)
          AND search_vector @@ parsed_query.value
        ORDER BY
            ts_rank_cd(search_vector, parsed_query.value) DESC,
            created_at DESC
        LIMIT $4
        """,
        guild_id,
        search_query,
        exclude_message_id,
        max(1, min(limit, 100)),
    )

    return [_stored_message(record) for record in records]


async def get_guild_messages_since(
    guild_id: int,
    since: datetime,
    *,
    limit: int = 250,
) -> list[StoredMessage]:
    records = await get_pool().fetch(
        """
        SELECT message_id, channel_id, author_name, is_bot, content, attachments, created_at
        FROM (
            SELECT
                message_id,
                channel_id,
                author_name,
                is_bot,
                content,
                attachments,
                created_at
            FROM discord_messages
            WHERE guild_id = $1
              AND created_at > $2
            ORDER BY created_at DESC
            LIMIT $3
        ) AS new_messages
        ORDER BY created_at ASC
        """,
        guild_id,
        since,
        max(1, min(limit, 1_000)),
    )
    return [_stored_message(record) for record in records]


async def search_follow_up_candidates(
    guild_id: int,
    *,
    limit: int = 50,
) -> list[StoredMessage]:
    records = await get_pool().fetch(
        """
        WITH parsed_query AS (
            SELECT websearch_to_tsquery(
                'english',
                'follow OR need OR should OR todo OR question OR issue OR problem '
                'OR remember OR track OR remind OR action OR pending OR fix'
            ) AS value
        )
        SELECT
            message_id,
            channel_id,
            author_name,
            is_bot,
            content,
            attachments,
            created_at
        FROM discord_messages, parsed_query
        WHERE guild_id = $1
          AND is_bot = FALSE
          AND search_vector @@ parsed_query.value
        ORDER BY
            ts_rank_cd(search_vector, parsed_query.value) DESC,
            created_at DESC
        LIMIT $2
        """,
        guild_id,
        max(1, min(limit, 200)),
    )
    return [_stored_message(record) for record in records]


async def get_channel_history_sync_state(
    channel_id: int,
) -> ChannelHistorySyncState:
    record = await get_pool().fetchrow(
        """
        SELECT last_message_id
        FROM discord_channel_sync_state
        WHERE channel_id = $1
        """,
        channel_id,
    )
    if record is None:
        return ChannelHistorySyncState(backfilled=False, last_message_id=None)

    return ChannelHistorySyncState(
        backfilled=True,
        last_message_id=record["last_message_id"],
    )


async def mark_channel_history_synced(
    channel_id: int,
    guild_id: int,
    last_message_id: int | None,
) -> None:
    await get_pool().execute(
        """
        INSERT INTO discord_channel_sync_state (
            channel_id,
            guild_id,
            last_message_id,
            backfilled_at,
            updated_at
        )
        VALUES ($1, $2, $3, NOW(), NOW())
        ON CONFLICT (channel_id) DO UPDATE SET
            guild_id = EXCLUDED.guild_id,
            last_message_id = EXCLUDED.last_message_id,
            updated_at = NOW()
        """,
        channel_id,
        guild_id,
        last_message_id,
    )


def _message_values(message: discord.Message) -> tuple[object, ...]:
    if message.guild is None:
        raise ValueError("Cannot store a direct message")

    reply_to_message_id = None
    if message.reference is not None:
        reply_to_message_id = message.reference.message_id

    attachments = [
        {
            "id": attachment.id,
            "filename": attachment.filename,
            "description": attachment.description,
            "content_type": attachment.content_type,
            "size": attachment.size,
            "url": attachment.url,
            "width": attachment.width,
            "height": attachment.height,
            "spoiler": attachment.is_spoiler(),
        }
        for attachment in message.attachments
    ]

    return (
        message.id,
        message.guild.id,
        message.channel.id,
        message.author.id,
        message.author.display_name,
        message.author.bot,
        message.content,
        reply_to_message_id,
        json.dumps(attachments),
        message.created_at,
        message.edited_at,
    )


def _stored_message(record) -> StoredMessage:
    return StoredMessage(
        message_id=record["message_id"],
        channel_id=record["channel_id"],
        author_name=record["author_name"],
        is_bot=record["is_bot"],
        content=record["content"],
        attachments=tuple(_decode_attachments(record["attachments"])),
        created_at=record["created_at"],
    )


def _decode_attachments(value: str | list[dict[str, object]]) -> list[dict[str, object]]:
    if isinstance(value, str):
        return json.loads(value)
    return value
