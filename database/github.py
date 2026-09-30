from collections.abc import Sequence
from dataclasses import dataclass

from database.db import get_pool


@dataclass(frozen=True)
class GitHubFileState:
    blob_sha: str
    indexed: bool
    last_error: str | None


@dataclass(frozen=True)
class StoredGitHubChunk:
    repository: str
    path: str
    default_branch: str | None
    web_url: str | None
    content: str


async def upsert_github_repository(
    *,
    full_name: str,
    default_branch: str,
    head_sha: str,
    web_url: str,
    is_private: bool,
) -> None:
    await get_pool().execute(
        """
        INSERT INTO github_repositories (
            full_name,
            default_branch,
            head_sha,
            web_url,
            is_private,
            last_error,
            updated_at
        )
        VALUES ($1, $2, $3, $4, $5, NULL, NOW())
        ON CONFLICT (full_name) DO UPDATE SET
            default_branch = EXCLUDED.default_branch,
            head_sha = EXCLUDED.head_sha,
            web_url = EXCLUDED.web_url,
            is_private = EXCLUDED.is_private,
            last_error = NULL,
            updated_at = NOW()
        """,
        full_name,
        default_branch,
        head_sha,
        web_url,
        is_private,
    )


async def get_github_file_state(
    repository: str,
    path: str,
) -> GitHubFileState | None:
    record = await get_pool().fetchrow(
        """
        SELECT blob_sha, indexed_at IS NOT NULL AS indexed, last_error
        FROM github_files
        WHERE repository = $1 AND path = $2
        """,
        repository,
        path,
    )
    if record is None:
        return None
    return GitHubFileState(
        blob_sha=record["blob_sha"],
        indexed=record["indexed"],
        last_error=record["last_error"],
    )


async def replace_github_file(
    *,
    repository: str,
    path: str,
    blob_sha: str,
    size_bytes: int | None,
    web_url: str,
    content_hash: str,
    chunks: Sequence[str],
) -> None:
    async with get_pool().acquire() as connection, connection.transaction():
        await connection.execute(
            """
                INSERT INTO github_files (
                    repository,
                    path,
                    blob_sha,
                    size_bytes,
                    web_url,
                    content_hash,
                    indexed_at,
                    last_error,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), NULL, NOW())
                ON CONFLICT (repository, path) DO UPDATE SET
                    blob_sha = EXCLUDED.blob_sha,
                    size_bytes = EXCLUDED.size_bytes,
                    web_url = EXCLUDED.web_url,
                    content_hash = EXCLUDED.content_hash,
                    indexed_at = NOW(),
                    last_error = NULL,
                    updated_at = NOW()
                """,
            repository,
            path,
            blob_sha,
            size_bytes,
            web_url,
            content_hash,
        )
        await connection.execute(
            "DELETE FROM github_chunks WHERE repository = $1 AND path = $2",
            repository,
            path,
        )
        if chunks:
            await connection.executemany(
                """
                    INSERT INTO github_chunks (
                        repository,
                        path,
                        chunk_index,
                        content
                    )
                    VALUES ($1, $2, $3, $4)
                    """,
                [
                    (repository, path, index, chunk)
                    for index, chunk in enumerate(chunks)
                ],
            )


async def record_github_file_error(
    *,
    repository: str,
    path: str,
    blob_sha: str,
    size_bytes: int | None,
    web_url: str,
    error: str,
) -> None:
    async with get_pool().acquire() as connection, connection.transaction():
        await connection.execute(
            """
                INSERT INTO github_files (
                    repository,
                    path,
                    blob_sha,
                    size_bytes,
                    web_url,
                    content_hash,
                    indexed_at,
                    last_error,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, NULL, NULL, $6, NOW())
                ON CONFLICT (repository, path) DO UPDATE SET
                    blob_sha = EXCLUDED.blob_sha,
                    size_bytes = EXCLUDED.size_bytes,
                    web_url = EXCLUDED.web_url,
                    content_hash = NULL,
                    indexed_at = NULL,
                    last_error = EXCLUDED.last_error,
                    updated_at = NOW()
                """,
            repository,
            path,
            blob_sha,
            size_bytes,
            web_url,
            error[:2_000],
        )
        await connection.execute(
            "DELETE FROM github_chunks WHERE repository = $1 AND path = $2",
            repository,
            path,
        )


async def finish_github_repository_sync(
    repository: str,
    paths: Sequence[str],
) -> None:
    unique_paths = list(dict.fromkeys(paths))
    async with get_pool().acquire() as connection, connection.transaction():
        if unique_paths:
            await connection.execute(
                """
                    DELETE FROM github_files
                    WHERE repository = $1
                      AND NOT (path = ANY($2::text[]))
                    """,
                repository,
                unique_paths,
            )
        else:
            await connection.execute(
                "DELETE FROM github_files WHERE repository = $1",
                repository,
            )
        await connection.execute(
            """
                UPDATE github_repositories
                SET
                    last_synced_at = NOW(),
                    file_count = $2,
                    last_error = NULL,
                    updated_at = NOW()
                WHERE full_name = $1
                """,
            repository,
            len(unique_paths),
        )


async def record_github_repository_error(repository: str, error: str) -> None:
    await get_pool().execute(
        """
        INSERT INTO github_repositories (full_name, last_error, updated_at)
        VALUES ($1, $2, NOW())
        ON CONFLICT (full_name) DO UPDATE SET
            last_error = EXCLUDED.last_error,
            updated_at = NOW()
        """,
        repository,
        error[:2_000],
    )


async def remove_unconfigured_github_repositories(
    repositories: Sequence[str],
) -> None:
    configured = list(dict.fromkeys(repository.casefold() for repository in repositories))
    if configured:
        await get_pool().execute(
            """
            DELETE FROM github_repositories
            WHERE NOT (lower(full_name) = ANY($1::text[]))
            """,
            configured,
        )
    else:
        await get_pool().execute("DELETE FROM github_repositories")


async def search_github_files(
    search_query: str,
    *,
    repositories: Sequence[str] | None = None,
    limit: int = 12,
) -> list[StoredGitHubChunk]:
    if not search_query or repositories == [] or repositories == ():
        return []
    records = await get_pool().fetch(
        """
        WITH parsed_query AS (
            SELECT websearch_to_tsquery('english', $1) AS value
        )
        SELECT
            chunks.repository,
            chunks.path,
            repositories.default_branch,
            files.web_url,
            chunks.content
        FROM github_chunks AS chunks
        JOIN github_files AS files
          ON files.repository = chunks.repository
         AND files.path = chunks.path
        JOIN github_repositories AS repositories
          ON repositories.full_name = chunks.repository
        CROSS JOIN parsed_query
        WHERE chunks.search_vector @@ parsed_query.value
          AND ($2::text[] IS NULL OR lower(chunks.repository) = ANY($2::text[]))
        ORDER BY
            ts_rank_cd(chunks.search_vector, parsed_query.value) DESC,
            chunks.repository,
            chunks.path,
            chunks.chunk_index
        LIMIT $3
        """,
        search_query,
        (
            [repository.casefold() for repository in repositories]
            if repositories is not None
            else None
        ),
        max(1, min(limit, 50)),
    )
    return [_stored_github_chunk(record) for record in records]


async def get_recent_github_chunks(
    repositories: Sequence[str],
    *,
    limit: int = 10,
) -> list[StoredGitHubChunk]:
    if not repositories:
        return []
    records = await get_pool().fetch(
        """
        WITH ranked_chunks AS (
            SELECT
                chunks.repository,
                chunks.path,
                chunks.content,
                ROW_NUMBER() OVER (
                    PARTITION BY chunks.repository, chunks.path
                    ORDER BY chunks.chunk_index
                ) AS chunk_rank
            FROM github_chunks AS chunks
            WHERE lower(chunks.repository) = ANY($1::text[])
        )
        SELECT
            ranked.repository,
            ranked.path,
            repositories.default_branch,
            files.web_url,
            ranked.content
        FROM ranked_chunks AS ranked
        JOIN github_files AS files
          ON files.repository = ranked.repository
         AND files.path = ranked.path
        JOIN github_repositories AS repositories
          ON repositories.full_name = ranked.repository
        WHERE ranked.chunk_rank = 1
        ORDER BY
            CASE
                WHEN lower(ranked.path) LIKE '%readme%' THEN 0
                ELSE 1
            END,
            ranked.repository,
            ranked.path
        LIMIT $2
        """,
        [repository.casefold() for repository in repositories],
        max(1, min(limit, 50)),
    )
    return [_stored_github_chunk(record) for record in records]


def _stored_github_chunk(record) -> StoredGitHubChunk:
    return StoredGitHubChunk(
        repository=record["repository"],
        path=record["path"],
        default_branch=record["default_branch"],
        web_url=record["web_url"],
        content=record["content"],
    )
