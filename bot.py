import asyncio
from contextlib import suppress
import logging
import os
import re

import discord
from discord.ext import commands
from openai import OpenAIError

from ai.client import ask_openai
from ai.context import (
    build_history_search_query,
    format_conversation_context,
    format_drive_context,
    format_jira_context,
    format_jira_overview_context,
)
from companion_api.server import start_companion_api
from database.db import close_database, connect_database
from database.drive_files import search_drive_files
from database.jira import get_recent_jira_issues, search_jira_issues
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
from google_drive.client import get_google_drive_root_ids, google_drive_is_configured
from google_drive.sync import run_google_drive_sync_forever, sync_google_drive
from jira.actions import propose_jira_action, propose_jira_action_from_message
from jira.client import (
    get_jira_project_keys,
    get_jira_sync_interval,
    jira_is_configured,
    validate_issue_key,
    validate_project_key,
)
from jira.sync import run_jira_sync_forever, sync_jira, sync_jira_issue
from project_updates.scheduler import (
    get_project_update_channel_id,
    publish_project_update,
    run_project_update_scheduler,
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
    drive_sync_task: asyncio.Task[None] | None = None
    jira_sync_task: asyncio.Task[None] | None = None
    project_update_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await connect_database()

    async def close(self) -> None:
        if self.history_sync_task is not None and not self.history_sync_task.done():
            self.history_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.history_sync_task

        if self.drive_sync_task is not None and not self.drive_sync_task.done():
            self.drive_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.drive_sync_task

        if self.jira_sync_task is not None and not self.jira_sync_task.done():
            self.jira_sync_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.jira_sync_task

        if self.project_update_task is not None and not self.project_update_task.done():
            self.project_update_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.project_update_task

        await super().close()


bot = JarrettBot(command_prefix="!", intents=intents)


def split_message(content: str, limit: int = 2_000) -> list[str]:
    return [content[start : start + limit] for start in range(0, len(content), limit)]


def extract_mention_question(message: discord.Message) -> str | None:
    if bot.user is None or not bot.user.mentioned_in(message):
        return None

    mention_pattern = rf"<@!?{bot.user.id}>"
    question = re.sub(mention_pattern, "", message.content)
    question = re.sub(r"\s+([,;:?!])", r"\1", question)
    return question.strip(" ,;:-")


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


async def answer_question(
    message: discord.Message,
    question: str,
    *,
    reply_to_message: bool,
) -> None:
    try:
        async with message.channel.typing():
            conversation_context = ""
            drive_context = ""
            jira_context = ""
            jira_project_keys: tuple[str, ...] = ()
            if message.guild is not None and is_listened_channel(message.channel):
                search_query = build_history_search_query(question)
                relevant_messages = []
                recent_messages = []
                try:
                    recent_messages = await get_recent_messages(
                        message.channel.id,
                        exclude_message_id=message.id,
                        limit=20,
                    )
                except Exception:
                    logger.exception("Could not load recent Discord conversation")

                try:
                    relevant_messages = await search_messages(
                        message.guild.id,
                        search_query,
                        exclude_message_id=message.id,
                    )
                except Exception:
                    logger.exception("Could not search long-term Discord history")

                conversation_context = format_conversation_context(
                    relevant_messages,
                    recent_messages,
                )

                if google_drive_is_configured():
                    try:
                        drive_chunks = await search_drive_files(
                            search_query,
                            root_ids=get_google_drive_root_ids(),
                        )
                        drive_context = format_drive_context(drive_chunks)
                    except Exception:
                        logger.exception("Could not load Google Drive context")

                try:
                    if jira_is_configured():
                        project_keys = get_jira_project_keys()
                        jira_project_keys = project_keys
                        refresh_jira = question_requests_jira_overview(question)
                        if refresh_jira:
                            try:
                                await sync_jira()
                            except Exception:
                                logger.exception(
                                    "Live Jira refresh failed; using cached Jira data"
                                )

                        referenced_issue_keys = ()
                        if refresh_jira or re.search(
                            r"\b[A-Z][A-Z0-9_]*-\d+\b",
                            question.upper(),
                        ):
                            referenced_issue_keys = referenced_jira_issue_keys(
                                question,
                                conversation_context,
                                project_keys,
                            )
                        for issue_key in referenced_issue_keys:
                            try:
                                await sync_jira_issue(issue_key)
                            except Exception:
                                logger.exception(
                                    "Could not refresh referenced Jira issue %s",
                                    issue_key,
                                )

                        jira_issues = await search_jira_issues(
                            search_query,
                            project_keys=project_keys,
                        )
                        jira_sections = [format_jira_context(jira_issues)]
                        if refresh_jira:
                            recent_issues = await get_recent_jira_issues(
                                project_keys,
                                limit=40,
                            )
                            jira_sections.append(
                                format_jira_overview_context(recent_issues)
                            )
                        jira_context = "\n\n".join(
                            section for section in jira_sections if section
                        )
                except Exception:
                    logger.exception("Could not load Jira context")

            bot_answer = await ask_openai(
                question,
                conversation_context,
                drive_context,
                jira_context,
                jira_project_keys,
            )
    except OpenAIError:
        logger.exception("OpenAI request failed")
        await message.channel.send(
            "I couldn't reach OpenAI. Please try again in a moment."
        )
        return

    if bot_answer.jira_action is not None:
        try:
            await propose_jira_action_from_message(
                message,
                bot_answer.jira_action.action_type,
                bot_answer.jira_action.payload,
            )
        except Exception:
            logger.exception("Could not create Jira approval request")
            await message.channel.send(
                "I couldn't create the Jira approval request. Check the "
                "deployment logs and try again.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        return

    answer = bot_answer.text

    for index, chunk in enumerate(split_message(answer)):
        if reply_to_message and index == 0:
            sent_message = await message.reply(
                chunk,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            sent_message = await message.channel.send(
                chunk,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if should_store_message(sent_message):
            await store_message(sent_message)


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

    if google_drive_is_configured() and (
        bot.drive_sync_task is None or bot.drive_sync_task.done()
    ):
        bot.drive_sync_task = asyncio.create_task(
            run_google_drive_sync_forever(),
            name="google-drive-sync",
        )
    elif get_google_drive_root_ids() or os.getenv(
        "GOOGLE_SERVICE_ACCOUNT_JSON_BASE64"
    ) or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):
        logger.warning(
            "Google Drive indexing is disabled because both "
            "GOOGLE_DRIVE_FOLDER_IDS and a Google service-account credential "
            "are required"
        )

    try:
        jira_configured = jira_is_configured()
        if jira_configured:
            get_jira_sync_interval()
    except ValueError:
        jira_configured = False
        logger.exception("Jira integration configuration is invalid")

    if jira_configured and (
        bot.jira_sync_task is None or bot.jira_sync_task.done()
    ):
        bot.jira_sync_task = asyncio.create_task(
            run_jira_sync_forever(),
            name="jira-sync",
        )
    elif any(
        os.getenv(name)
        for name in (
            "JIRA_BASE_URL",
            "JIRA_EMAIL",
            "JIRA_API_TOKEN",
            "JIRA_PROJECT_KEYS",
        )
    ) and not jira_configured:
        logger.warning(
            "Jira indexing is disabled because JIRA_BASE_URL, JIRA_EMAIL, "
            "JIRA_API_TOKEN, and JIRA_PROJECT_KEYS are all required"
        )

    try:
        project_update_channel_id = get_project_update_channel_id()
    except ValueError:
        logger.exception("Project update scheduling is not configured correctly")
    else:
        if project_update_channel_id is not None and (
            bot.project_update_task is None or bot.project_update_task.done()
        ):
            bot.project_update_task = asyncio.create_task(
                run_project_update_scheduler(bot),
                name="project-update-scheduler",
            )


@bot.event
async def on_message(message: discord.Message):
    if should_store_message(message):
        await store_message(message)

    if message.author.bot:
        return

    mention_question = extract_mention_question(message)
    if mention_question is not None:
        if message.guild is not None and not is_listened_channel(message.channel):
            await message.reply(
                "This channel is not included in `LISTEN_CHANNEL_IDS`, so I "
                "cannot use conversation memory or connected Jira tools here. "
                "Add this channel's ID to that Railway variable and redeploy me.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not mention_question:
            await message.reply(
                "Mention me followed by a question, for example: "
                "`@JAS AI what did we decide about cameras?`",
                mention_author=False,
            )
        else:
            await answer_question(
                message,
                mention_question,
                reply_to_message=True,
            )
        return

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

    await answer_question(
        ctx.message,
        question,
        reply_to_message=False,
    )


@bot.command()
@commands.is_owner()
async def syncdrive(ctx):
    if not google_drive_is_configured():
        await ctx.send("Google Drive indexing is not configured.")
        return

    await ctx.send("Starting Google Drive sync...")
    try:
        result = await sync_google_drive()
    except Exception:
        logger.exception("Manual Google Drive sync failed")
        await ctx.send("Google Drive sync failed. Check the deployment logs.")
        return

    await ctx.send(
        "Google Drive sync complete: "
        f"{result.files_seen} file(s) found, "
        f"{result.files_indexed} indexed, "
        f"{result.files_unchanged} unchanged, "
        f"{result.files_skipped} skipped."
    )


@bot.command()
@commands.is_owner()
async def projectupdate(ctx):
    try:
        channel_id = get_project_update_channel_id()
    except ValueError:
        await ctx.send("PROJECT_UPDATE_CHANNEL_ID must be a numeric Discord ID.")
        return

    if channel_id is None:
        await ctx.send("PROJECT_UPDATE_CHANNEL_ID is not configured.")
        return

    await ctx.send("Generating a project intelligence update...")
    try:
        result = await publish_project_update(bot, force=True)
    except Exception:
        logger.exception("Manual project update failed")
        await ctx.send("Project update failed. Check the deployment logs.")
        return

    if result is None:
        await ctx.send("A project update is already running.")
    else:
        await ctx.send(f"Project update posted in <#{result.channel_id}>.")


async def jira_command_is_available(ctx: commands.Context) -> bool:
    if ctx.guild is None or not is_listened_channel(ctx.channel):
        await ctx.send(
            "Jira commands can only be used in a server channel included in "
            "`LISTEN_CHANNEL_IDS`."
        )
        return False
    try:
        configured = jira_is_configured()
    except ValueError as exc:
        await ctx.send(f"Jira configuration is invalid: {exc}")
        return False
    if not configured:
        await ctx.send("Jira is not configured on this deployment.")
        return False
    return True


def question_requests_jira_overview(question: str) -> bool:
    terms = set(re.findall(r"[a-z0-9]+", question.casefold()))
    direct_terms = {
        "backlog",
        "board",
        "comment",
        "comments",
        "jira",
        "sprint",
        "ticket",
        "tickets",
        "update",
        "updates",
        "worklog",
    }
    if terms & direct_terms:
        return True

    work_terms = {"bug", "bugs", "epic", "epics", "issue", "issues", "task", "tasks"}
    state_terms = {
        "assigned",
        "closed",
        "current",
        "currently",
        "done",
        "open",
        "pending",
        "status",
    }
    return bool(terms & work_terms and terms & state_terms)


def referenced_jira_issue_keys(
    question: str,
    conversation_context: str,
    project_keys: tuple[str, ...],
    *,
    limit: int = 5,
) -> tuple[str, ...]:
    recent_marker = "RECENT CHANNEL CONVERSATION (oldest to newest):"
    recent_context = conversation_context.rsplit(recent_marker, 1)[-1]
    matches = re.findall(
        r"\b[A-Z][A-Z0-9_]*-\d+\b",
        question.upper() + "\n" + recent_context.upper(),
    )
    allowed_projects = set(project_keys)
    issue_keys = tuple(
        dict.fromkeys(
            issue_key
            for issue_key in matches
            if issue_key.rsplit("-", 1)[0] in allowed_projects
        )
    )
    return issue_keys[-limit:]


@bot.group(name="jira", invoke_without_command=True)
async def jira_group(ctx):
    await ctx.send(
        "Jira commands: `!jira search`, `create`, `edit`, `comment`, "
        "`transition`, `assign`, `plan`, and `sync`. Every write command "
        "requires confirmation before Jira is changed."
    )


@jira_group.command(name="search")
async def jira_search(ctx, *, terms: str | None = None):
    if not await jira_command_is_available(ctx):
        return
    if not terms:
        await ctx.send("Usage: `!jira search search words or ISSUE-123`")
        return

    search_query = build_history_search_query(terms)
    issues = await search_jira_issues(
        search_query,
        project_keys=get_jira_project_keys(),
        limit=10,
    )
    if not issues:
        await ctx.send("No indexed Jira issues matched that search.")
        return

    lines = []
    for issue in issues:
        summary = discord.utils.escape_markdown(issue.summary)[:180]
        status = discord.utils.escape_markdown(issue.status)
        assignee = discord.utils.escape_markdown(issue.assignee or "Unassigned")
        lines.append(
            f"[{issue.issue_key}]({issue.web_url}) - {summary} "
            f"({status}; {assignee})"
        )
    await ctx.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


@jira_group.command(name="create")
async def jira_create(ctx, *, details: str | None = None):
    if not await jira_command_is_available(ctx):
        return
    parts = _jira_command_parts(details, 4)
    if parts is None:
        await ctx.send(
            "Usage: `!jira create PROJECT | ISSUE TYPE | SUMMARY | DESCRIPTION`"
        )
        return
    project_key, issue_type, summary, description = parts
    try:
        project_key = validate_project_key(project_key)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    if project_key not in get_jira_project_keys():
        await ctx.send("That project is not included in `JIRA_PROJECT_KEYS`.")
        return
    if not issue_type or not summary:
        await ctx.send("Issue type and summary cannot be empty.")
        return
    await propose_jira_action(
        ctx,
        "create",
        {
            "project_key": project_key,
            "issue_type": issue_type,
            "summary": summary,
            "description": description,
        },
    )


@jira_group.command(name="edit")
async def jira_edit(ctx, *, details: str | None = None):
    if not await jira_command_is_available(ctx):
        return
    parts = _jira_command_parts(details, 3)
    if parts is None:
        await ctx.send(
            "Usage: `!jira edit ISSUE-123 | summary/description/priority/labels | VALUE`"
        )
        return
    issue_key, field, value = parts
    try:
        issue_key = validate_issue_key(issue_key)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    if not _jira_issue_is_configured(issue_key):
        await ctx.send("That issue is outside the projects in `JIRA_PROJECT_KEYS`.")
        return
    field = field.casefold()
    if field not in {"summary", "description", "priority", "labels"}:
        await ctx.send("Editable fields are summary, description, priority, and labels.")
        return
    await propose_jira_action(
        ctx,
        "edit",
        {"issue_key": issue_key, "field": field, "value": value},
    )


@jira_group.command(name="comment")
async def jira_comment(ctx, *, details: str | None = None):
    if not await jira_command_is_available(ctx):
        return
    parts = _jira_command_parts(details, 2)
    if parts is None:
        await ctx.send("Usage: `!jira comment ISSUE-123 | COMMENT TEXT`")
        return
    issue_key, comment = parts
    try:
        issue_key = validate_issue_key(issue_key)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    if not _jira_issue_is_configured(issue_key):
        await ctx.send("That issue is outside the projects in `JIRA_PROJECT_KEYS`.")
        return
    if not comment:
        await ctx.send("The comment cannot be empty.")
        return
    await propose_jira_action(
        ctx,
        "comment",
        {"issue_key": issue_key, "comment": comment},
    )


@jira_group.command(name="transition")
async def jira_transition(ctx, *, details: str | None = None):
    if not await jira_command_is_available(ctx):
        return
    parts = _jira_command_parts(details, 2)
    if parts is None:
        await ctx.send("Usage: `!jira transition ISSUE-123 | STATUS NAME`")
        return
    issue_key, status = parts
    try:
        issue_key = validate_issue_key(issue_key)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    if not _jira_issue_is_configured(issue_key):
        await ctx.send("That issue is outside the projects in `JIRA_PROJECT_KEYS`.")
        return
    if not status:
        await ctx.send("The status cannot be empty.")
        return
    await propose_jira_action(
        ctx,
        "transition",
        {"issue_key": issue_key, "status": status},
    )


@jira_group.command(name="assign")
async def jira_assign(ctx, *, details: str | None = None):
    if not await jira_command_is_available(ctx):
        return
    parts = _jira_command_parts(details, 2)
    if parts is None:
        await ctx.send(
            "Usage: `!jira assign ISSUE-123 | PERSON NAME OR EMAIL` or `| unassigned`"
        )
        return
    issue_key, account_id = parts
    try:
        issue_key = validate_issue_key(issue_key)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    if not _jira_issue_is_configured(issue_key):
        await ctx.send("That issue is outside the projects in `JIRA_PROJECT_KEYS`.")
        return
    if not account_id:
        await ctx.send("The assignee cannot be empty; use `unassigned` to clear it.")
        return
    normalized_assignee = (
        None if account_id.casefold() in {"none", "unassigned"} else account_id
    )
    await propose_jira_action(
        ctx,
        "assign",
        {"issue_key": issue_key, "assignee": normalized_assignee},
    )


@jira_group.command(name="plan")
async def jira_plan(ctx, *, details: str | None = None):
    if not await jira_command_is_available(ctx):
        return
    parts = _jira_command_parts(details, 4)
    if parts is None:
        await ctx.send(
            "Usage: `!jira plan ISSUE-123 | current/- | STORY POINTS/- | "
            "PRIORITY/-`"
        )
        return
    issue_key, sprint, story_points, priority = parts
    try:
        issue_key = validate_issue_key(issue_key)
    except ValueError as exc:
        await ctx.send(str(exc))
        return
    if not _jira_issue_is_configured(issue_key):
        await ctx.send("That issue is outside the projects in `JIRA_PROJECT_KEYS`.")
        return

    no_change_values = {"", "-", "none", "unchanged"}
    sprint_value = sprint.casefold()
    if sprint_value not in no_change_values | {"current"}:
        await ctx.send("Sprint must be `current` or `-` for no change.")
        return
    points_value = None if story_points.casefold() in no_change_values else story_points
    priority_value = None if priority.casefold() in no_change_values else priority
    await propose_jira_action(
        ctx,
        "plan",
        {
            "issue_key": issue_key,
            "move_to_current_sprint": sprint_value == "current",
            "story_points": points_value,
            "priority": priority_value,
        },
    )


@jira_group.command(name="sync")
@commands.is_owner()
async def jira_sync(ctx):
    if not await jira_command_is_available(ctx):
        return
    await ctx.send("Starting Jira sync...")
    try:
        result = await sync_jira()
    except Exception:
        logger.exception("Manual Jira sync failed")
        await ctx.send("Jira sync failed. Check the deployment logs.")
        return
    await ctx.send(
        f"Jira sync complete: {result.issues_synced} changed issue(s) imported "
        f"across {result.projects_synced} project(s). A zero count means no "
        "new changes were found, not that the Jira index is empty."
    )


def _jira_command_parts(value: str | None, count: int) -> list[str] | None:
    if value is None:
        return None
    parts = [part.strip() for part in value.split("|", count - 1)]
    if len(parts) != count or any(not part for part in parts[:-1]):
        return None
    return parts


def _jira_issue_is_configured(issue_key: str) -> bool:
    return issue_key.rsplit("-", 1)[0] in get_jira_project_keys()


def _discord_login_retry_delay(attempt: int) -> int:
    return min(60 * (2 ** min(attempt, 4)), 15 * 60)


async def run_bot(
    client: discord.Client = bot,
    token: str = DISCORD_TOKEN,
) -> None:
    attempt = 0
    while True:
        try:
            async with client:
                await client.start(token)
            return
        except discord.LoginFailure:
            raise
        except discord.HTTPException as exc:
            if exc.status != 429:
                raise

            delay = _discord_login_retry_delay(attempt)
            attempt += 1
            logger.warning(
                "Discord temporarily rate-limited bot login; retrying in %s "
                "seconds without restarting the container",
                delay,
            )
            await asyncio.sleep(delay)
            client.clear()


async def run_application() -> None:
    await connect_database()
    companion_api = await start_companion_api()
    try:
        await run_bot()
    finally:
        if companion_api is not None:
            await companion_api.close()
        await close_database()


if __name__ == "__main__":
    try:
        asyncio.run(run_application())
    except KeyboardInterrupt:
        pass
