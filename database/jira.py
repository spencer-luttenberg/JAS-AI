import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence
from uuid import UUID, uuid4

from database.db import get_pool
from jira.models import JiraIssue


@dataclass(frozen=True)
class StoredJiraIssue:
    issue_key: str
    project_key: str
    summary: str
    description: str
    issue_type: str
    status: str
    priority: str | None
    assignee: str | None
    labels: tuple[str, ...]
    activity_text: str
    web_url: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PendingJiraAction:
    action_id: UUID
    requester_id: int
    guild_id: int
    channel_id: int
    action_type: str
    payload: dict[str, Any]
    status: str
    created_at: datetime
    expires_at: datetime


async def save_jira_issue(issue: JiraIssue) -> None:
    labels_text = " ".join(issue.labels)
    activity_text = _build_activity_text(issue)
    raw_data = json.dumps(issue.raw_data, ensure_ascii=True, default=str)

    async with get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO jira_issues (
                    issue_key,
                    issue_id,
                    project_key,
                    summary,
                    description,
                    issue_type,
                    status,
                    priority,
                    assignee,
                    reporter,
                    labels,
                    labels_text,
                    activity_text,
                    web_url,
                    created_at,
                    updated_at,
                    raw_data,
                    synced_at
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11::jsonb, $12, $13, $14, $15, $16, $17::jsonb, NOW()
                )
                ON CONFLICT (issue_key) DO UPDATE SET
                    issue_id = EXCLUDED.issue_id,
                    project_key = EXCLUDED.project_key,
                    summary = EXCLUDED.summary,
                    description = EXCLUDED.description,
                    issue_type = EXCLUDED.issue_type,
                    status = EXCLUDED.status,
                    priority = EXCLUDED.priority,
                    assignee = EXCLUDED.assignee,
                    reporter = EXCLUDED.reporter,
                    labels = EXCLUDED.labels,
                    labels_text = EXCLUDED.labels_text,
                    activity_text = EXCLUDED.activity_text,
                    web_url = EXCLUDED.web_url,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    raw_data = EXCLUDED.raw_data,
                    synced_at = NOW()
                """,
                issue.issue_key,
                issue.issue_id,
                issue.project_key,
                issue.summary,
                issue.description,
                issue.issue_type,
                issue.status,
                issue.priority,
                issue.assignee,
                issue.reporter,
                json.dumps(issue.labels, ensure_ascii=True),
                labels_text,
                activity_text,
                issue.web_url,
                issue.created_at,
                issue.updated_at,
                raw_data,
            )
            await connection.execute(
                "DELETE FROM jira_comments WHERE issue_key = $1",
                issue.issue_key,
            )
            if issue.comments:
                await connection.executemany(
                    """
                    INSERT INTO jira_comments (
                        comment_id, issue_key, author_name, body, created_at, updated_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    [
                        (
                            comment.comment_id,
                            issue.issue_key,
                            comment.author_name,
                            comment.body,
                            comment.created_at,
                            comment.updated_at,
                        )
                        for comment in issue.comments
                    ],
                )

            await connection.execute(
                "DELETE FROM jira_changes WHERE issue_key = $1",
                issue.issue_key,
            )
            if issue.changes:
                await connection.executemany(
                    """
                    INSERT INTO jira_changes (
                        history_id,
                        item_index,
                        issue_key,
                        author_name,
                        field,
                        from_value,
                        to_value,
                        created_at
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    [
                        (
                            change.history_id,
                            change.item_index,
                            issue.issue_key,
                            change.author_name,
                            change.field,
                            change.from_value,
                            change.to_value,
                            change.created_at,
                        )
                        for change in issue.changes
                    ],
                )


async def get_jira_last_sync(project_key: str) -> datetime | None:
    return await get_pool().fetchval(
        "SELECT last_synced_at FROM jira_sync_state WHERE project_key = $1",
        project_key,
    )


async def mark_jira_synced(project_key: str, synced_at: datetime) -> None:
    await get_pool().execute(
        """
        INSERT INTO jira_sync_state (project_key, last_synced_at)
        VALUES ($1, $2)
        ON CONFLICT (project_key) DO UPDATE SET
            last_synced_at = EXCLUDED.last_synced_at
        """,
        project_key,
        synced_at,
    )


async def search_jira_issues(
    search_query: str,
    *,
    project_keys: Sequence[str] | None = None,
    limit: int = 10,
) -> list[StoredJiraIssue]:
    if not search_query:
        return []
    records = await get_pool().fetch(
        """
        WITH parsed_query AS (
            SELECT websearch_to_tsquery('english', $1) AS value
        )
        SELECT
            issue_key,
            project_key,
            summary,
            description,
            issue_type,
            status,
            priority,
            assignee,
            labels,
            activity_text,
            web_url,
            created_at,
            updated_at
        FROM jira_issues, parsed_query
        WHERE search_vector @@ parsed_query.value
          AND ($2::text[] IS NULL OR project_key = ANY($2::text[]))
        ORDER BY
            ts_rank_cd(search_vector, parsed_query.value) DESC,
            updated_at DESC
        LIMIT $3
        """,
        search_query,
        list(project_keys) if project_keys is not None else None,
        max(1, min(limit, 50)),
    )
    return [_stored_issue(record) for record in records]


async def get_recent_jira_issues(
    project_keys: Sequence[str],
    *,
    since: datetime | None = None,
    limit: int = 40,
) -> list[StoredJiraIssue]:
    if not project_keys:
        return []
    records = await get_pool().fetch(
        """
        SELECT
            issue_key,
            project_key,
            summary,
            description,
            issue_type,
            status,
            priority,
            assignee,
            labels,
            activity_text,
            web_url,
            created_at,
            updated_at
        FROM jira_issues
        WHERE project_key = ANY($1::text[])
          AND ($2::timestamptz IS NULL OR updated_at > $2)
        ORDER BY updated_at DESC
        LIMIT $3
        """,
        list(project_keys),
        since,
        max(1, min(limit, 200)),
    )
    return [_stored_issue(record) for record in records]


async def create_pending_jira_action(
    *,
    requester_id: int,
    guild_id: int,
    channel_id: int,
    action_type: str,
    payload: dict[str, Any],
    lifetime_minutes: int = 15,
) -> PendingJiraAction:
    action_id = uuid4()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=lifetime_minutes)
    record = await get_pool().fetchrow(
        """
        INSERT INTO jira_pending_actions (
            action_id,
            requester_id,
            guild_id,
            channel_id,
            action_type,
            payload,
            expires_at
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
        RETURNING *
        """,
        action_id,
        requester_id,
        guild_id,
        channel_id,
        action_type,
        json.dumps(payload, ensure_ascii=True),
        expires_at,
    )
    return _pending_action(record)


async def get_pending_jira_action(action_id: UUID) -> PendingJiraAction | None:
    record = await get_pool().fetchrow(
        "SELECT * FROM jira_pending_actions WHERE action_id = $1",
        action_id,
    )
    return _pending_action(record) if record is not None else None


async def claim_pending_jira_action(
    action_id: UUID,
    requester_id: int,
) -> PendingJiraAction | None:
    record = await get_pool().fetchrow(
        """
        UPDATE jira_pending_actions
        SET status = 'executing'
        WHERE action_id = $1
          AND requester_id = $2
          AND status = 'pending'
          AND expires_at > NOW()
        RETURNING *
        """,
        action_id,
        requester_id,
    )
    return _pending_action(record) if record is not None else None


async def cancel_pending_jira_action(action_id: UUID, requester_id: int) -> bool:
    result = await get_pool().execute(
        """
        UPDATE jira_pending_actions
        SET status = 'cancelled', completed_at = NOW()
        WHERE action_id = $1
          AND requester_id = $2
          AND status = 'pending'
          AND expires_at > NOW()
        """,
        action_id,
        requester_id,
    )
    return result == "UPDATE 1"


async def expire_pending_jira_action(action_id: UUID) -> bool:
    result = await get_pool().execute(
        """
        UPDATE jira_pending_actions
        SET
            status = 'cancelled',
            completed_at = NOW(),
            result_text = 'Approval expired'
        WHERE action_id = $1 AND status = 'pending'
        """,
        action_id,
    )
    return result == "UPDATE 1"


async def complete_pending_jira_action(action_id: UUID, result_text: str) -> None:
    await get_pool().execute(
        """
        UPDATE jira_pending_actions
        SET status = 'completed', completed_at = NOW(), result_text = $2
        WHERE action_id = $1 AND status = 'executing'
        """,
        action_id,
        result_text[:2_000],
    )


async def fail_pending_jira_action(action_id: UUID, error: str) -> None:
    await get_pool().execute(
        """
        UPDATE jira_pending_actions
        SET status = 'failed', completed_at = NOW(), error = $2
        WHERE action_id = $1 AND status = 'executing'
        """,
        action_id,
        error[:2_000],
    )


def _build_activity_text(issue: JiraIssue, max_characters: int = 100_000) -> str:
    lines = [
        f"Comment by {comment.author_name}: {comment.body}"
        for comment in issue.comments
        if comment.body
    ]
    lines.extend(
        f"Change by {change.author_name}: {change.field} "
        f"from {change.from_value or '(empty)'} to {change.to_value or '(empty)'}"
        for change in issue.changes
    )
    return "\n".join(lines)[-max_characters:]


def _stored_issue(record) -> StoredJiraIssue:
    labels = record["labels"]
    if isinstance(labels, str):
        labels = json.loads(labels)
    return StoredJiraIssue(
        issue_key=record["issue_key"],
        project_key=record["project_key"],
        summary=record["summary"],
        description=record["description"],
        issue_type=record["issue_type"],
        status=record["status"],
        priority=record["priority"],
        assignee=record["assignee"],
        labels=tuple(labels or ()),
        activity_text=record["activity_text"],
        web_url=record["web_url"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _pending_action(record) -> PendingJiraAction:
    payload = record["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return PendingJiraAction(
        action_id=record["action_id"],
        requester_id=record["requester_id"],
        guild_id=record["guild_id"],
        channel_id=record["channel_id"],
        action_type=record["action_type"],
        payload=payload,
        status=record["status"],
        created_at=record["created_at"],
        expires_at=record["expires_at"],
    )
