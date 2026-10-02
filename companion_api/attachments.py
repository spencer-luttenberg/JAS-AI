"""Attachment wire format shared with JAS-AI's companion_api/attachments.py.

Keep validation and limits in sync when changing either independently deployed app.
Files stay inline in the conversation; they are never executed or saved to disk.
"""

import base64
import binascii
from pathlib import PurePosixPath
from typing import Any

MAX_ATTACHMENTS = 5
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_CONTEXT_ATTACHMENTS = 20
MAX_REQUEST_BYTES = 30 * 1024 * 1024
MAX_TEXT_CHARACTERS = 100_000
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
DOCUMENT_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".rtf": "application/rtf",
}
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".py",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".cs",
    ".java",
    ".rs",
    ".go",
    ".sh",
    ".ps1",
    ".sql",
    ".ini",
    ".cfg",
    ".toml",
    ".log",
    ".usf",
    ".ush",
    ".uproject",
    ".uplugin",
}


class AttachmentValidationError(ValueError):
    """Invalid attachment data received over the wire."""


def attachment_config() -> dict[str, Any]:
    return {
        "extensions": sorted(
            IMAGE_TYPES.keys() | DOCUMENT_TYPES.keys() | TEXT_EXTENSIONS
        ),
        "image_extensions": sorted(IMAGE_TYPES),
        "max_files": MAX_ATTACHMENTS,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "max_text_characters": MAX_TEXT_CHARACTERS,
    }


def decode_text(data: bytes, name: str) -> str:
    encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeError as exc:
        raise ValueError(
            f"{name}: text files must use UTF-8 or UTF-16 encoding."
        ) from exc
    if any(ord(char) < 32 and char not in "\t\r\n\f" for char in text):
        raise ValueError(f"{name}: this appears to be a binary file, not text.")
    if len(text) > MAX_TEXT_CHARACTERS:
        raise ValueError(f"{name}: text files must contain at most 100,000 characters.")
    return text


def validate_attachments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise ValueError("Attach at most 5 files per message.")
    attachments = []
    for item in value:
        if not isinstance(item, dict):
            raise AttachmentValidationError("Each attachment must be an object.")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 255:
            raise ValueError(
                "Each attachment needs a filename of at most 255 characters."
            )
        name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
        if not name or any(ord(char) < 32 for char in name):
            raise ValueError("Invalid attachment filename.")
        extension = PurePosixPath(name).suffix.lower()
        mime_type = IMAGE_TYPES.get(extension) or DOCUMENT_TYPES.get(extension)
        if mime_type is None and extension not in TEXT_EXTENSIONS:
            raise ValueError(
                f"{name}: unsupported file type. Use images, documents, or text/code files."
            )
        encoded = item.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError(f"{name}: the attachment is empty.")
        if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise ValueError(f"{name}: files must be 10 MB or smaller.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{name}: invalid attachment encoding.") from exc
        if not data or len(data) > MAX_FILE_BYTES:
            raise ValueError(f"{name}: files must be nonempty and 10 MB or smaller.")
        if mime_type is None:
            decode_text(data, name)
            mime_type = "text/plain"
        else:
            signatures = {
                "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
                "image/jpeg": data.startswith(b"\xff\xd8\xff"),
                "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
                "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
                "application/pdf": data.startswith(b"%PDF-"),
            }
            if mime_type in signatures and not signatures[mime_type]:
                raise ValueError(f"{name}: file contents do not match its extension.")
        attachments.append(
            {
                "name": name,
                "mime_type": mime_type,
                "data": encoded,
                "size": len(data),
            }
        )
    validate_attachment_budget(attachments)
    return attachments


def validate_attachment_budget(attachments: list[dict[str, Any]]) -> None:
    if len(attachments) > MAX_CONTEXT_ATTACHMENTS:
        raise ValueError(
            "The conversation can include at most 20 attachments at a time."
        )
    if sum(item["size"] for item in attachments) > MAX_TOTAL_BYTES:
        raise ValueError("Attachments must total 20 MB or less.")
