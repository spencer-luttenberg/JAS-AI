import json
import re
from collections.abc import Sequence

from database.drive_files import StoredDriveChunk
from database.jira import StoredJiraIssue
from database.messages import StoredMessage


SEARCH_STOP_WORDS = {
    "about",
    "again",
    "could",
    "did",
    "does",
    "from",
    "have",
    "into",
    "just",
    "know",
    "like",
    "remember",
    "said",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "they",
    "this",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
}


def build_history_search_query(question: str, max_terms: int = 12) -> str:
    terms: list[str] = []
    for term in re.findall(r"[a-z0-9]{3,}", question.casefold()):
        if term in SEARCH_STOP_WORDS or term in terms:
            continue
        terms.append(term)
        if len(terms) == max_terms:
            break

    return " OR ".join(terms)


def format_conversation_context(
    relevant_messages: Sequence[StoredMessage],
    recent_messages: Sequence[StoredMessage] = (),
    *,
    max_characters: int = 16_000,
) -> str:
    recent_ids = {message.message_id for message in recent_messages}
    relevant_messages = [
        message
        for message in relevant_messages
        if message.message_id not in recent_ids
    ]

    recent_budget = min(6_000, max_characters // 2)
    relevant_budget = max_characters - recent_budget

    relevant_lines = _fit_lines(
        [_serialize_message(message) for message in relevant_messages],
        relevant_budget,
        keep_end=False,
    )
    recent_lines = _fit_lines(
        [_serialize_message(message) for message in recent_messages],
        recent_budget,
        keep_end=True,
    )

    sections: list[str] = []
    if relevant_lines:
        sections.append(
            "RELEVANT LONG-TERM HISTORY (ranked by PostgreSQL search):\n"
            + "\n".join(relevant_lines)
        )
    if recent_lines:
        sections.append(
            "RECENT CHANNEL CONVERSATION (oldest to newest):\n"
            + "\n".join(recent_lines)
        )

    return "\n\n".join(sections)


def format_drive_context(
    chunks: Sequence[StoredDriveChunk],
    *,
    max_characters: int = 12_000,
) -> str:
    lines = [
        json.dumps(
            {
                "file_id": chunk.file_id,
                "file_name": chunk.file_name,
                "source_url": chunk.web_view_link,
                "modified_at": (
                    chunk.modified_at.isoformat() if chunk.modified_at else None
                ),
                "content": chunk.content,
            },
            ensure_ascii=True,
        )
        for chunk in chunks
    ]
    selected_lines = _fit_lines(lines, max_characters, keep_end=False)
    if not selected_lines:
        return ""
    return "RELEVANT GOOGLE DRIVE FILE EXCERPTS:\n" + "\n".join(selected_lines)


def format_jira_context(
    issues: Sequence[StoredJiraIssue],
    *,
    max_characters: int = 12_000,
) -> str:
    lines = [
        json.dumps(
            {
                "issue_key": issue.issue_key,
                "project_key": issue.project_key,
                "summary": issue.summary,
                "description": issue.description[:3_000],
                "issue_type": issue.issue_type,
                "status": issue.status,
                "priority": issue.priority,
                "assignee": issue.assignee,
                "labels": issue.labels,
                "recent_comments_and_changes": issue.activity_text[-6_000:],
                "updated_at": issue.updated_at.isoformat(),
                "source_url": issue.web_url,
            },
            ensure_ascii=True,
        )
        for issue in issues
    ]
    selected_lines = _fit_lines(lines, max_characters, keep_end=False)
    if not selected_lines:
        return ""
    return "RELEVANT JIRA ISSUES, COMMENTS, AND CHANGES:\n" + "\n".join(selected_lines)


def format_project_activity_context(
    new_messages: Sequence[StoredMessage],
    follow_up_candidates: Sequence[StoredMessage],
    *,
    max_characters: int = 24_000,
) -> str:
    new_ids = {message.message_id for message in new_messages}
    follow_up_candidates = [
        message
        for message in follow_up_candidates
        if message.message_id not in new_ids
    ]

    new_budget = min(17_000, max_characters * 3 // 4)
    follow_up_budget = max_characters - new_budget
    new_lines = _fit_lines(
        [_serialize_message(message) for message in new_messages],
        new_budget,
        keep_end=True,
    )
    follow_up_lines = _fit_lines(
        [_serialize_message(message) for message in follow_up_candidates],
        follow_up_budget,
        keep_end=False,
    )

    sections: list[str] = []
    if new_lines:
        sections.append(
            "NEW DISCORD ACTIVITY SINCE THE PREVIOUS UPDATE:\n"
            + "\n".join(new_lines)
        )
    if follow_up_lines:
        sections.append(
            "POSSIBLE LONG-RUNNING FOLLOW-UP CANDIDATES "
            "(keyword matches; some may already be resolved):\n"
            + "\n".join(follow_up_lines)
        )
    return "\n\n".join(sections)


def _serialize_message(message: StoredMessage) -> str:
    attachment_names = [
        attachment.get("filename")
        for attachment in message.attachments
        if attachment.get("filename")
    ]
    return json.dumps(
        {
            "message_id": message.message_id,
            "channel_id": message.channel_id,
            "timestamp": message.created_at.isoformat(),
            "author": message.author_name,
            "is_bot": message.is_bot,
            "content": message.content,
            "attachments": attachment_names,
        },
        ensure_ascii=True,
    )


def _fit_lines(lines: list[str], budget: int, *, keep_end: bool) -> list[str]:
    selected: list[str] = []
    character_count = 0
    candidates = reversed(lines) if keep_end else iter(lines)

    for line in candidates:
        added_characters = len(line) + (1 if selected else 0)
        if character_count + added_characters > budget:
            break
        selected.append(line)
        character_count += added_characters

    if keep_end:
        selected.reverse()
    return selected
