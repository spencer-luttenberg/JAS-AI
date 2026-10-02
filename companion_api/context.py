import os
from dataclasses import dataclass

from ai.context import (
    build_history_search_query,
    format_conversation_context,
    format_drive_context,
    format_github_context,
    format_jira_context,
)
from database.drive_files import get_recent_drive_chunks, search_drive_files
from database.github import get_recent_github_chunks, search_github_files
from database.jira import get_recent_jira_issues, search_jira_issues
from database.messages import get_recent_guild_messages, search_messages
from github.client import get_github_repositories, github_is_configured
from google_drive.client import get_google_drive_root_ids, google_drive_is_configured
from jira.client import get_jira_project_keys, jira_is_configured


@dataclass(frozen=True)
class SharedContext:
    discord: str
    drive: str
    github: str
    jira: str
    source_counts: dict[str, int]


def format_shared_context(
    context: SharedContext,
    *,
    max_characters: int = 24_000,
) -> str:
    """Render useful project memory within a stable, caller-selected budget."""
    if not 4_000 <= max_characters <= 30_000:
        raise ValueError("max_characters must be between 4,000 and 30,000")
    sections = [
        ("Discord", context.discord, 0.32),
        ("GitHub", context.github, 0.30),
        ("Google Drive", context.drive, 0.20),
        ("Jira", context.jira, 0.18),
    ]
    available = [
        (title, text.strip(), weight)
        for title, text, weight in sections
        if text.strip()
    ]
    if not available:
        return ""

    # Reserve a weighted slice for every populated source, then let shorter
    # sources donate their unused space to the remaining relevant excerpts.
    heading_cost = sum(len(f"## {title}\n\n") for title, _, _ in available)
    content_budget = max(0, max_characters - heading_cost - (len(available) - 1) * 2)
    weight_total = sum(weight for _, _, weight in available)
    allocations = {
        title: min(len(text), int(content_budget * weight / weight_total))
        for title, text, weight in available
    }
    unused = content_budget - sum(allocations.values())
    for title, text, _ in available:
        if unused <= 0:
            break
        extra = min(len(text) - allocations[title], unused)
        allocations[title] += extra
        unused -= extra

    rendered = []
    for title, text, _ in available:
        limit = allocations[title]
        excerpt = text[:limit]
        if limit < len(text) and limit > 1:
            excerpt = excerpt[:-1].rstrip() + "…"
        rendered.append(f"## {title}\n\n{excerpt}")
    return "\n\n".join(rendered)[:max_characters]


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

    github_chunks = []
    if github_is_configured():
        repositories = get_github_repositories()
        github_chunks = (
            await search_github_files(
                search_query,
                repositories=repositories,
                limit=14,
            )
            if search_query
            else await get_recent_github_chunks(repositories, limit=10)
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
        github=format_github_context(github_chunks, max_characters=16_000),
        jira=format_jira_context(jira_issues, max_characters=14_000),
        source_counts={
            "discord_messages": len(relevant_messages) + len(recent_messages),
            "drive_chunks": len(drive_chunks),
            "github_chunks": len(github_chunks),
            "jira_issues": len(jira_issues),
        },
    )
