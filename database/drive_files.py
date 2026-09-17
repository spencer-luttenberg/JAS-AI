from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from database.db import get_pool


@dataclass(frozen=True)
class DriveFileState:
    file_id: str
    name: str
    mime_type: str
    modified_at: datetime | None
    version: str | None
    indexed: bool
    last_error: str | None


@dataclass(frozen=True)
class StoredDriveChunk:
    file_id: str
    file_name: str
    mime_type: str
    web_view_link: str | None
    modified_at: datetime | None
    content: str


async def get_drive_file_state(file_id: str) -> DriveFileState | None:
    record = await get_pool().fetchrow(
        """
        SELECT
            file_id,
            name,
            mime_type,
            modified_at,
            version,
            indexed_at IS NOT NULL AS indexed,
            last_error
        FROM google_drive_files
        WHERE file_id = $1
        """,
        file_id,
    )
    if record is None:
        return None

    return DriveFileState(
        file_id=record["file_id"],
        name=record["name"],
        mime_type=record["mime_type"],
        modified_at=record["modified_at"],
        version=record["version"],
        indexed=record["indexed"],
        last_error=record["last_error"],
    )


async def replace_drive_file(
    *,
    file_id: str,
    name: str,
    mime_type: str,
    web_view_link: str | None,
    modified_at: datetime | None,
    version: str | None,
    content_hash: str,
    chunks: Sequence[str],
) -> None:
    async with get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO google_drive_files (
                    file_id,
                    name,
                    mime_type,
                    web_view_link,
                    modified_at,
                    version,
                    content_hash,
                    indexed_at,
                    last_error,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, NOW(), NULL, NOW())
                ON CONFLICT (file_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    mime_type = EXCLUDED.mime_type,
                    web_view_link = EXCLUDED.web_view_link,
                    modified_at = EXCLUDED.modified_at,
                    version = EXCLUDED.version,
                    content_hash = EXCLUDED.content_hash,
                    indexed_at = NOW(),
                    last_error = NULL,
                    updated_at = NOW()
                """,
                file_id,
                name,
                mime_type,
                web_view_link,
                modified_at,
                version,
                content_hash,
            )
            await connection.execute(
                "DELETE FROM google_drive_chunks WHERE file_id = $1",
                file_id,
            )
            if chunks:
                await connection.executemany(
                    """
                    INSERT INTO google_drive_chunks (
                        file_id,
                        chunk_index,
                        file_name,
                        content
                    )
                    VALUES ($1, $2, $3, $4)
                    """,
                    [
                        (file_id, index, name, chunk)
                        for index, chunk in enumerate(chunks)
                    ],
                )


async def record_drive_file_error(
    *,
    file_id: str,
    name: str,
    mime_type: str,
    web_view_link: str | None,
    modified_at: datetime | None,
    version: str | None,
    error: str,
) -> None:
    async with get_pool().acquire() as connection:
        async with connection.transaction():
            await connection.execute(
                """
                INSERT INTO google_drive_files (
                    file_id,
                    name,
                    mime_type,
                    web_view_link,
                    modified_at,
                    version,
                    content_hash,
                    indexed_at,
                    last_error,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NULL, NULL, $7, NOW())
                ON CONFLICT (file_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    mime_type = EXCLUDED.mime_type,
                    web_view_link = EXCLUDED.web_view_link,
                    modified_at = EXCLUDED.modified_at,
                    version = EXCLUDED.version,
                    content_hash = NULL,
                    indexed_at = NULL,
                    last_error = EXCLUDED.last_error,
                    updated_at = NOW()
                """,
                file_id,
                name,
                mime_type,
                web_view_link,
                modified_at,
                version,
                error[:2_000],
            )
            await connection.execute(
                "DELETE FROM google_drive_chunks WHERE file_id = $1",
                file_id,
            )


async def finish_drive_root_sync(root_id: str, file_ids: Sequence[str]) -> None:
    ids = list(dict.fromkeys(file_ids))

    async with get_pool().acquire() as connection:
        async with connection.transaction():
            if ids:
                await connection.execute(
                    """
                    INSERT INTO google_drive_file_roots (root_id, file_id)
                    SELECT $1, listed.file_id
                    FROM unnest($2::text[]) AS listed(file_id)
                    ON CONFLICT (root_id, file_id) DO NOTHING
                    """,
                    root_id,
                    ids,
                )
                await connection.execute(
                    """
                    DELETE FROM google_drive_file_roots
                    WHERE root_id = $1
                      AND NOT (file_id = ANY($2::text[]))
                    """,
                    root_id,
                    ids,
                )
            else:
                await connection.execute(
                    "DELETE FROM google_drive_file_roots WHERE root_id = $1",
                    root_id,
                )

            await connection.execute(
                """
                DELETE FROM google_drive_files AS files
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM google_drive_file_roots AS roots
                    WHERE roots.file_id = files.file_id
                )
                """
            )
            await connection.execute(
                """
                INSERT INTO google_drive_sync_state (
                    root_id,
                    last_synced_at,
                    file_count
                )
                VALUES ($1, NOW(), $2)
                ON CONFLICT (root_id) DO UPDATE SET
                    last_synced_at = NOW(),
                    file_count = EXCLUDED.file_count
                """,
                root_id,
                len(ids),
            )


async def remove_unconfigured_drive_roots(root_ids: Sequence[str]) -> None:
    ids = list(dict.fromkeys(root_ids))
    async with get_pool().acquire() as connection:
        async with connection.transaction():
            if ids:
                await connection.execute(
                    """
                    DELETE FROM google_drive_file_roots
                    WHERE NOT (root_id = ANY($1::text[]))
                    """,
                    ids,
                )
                await connection.execute(
                    """
                    DELETE FROM google_drive_sync_state
                    WHERE NOT (root_id = ANY($1::text[]))
                    """,
                    ids,
                )
            else:
                await connection.execute("DELETE FROM google_drive_file_roots")
                await connection.execute("DELETE FROM google_drive_sync_state")

            await connection.execute(
                """
                DELETE FROM google_drive_files AS files
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM google_drive_file_roots AS roots
                    WHERE roots.file_id = files.file_id
                )
                """
            )


async def search_drive_files(
    search_query: str,
    *,
    root_ids: Sequence[str] | None = None,
    limit: int = 10,
) -> list[StoredDriveChunk]:
    if not search_query or root_ids == [] or root_ids == ():
        return []

    records = await get_pool().fetch(
        """
        WITH parsed_query AS (
            SELECT websearch_to_tsquery('english', $1) AS value
        )
        SELECT
            chunks.file_id,
            chunks.file_name,
            files.mime_type,
            files.web_view_link,
            files.modified_at,
            chunks.content
        FROM google_drive_chunks AS chunks
        JOIN google_drive_files AS files USING (file_id)
        CROSS JOIN parsed_query
        WHERE chunks.search_vector @@ parsed_query.value
          AND (
              $2::text[] IS NULL
              OR EXISTS (
                  SELECT 1
                  FROM google_drive_file_roots AS roots
                  WHERE roots.file_id = chunks.file_id
                    AND roots.root_id = ANY($2::text[])
              )
          )
        ORDER BY
            ts_rank_cd(chunks.search_vector, parsed_query.value) DESC,
            files.modified_at DESC NULLS LAST,
            chunks.chunk_index
        LIMIT $3
        """,
        search_query,
        list(root_ids) if root_ids is not None else None,
        max(1, min(limit, 50)),
    )

    return [
        StoredDriveChunk(
            file_id=record["file_id"],
            file_name=record["file_name"],
            mime_type=record["mime_type"],
            web_view_link=record["web_view_link"],
            modified_at=record["modified_at"],
            content=record["content"],
        )
        for record in records
    ]
