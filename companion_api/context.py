import os
from dataclasses import dataclass

from ai.context import (
    build_history_search_query,
    format_conversation_context,
    format_drive_context,
    format_jira_context,
)
from database.drive_files import get_recent_drive_chunks, search_drive_files
from database.jira import get_recent_jira_issues, search_jira_issues
from database.messages import get_recent_guild_messages, search_messages
from google_drive.client import get_google_drive_root_ids, google_drive_is_configured
from jira.client import get_jira_project_keys, jira_is_configured


@dataclass(frozen=True)
class SharedContext:
    discord: str
    drive: str
    jira: str
    source_counts: dict[str, int]


def get_companion_guild_id() -> int:
    value = os.getenv("COMPANION_GUILD_ID", "").strip()
    if not value:
        raise RuntimeError("COMPANION_GUILD_ID is required for the companion API")
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("COMPANION_GUILD_ID must be a Discord server ID") from exc


async def build_shared_context(question: str) -> SharedContext:
    search_query = build_history_search_query(question)
    guild_id = get_companion_guild_id()

    recent_messages = await get_recent_guild_messages(guild_id, limit=30)
    relevant_messages = (
        await search_messages(guild_id, search_query, limit=24) if search_query else []
    )

    drive_chunks = []
    if google_drive_is_configured():
        root_ids = get_google_drive_root_ids()
        drive_chunks = (
            await search_drive_files(search_query, root_ids=root_ids, limit=12)
            if search_query
            else await get_recent_drive_chunks(root_ids, limit=8)
        )

    jira_issues = []
    if jira_is_configured():
        project_keys = get_jira_project_keys()
        relevant_issues = (
            await search_jira_issues(
                search_query,
                project_keys=project_keys,
                limit=12,
            )
            if search_query
            else []
        )
        recent_issues = await get_recent_jira_issues(project_keys, limit=12)
        issues_by_key = {
            issue.issue_key: issue for issue in [*relevant_issues, *recent_issues]
        }
        jira_issues = list(issues_by_key.values())

    return SharedContext(
        discord=format_conversation_context(
            relevant_messages,
            recent_messages,
            max_characters=18_000,
        ),
        drive=format_drive_context(drive_chunks, max_characters=12_000),
        jira=format_jira_context(jira_issues, max_characters=14_000),
        source_counts={
            "discord_messages": len(relevant_messages) + len(recent_messages),
            "drive_chunks": len(drive_chunks),
            "jira_issues": len(jira_issues),
        },
    )
