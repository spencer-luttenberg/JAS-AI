import json
import re
from collections.abc import Sequence

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
