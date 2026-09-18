import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os

import discord

from ai.client import generate_project_update
from ai.context import format_drive_context, format_project_activity_context
from database.drive_files import get_recent_drive_chunks
from database.messages import (
    get_guild_messages_since,
    search_follow_up_candidates,
)
from database.project_updates import (
    claim_project_update,
    complete_project_update,
    fail_project_update,
)
from google_drive.client import get_google_drive_root_ids, google_drive_is_configured


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectUpdateResult:
    channel_id: int
    message_count: int


def get_project_update_channel_id() -> int | None:
    value = os.getenv("PROJECT_UPDATE_CHANNEL_ID", "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("PROJECT_UPDATE_CHANNEL_ID must be a numeric Discord ID") from exc


def get_project_update_interval_seconds() -> int:
    raw_value = os.getenv("PROJECT_UPDATE_INTERVAL_HOURS", "12")
    try:
        hours = float(raw_value)
    except ValueError as exc:
        raise ValueError("PROJECT_UPDATE_INTERVAL_HOURS must be a number") from exc
    if hours < 1:
        raise ValueError("PROJECT_UPDATE_INTERVAL_HOURS must be at least 1")
    return int(hours * 60 * 60)


async def run_project_update_scheduler(bot: discord.Client) -> None:
    try:
        startup_delay = _nonnegative_int_environment_value(
            "PROJECT_UPDATE_STARTUP_DELAY_SECONDS",
            300,
        )
        interval_seconds = get_project_update_interval_seconds()
    except ValueError:
        logger.exception("Project update scheduler configuration is invalid")
        return

    await asyncio.sleep(startup_delay)

    check_interval = min(300, interval_seconds)
    while True:
        try:
            await publish_project_update(bot, force=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled project update failed")
        await asyncio.sleep(check_interval)


async def publish_project_update(
    bot: discord.Client,
    *,
    force: bool,
) -> ProjectUpdateResult | None:
    channel_id = get_project_update_channel_id()
    if channel_id is None:
        raise RuntimeError("PROJECT_UPDATE_CHANNEL_ID is required")

    channel = bot.get_channel(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    guild = getattr(channel, "guild", None)
    if guild is None or not hasattr(channel, "send"):
        raise RuntimeError("PROJECT_UPDATE_CHANNEL_ID must identify a server text channel")

    interval_seconds = get_project_update_interval_seconds()
    claim = await claim_project_update(
        channel_id,
        interval_seconds,
        force=force,
    )
    if claim is None:
        return None

    try:
        since = claim.previous_completed_at or (
            datetime.now(timezone.utc) - timedelta(days=7)
        )
        new_messages, follow_ups = await asyncio.gather(
            get_guild_messages_since(guild.id, since),
            search_follow_up_candidates(guild.id),
        )
        activity_context = format_project_activity_context(
            new_messages,
            follow_ups,
        )

        drive_context = ""
        if google_drive_is_configured():
            drive_chunks = await get_recent_drive_chunks(
                get_google_drive_root_ids()
            )
            drive_context = format_drive_context(drive_chunks)

        update = await generate_project_update(
            project_name=os.getenv("PROJECT_NAME", "Jarrett AI"),
            since=since,
            activity_context=activity_context,
            drive_context=drive_context,
            web_search_topics=os.getenv("PROJECT_WEB_SEARCH_TOPICS", ""),
        )

        sent_messages = []
        for chunk in _split_message(update):
            sent_messages.append(
                await channel.send(
                    chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            )
        await complete_project_update(
            channel_id,
            claim.run_id,
            sent_messages[-1].id,
        )
        logger.info(
            "Posted project update to channel %s in %s message(s)",
            channel_id,
            len(sent_messages),
        )
        return ProjectUpdateResult(channel_id, len(sent_messages))
    except Exception as exc:
        await fail_project_update(channel_id, claim.run_id, str(exc))
        raise


def _split_message(content: str, limit: int = 2_000) -> list[str]:
    chunks: list[str] = []
    remaining = content.strip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = max(
            remaining.rfind("\n\n", 0, limit + 1),
            remaining.rfind("\n", 0, limit + 1),
            remaining.rfind(" ", 0, limit + 1),
        )
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def _nonnegative_int_environment_value(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value
