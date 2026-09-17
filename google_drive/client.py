import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
import io
import json
import os
import re
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"

GOOGLE_EXPORT_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    "application/vnd.google-apps.drawing": "application/pdf",
}

SUPPORTED_BINARY_MIME_TYPES = {
    "application/pdf",
    "application/json",
    "application/xml",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

EXTENSION_MIME_TYPES = {
    ".csv": "text/csv",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".htm": "text/html",
    ".html": "text/html",
    ".ini": "text/plain",
    ".json": "application/json",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".py": "text/plain",
    ".rst": "text/plain",
    ".sql": "text/plain",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xml": "application/xml",
    ".yaml": "text/plain",
    ".yml": "text/plain",
}


@dataclass(frozen=True)
class DriveFile:
    file_id: str
    name: str
    mime_type: str
    modified_at: datetime | None
    version: str | None
    web_view_link: str | None
    size: int | None
    can_download: bool


@dataclass(frozen=True)
class DownloadedDriveFile:
    data: bytes
    mime_type: str


class GoogleDriveReader:
    def __init__(self, service: Any, *, max_file_bytes: int) -> None:
        self._service = service
        self._max_file_bytes = max_file_bytes

    async def walk_files(self, root_id: str) -> AsyncIterator[DriveFile]:
        pending_folders = [root_id]
        visited_folders: set[str] = set()

        while pending_folders:
            folder_id = pending_folders.pop()
            if folder_id in visited_folders:
                continue
            visited_folders.add(folder_id)

            children = await asyncio.to_thread(self._list_children, folder_id)
            for child in children:
                if child.mime_type == FOLDER_MIME_TYPE:
                    pending_folders.append(child.file_id)
                else:
                    yield child

    async def download_file(self, file: DriveFile) -> DownloadedDriveFile:
        extraction_mime_type = get_extraction_mime_type(file)
        if extraction_mime_type is None:
            raise ValueError(f"Unsupported file type: {file.mime_type}")
        if not file.can_download:
            raise ValueError("Google Drive does not allow this file to be downloaded")
        if file.size is not None and file.size > self._max_file_bytes:
            raise ValueError(
                f"File is larger than the {self._max_file_bytes}-byte download limit"
            )

        data = await asyncio.to_thread(
            self._download_file,
            file,
            extraction_mime_type,
        )
        return DownloadedDriveFile(data=data, mime_type=extraction_mime_type)

    def _list_children(self, folder_id: str) -> list[DriveFile]:
        files: list[DriveFile] = []
        page_token = None

        while True:
            response = (
                self._service.files()
                .list(
                    q=f"'{_escape_query_value(folder_id)}' in parents and trashed = false",
                    fields=(
                        "nextPageToken,files("
                        "id,name,mimeType,modifiedTime,version,webViewLink,size,"
                        "capabilities(canDownload))"
                    ),
                    pageSize=1_000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )
            files.extend(_drive_file(item) for item in response.get("files", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return files

    def _download_file(
        self,
        file: DriveFile,
        extraction_mime_type: str,
    ) -> bytes:
        if file.mime_type in GOOGLE_EXPORT_TYPES:
            request = self._service.files().export_media(
                fileId=file.file_id,
                mimeType=extraction_mime_type,
            )
        else:
            request = self._service.files().get_media(
                fileId=file.file_id,
                supportsAllDrives=True,
            )

        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request, chunksize=1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
            if buffer.tell() > self._max_file_bytes:
                raise ValueError(
                    f"Downloaded content exceeded the {self._max_file_bytes}-byte limit"
                )
        return buffer.getvalue()


def google_drive_is_configured() -> bool:
    return bool(get_google_drive_root_ids()) and bool(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64")
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    )


def get_google_drive_root_ids() -> tuple[str, ...]:
    raw_ids = os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "")
    return tuple(
        dict.fromkeys(
            folder_id
            for value in raw_ids.split(",")
            if (folder_id := _normalize_folder_id(value))
        )
    )


def get_google_drive_sync_interval() -> int:
    raw_interval = os.getenv("GOOGLE_DRIVE_SYNC_INTERVAL_SECONDS", "3600")
    try:
        interval = int(raw_interval)
    except ValueError as exc:
        raise ValueError(
            "GOOGLE_DRIVE_SYNC_INTERVAL_SECONDS must be a whole number"
        ) from exc
    return max(300, interval)


def build_google_drive_reader() -> GoogleDriveReader:
    credentials_info = _load_service_account_info()
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=[DRIVE_READONLY_SCOPE],
    )
    service = build(
        "drive",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )
    max_file_bytes = _positive_int_environment_value(
        "GOOGLE_DRIVE_MAX_FILE_BYTES",
        20 * 1024 * 1024,
    )
    return GoogleDriveReader(service, max_file_bytes=max_file_bytes)


def get_extraction_mime_type(file: DriveFile) -> str | None:
    if file.mime_type in GOOGLE_EXPORT_TYPES:
        return GOOGLE_EXPORT_TYPES[file.mime_type]
    if file.mime_type.startswith("text/"):
        return file.mime_type
    if file.mime_type in SUPPORTED_BINARY_MIME_TYPES:
        return file.mime_type

    extension = os.path.splitext(file.name.casefold())[1]
    return EXTENSION_MIME_TYPES.get(extension)


def _load_service_account_info() -> dict[str, Any]:
    encoded_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON_BASE64")
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if encoded_json:
        try:
            raw_json = base64.b64decode(encoded_json, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 is not valid base64-encoded JSON"
            ) from exc
    if not raw_json:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON_BASE64 or GOOGLE_SERVICE_ACCOUNT_JSON "
            "is required"
        )

    try:
        info = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError("The Google service-account credential is not valid JSON") from exc
    if not isinstance(info, dict):
        raise ValueError("The Google service-account credential must be a JSON object")
    return info


def _drive_file(item: dict[str, Any]) -> DriveFile:
    modified_at = None
    if item.get("modifiedTime"):
        modified_at = datetime.fromisoformat(item["modifiedTime"].replace("Z", "+00:00"))

    raw_size = item.get("size")
    return DriveFile(
        file_id=item["id"],
        name=item.get("name", "Untitled"),
        mime_type=item.get("mimeType", "application/octet-stream"),
        modified_at=modified_at,
        version=str(item["version"]) if item.get("version") is not None else None,
        web_view_link=item.get("webViewLink"),
        size=int(raw_size) if raw_size is not None else None,
        can_download=item.get("capabilities", {}).get("canDownload", True),
    )


def _normalize_folder_id(value: str) -> str:
    value = value.strip()
    if not value:
        return ""

    folder_match = re.search(r"/folders/([^/?#]+)", value)
    if folder_match:
        return folder_match.group(1)
    query_match = re.search(r"[?&]id=([^&#]+)", value)
    if query_match:
        return query_match.group(1)
    return value


def _escape_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _positive_int_environment_value(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value
