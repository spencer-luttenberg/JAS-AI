import asyncio
from dataclasses import dataclass
import hashlib
import logging

from database.drive_files import (
    finish_drive_root_sync,
    get_drive_file_state,
    record_drive_file_error,
    remove_unconfigured_drive_roots,
    replace_drive_file,
)
from google_drive.client import (
    DriveFile,
    build_google_drive_reader,
    get_google_drive_root_ids,
    get_google_drive_sync_interval,
)
from google_drive.extract import chunk_text, extract_text


logger = logging.getLogger(__name__)
_sync_lock = asyncio.Lock()


@dataclass(frozen=True)
class DriveSyncResult:
    roots: int = 0
    files_seen: int = 0
    files_indexed: int = 0
    files_unchanged: int = 0
    files_skipped: int = 0

    def add(self, other: "DriveSyncResult") -> "DriveSyncResult":
        return DriveSyncResult(
            roots=self.roots + other.roots,
            files_seen=self.files_seen + other.files_seen,
            files_indexed=self.files_indexed + other.files_indexed,
            files_unchanged=self.files_unchanged + other.files_unchanged,
            files_skipped=self.files_skipped + other.files_skipped,
        )


async def sync_google_drive() -> DriveSyncResult:
    async with _sync_lock:
        reader = build_google_drive_reader()
        total = DriveSyncResult()
        root_ids = get_google_drive_root_ids()
        for root_id in root_ids:
            result = await _sync_root(reader, root_id)
            total = total.add(result)
        await remove_unconfigured_drive_roots(root_ids)
        return total


async def run_google_drive_sync_forever() -> None:
    interval = get_google_drive_sync_interval()
    while True:
        try:
            result = await sync_google_drive()
            logger.info(
                "Google Drive sync complete: %s root(s), %s file(s) seen, "
                "%s indexed, %s unchanged, %s skipped",
                result.roots,
                result.files_seen,
                result.files_indexed,
                result.files_unchanged,
                result.files_skipped,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Google Drive sync failed")

        await asyncio.sleep(interval)


async def _sync_root(reader, root_id: str) -> DriveSyncResult:
    logger.info("Starting Google Drive sync for folder %s", root_id)
    seen_file_ids: list[str] = []
    indexed = 0
    unchanged = 0
    skipped = 0

    async for file in reader.walk_files(root_id):
        seen_file_ids.append(file.file_id)
        state = await get_drive_file_state(file.file_id)
        if _is_current(state, file):
            unchanged += 1
            continue

        try:
            downloaded = await reader.download_file(file)
            text = await asyncio.to_thread(
                extract_text,
                downloaded.data,
                downloaded.mime_type,
                file.name,
            )
            chunks = chunk_text(text)
            await replace_drive_file(
                file_id=file.file_id,
                name=file.name,
                mime_type=file.mime_type,
                web_view_link=file.web_view_link,
                modified_at=file.modified_at,
                version=file.version,
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                chunks=chunks,
            )
            indexed += 1
        except Exception as exc:
            skipped += 1
            logger.warning(
                "Could not index Google Drive file %s (%s): %s",
                file.name,
                file.file_id,
                exc,
            )
            await record_drive_file_error(
                file_id=file.file_id,
                name=file.name,
                mime_type=file.mime_type,
                web_view_link=file.web_view_link,
                modified_at=file.modified_at,
                version=file.version,
                error=str(exc),
            )

    await finish_drive_root_sync(root_id, seen_file_ids)
    logger.info(
        "Finished Google Drive folder %s; saw %s file(s)",
        root_id,
        len(seen_file_ids),
    )
    return DriveSyncResult(
        roots=1,
        files_seen=len(seen_file_ids),
        files_indexed=indexed,
        files_unchanged=unchanged,
        files_skipped=skipped,
    )


def _is_current(state, file: DriveFile) -> bool:
    if state is None:
        return False
    metadata_matches = (
        state.name == file.name
        and state.mime_type == file.mime_type
        and state.modified_at == file.modified_at
        and state.version == file.version
    )
    permanent_skip = bool(
        state.last_error
        and state.last_error.startswith(
            (
                "Unsupported file type:",
                "Google Drive does not allow this file to be downloaded",
                "File is larger than the",
            )
        )
    )
    return metadata_matches and (state.indexed or permanent_skip)
