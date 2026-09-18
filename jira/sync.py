import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging

from database.jira import get_jira_last_sync, mark_jira_synced, save_jira_issue
from jira.client import JiraClient, get_jira_project_keys, get_jira_sync_interval


logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()


@dataclass(frozen=True)
class JiraSyncResult:
    projects_synced: int
    issues_synced: int


async def sync_jira() -> JiraSyncResult:
    project_keys = get_jira_project_keys()
    if not project_keys:
        raise RuntimeError("JIRA_PROJECT_KEYS is required")

    async with _sync_lock:
        projects_synced = 0
        issues_synced = 0
        async with JiraClient.from_environment() as client:
            for project_key in project_keys:
                sync_started_at = datetime.now(timezone.utc)
                last_sync = await get_jira_last_sync(project_key)
                jql = _project_sync_jql(project_key, last_sync)
                project_issue_count = 0

                async for raw_issue in client.search_issues(jql):
                    issue = await client.hydrate_issue(raw_issue)
                    await save_jira_issue(issue)
                    project_issue_count += 1

                await mark_jira_synced(project_key, sync_started_at)
                projects_synced += 1
                issues_synced += project_issue_count
                logger.info(
                    "Jira sync completed for %s: %s issue(s)",
                    project_key,
                    project_issue_count,
                )

        return JiraSyncResult(projects_synced, issues_synced)


async def sync_jira_issue(issue_key: str) -> None:
    async with JiraClient.from_environment() as client:
        raw_issue = await client.get_issue(issue_key)
        issue = await client.hydrate_issue(raw_issue)
    await save_jira_issue(issue)


async def run_jira_sync_forever() -> None:
    interval = get_jira_sync_interval()
    while True:
        try:
            await sync_jira()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Jira sync failed")
        await asyncio.sleep(interval)


def _project_sync_jql(project_key: str, last_sync: datetime | None) -> str:
    clauses = [f'project = "{project_key}"']
    if last_sync is not None:
        overlap_start = last_sync.astimezone(timezone.utc) - timedelta(minutes=5)
        clauses.append(f'updated >= "{overlap_start:%Y-%m-%d %H:%M}"')
    return " AND ".join(clauses) + " ORDER BY updated ASC"
