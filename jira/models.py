from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class JiraComment:
    comment_id: str
    author_name: str
    body: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class JiraChange:
    history_id: str
    item_index: int
    author_name: str
    field: str
    from_value: str | None
    to_value: str | None
    created_at: datetime


@dataclass(frozen=True)
class JiraIssue:
    issue_id: str
    issue_key: str
    project_key: str
    summary: str
    description: str
    issue_type: str
    status: str
    priority: str | None
    assignee: str | None
    reporter: str | None
    labels: tuple[str, ...]
    web_url: str
    created_at: datetime
    updated_at: datetime
    comments: tuple[JiraComment, ...]
    changes: tuple[JiraChange, ...]
    raw_data: dict[str, Any]
