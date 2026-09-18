from collections.abc import AsyncIterator
from datetime import datetime
import asyncio
import json
import os
import re
from typing import Any
from urllib.parse import quote

import aiohttp

from jira.adf import adf_to_text, text_to_adf
from jira.models import JiraChange, JiraComment, JiraIssue


ISSUE_FIELDS = [
    "project",
    "summary",
    "description",
    "issuetype",
    "status",
    "priority",
    "assignee",
    "reporter",
    "labels",
    "created",
    "updated",
]
PROJECT_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$")


class JiraAPIError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Jira API returned {status}: {message}")
        self.status = status


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.api_token = api_token
        self._session: aiohttp.ClientSession | None = None

    @classmethod
    def from_environment(cls) -> "JiraClient":
        missing = [
            name
            for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
            if not os.getenv(name)
        ]
        if missing:
            raise ValueError(f"Missing Jira configuration: {', '.join(missing)}")
        return cls(
            os.environ["JIRA_BASE_URL"],
            os.environ["JIRA_EMAIL"],
            os.environ["JIRA_API_TOKEN"],
        )

    async def __aenter__(self) -> "JiraClient":
        timeout = aiohttp.ClientTimeout(total=60)
        self._session = aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(self.email, self.api_token),
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
        return self

    async def __aexit__(self, *_args) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def search_issues(self, jql: str) -> AsyncIterator[dict[str, Any]]:
        next_page_token = None
        while True:
            body: dict[str, Any] = {
                "jql": jql,
                "fields": ISSUE_FIELDS,
                "maxResults": 100,
            }
            if next_page_token:
                body["nextPageToken"] = next_page_token
            response = await self.request("POST", "/rest/api/3/search/jql", json=body)
            for issue in response.get("issues", []):
                yield issue

            next_page_token = response.get("nextPageToken")
            if response.get("isLast", not next_page_token) or not next_page_token:
                break

    async def get_issue(self, issue_key: str) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"/rest/api/3/issue/{quote(validate_issue_key(issue_key))}",
            params={"fields": ",".join(ISSUE_FIELDS)},
        )

    async def hydrate_issue(self, issue: dict[str, Any]) -> JiraIssue:
        issue_key = validate_issue_key(issue["key"])
        comments, changes = await self._get_comments(issue_key), await self._get_changes(
            issue_key
        )
        return _jira_issue(self.base_url, issue, comments, changes)

    async def create_issue(
        self,
        *,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
    ) -> str:
        fields: dict[str, Any] = {
            "project": {"key": validate_project_key(project_key)},
            "issuetype": {"name": issue_type.strip()},
            "summary": summary.strip(),
        }
        if description.strip():
            fields["description"] = text_to_adf(description.strip())
        result = await self.request(
            "POST",
            "/rest/api/3/issue",
            json={"fields": fields},
        )
        return validate_issue_key(result["key"])

    async def edit_issue(self, issue_key: str, field: str, value: str) -> str:
        issue_key = validate_issue_key(issue_key)
        field = field.casefold().strip()
        if field == "summary":
            fields: dict[str, Any] = {"summary": value.strip()}
        elif field == "description":
            fields = {"description": text_to_adf(value.strip())}
        elif field == "priority":
            fields = {"priority": {"name": value.strip()}}
        elif field == "labels":
            fields = {
                "labels": [label.strip() for label in value.split(",") if label.strip()]
            }
        else:
            raise ValueError("Editable fields are summary, description, priority, and labels")

        await self.request(
            "PUT",
            f"/rest/api/3/issue/{quote(issue_key)}",
            json={"fields": fields},
        )
        return issue_key

    async def add_comment(self, issue_key: str, body: str) -> str:
        issue_key = validate_issue_key(issue_key)
        await self.request(
            "POST",
            f"/rest/api/3/issue/{quote(issue_key)}/comment",
            json={"body": text_to_adf(body.strip())},
        )
        return issue_key

    async def transition_issue(self, issue_key: str, status_name: str) -> str:
        issue_key = validate_issue_key(issue_key)
        response = await self.request(
            "GET",
            f"/rest/api/3/issue/{quote(issue_key)}/transitions",
        )
        transitions = response.get("transitions", [])
        match = next(
            (
                transition
                for transition in transitions
                if transition.get("name", "").casefold() == status_name.strip().casefold()
                or transition.get("to", {}).get("name", "").casefold()
                == status_name.strip().casefold()
            ),
            None,
        )
        if match is None:
            available = ", ".join(
                transition.get("name", "Unknown") for transition in transitions
            )
            raise ValueError(f"Transition not available. Available transitions: {available}")

        await self.request(
            "POST",
            f"/rest/api/3/issue/{quote(issue_key)}/transitions",
            json={"transition": {"id": match["id"]}},
        )
        return issue_key

    async def assign_issue(self, issue_key: str, assignee: str | None) -> str:
        issue_key = validate_issue_key(issue_key)
        account_id = None
        if assignee is not None:
            account_id = await self._resolve_assignable_user(issue_key, assignee)
        await self.request(
            "PUT",
            f"/rest/api/3/issue/{quote(issue_key)}/assignee",
            json={"accountId": account_id},
        )
        return issue_key

    async def _resolve_assignable_user(self, issue_key: str, query: str) -> str:
        users = await self.request(
            "GET",
            "/rest/api/3/user/assignable/search",
            params={"issueKey": issue_key, "query": query.strip(), "maxResults": 20},
        )
        normalized = query.strip().casefold()
        exact_matches = [
            user
            for user in users
            if normalized
            in {
                str(user.get("accountId", "")).casefold(),
                str(user.get("displayName", "")).casefold(),
                str(user.get("emailAddress", "")).casefold(),
            }
        ]
        candidates = exact_matches or users
        if len(candidates) == 1 and candidates[0].get("accountId"):
            return str(candidates[0]["accountId"])
        if not candidates:
            raise ValueError(f"No assignable Jira user matched {query!r}")

        names = ", ".join(
            str(user.get("displayName") or user.get("accountId"))
            for user in candidates[:8]
        )
        raise ValueError(f"Assignee is ambiguous. Matching users: {names}")

    async def _get_comments(self, issue_key: str) -> tuple[JiraComment, ...]:
        comments: list[JiraComment] = []
        start_at = 0
        while True:
            response = await self.request(
                "GET",
                f"/rest/api/3/issue/{quote(issue_key)}/comment",
                params={"startAt": start_at, "maxResults": 100, "orderBy": "created"},
            )
            values = response.get("comments", [])
            comments.extend(_jira_comment(value) for value in values)
            if not values:
                return tuple(comments)
            start_at += len(values)
            if start_at >= response.get("total", start_at):
                return tuple(comments)

    async def _get_changes(self, issue_key: str) -> tuple[JiraChange, ...]:
        changes: list[JiraChange] = []
        start_at = 0
        while True:
            response = await self.request(
                "GET",
                f"/rest/api/3/issue/{quote(issue_key)}/changelog",
                params={"startAt": start_at, "maxResults": 100},
            )
            values = response.get("values", [])
            for history in values:
                for index, item in enumerate(history.get("items", [])):
                    changes.append(_jira_change(history, index, item))
            if not values:
                return tuple(changes)
            start_at += len(values)
            if start_at >= response.get("total", start_at):
                return tuple(changes)

    async def request(self, method: str, path: str, **kwargs) -> Any:
        if self._session is None:
            raise RuntimeError("JiraClient must be used as an async context manager")

        headers = kwargs.pop("headers", {})
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"
        for attempt in range(4):
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                **kwargs,
            ) as response:
                response_text = await response.text()
                if response.status in {429, 500, 502, 503, 504} and attempt < 3:
                    retry_after = response.headers.get("Retry-After", "")
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = 2**attempt
                    await asyncio.sleep(max(1, min(delay, 60)))
                    continue

                if response.status >= 400:
                    message = _jira_error_message(response_text)
                    raise JiraAPIError(response.status, message or response.reason)
                if response.status == 204 or not response_text:
                    return None
                return json.loads(response_text)

        raise RuntimeError("Jira request retry loop ended unexpectedly")


def jira_is_configured() -> bool:
    return bool(get_jira_project_keys()) and all(
        os.getenv(name) for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
    )


def get_jira_project_keys() -> tuple[str, ...]:
    keys = tuple(
        dict.fromkeys(
            value.strip().upper()
            for value in os.getenv("JIRA_PROJECT_KEYS", "").split(",")
            if value.strip()
        )
    )
    for key in keys:
        validate_project_key(key)
    return keys


def get_jira_sync_interval() -> int:
    raw_value = os.getenv("JIRA_SYNC_INTERVAL_SECONDS", "600")
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError("JIRA_SYNC_INTERVAL_SECONDS must be a whole number") from exc
    return max(300, value)


def validate_project_key(value: str) -> str:
    value = value.strip().upper()
    if not PROJECT_KEY_PATTERN.fullmatch(value):
        raise ValueError("Invalid Jira project key")
    return value


def validate_issue_key(value: str) -> str:
    value = value.strip().upper()
    if not ISSUE_KEY_PATTERN.fullmatch(value):
        raise ValueError("Invalid Jira issue key")
    return value


def _jira_issue(
    base_url: str,
    issue: dict[str, Any],
    comments: tuple[JiraComment, ...],
    changes: tuple[JiraChange, ...],
) -> JiraIssue:
    fields = issue.get("fields", {})
    return JiraIssue(
        issue_id=str(issue["id"]),
        issue_key=validate_issue_key(issue["key"]),
        project_key=validate_project_key(fields["project"]["key"]),
        summary=fields.get("summary") or "",
        description=adf_to_text(fields.get("description")),
        issue_type=_nested_name(fields.get("issuetype")),
        status=_nested_name(fields.get("status")),
        priority=_optional_nested_name(fields.get("priority")),
        assignee=_optional_display_name(fields.get("assignee")),
        reporter=_optional_display_name(fields.get("reporter")),
        labels=tuple(fields.get("labels") or []),
        web_url=f"{base_url}/browse/{issue['key']}",
        created_at=_parse_jira_datetime(fields["created"]),
        updated_at=_parse_jira_datetime(fields["updated"]),
        comments=comments,
        changes=changes,
        raw_data=issue,
    )


def _jira_comment(value: dict[str, Any]) -> JiraComment:
    return JiraComment(
        comment_id=str(value["id"]),
        author_name=_optional_display_name(value.get("author")) or "Unknown",
        body=adf_to_text(value.get("body")),
        created_at=_parse_jira_datetime(value["created"]),
        updated_at=_parse_jira_datetime(value.get("updated", value["created"])),
    )


def _jira_change(
    history: dict[str, Any],
    item_index: int,
    item: dict[str, Any],
) -> JiraChange:
    return JiraChange(
        history_id=str(history["id"]),
        item_index=item_index,
        author_name=_optional_display_name(history.get("author")) or "Unknown",
        field=item.get("field", "Unknown"),
        from_value=item.get("fromString"),
        to_value=item.get("toString"),
        created_at=_parse_jira_datetime(history["created"]),
    )


def _parse_jira_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _nested_name(value: dict[str, Any] | None) -> str:
    return str((value or {}).get("name") or "Unknown")


def _optional_nested_name(value: dict[str, Any] | None) -> str | None:
    return str(value["name"]) if value and value.get("name") else None


def _optional_display_name(value: dict[str, Any] | None) -> str | None:
    return str(value["displayName"]) if value and value.get("displayName") else None


def _jira_error_message(response_text: str) -> str:
    try:
        error_data = json.loads(response_text)
    except (TypeError, json.JSONDecodeError):
        return response_text
    if not isinstance(error_data, dict):
        return response_text
    messages = error_data.get("errorMessages", [])
    field_errors = error_data.get("errors", {})
    if not isinstance(messages, list):
        messages = [str(messages)]
    if not isinstance(field_errors, dict):
        field_errors = {"error": str(field_errors)}
    return "; ".join(
        [
            *(str(message) for message in messages),
            *(f"{key}: {value}" for key, value in field_errors.items()),
        ]
    )
