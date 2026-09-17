import asyncio
from contextlib import suppress
import logging
import os

import discord
from discord.ext import commands
from openai import OpenAIError

from ai.client import ask_openai
from ai.context import build_history_search_query, format_conversation_context
from database.db import close_database, connect_database
from database.messages import (
    delete_message,
    delete_messages,
    get_channel_history_sync_state,
    get_recent_messages,
    mark_channel_history_synced,
    save_messages,
    save_message,
    search_messages,
)


DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
LISTEN_CHANNEL_IDS = {
    int(channel_id.strip())
    for channel_id in os.getenv("LISTEN_CHANNEL_IDS", "").split(",")
    if channel_id.strip()
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True


class JarrettBot(commands.Bot):
    history_sync_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await connect_database()

    async def close(self) -> None:
        if self.history_sync_task is not None and not self.history_sync_task.done():
            self.history_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.history_sync_task

        await close_database()
        await super().close()


bot = JarrettBot(command_prefix="!", intents=intents)


def split_message(content: str, limit: int = 2_000) -> list[str]:
    return [content[start : start + limit] for start in range(0, len(content), limit)]


def is_listened_channel(channel: discord.abc.GuildChannel | discord.Thread) -> bool:
    parent_id = getattr(channel, "parent_id", None)
    return channel.id in LISTEN_CHANNEL_IDS or parent_id in LISTEN_CHANNEL_IDS


def should_store_message(message: discord.Message) -> bool:
    if message.guild is None or not is_listened_channel(message.channel):
        return False

    is_own_message = bot.user is not None and message.author.id == bot.user.id
    return not message.author.bot or is_own_message


async def store_message(message: discord.Message) -> None:
    try:
        await save_message(message)
    except Exception:
        logger.exception("Could not store Discord message %s", message.id)


async def sync_channel_history(channel_id: int) -> None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            logger.exception("Could not fetch configured channel %s", channel_id)
            return

    guild = getattr(channel, "guild", None)
    if guild is None or not hasattr(channel, "history"):
        logger.warning("Configured channel %s has no readable guild history", channel_id)
        return

    state = await get_channel_history_sync_state(channel_id)
    after = None
    if state.backfilled and state.last_message_id is not None:
        after = discord.Object(id=state.last_message_id)

    mode = "catch-up" if state.backfilled else "full backfill"
    logger.info("Starting %s for Discord channel %s", mode, channel_id)

    saved_count = 0
    last_message_id = state.last_message_id
    batch: list[discord.Message] = []

    try:
        async for message in channel.history(
            limit=None,
            after=after,
            oldest_first=True,
        ):
            last_message_id = message.id
            if should_store_message(message):
                batch.append(message)

            if len(batch) >= 100:
                await save_messages(batch)
                saved_count += len(batch)
                batch.clear()

        if batch:
            await save_messages(batch)
            saved_count += len(batch)

        await mark_channel_history_synced(
            channel_id,
            guild.id,
            last_message_id,
        )
        logger.info(
            "Finished %s for channel %s; stored %s message(s)",
            mode,
            channel_id,
            saved_count,
        )
    except discord.Forbidden:
        logger.error(
            "Cannot read history for channel %s; grant View Channel and "
            "Read Message History permissions",
            channel_id,
        )
    except discord.HTTPException:
        logger.exception("Discord history sync failed for channel %s", channel_id)
    except Exception:
        logger.exception("Database history sync failed for channel %s", channel_id)


async def sync_all_channel_history() -> None:
    for channel_id in sorted(LISTEN_CHANNEL_IDS):
        await sync_channel_history(channel_id)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    print(f"Connected to {len(bot.guilds)} server(s)")
    print(f"Recording {len(LISTEN_CHANNEL_IDS)} configured channel(s)")

    if bot.history_sync_task is None:
        bot.history_sync_task = asyncio.create_task(
            sync_all_channel_history(),
            name="discord-history-sync",
        )


@bot.event
async def on_message(message: discord.Message):
    if should_store_message(message):
        await store_message(message)

    if not message.author.bot:
        await bot.process_commands(message)


@bot.event
async def on_raw_message_edit(payload: discord.RawMessageUpdateEvent):
    if payload.guild_id is None:
        return

    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except discord.HTTPException:
            logger.exception("Could not fetch edited message channel %s", payload.channel_id)
            return

    if not is_listened_channel(channel):
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.HTTPException:
        logger.exception("Could not fetch edited message %s", payload.message_id)
        return

    if should_store_message(message):
        await store_message(message)


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
    if payload.guild_id is None:
        return

    try:
        await delete_message(payload.message_id)
    except Exception:
        logger.exception("Could not delete Discord message %s", payload.message_id)


@bot.event
async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent):
    if payload.guild_id is None:
        return

    try:
        await delete_messages(payload.message_ids)
    except Exception:
        logger.exception("Could not delete a batch of Discord messages")


@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")


@bot.command()
async def hello(ctx):
    await ctx.send(f"Hello {ctx.author.mention}!")


@bot.command()
async def ask(ctx, *, question: str | None = None):
    if not question:
        await ctx.send("Usage: `!ask your question here`")
        return

    try:
        async with ctx.typing():
            conversation_context = ""
            if ctx.guild is not None and is_listened_channel(ctx.channel):
                try:
                    search_query = build_history_search_query(question)
                    relevant_messages = await search_messages(
                        ctx.guild.id,
                        search_query,
                        exclude_message_id=ctx.message.id,
                    )
                    recent_messages = await get_recent_messages(
                        ctx.channel.id,
                        exclude_message_id=ctx.message.id,
                        limit=15,
                    )
                    conversation_context = format_conversation_context(
                        relevant_messages,
                        recent_messages,
                    )
                except Exception:
                    logger.exception("Could not load Discord history context")

            answer = await ask_openai(question, conversation_context)
    except OpenAIError:
        logger.exception("OpenAI request failed")
        await ctx.send("I couldn't reach OpenAI. Please try again in a moment.")
        return

    for chunk in split_message(answer):
        await ctx.send(chunk, allowed_mentions=discord.AllowedMentions.none())


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
